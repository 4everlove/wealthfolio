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

Usage:
  python3 scripts/import/schwab_to_wf.py \\
    ~/Downloads/schwab_transactions.csv \\
    ~/Downloads/wf_schwab.csv \\
    --account-id "Shwab RothIRA"
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sys
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


def convert(rows: list[dict], account_id: str) -> list[dict]:
    out = []
    skipped = {"Journal": 0}
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

    return out, skipped


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input_csv", type=Path)
    ap.add_argument("output_csv", type=Path)
    ap.add_argument("--account-id", required=True, help="Wealthfolio account name/id")
    args = ap.parse_args()

    with args.input_csv.open() as f:
        rows = list(csv.DictReader(f))

    out_rows, skipped = convert(rows, args.account_id)

    with args.output_csv.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=WF_HEADER)
        w.writeheader()
        for r in out_rows:
            w.writerow(r)

    print(f"Read {len(rows)} Schwab rows → wrote {len(out_rows)} Wealthfolio rows to {args.output_csv}")
    if skipped["Journal"]:
        print(f"  Skipped {skipped['Journal']} Journal rows (paired book adjustments, net-zero)")


if __name__ == "__main__":
    main()
