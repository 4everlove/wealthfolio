#!/usr/bin/env python3
"""Convert a Schwab account transactions CSV to a Wealthfolio import CSV.

Schwab CSV columns:
  Date, Action, Symbol, Description, Quantity, Price, Fees & Comm, Amount

Handled actions:
  - Buy to Open / Sell to Open  → BUY / SELL (open new position)
  - Buy to Close / Sell to Close → BUY / SELL with subtype POSITION_CLOSE
  - Expired                     → ADJUSTMENT subtype OPTION_EXPIRY (abs qty)
  - Bank Interest               → INTEREST
  - Security Transfer           → DEPOSIT (positive amount) / WITHDRAWAL
  - Journal                     → skipped (paired net-zero book adjustments)

Symbol conversion: Schwab option symbol `WDC 08/21/2026 557.50 C`
                   → OCC 21-char        `WDC   260821C00557500`

Date: `MM/DD/YYYY as of MM/DD/YYYY` → use the second (as-of) date.

Aggregation (--aggregate-closed): for each unique OCC contract, if the whole
lifecycle nets to zero (fully expired/closed), roll all of its cash into a
single CREDIT per (settlement_day, underlying). Currently open contracts
(net qty ≠ 0) are NOT emitted — they'll show up only after they settle.
Trades off intraday position visibility for a ~10-25× row reduction.

Usage:
  python3 scripts/import/schwab_to_wf.py \\
    ~/Downloads/schwab_transactions.csv \\
    ~/Downloads/wf_schwab.csv \\
    --account-id "Shwab RothIRA" \\
    [--aggregate-closed]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

WF_HEADER = [
    "date", "symbol", "instrumentType", "quantity", "activityType",
    "unitPrice", "currency", "fee", "tax", "amount", "fxRate",
    "subtype", "quoteMode", "accountId", "notes", "sourceRecordId",
]

OPTION_RE = re.compile(r"^([A-Z0-9.]+)\s+(\d\d)/(\d\d)/(\d{4})\s+([\d.]+)\s+([CP])$")
DATE_RE = re.compile(r"^(\d\d)/(\d\d)/(\d{4})(?:\s+as of\s+(\d\d)/(\d\d)/(\d{4}))?$")


def parse_money(s: str) -> Decimal:
    if not s: return Decimal(0)
    s = s.replace("$", "").replace(",", "").strip()
    if not s: return Decimal(0)
    return Decimal(s)


def parse_date(raw: str) -> str:
    """Return ISO date. Uses 'as of' date when present."""
    m = DATE_RE.match(raw.strip())
    if not m:
        raise ValueError(f"Unrecognized date: {raw!r}")
    if m.group(4):
        mm, dd, yyyy = m.group(4), m.group(5), m.group(6)
    else:
        mm, dd, yyyy = m.group(1), m.group(2), m.group(3)
    return f"{yyyy}-{mm}-{dd}"


def occ_symbol(schwab_sym: str) -> str | None:
    """Convert Schwab option symbol to 21-char OCC symbol."""
    m = OPTION_RE.match(schwab_sym.strip())
    if not m:
        return None
    root, mm, dd, yyyy, strike, cp = m.groups()
    yymmdd = f"{yyyy[2:]}{mm}{dd}"
    strike_int = int(round(float(strike) * 1000))
    return f"{root:<6}{yymmdd}{cp}{strike_int:08d}"


def stable_id(*parts) -> str:
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:16]
    return f"schwab:{h}"


def underlying_from_occ(occ: str) -> str:
    """First 6 chars of OCC, stripped — e.g. 'WDC   260821C00557500' → 'WDC'."""
    return occ[:6].strip()


def short_contract(occ: str) -> str:
    """Human-readable tail: 'WDC   260821C00557500' → '260821C557.5'."""
    tail = occ[6:]  # e.g. '260821C00557500'
    yymmdd = tail[:6]
    cp = tail[6]
    strike_str = tail[7:]  # 8 digits, strike * 1000
    strike = Decimal(strike_str) / Decimal(1000)
    # Trim trailing zeros only after decimal: 557.500 → 557.5, 360.000 → 360
    s = format(strike, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return f"{yymmdd}{cp}{s}"


OPTION_ACTIONS = {"Buy to Open", "Sell to Open", "Buy to Close", "Sell to Close", "Expired"}


def net_qty_delta(action: str, signed_qty: Decimal) -> Decimal:
    """Position-walker delta. Schwab qty is positive for Open/Close, signed for Expired."""
    if action == "Buy to Open":
        return signed_qty
    if action == "Sell to Open":
        return -signed_qty
    if action == "Buy to Close":
        return signed_qty
    if action == "Sell to Close":
        return -signed_qty
    if action == "Expired":
        # Schwab's Expired qty is the signed delta applied to the position
        # (positive when a short expires, negative when a long expires).
        return signed_qty
    return Decimal(0)


def aggregate_closed(option_rows: list[dict]) -> tuple[list[tuple], list[dict], dict]:
    """Aggregate closed contract lifecycles into (settlement_day, underlying) buckets.

    Returns (aggregates, still_open_rows, stats):
      aggregates: list of (day, underlying, cash, contracts_set, activity_count)
      still_open_rows: original rows for contracts whose net qty ≠ 0 (dropped
                      from output under user's design)
      stats: dict with counters
    """
    by_contract: dict[str, list[dict]] = defaultdict(list)
    for row in option_rows:
        occ = occ_symbol(row["Symbol"])
        if not occ:
            raise ValueError(f"Unrecognized option symbol: {row['Symbol']!r}")
        by_contract[occ].append(row)

    settled: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"cash": Decimal(0), "contracts": set(), "activity_count": 0}
    )
    still_open: list[dict] = []
    stats = {"contracts_closed": 0, "contracts_open": 0, "activities_folded": 0, "activities_dropped": 0}

    for occ, activities in by_contract.items():
        activities.sort(key=lambda r: parse_date(r["Date"]))
        net = Decimal(0)
        for r in activities:
            try:
                q = Decimal(r["Quantity"])
            except Exception:
                q = Decimal(0)
            net += net_qty_delta(r["Action"], q)

        if net == 0:
            # Contract fully settled — fold lifecycle cash into bucket
            settlement_day = parse_date(activities[-1]["Date"])
            underlying = underlying_from_occ(occ)
            key = (settlement_day, underlying)
            for r in activities:
                settled[key]["cash"] += parse_money(r["Amount"])
            settled[key]["contracts"].add(occ)
            settled[key]["activity_count"] += len(activities)
            stats["contracts_closed"] += 1
            stats["activities_folded"] += len(activities)
        else:
            still_open.extend(activities)
            stats["contracts_open"] += 1
            stats["activities_dropped"] += len(activities)

    aggregates = [
        (day, underlying, info["cash"], info["contracts"], info["activity_count"])
        for (day, underlying), info in sorted(settled.items())
    ]
    return aggregates, still_open, stats


def convert(rows: list[dict], account_id: str, aggregate: bool = False) -> tuple[list[dict], dict]:
    out = []
    skipped = {"Journal": 0}

    if aggregate:
        # Split into option lifecycle rows vs cash rows
        option_rows = []
        cash_rows = []
        for row in rows:
            action = row["Action"]
            if action == "Journal":
                skipped["Journal"] += 1
                continue
            if action in OPTION_ACTIONS:
                option_rows.append(row)
            else:
                cash_rows.append(row)

        aggregates, still_open, stats = aggregate_closed(option_rows)
        skipped["still_open_activities_dropped"] = stats["activities_dropped"]
        skipped["still_open_contracts"] = stats["contracts_open"]
        skipped["contracts_closed"] = stats["contracts_closed"]
        skipped["activities_folded"] = stats["activities_folded"]

        for day, underlying, cash, contracts, act_count in aggregates:
            out.append({
                "date": day,
                "symbol": "",
                "instrumentType": "",
                "quantity": "1",
                "activityType": "CREDIT",
                "unitPrice": str(cash),
                "currency": "USD",
                "fee": "0",
                "tax": "0",
                "amount": str(cash),
                "fxRate": "",
                "subtype": "SCHWAB_LIFECYCLE",
                "quoteMode": "",
                "accountId": account_id,
                "notes": f"Schwab {underlying} settlement: {', '.join(sorted(short_contract(c) for c in contracts))}",
                "sourceRecordId": stable_id(account_id, day, underlying, "SCHWAB_LIFECYCLE"),
            })

        # Cash rows (interest, transfer) still emit as-is
        out.extend(_convert_per_row(cash_rows, account_id, skipped))
        return out, skipped

    # Non-aggregated path
    out.extend(_convert_per_row(rows, account_id, skipped))
    return out, skipped


def _convert_per_row(rows: list[dict], account_id: str, skipped: dict) -> list[dict]:
    out = []
    for row in rows:
        action = row["Action"]
        if action == "Journal":
            skipped["Journal"] += 1
            continue

        date_iso = parse_date(row["Date"])
        sym_raw = row["Symbol"]
        qty_raw = row["Quantity"]
        price = parse_money(row["Price"])
        fee = parse_money(row["Fees & Comm"])
        amount = parse_money(row["Amount"])

        # Common OCC symbol for option rows
        occ = occ_symbol(sym_raw) if sym_raw else ""

        if action in ("Buy to Open", "Sell to Open", "Buy to Close", "Sell to Close"):
            if not occ:
                raise ValueError(f"Unrecognized option symbol on trade row: {sym_raw!r}")
            qty = abs(Decimal(qty_raw))
            direction = "BUY" if action.startswith("Buy") else "SELL"
            subtype = "POSITION_CLOSE" if action.endswith("Close") else ""
            out.append({
                "date": date_iso,
                "symbol": occ,
                "instrumentType": "OPTION",
                "quantity": str(qty),
                "activityType": direction,
                "unitPrice": str(price),
                "currency": "USD",
                "fee": str(fee),
                "tax": "0",
                "amount": "",
                "fxRate": "",
                "subtype": subtype,
                "quoteMode": "",
                "accountId": account_id,
                "notes": f"Schwab {action}: {row['Description']}",
                "sourceRecordId": stable_id(account_id, date_iso, action, occ, qty_raw, price, amount),
            })

        elif action == "Expired":
            if not occ:
                raise ValueError(f"Unrecognized option symbol on Expired row: {sym_raw!r}")
            qty = abs(Decimal(qty_raw))
            out.append({
                "date": date_iso,
                "symbol": occ,
                "instrumentType": "OPTION",
                "quantity": str(qty),
                "activityType": "ADJUSTMENT",
                "unitPrice": "0",
                "currency": "USD",
                "fee": "0",
                "tax": "0",
                "amount": "0",
                "fxRate": "",
                "subtype": "OPTION_EXPIRY",
                "quoteMode": "",
                "accountId": account_id,
                "notes": f"Schwab Expired: {row['Description']}",
                "sourceRecordId": stable_id(account_id, date_iso, "Expired", occ, qty_raw),
            })

        elif action == "Bank Interest":
            out.append({
                "date": date_iso,
                "symbol": "",
                "instrumentType": "",
                "quantity": "1",
                "activityType": "INTEREST",
                "unitPrice": str(amount),
                "currency": "USD",
                "fee": "0",
                "tax": "0",
                "amount": str(amount),
                "fxRate": "",
                "subtype": "",
                "quoteMode": "",
                "accountId": account_id,
                "notes": f"Schwab: {row['Description']}",
                "sourceRecordId": stable_id(account_id, date_iso, "Bank Interest", amount, row['Description']),
            })

        elif action == "Security Transfer":
            # Amount is positive for inbound. Description gives ACAT / ATON info.
            activity_type = "DEPOSIT" if amount >= 0 else "WITHDRAWAL"
            out.append({
                "date": date_iso,
                "symbol": "",
                "instrumentType": "",
                "quantity": "1",
                "activityType": activity_type,
                "unitPrice": str(abs(amount)),
                "currency": "USD",
                "fee": "0",
                "tax": "0",
                "amount": str(abs(amount)),
                "fxRate": "",
                "subtype": "",
                "quoteMode": "",
                "accountId": account_id,
                "notes": f"Schwab Security Transfer: {row['Description']}",
                "sourceRecordId": stable_id(account_id, date_iso, "Security Transfer", amount, row['Description']),
            })

        else:
            print(f"WARN: unhandled action {action!r} on {date_iso} — skipped", file=sys.stderr)

    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input_csv", type=Path)
    ap.add_argument("output_csv", type=Path)
    ap.add_argument("--account-id", required=True, help="Wealthfolio account name/id")
    ap.add_argument("--aggregate-closed", action="store_true",
                    help="Fold each fully-settled contract's lifecycle into a single "
                         "CREDIT per (settlement_day, underlying). Currently-open contracts "
                         "are omitted; they'll appear only after they settle.")
    args = ap.parse_args()

    with args.input_csv.open() as f:
        rows = list(csv.DictReader(f))

    out_rows, skipped = convert(rows, args.account_id, aggregate=args.aggregate_closed)

    with args.output_csv.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=WF_HEADER)
        w.writeheader()
        for r in out_rows:
            w.writerow(r)

    print(f"Read {len(rows)} Schwab rows → wrote {len(out_rows)} Wealthfolio rows to {args.output_csv}")
    if skipped.get("Journal"):
        print(f"  Skipped {skipped['Journal']} Journal rows (paired book adjustments, net-zero)")
    if args.aggregate_closed:
        print(f"  Aggregation: {skipped.get('contracts_closed',0)} closed contracts "
              f"({skipped.get('activities_folded',0)} activities) → folded into daily-per-underlying CREDITs")
        print(f"  Currently-open: {skipped.get('still_open_contracts',0)} contracts "
              f"({skipped.get('still_open_activities_dropped',0)} activities) omitted from output")


if __name__ == "__main__":
    main()
