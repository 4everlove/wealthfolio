#!/usr/bin/env python3
"""Convert a HealthEquity transaction export (HTML-in-.xls) to a Wealthfolio import CSV.

Rules applied (per user spec):
  - "Employee Contribution" / "Employer Contribution" -> DEPOSIT (absolute amount).
  - "Investment: XXXXX" -> BUY (amount negative) or SELL (amount positive), where
    quantity = |amount| / Yahoo close on that date, unitPrice = close.
  - "Interest ..." -> INTEREST (absolute amount).
  - "Investment Admin Fee ..." -> FEE (absolute amount).
  - Synthesised DRIP rows: for every Yahoo distribution date on any held symbol,
    emit a DIVIDEND row with subtype=DRIP where amount = distribution * holdings,
    unitPrice = close on that date, quantity = amount / unitPrice.

Weekend / holiday transaction dates fall back to the previous available trading
close (Yahoo has no bar those days).

Usage:
  python scripts/import/healthequity_to_wf_csv.py \
      /path/to/TransactionHistory.xls \
      /path/to/output.csv \
      [--quantity-decimals 5]

Dependencies:
  yfinance, beautifulsoup4, lxml
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable

try:
    import yfinance as yf
    from bs4 import BeautifulSoup
except ImportError as exc:
    sys.exit(f"missing dependency: {exc}. Install with `pip install yfinance beautifulsoup4 lxml`.")


# ─────────────────────────────────────────────────────────────────────────────
# Parsing
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RawTxn:
    date: date
    transaction: str
    amount: Decimal


AMOUNT_RE = re.compile(r"^\s*\(?\s*\$?\s*(-?[\d,]+(?:\.\d+)?)\s*\)?\s*$")


def parse_amount(cell: str) -> Decimal:
    """HealthEquity uses '$1,234.56' for positives and '($1,234.56)' for negatives."""
    cell = cell.replace("\xa0", " ").strip()
    if not cell:
        return Decimal("0")
    negative = cell.startswith("(") and cell.endswith(")")
    m = AMOUNT_RE.match(cell)
    if not m:
        raise ValueError(f"unrecognised amount: {cell!r}")
    value = Decimal(m.group(1).replace(",", ""))
    return -value if negative else value


def parse_export(path: Path) -> list[RawTxn]:
    """Parse the HealthEquity HTML-in-.xls export into RawTxn rows."""
    with path.open("r", encoding="utf-8", errors="replace") as f:
        soup = BeautifulSoup(f.read(), "lxml")

    out: list[RawTxn] = []
    for tr in soup.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 3:
            continue
        date_cell = cells[0].get_text(strip=True)
        txn_cell = cells[1].get_text(strip=True)
        amount_cell = cells[2].get_text(strip=True)
        if date_cell.lower() == "date":
            continue
        try:
            d = datetime.strptime(date_cell, "%m/%d/%Y").date()
        except ValueError:
            continue
        try:
            amount = parse_amount(amount_cell)
        except ValueError:
            continue
        out.append(RawTxn(d, txn_cell, amount))

    out.sort(key=lambda t: t.date)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Yahoo lookups
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SymbolData:
    close_by_date: dict[date, Decimal] = field(default_factory=dict)
    dividends_by_date: dict[date, Decimal] = field(default_factory=dict)
    sorted_close_dates: list[date] = field(default_factory=list)

    def close_on_or_before(self, d: date) -> Decimal | None:
        """Return the close on date `d`, or the most recent close before it."""
        if d in self.close_by_date:
            return self.close_by_date[d]
        # binary search over sorted_close_dates
        lo, hi = 0, len(self.sorted_close_dates)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.sorted_close_dates[mid] <= d:
                lo = mid + 1
            else:
                hi = mid
        idx = lo - 1
        if idx < 0:
            return None
        return self.close_by_date[self.sorted_close_dates[idx]]

    def close_strictly_before(self, d: date) -> Decimal | None:
        """Return the most recent close strictly before date `d`."""
        # binary search for greatest date < d
        lo, hi = 0, len(self.sorted_close_dates)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.sorted_close_dates[mid] < d:
                lo = mid + 1
            else:
                hi = mid
        idx = lo - 1
        if idx < 0:
            return None
        return self.close_by_date[self.sorted_close_dates[idx]]


def fetch_symbol(symbol: str, start: date, end: date) -> SymbolData:
    """Fetch daily closes + dividends for `symbol` between `start` and `end` inclusive."""
    ticker = yf.Ticker(symbol)
    # auto_adjust=False keeps the un-adjusted close for cost-basis math.
    hist = ticker.history(
        start=start.isoformat(),
        end=(end.toordinal() + 1) and datetime.fromordinal(end.toordinal() + 1).date().isoformat(),
        auto_adjust=False,
    )
    data = SymbolData()
    for ts, row in hist.iterrows():
        d = ts.date()
        data.close_by_date[d] = Decimal(str(row["Close"]))
    data.sorted_close_dates = sorted(data.close_by_date.keys())

    divs = ticker.dividends
    for ts, val in divs.items():
        d = ts.date()
        if start <= d <= end and val > 0:
            data.dividends_by_date[d] = Decimal(str(val))
    return data


# ─────────────────────────────────────────────────────────────────────────────
# Event stream
# ─────────────────────────────────────────────────────────────────────────────

CSV_HEADER = [
    "date", "symbol", "instrumentType", "quantity", "activityType",
    "unitPrice", "currency", "fee", "tax", "amount", "fxRate", "subtype",
]


def dec(value: Decimal | int | float, places: int) -> str:
    q = Decimal(10) ** -places
    return str(Decimal(value).quantize(q, rounding=ROUND_HALF_UP))


def build_rows(
    txns: list[RawTxn],
    symbol_data: dict[str, SymbolData],
    quantity_decimals: int,
    price_decimals: int = 4,
    amount_decimals: int = 2,
    final_holdings: dict[str, Decimal] | None = None,
    drip_price: str = "prev",  # "prev" (previous trading close) | "same" (ex-div close)
) -> list[dict]:
    holdings: dict[str, Decimal] = {}
    rows: list[dict] = []

    # Build an event stream: real transactions + synthesised dividend events.
    events: list[tuple[date, int, str, object]] = []
    # priority within a day: 0 = dividend (applies to prior-day holdings), 1 = txn
    for txn in txns:
        events.append((txn.date, 1, "txn", txn))
    for sym, data in symbol_data.items():
        for d, amount in data.dividends_by_date.items():
            events.append((d, 0, "div", (sym, amount)))
    events.sort(key=lambda e: (e[0], e[1]))

    investment_re = re.compile(r"^Investment:\s*(\S+)")

    # Precompute the last Investment date per symbol so cleanup SELLs zero out
    # the position instead of leaving accumulated DRIP drift behind.
    last_investment_date: dict[str, date] = {}
    for t in txns:
        m = investment_re.match(t.transaction)
        if m:
            sym = m.group(1)
            if sym not in last_investment_date or t.date > last_investment_date[sym]:
                last_investment_date[sym] = t.date

    for ev_date, _, kind, payload in events:
        if kind == "div":
            sym, div_per_share = payload
            held = holdings.get(sym, Decimal("0"))
            if held <= 0:
                continue
            # Mutual fund NAV drops on ex-div morning; the reinvestment price
            # that most closely matches HealthEquity's actual DRIP execution is
            # the previous trading day's close. Fall back to same-day if the
            # user overrides via CLI.
            if drip_price == "prev":
                close = symbol_data[sym].close_strictly_before(ev_date)
            else:
                close = symbol_data[sym].close_on_or_before(ev_date)
            if close is None or close <= 0:
                print(f"! DRIP skipped: no close for {sym} on {ev_date}", file=sys.stderr)
                continue
            amount = held * div_per_share
            reinvested_qty = amount / close
            holdings[sym] = held + reinvested_qty
            rows.append({
                "date": ev_date.isoformat(),
                "symbol": sym,
                "instrumentType": "EQUITY",
                "quantity": dec(reinvested_qty, quantity_decimals),
                "activityType": "DIVIDEND",
                "unitPrice": dec(close, price_decimals),
                "currency": "USD",
                "fee": "0",
                "tax": "0",
                "amount": dec(amount, amount_decimals),
                "fxRate": "",
                "subtype": "DRIP",
            })
            continue

        # kind == "txn"
        txn: RawTxn = payload
        t = txn.transaction
        amt = txn.amount

        if t.startswith("Investment:"):
            m = investment_re.match(t)
            if not m:
                print(f"! unrecognised investment row: {t!r}", file=sys.stderr)
                continue
            sym = m.group(1)
            data = symbol_data.get(sym)
            if data is None:
                print(f"! no Yahoo data cached for {sym}", file=sys.stderr)
                continue
            close = data.close_on_or_before(txn.date)
            if close is None or close <= 0:
                print(f"! txn skipped: no close for {sym} on {txn.date}", file=sys.stderr)
                continue
            cash = abs(amt)
            qty = cash / close
            action = "SELL" if amt > 0 else "BUY"
            if action == "BUY":
                holdings[sym] = holdings.get(sym, Decimal("0")) + qty
            else:
                # SELL clamp: when this SELL is the final Investment: entry for
                # the symbol, treat as broker "sell all" so no residual drift
                # from synthesised DRIP is left behind. Partial mid-history
                # sells stay at their Yahoo-implied quantity; use
                # --final-holdings for post-hoc reconciliation instead.
                current = holdings.get(sym, Decimal("0"))
                is_last = txn.date == last_investment_date.get(sym)
                if is_last and current > 0:
                    if qty != current:
                        print(
                            f"* SELL clamp {sym} {txn.date}: implied {qty:.5f} "
                            f"→ {current:.5f} (sell-all)",
                            file=sys.stderr,
                        )
                    qty = current
                holdings[sym] = current - qty
            rows.append({
                "date": txn.date.isoformat(),
                "symbol": sym,
                "instrumentType": "EQUITY",
                "quantity": dec(qty, quantity_decimals),
                "activityType": action,
                "unitPrice": dec(close, price_decimals),
                "currency": "USD",
                "fee": "0",
                "tax": "0",
                "amount": dec(cash, amount_decimals),
                "fxRate": "",
                "subtype": "",
            })
        elif "Contribution" in t:  # Employee or Employer
            rows.append({
                "date": txn.date.isoformat(),
                "symbol": "",
                "instrumentType": "",
                "quantity": "",
                "activityType": "DEPOSIT",
                "unitPrice": "",
                "currency": "USD",
                "fee": "0",
                "tax": "0",
                "amount": dec(abs(amt), amount_decimals),
                "fxRate": "",
                "subtype": "",
            })
        elif t.startswith("Interest"):
            rows.append({
                "date": txn.date.isoformat(),
                "symbol": "",
                "instrumentType": "",
                "quantity": "",
                "activityType": "INTEREST",
                "unitPrice": "",
                "currency": "USD",
                "fee": "0",
                "tax": "0",
                "amount": dec(abs(amt), amount_decimals),
                "fxRate": "",
                "subtype": "",
            })
        elif t.startswith("Investment Admin Fee"):
            rows.append({
                "date": txn.date.isoformat(),
                "symbol": "",
                "instrumentType": "",
                "quantity": "",
                "activityType": "FEE",
                "unitPrice": "",
                "currency": "USD",
                "fee": "0",
                "tax": "0",
                "amount": dec(abs(amt), amount_decimals),
                "fxRate": "",
                "subtype": "",
            })
        else:
            print(f"! unrecognised transaction: {t!r}", file=sys.stderr)

    # Post-hoc reconciliation for accumulated drift (mainly from synthesised DRIP
    # rounding vs actual broker execution). Emits ADJUSTMENT rows dated the day
    # after the last transaction. NOTE: Wealthfolio's current holdings compiler
    # treats ADJUSTMENT without a recognised subtype as a no-op, so these rows
    # document the intent but you may need to nudge holdings manually in-app.
    if final_holdings:
        reconcile_date = (max(t.date for t in txns) if txns else date.today())
        for sym, target in final_holdings.items():
            actual = holdings.get(sym, Decimal("0"))
            delta = target - actual
            if abs(delta) < Decimal("0.00001"):
                continue
            data = symbol_data.get(sym)
            last_close = data.close_on_or_before(reconcile_date) if data else None
            print(
                f"* reconcile {sym}: computed {actual} vs target {target} "
                f"(delta {delta:+})",
                file=sys.stderr,
            )
            rows.append({
                "date": reconcile_date.isoformat(),
                "symbol": sym,
                "instrumentType": "EQUITY",
                "quantity": dec(abs(delta), quantity_decimals),
                "activityType": "ADJUSTMENT",
                "unitPrice": dec(last_close, price_decimals) if last_close else "",
                "currency": "USD",
                "fee": "0",
                "tax": "0",
                "amount": "0",
                "fxRate": "",
                "subtype": "",
            })
            holdings[sym] = target

    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("input", type=Path, help="HealthEquity TransactionHistory.xls (actually HTML)")
    ap.add_argument("output", type=Path, help="Wealthfolio-importable CSV path")
    ap.add_argument("--quantity-decimals", type=int, default=5, help="quantity precision (default 5)")
    ap.add_argument(
        "--final-holdings",
        action="append",
        default=[],
        metavar="SYM=QTY",
        help="expected final quantity for a symbol; emits an ADJUSTMENT row to "
             "reconcile drift. Repeatable (e.g. --final-holdings VIIIX=69.490).",
    )
    ap.add_argument(
        "--drip-price",
        choices=("prev", "same"),
        default="prev",
        help="close used for synthesised DRIP reinvestment. 'prev' (default) uses "
             "the previous trading day's close, which best matches broker "
             "execution because mutual fund NAV drops on ex-div morning. 'same' "
             "uses the ex-div date close.",
    )
    args = ap.parse_args()

    final_holdings: dict[str, Decimal] = {}
    for spec in args.final_holdings:
        if "=" not in spec:
            sys.exit(f"--final-holdings expects SYM=QTY, got {spec!r}")
        sym, qty = spec.split("=", 1)
        final_holdings[sym.strip().upper()] = Decimal(qty.strip())

    txns = parse_export(args.input)
    if not txns:
        sys.exit("no transactions parsed")
    print(f"parsed {len(txns)} transactions ({txns[0].date} → {txns[-1].date})", file=sys.stderr)

    # Extract symbols from Investment: rows.
    symbols = sorted({
        m.group(1)
        for t in txns
        if (m := re.match(r"^Investment:\s*(\S+)", t.transaction))
    })
    print(f"symbols: {symbols}", file=sys.stderr)

    start = txns[0].date
    end = date.today()
    symbol_data: dict[str, SymbolData] = {}
    for sym in symbols:
        print(f"fetching Yahoo history for {sym} …", file=sys.stderr)
        symbol_data[sym] = fetch_symbol(sym, start, end)
        print(
            f"  {sym}: {len(symbol_data[sym].close_by_date)} closes, "
            f"{len(symbol_data[sym].dividends_by_date)} dividend events",
            file=sys.stderr,
        )

    rows = build_rows(
        txns,
        symbol_data,
        quantity_decimals=args.quantity_decimals,
        final_holdings=final_holdings or None,
        drip_price=args.drip_price,
    )

    with args.output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
        writer.writeheader()
        writer.writerows(rows)

    print(f"wrote {len(rows)} rows to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
