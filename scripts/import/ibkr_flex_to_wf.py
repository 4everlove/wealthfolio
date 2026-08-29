#!/usr/bin/env python3
"""Convert an IBKR Flex Query CSV to a Wealthfolio import CSV.

Expected Flex sections in the CSV (in any order, each preceded by its header row):
  1. Trades           — every fill, plus book trades for expiry/assignment
  2. OptionEAE        — Option Exercise/Assignment/Expiration + Cash Settlement rows
  3. MtM Prices       — daily marks (ignored)
  4. CashTransactions — dividends, interest, fees, deposits, withdrawals
  5. Transfers        — ACATS/ATON in for cash and securities (optional but
                        recommended; without it, ACATS-in cash must be posted
                        manually to explain apparent negative cash balances)
  6. CorporateActions — equity forward/reverse splits emitted as SPLIT rows.
                        Options splits, spinoffs, mergers, ticker changes are
                        warned + skipped (they need manual close/open pairs).

Behavior:
  - Round-trip predicate: for each (account, asset), if position was 0 at
    start of day AND back to 0 at end of day, that day's fills are aggregated
    into a single CREDIT (or FEE if negative) row per (date, currency, bucket).
    Buckets: SPXW (SPX/SPXW/XSP/NDX/RUT/VIX index options), STK, FUT, OPT.
  - Multi-day holds emit per-trade BUY/SELL rows.
  - Expiry (Ep/Ex) at $0 → ADJUSTMENT with subtype OPTION_EXPIRY (no cash).
  - Assignment (A) on OPT → same ADJUSTMENT(OPTION_EXPIRY); the paired STK
    row in Trades is emitted as BUY/SELL at strike.
  - OptionEAE Cash Settlement → ADJUSTMENT cash row for the settlement amount
    (folds into the daily aggregate when the underlying option round-tripped
    that day; otherwise emitted as a standalone cash line).
  - OptionEAE Buy/Sell STK rows: skipped (dedup by TradeID against Trades).
  - Cash txns: Dividends → DIVIDEND, Broker Interest → INTEREST,
               Other Fees → FEE, Deposits/Withdrawals → DEPOSIT/WITHDRAWAL.

Usage:
  python scripts/import/ibkr_flex_to_wf.py \
      /path/to/flex.csv \
      /path/to/output.csv \
      --account-map ~/.wealthfolio/ibkr_accounts.yml
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Optional source timezone for parsed DateTime. When set via --source-tz, each
# emitted ISO datetime carries the correct UTC offset (DST-aware).
_SOURCE_TZ: ZoneInfo | None = None



# ---------- Section detection ----------

_TRADES_BASE = (
    "ClientAccountID",
    "CurrencyPrimary",
    "AssetClass",
    "Symbol",
    "Description",
    "UnderlyingSymbol",
    "Multiplier",
    "Strike",
    "Expiry",
    "Put/Call",
    "TradeDate",
    "TransactionType",
    "Quantity",
    "TradePrice",
    "IBCommission",
    "IBCommissionCurrency",
    "NetCash",
    "Notes/Codes",
    "OrigOrderID",
    "Buy/Sell",
    "OrderTime",
    "TradeID",
)
# DateTime column added in a later Flex Query revision; optional for back-compat.
# Contains "YYYYMMDD;HHMMSS" reflecting actual execution (e.g. Sunday-evening CME
# trades) rather than IBKR's TradeDate settlement convention.
_TRADES_WITH_DATETIME = _TRADES_BASE + ("DateTime",)

SECTION_HEADERS = {
    "trades": _TRADES_WITH_DATETIME,
    "option_eae": (
        "ClientAccountID",
        "CurrencyPrimary",
        "UnderlyingSymbol",
        "Multiplier",
        "Strike",
        "Expiry",
        "Put/Call",
        "Date",
        "Transaction Type",
        "Quantity",
        "Trade Price",
        "Close Price",
        "Proceeds",
        "Comm/Tax",
        "TradeID",
    ),
    "mtm_prices": (
        "ClientAccountID",
        "CurrencyPrimary",
        "AssetClass",
        "Symbol",
        "UnderlyingSymbol",
        "Multiplier",
        "Strike",
        "Expiry",
        "Put/Call",
        "Price",
    ),
    "cash_txn": (
        "ClientAccountID",
        "CurrencyPrimary",
        "Description",
        "Date/Time",
        "Amount",
        "Type",
    ),
    # OpenPositions: field set varies across Flex Query configs. Detected
    # by signature (starts with ClientAccountID + AssetClass + Symbol +
    # UnderlyingSymbol) rather than exact-match tuple.
    "open_positions": None,
    # Transfers: ACATS/ATON in/out for cash and securities. Detected by
    # presence of TransferCompany + CashTransfer + TransactionID columns
    # (the last two must be enabled in the Flex Query).
    "transfers": None,
    # CorporateActions: splits, spinoffs, mergers, ticker changes. Detected
    # by presence of ActionID + Type + Value (distinct from Trades'
    # TransactionType/TradeID and Transfers' TransferCompany).
    "corporate_actions": None,
}


def _is_open_positions_header(row: list[str]) -> bool:
    return (
        len(row) >= 8
        and row[0] == "ClientAccountID"
        and row[1] == "AssetClass"
        and row[2] == "Symbol"
        and row[3] == "UnderlyingSymbol"
        and "CostBasisMoney" in row
    )


def _is_transfers_header(row: list[str]) -> bool:
    return (
        row
        and row[0] == "ClientAccountID"
        and "TransferCompany" in row
        and "CashTransfer" in row
    )


def _is_corporate_actions_header(row: list[str]) -> bool:
    return (
        row
        and row[0] == "ClientAccountID"
        and "ActionID" in row
        and "Value" in row
        and "Report Date" in row
    )

INDEX_UNDERLYINGS = {"SPX", "SPXW", "XSP", "NDX", "RUT", "VIX", "RUTW", "NDXP"}


def parse_sections(csv_path: Path) -> dict[str, list[dict]]:
    """Split a multi-section IBKR Flex CSV into named record lists."""
    sections: dict[str, list[dict]] = {k: [] for k in SECTION_HEADERS}
    current_section: str | None = None
    current_header: tuple[str, ...] | None = None

    # Old Flex Query variants may omit the DateTime column on Trades. Accept
    # either shape and let downstream code fall back to TradeDate when absent.
    trades_variants = {_TRADES_BASE: _TRADES_BASE, _TRADES_WITH_DATETIME: _TRADES_WITH_DATETIME}

    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            row_tuple = tuple(row)
            matched = None
            for name, header in SECTION_HEADERS.items():
                if header is not None and row_tuple == header:
                    matched = name
                    break
            if matched is None and row_tuple in trades_variants:
                matched = "trades"
                current_section = matched
                current_header = trades_variants[row_tuple]
                continue
            if matched is None and _is_open_positions_header(row):
                matched = "open_positions"
                current_section = matched
                current_header = row_tuple  # capture the actual field set
                continue
            if matched is None and _is_transfers_header(row):
                matched = "transfers"
                current_section = matched
                current_header = row_tuple  # capture the actual field set
                continue
            if matched is None and _is_corporate_actions_header(row):
                matched = "corporate_actions"
                current_section = matched
                current_header = row_tuple  # capture the actual field set
                continue
            if matched is not None:
                current_section = matched
                current_header = SECTION_HEADERS[matched]
                continue
            if current_section is None or current_header is None:
                continue
            if len(row) != len(current_header):
                continue
            sections[current_section].append(dict(zip(current_header, row)))
    return sections


# ---------- Symbol composition ----------

MONTH_ABBR = {
    1: "JAN", 2: "FEB", 3: "MAR", 4: "APR", 5: "MAY", 6: "JUN",
    7: "JUL", 8: "AUG", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC",
}


def compose_occ_symbol(root: str, expiry_yyyymmdd: str, put_call: str, strike: str) -> str:
    """Build OCC 21-char option symbol: 6-char root + YYMMDD + C/P + 8-digit strike*1000."""
    d = datetime.strptime(expiry_yyyymmdd, "%Y%m%d").date()
    yy_mm_dd = d.strftime("%y%m%d")
    strike_int = int(round(Decimal(strike) * 1000))
    return f"{root.ljust(6)}{yy_mm_dd}{put_call.upper()}{strike_int:08d}"


def normalize_trades_symbol(sym: str, asset_class: str) -> str:
    """Trades section already gives us usable symbols; normalize spacing for options."""
    if asset_class == "OPT":
        # IBKR format has variable inner whitespace; collapse to a single space
        # only in the root area. Actual OCC standard uses space-padded 6-char root.
        # e.g. "IBIT  260605C00042500" -> "IBIT  260605C00042500" (already OK).
        # For SPXW: "SPXW  260601C07575000" -> keep as-is.
        return sym
    return sym.strip()


# ---------- Round-trip detector ----------

@dataclass
class DailyPosition:
    start_qty: Decimal = Decimal(0)
    end_qty: Decimal = Decimal(0)
    trades: list[dict] = field(default_factory=list)


def bucket_for(asset_class: str, underlying: str) -> str:
    if asset_class == "OPT" and underlying in INDEX_UNDERLYINGS:
        return "SPXW"
    if asset_class == "OPT":
        return "OPT"
    if asset_class == "FOP":
        # Futures options — IBKR proprietary symbol format, no OCC. Multi-day
        # FOPs would need pseudo-asset design; for now only 0DTE FOPs aggregate.
        return "FOP"
    if asset_class == "FUT":
        return "FUT"
    if asset_class == "STK":
        return "STK"
    return "OTHER"


def execution_date(row: dict) -> str:
    """Actual execution date (YYYYMMDD) from Trades row.

    Prefers the DateTime column (contains YYYYMMDD;HHMMSS) so overnight CME
    futures trades that IBKR reports with TradeDate set to the T+1 settlement
    convention still bucket on the calendar day they actually executed.
    Falls back to TradeDate when DateTime is absent (older Flex Query variants).
    """
    dt = (row.get("DateTime") or "").strip()
    if dt:
        return dt.split(";", 1)[0]
    return row["TradeDate"]


def execution_iso(row: dict) -> str:
    """ISO 8601 datetime string for Wealthfolio's activity date.

    Uses full YYYY-MM-DDTHH:MM:SS when DateTime is present, else date only.
    When --source-tz is set the emitted string carries a UTC offset computed
    for that specific instant (DST-aware); otherwise emitted naive.
    """
    dt = (row.get("DateTime") or "").strip()
    if dt and ";" in dt:
        ymd, hms = dt.split(";", 1)
        if len(hms) == 6:
            naive = datetime(
                int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]),
                int(hms[:2]), int(hms[2:4]), int(hms[4:6]),
            )
            if _SOURCE_TZ is not None:
                aware = naive.replace(tzinfo=_SOURCE_TZ)
                # isoformat() emits e.g. "2019-09-04T21:53:25-04:00"
                return aware.isoformat()
            return naive.isoformat(timespec="seconds")
    d = execution_date(row)
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def futures_root_from_symbol(symbol: str) -> str:
    """Best-effort root extraction from a CME futures symbol.

    Handles common shapes: ESM6, MESU9, MESH27, 6EM26 → ES, MES, MES, 6E.
    Strips a trailing month code (F/G/H/J/K/M/N/Q/U/V/X/Z) + 1–2 digit year.
    Falls back to the symbol as-is when the pattern doesn't match."""
    s = symbol.strip().upper()
    if not s:
        return s
    import re
    m = re.match(r"^(.*?)([FGHJKMNQUVXZ])(\d{1,2})$", s)
    return m.group(1) if m else s


def signed_qty(row: dict) -> Decimal:
    """Signed quantity from Trades row: positive for BUY, negative for SELL."""
    qty = Decimal(row["Quantity"])
    # IBKR Quantity is already signed on many rows, but Buy/Sell is the truth.
    if row.get("Buy/Sell") == "SELL":
        return -abs(qty)
    return abs(qty)


def is_zero_price_expiry(row: dict) -> bool:
    """Book trade at price 0 with expiry/assignment code."""
    if row.get("TransactionType") != "BookTrade":
        return False
    try:
        if Decimal(row["TradePrice"]) != 0:
            return False
    except Exception:
        return False
    return (row.get("Notes/Codes") or "").upper() in {"A", "EP", "EX"}


def consolidate_fills(rows: list[dict]) -> list[dict]:
    """Merge child fills of the same parent order into a single row.

    IBKR reports every partial fill of an order as a separate Trades row
    (same DateTime, same TradePrice, same account+symbol+side). This
    inflates row counts on days that would otherwise emit per-trade.
    Merge them: sum Quantity, IBCommission, and NetCash. Keep the first
    row's identity fields (TradeID, OrderTime, Notes/Codes).

    Only merges rows that share ALL of (account, symbol, TransactionType,
    Buy/Sell, DateTime, TradePrice). BookTrade Ep rows (price=0) never
    merge with regular fills because TradePrice differs."""
    from collections import defaultdict
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        key = (
            r.get("ClientAccountID") or "",
            r.get("Symbol") or "",
            r.get("TransactionType") or "",
            r.get("Buy/Sell") or "",
            r.get("DateTime") or r.get("TradeDate") or "",
            r.get("TradePrice") or "",
        )
        groups[key].append(r)

    out: list[dict] = []
    for key, fills in groups.items():
        if len(fills) == 1:
            out.append(fills[0])
            continue
        # Merge — pick first row as template (keeps TradeID, OrderTime, etc.)
        merged = dict(fills[0])
        qty_sum = Decimal(0)
        comm_sum = Decimal(0)
        cash_sum = Decimal(0)
        for f in fills:
            try:
                qty_sum += Decimal(f.get("Quantity") or "0")
            except Exception:
                pass
            try:
                comm_sum += Decimal(f.get("IBCommission") or "0")
            except Exception:
                pass
            try:
                cash_sum += Decimal(f.get("NetCash") or "0")
            except Exception:
                pass
        merged["Quantity"] = str(qty_sum)
        merged["IBCommission"] = str(comm_sum)
        merged["NetCash"] = str(cash_sum)
        out.append(merged)
    # Preserve original ordering by earliest TradeID within groups
    out.sort(key=lambda r: (r.get("DateTime") or r.get("TradeDate") or "", r.get("TradeID") or ""))
    return out


# ---------- Wealthfolio row emitter ----------

WF_HEADER = [
    "date", "symbol", "instrumentType", "quantity", "activityType",
    "unitPrice", "currency", "fee", "tax", "amount", "fxRate", "subtype",
    "quoteMode", "accountId", "notes", "sourceRecordId",
]


def wf_row(**kw) -> dict:
    row = {k: "" for k in WF_HEADER}
    row.update(kw)
    return row


def synth_id(prefix: str, *parts) -> str:
    key = "|".join(str(p) for p in parts)
    return f"{prefix}:{hashlib.sha1(key.encode()).hexdigest()[:16]}"


def fmt_amount(x: Decimal) -> str:
    return f"{x:.6f}".rstrip("0").rstrip(".") or "0"


# ---------- Converters ----------

def convert_trades(
    trades: list[dict],
    cash_settlements: dict[tuple[str, str, str, str, str, str, str], Decimal],
    account_map: dict[str, str],
    warnings: list[str],
    initial_positions: dict[tuple[str, str], Decimal] | None = None,
) -> list[dict]:
    """Produce Wealthfolio rows from Trades + folded-in cash settlements.

    cash_settlements: keyed by (account, currency, underlying, expiry, put_call,
    strike_str, date) → Proceeds sum. Consumed as we emit per-trade rows or
    folded into daily aggregates for round-tripped assets.

    initial_positions: seed for the per-asset position walk. Without a seed,
    the walk starts at zero and can misclassify a "managed existing position"
    day-trade (e.g. sell 100 + buy 100 same day, but the account had a
    persistent +100 long all along) as a round-trip → aggregated in error.
    Keyed by (account_id, IBKR symbol).
    """
    output: list[dict] = []
    initial_positions = initial_positions or {}

    # Consolidate multi-fill child orders before bucketing. IBKR reports
    # every partial fill separately even though they share DateTime, price,
    # account, symbol, and side — which inflates per-trade emit output on
    # non-aggregating days.
    trades = consolidate_fills(trades)

    # Bucket trades by (account, symbol) so we can walk chronologically and
    # find round-trip days per asset.
    per_asset: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in trades:
        acct = row["ClientAccountID"]
        # Skip rows with no useful account (headers etc.)
        if not acct:
            continue
        key = (acct, row["Symbol"])
        per_asset[key].append(row)

    # For aggregation: (account, date, currency, bucket) → running sum of NetCash
    # NetCash already has commission netted in — do NOT add fee separately (would
    # double-count when Wealthfolio's CREDIT handler subtracts fee from amount).
    agg_cash: dict[tuple[str, date, str, str], Decimal] = defaultdict(lambda: Decimal(0))
    agg_commission_ref: dict[tuple[str, date, str, str], Decimal] = defaultdict(lambda: Decimal(0))
    agg_settle_keys: set[tuple[str, str, str, str, str, str, str]] = set()

    per_trade_rows: list[dict] = []

    for (acct, symbol), rows in per_asset.items():
        rows.sort(key=lambda r: (execution_date(r), r.get("OrderTime", "")))
        # Seed the walk from OpenPositions so persistent-position round-trips
        # aren't misclassified as day-trades. Note: IBKR's OpenPositions
        # Symbol column uses the OCC-format for options (with padded root)
        # but the Trades section uses IBKR's proprietary format for FOPs
        # (e.g. `E1DK6 C7355`). Match on the Symbol as it appears in Trades.
        running_qty = initial_positions.get((acct, symbol), Decimal(0))
        # Walk day by day, keyed on execution date (not IBKR's TradeDate
        # settlement convention), so overnight round-trips bucket correctly.
        by_day: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            by_day[execution_date(r)].append(r)

        days_sorted = sorted(by_day.keys())
        for d in days_sorted:
            start_qty = running_qty
            day_rows = by_day[d]
            end_qty = start_qty + sum(signed_qty(r) for r in day_rows)

            # Unified rule: if the day ends flat for this contract, roll
            # everything (intraday trades + Ep + cash settlement) into the
            # daily P&L bucket. If a carryover survived from a prior day,
            # emit ONE synthetic ADJUSTMENT(OPTION_EXPIRY) to close it.
            #
            # Position walk stays exact (CREDIT is qty-neutral; synth
            # ADJUSTMENT closes the carryover exactly). Cash flow stays
            # exact (CREDIT captures every NetCash + cash settlement).
            # Per-lot realized P&L becomes an approximation — the
            # carryover cost basis is released by the ADJUSTMENT and the
            # intraday-opened lots' cost basis is folded into the CREDIT
            # amount. Total P&L matches IBKR.
            if end_qty == 0 and day_rows:
                first = day_rows[0]
                ac = first["AssetClass"]
                und = first["UnderlyingSymbol"]
                bucket = bucket_for(ac, und)
                currency = first["CurrencyPrimary"]
                trade_date = datetime.strptime(d, "%Y%m%d").date()
                key = (acct, trade_date, currency, bucket)
                for r in day_rows:
                    try:
                        agg_cash[key] += Decimal(r["NetCash"] or "0")
                    except Exception:
                        pass
                    try:
                        agg_commission_ref[key] += abs(Decimal(r["IBCommission"] or "0"))
                    except Exception:
                        pass
                # Fold matching OptionEAE Cash Settlement into the bucket
                # ONCE per unique settle key. Applies to option days only.
                if ac == "OPT":
                    seen_in_day: set = set()
                    for r in day_rows:
                        settle_key = _settle_key_from_trade(r)
                        if settle_key in seen_in_day:
                            continue
                        seen_in_day.add(settle_key)
                        if settle_key in cash_settlements and settle_key not in agg_settle_keys:
                            agg_cash[key] += cash_settlements[settle_key]
                            agg_settle_keys.add(settle_key)
                # Emit a synthetic close if a carryover is being reduced to
                # zero today. Uses ADJUSTMENT(OPTION_EXPIRY) for OPT/FOP
                # (WF's holdings engine reduces position magnitude by qty
                # via FIFO); for STK/FUT, emit a matching-sign SELL/BUY
                # at $0 so the walk closes correctly.
                if start_qty != 0:
                    symbol = normalize_trades_symbol(first["Symbol"], ac)
                    if ac == "FOP":
                        underlying_root = futures_root_from_symbol(first.get("UnderlyingSymbol") or "")
                        put_call = first.get("Put/Call") or ""
                        strike = first.get("Strike") or ""
                        expiry = first.get("Expiry") or ""
                        if underlying_root and put_call and strike and expiry:
                            try:
                                symbol = compose_occ_symbol(underlying_root, expiry, put_call, strike)
                            except Exception:
                                pass
                    instrument_type = {
                        "STK": "EQUITY", "OPT": "OPTION",
                        "FUT": "FUTURES", "FOP": "FUTURES_OPTION",
                    }.get(ac, "")
                    close_qty = abs(start_qty)
                    wf_acct = account_map.get(acct, acct)
                    if ac in ("OPT", "FOP"):
                        per_trade_rows.append(wf_row(
                            date=trade_date.isoformat(),
                            symbol=symbol,
                            instrumentType=instrument_type,
                            quantity=fmt_amount(close_qty),
                            activityType="ADJUSTMENT",
                            unitPrice="0",
                            currency=currency,
                            fee="0", tax="0", amount="0",
                            subtype="OPTION_EXPIRY",
                            quoteMode="MANUAL" if ac == "FOP" else "",
                            accountId=wf_acct,
                            notes="IBKR synthetic close of carryover (day ended flat, intraday folded into daily P&L)",
                            sourceRecordId=synth_id("ibkr_synth_close", acct, symbol, trade_date.isoformat(), str(close_qty)),
                        ))
                    else:
                        # Equity or futures: no OPTION_EXPIRY handling. Emit
                        # a SELL (for long carryover) or BUY (for short) at
                        # $0 that closes the position without cash impact.
                        activity = "SELL" if start_qty > 0 else "BUY"
                        per_trade_rows.append(wf_row(
                            date=trade_date.isoformat(),
                            symbol=symbol,
                            instrumentType=instrument_type,
                            quantity=fmt_amount(close_qty),
                            activityType=activity,
                            unitPrice="0",
                            currency=currency,
                            fee="0", tax="0", amount="",
                            accountId=wf_acct,
                            notes="IBKR synthetic close of carryover (day ended flat, intraday folded into daily P&L)",
                            sourceRecordId=synth_id("ibkr_synth_close", acct, symbol, trade_date.isoformat(), str(close_qty)),
                        ))
            else:
                # Position survives — emit per-trade rows.
                for r in day_rows:
                    for row_out in _emit_trade_row(r, acct, account_map, cash_settlements, agg_settle_keys, warnings):
                        per_trade_rows.append(row_out)
            running_qty = end_qty

    # Emit any orphan cash settlements (settlement without matching trade in window).
    for settle_key, proceeds in cash_settlements.items():
        if settle_key in agg_settle_keys:
            continue
        (acct, currency, underlying, expiry, put_call, strike_str, dstr) = settle_key
        trade_date = datetime.strptime(dstr, "%Y%m%d").date()
        occ = compose_occ_symbol(underlying, expiry, put_call, strike_str)
        wf_acct = account_map.get(acct, acct)
        # Emit as CREDIT (signed): ADJUSTMENT without OPTION_EXPIRY subtype is a
        # no-op in Wealthfolio's holdings calculator and would leak the cash.
        per_trade_rows.append(wf_row(
            date=trade_date.isoformat(),
            symbol="",
            instrumentType="",
            quantity="1",
            activityType="CREDIT",
            unitPrice=fmt_amount(proceeds),
            currency=currency,
            fee="0",
            tax="0",
            amount=fmt_amount(proceeds),
            subtype="IBKR_CASH_SETTLEMENT",
            accountId=wf_acct,
            notes=f"IBKR cash settlement: {occ}",
            sourceRecordId=synth_id("ibkr_eae", acct, occ, dstr),
        ))

    # Emit aggregated CREDIT rows with SIGNED amount + gross-of-fee. Wealthfolio
    # handle_income computes net = amount - fee - tax, applied signed, so this
    # works symmetrically for winning and losing days.
    #   NetCash sum is fee-netted, so amount = NetCash + commission (grosses up).
    for (acct, trade_date, currency, bucket), amt in agg_cash.items():
        wf_acct = account_map.get(acct, acct)
        commission = agg_commission_ref[(acct, trade_date, currency, bucket)]
        # gross = net + fee; Wealthfolio computes net_amount = amount - fee.
        # Works symmetrically for winning (positive amt) and losing (negative amt) days.
        gross = amt + commission
        output.append(wf_row(
            date=trade_date.isoformat(),
            symbol="",
            instrumentType="",
            quantity="1",
            activityType="CREDIT",
            unitPrice=fmt_amount(gross),
            currency=currency,
            fee=fmt_amount(commission),
            tax="0",
            amount=fmt_amount(gross),
            subtype="IBKR_DAILY",
            accountId=wf_acct,
            notes=f"IBKR {bucket} daily P&L (gross of commission)",
            sourceRecordId=synth_id("ibkr_agg", acct, trade_date, currency, bucket),
        ))

    output.extend(per_trade_rows)
    return output


def _settle_key_from_trade(row: dict) -> tuple[str, str, str, str, str, str, str]:
    """Compose the same key format used for OptionEAE Cash Settlement lookups."""
    return (
        row["ClientAccountID"],
        row["CurrencyPrimary"],
        row["UnderlyingSymbol"],
        row["Expiry"],
        row["Put/Call"],
        row["Strike"],
        row["TradeDate"],
    )


def _emit_trade_row(
    row: dict,
    acct: str,
    account_map: dict[str, str],
    cash_settlements: dict,
    agg_settle_keys: set,
    warnings: list[str],
) -> list[dict]:
    ac = row["AssetClass"]
    # Use full execution datetime when DateTime is present so overnight trades
    # land on the correct calendar day AND preserve HH:MM:SS in Wealthfolio.
    # Zero-price expiry BookTrades have DateTime blank → falls back to date.
    d_iso = execution_iso(row)
    d = datetime.strptime(execution_date(row), "%Y%m%d").date()
    wf_acct = account_map.get(acct, acct)
    currency = row["CurrencyPrimary"]
    symbol = normalize_trades_symbol(row["Symbol"], ac)
    qty = abs(Decimal(row["Quantity"]))
    buy_sell = row["Buy/Sell"]
    trade_id = row.get("TradeID") or ""

    instrument_type = {
        "STK": "EQUITY",
        "OPT": "OPTION",
        "FUT": "FUTURES",
        # FOP: emitted as FUTURES_OPTION (native type). Multiplier and
        # underlying's contract month resolved by Wealthfolio at asset
        # creation from the underlying's CONTRACT_SPECS entry.
        "FOP": "FUTURES_OPTION",
    }.get(ac, "")
    if not instrument_type:
        warnings.append(f"Skipped unknown AssetClass={ac} symbol={symbol}")
        return []

    # For FOP, compose an OCC-format symbol from the underlying's futures
    # root (strip month/year suffix) + Expiry + P/C + Strike. The
    # FuturesOption metadata (multiplier, tick, exchange) is derived server-
    # side from the underlying root's CONTRACT_SPECS lookup.
    if ac == "FOP":
        underlying_root = futures_root_from_symbol(row.get("UnderlyingSymbol") or "")
        put_call = row.get("Put/Call") or ""
        strike = row.get("Strike") or ""
        expiry = row.get("Expiry") or ""
        if underlying_root and put_call and strike and expiry:
            try:
                symbol = compose_occ_symbol(underlying_root, expiry, put_call, strike)
            except Exception:
                pass  # keep IBKR's proprietary symbol as fallback

    # Zero-price expiry / exercise / assignment on OPT/FOP → ADJUSTMENT(OPTION_EXPIRY).
    # instrumentType must match the underlying asset type (OPTION vs
    # FUTURES_OPTION); using a hardcoded OPTION for FOP would create a
    # duplicate asset and leave the real position open forever.
    if ac in ("OPT", "FOP") and is_zero_price_expiry(row):
        expiry_quote_mode = "MANUAL" if ac == "FOP" else ""
        # Use BookTrade DateTime (16:20 ET for SPXW; expiry-day close for others)
        # so the ADJUSTMENT lands AFTER intraday fills on the same day. Emitting
        # at midnight (d.isoformat()) fired before intraday buys and left ghost
        # positions on days that had both an expiry and other same-day trades.
        out = [wf_row(
            date=d_iso,
            symbol=symbol,
            instrumentType=instrument_type,
            quantity=fmt_amount(qty),
            activityType="ADJUSTMENT",
            unitPrice="0",
            currency=currency,
            fee="0", tax="0", amount="0",
            subtype="OPTION_EXPIRY",
            quoteMode=expiry_quote_mode,
            accountId=wf_acct,
            notes=f"IBKR {row.get('Notes/Codes') or ''} book close",
            sourceRecordId=synth_id("ibkr_trade", trade_id),
        )]
        # Fold paired OptionEAE Cash Settlement (ITM cash-settled index options)
        # into a companion CREDIT cash row. ADJUSTMENT without OPTION_EXPIRY
        # subtype is a no-op in the holdings calculator and would leak the cash.
        settle_key = _settle_key_from_trade(row)
        proceeds = cash_settlements.get(settle_key)
        if proceeds is not None and proceeds != 0:
            agg_settle_keys.add(settle_key)
            out.append(wf_row(
                date=d_iso,
                symbol="",
                instrumentType="",
                quantity="1",
                activityType="CREDIT",
                unitPrice=fmt_amount(proceeds),
                currency=currency,
                fee="0", tax="0",
                amount=fmt_amount(proceeds),
                subtype="IBKR_CASH_SETTLEMENT",
                accountId=wf_acct,
                notes=f"IBKR cash settlement: {symbol}",
                sourceRecordId=synth_id("ibkr_eae", acct, symbol, row['TradeDate']),
            ))
        return out

    # Standard BUY/SELL.
    try:
        unit_price = Decimal(row["TradePrice"])
    except Exception:
        unit_price = Decimal(0)
    try:
        net_cash = Decimal(row["NetCash"] or "0")
    except Exception:
        net_cash = Decimal(0)
    try:
        commission = abs(Decimal(row["IBCommission"] or "0"))
    except Exception:
        commission = Decimal(0)

    # FOP: no market data provider covers futures options; mark MANUAL so
    # Wealthfolio doesn't attempt quote sync (falls back to cost-basis MV).
    quote_mode = "MANUAL" if ac == "FOP" else ""

    # Leave amount blank for BUY/SELL; Wealthfolio computes qty × price × multiplier.
    # IBKR NetCash on FUT rows is variation margin, not notional — using it would
    # mismatch WF's cost-basis semantics.
    # Use ISO datetime (d_iso) to preserve HH:MM:SS; OPTION_EXPIRY and aggregate
    # rows above stay date-only since those events are end-of-day by nature.
    return [wf_row(
        date=d_iso,
        symbol=symbol,
        instrumentType=instrument_type,
        quantity=fmt_amount(qty),
        activityType=buy_sell,
        unitPrice=fmt_amount(unit_price),
        currency=currency,
        fee=fmt_amount(commission),
        tax="0",
        amount="",
        quoteMode=quote_mode,
        accountId=wf_acct,
        # Include TradeID in notes so WF's idempotency key (which uses only
        # date+notes, not full timestamp or sourceRecordId) differs between
        # multi-fill orders on the same day at the same price. Without this,
        # WF silently drops all but one fill.
        notes=f"IBKR {row.get('Notes/Codes') or ''} #{trade_id}".strip(),
        sourceRecordId=synth_id("ibkr_trade", trade_id),
    )]


def convert_cash_txns(cash_rows: list[dict], account_map: dict[str, str]) -> list[dict]:
    output = []
    # Fees aggregated to one row per (account, date, currency); most days have
    # 5-10 small market-data fee items that add no signal individually.
    fee_agg: dict[tuple[str, date, str], Decimal] = defaultdict(lambda: Decimal(0))
    fee_agg_count: dict[tuple[str, date, str], int] = defaultdict(int)

    for r in cash_rows:
        acct = r["ClientAccountID"]
        if not acct:
            continue
        wf_acct = account_map.get(acct, acct)
        currency = r["CurrencyPrimary"]
        try:
            amount = Decimal(r["Amount"] or "0")
        except Exception:
            continue
        dt = r["Date/Time"].split(";")[0]
        try:
            d = datetime.strptime(dt, "%Y%m%d").date()
        except ValueError:
            continue
        typ = r["Type"]
        description = r["Description"]

        activity_type = None
        subtype = ""
        symbol = ""
        instrument_type = ""
        if typ == "Dividends":
            activity_type = "DIVIDEND"
            # Description begins with ticker: "HYD(US...) CASH DIVIDEND ..."
            tick = description.split("(")[0].strip()
            symbol = tick
            instrument_type = "EQUITY"
        elif typ == "Broker Interest Received":
            activity_type = "INTEREST"
        elif typ == "Broker Interest Paid":
            # Route to FEE — Wealthfolio's INTEREST is always magnitude
            # (STAKING_REWARD etc.), so a negative interest amount would be
            # abs'd on import. FEE always subtracts cash, matching the intent.
            activity_type = "FEE"
            amount = abs(amount)
        elif typ == "Other Fees":
            # Sum SIGNED into daily aggregate; emitted after the loop as
            # FEE (net outflow) or CREDIT (net refund). Using abs() would
            # treat refunds of prior-month fees as new fees and double-
            # count them.
            fee_agg[(acct, d, currency)] += amount
            fee_agg_count[(acct, d, currency)] += 1
            continue
        elif typ in ("Deposits & Withdrawals", "Deposits/Withdrawals", "Deposits", "Withdrawals"):
            activity_type = "DEPOSIT" if amount >= 0 else "WITHDRAWAL"
            amount = abs(amount)
        else:
            # Unknown — book as ADJUSTMENT cash line to preserve NLV
            activity_type = "ADJUSTMENT"

        output.append(wf_row(
            date=d.isoformat(),
            symbol=symbol,
            instrumentType=instrument_type,
            quantity="1",
            activityType=activity_type,
            unitPrice=fmt_amount(amount),
            currency=currency,
            fee="0", tax="0",
            amount=fmt_amount(amount),
            subtype=subtype,
            accountId=wf_acct,
            notes=description[:200],
            sourceRecordId=synth_id("ibkr_cash", acct, d, description, r["Amount"]),
        ))

    for (acct, d, currency), total in fee_agg.items():
        if total == 0:
            continue
        wf_acct = account_map.get(acct, acct)
        count = fee_agg_count[(acct, d, currency)]
        # Positive net total is a net refund of prior-month fees; emit as
        # CREDIT (cash in). Negative net total is a net fee; emit as FEE
        # with a positive magnitude.
        if total < 0:
            activity_type = "FEE"
            magnitude = -total
            note = f"IBKR daily fees ({count} items)"
        else:
            activity_type = "CREDIT"
            magnitude = total
            note = f"IBKR daily fee refund ({count} items)"
        output.append(wf_row(
            date=d.isoformat(),
            symbol="",
            instrumentType="",
            quantity="1",
            activityType=activity_type,
            unitPrice=fmt_amount(magnitude),
            currency=currency,
            fee="0", tax="0",
            amount=fmt_amount(magnitude),
            accountId=wf_acct,
            notes=note,
            sourceRecordId=synth_id("ibkr_fee_agg", acct, d, currency),
        ))
    return output


# ---------- OpenPositions seeding ----------

def build_initial_positions(
    rows: list[dict],
) -> dict[tuple[str, str], Decimal]:
    """Compute signed starting quantity per (account, symbol) from OpenPositions.

    Used to seed convert_trades' position walker so managed-position
    round-trips (e.g. sell 100 + buy 100 same day on an existing +100 long)
    don't get misclassified as day-trade round-trips.

    Symbol matching intentionally uses the raw OpenPositions Symbol string so
    it aligns with the Trades section's Symbol column verbatim (both OCC-
    format for OPT, futures ticker for FUT, OCC-with-padded-root for FOP)."""
    initial: dict[tuple[str, str], Decimal] = {}
    for r in rows:
        acct = r.get("ClientAccountID") or ""
        symbol = r.get("Symbol") or ""
        if not acct or not symbol:
            continue
        side = (r.get("Side") or "Long").upper()
        try:
            multiplier = Decimal(r.get("Multiplier") or "1")
        except Exception:
            multiplier = Decimal(1)
        try:
            cost_money = Decimal(r.get("CostBasisMoney") or "0")
        except Exception:
            cost_money = Decimal(0)
        try:
            cost_price = Decimal(r.get("CostBasisPrice") or "0")
        except Exception:
            cost_price = Decimal(0)
        qty_raw = r.get("Quantity")
        try:
            qty = abs(Decimal(qty_raw)) if qty_raw else None
        except Exception:
            qty = None
        if qty is None:
            if cost_price == 0 or multiplier == 0:
                continue
            qty = abs(cost_money / (cost_price * multiplier))
        signed = -qty if side == "SHORT" else qty
        initial[(acct, symbol)] = initial.get((acct, symbol), Decimal(0)) + signed
    return initial


def convert_open_positions(
    rows: list[dict],
    account_map: dict[str, str],
    warnings: list[str],
) -> list[dict]:
    """Emit TRANSFER_IN rows from OpenPositions so per-day position walks are
    seeded correctly. Without this, the walk assumes flat at time zero and
    misclassifies any activity on positions opened before the window.

    Seed date is derived from each row's ReportDate field (YYYYMMDD).
    Long positions become TRANSFER_IN. Short positions are not supported yet
    (Wealthfolio's TRANSFER_IN doesn't accept negative quantity) and are
    warned + skipped."""
    output: list[dict] = []
    for r in rows:
        acct = r.get("ClientAccountID") or ""
        if not acct:
            continue
        report_date_raw = (r.get("ReportDate") or "").strip()
        if not report_date_raw:
            warnings.append(
                f"OpenPositions row for {r.get('Symbol')} missing ReportDate; skipped. "
                "Enable ReportDate in the Flex Query OpenPositions field selection."
            )
            continue
        try:
            seed_date = datetime.strptime(report_date_raw, "%Y%m%d").date().isoformat()
        except ValueError:
            warnings.append(
                f"OpenPositions row for {r.get('Symbol')}: bad ReportDate '{report_date_raw}'"
            )
            continue
        asset_class = r.get("AssetClass") or ""
        symbol = r.get("Symbol") or ""
        underlying = r.get("UnderlyingSymbol") or ""
        currency = r.get("CurrencyPrimary") or "USD"
        side = (r.get("Side") or "Long").upper()
        try:
            multiplier = Decimal(r.get("Multiplier") or "1")
        except Exception:
            multiplier = Decimal(1)
        try:
            cost_money = Decimal(r.get("CostBasisMoney") or "0")
        except Exception:
            cost_money = Decimal(0)
        try:
            cost_price = Decimal(r.get("CostBasisPrice") or "0")
        except Exception:
            cost_price = Decimal(0)

        # Prefer explicit Quantity if the field is present; otherwise derive
        # from CostBasisMoney / (CostBasisPrice * Multiplier).
        qty_raw = r.get("Quantity")
        try:
            qty = abs(Decimal(qty_raw)) if qty_raw else None
        except Exception:
            qty = None
        if qty is None:
            if cost_price == 0 or multiplier == 0:
                warnings.append(
                    f"OpenPositions: cannot derive quantity for {symbol} — "
                    "no Quantity field and CostBasisPrice/Multiplier is zero"
                )
                continue
            qty = abs(cost_money / (cost_price * multiplier))
        qty = qty.quantize(Decimal("0.000001"))

        if side == "SHORT":
            warnings.append(
                f"OpenPositions: short seed for {symbol} skipped (not yet supported); "
                "affects any subsequent cover-buy accounting"
            )
            continue

        instrument_type = {
            "STK": "EQUITY",
            "OPT": "OPTION",
            "FUT": "FUTURES",
            "FOP": "OPTION",  # closest match until pseudo-asset design lands
        }.get(asset_class)
        if instrument_type is None:
            warnings.append(
                f"OpenPositions: unsupported AssetClass={asset_class} for {symbol}; skipped"
            )
            continue

        # Compose OCC when Symbol lacks it (rare — usually IBKR's Symbol already
        # is OCC-like for OPT).
        if asset_class == "OPT" and " " not in symbol and underlying:
            put_call = r.get("Put/Call") or ""
            strike = r.get("Strike") or ""
            expiry = r.get("Expiry") or ""
            if put_call and strike and expiry:
                symbol = compose_occ_symbol(underlying, expiry, put_call, strike)

        wf_acct = account_map.get(acct, acct)
        # unit_price for TRANSFER_IN is the per-unit cost basis. For assets
        # with a contract multiplier > 1 Wealthfolio expects the per-share
        # price (multiplier applied downstream). CostBasisPrice from IBKR
        # already matches that convention.
        output.append(wf_row(
            date=seed_date,
            symbol=symbol,
            instrumentType=instrument_type,
            quantity=fmt_amount(qty),
            activityType="TRANSFER_IN",
            unitPrice=fmt_amount(cost_price),
            currency=currency,
            fee="0",
            tax="0",
            amount=fmt_amount(cost_money),
            accountId=wf_acct,
            notes=f"IBKR OpenPositions seed as of {seed_date}",
            sourceRecordId=synth_id("ibkr_seed", acct, symbol, seed_date),
        ))
    return output


# ---------- Transfers (ACATS) ----------

def convert_transfers(
    rows: list[dict],
    account_map: dict[str, str],
    warnings: list[str],
) -> list[dict]:
    """Emit DEPOSIT/TRANSFER_IN rows from the Transfers section (ACATS/ATON).

    IBKR reports each position transfer as a *pair* of rows sharing
    (ClientAccountID, Symbol, Date):
      - a "summary" row with TransactionID + PositionAmount + CashTransfer
      - one or more "lot" rows with TransferPrice + CostBasis + OpenDateTime
    Cash transfers use AssetClass=CASH and are a single summary row.

    Cash: DEPOSIT for positive CashTransfer, WITHDRAWAL for negative.
    Securities: only inbound (CostBasis>0 with TransferPrice>0). Outbound
    security transfers are warned + skipped (WF TRANSFER_OUT semantics
    aren't validated for options/short positions yet)."""
    output: list[dict] = []

    # Group by (acct, symbol, date) so we can pair summary + lot rows.
    grouped: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for r in rows:
        acct = r.get("ClientAccountID") or ""
        if not acct:
            continue
        key = (acct, r.get("Symbol") or "", r.get("Date") or "")
        grouped[key].append(r)

    for (acct, symbol, date_raw), group in grouped.items():
        wf_acct = account_map.get(acct, acct)
        try:
            date_iso = datetime.strptime(date_raw, "%Y%m%d").date().isoformat()
        except ValueError:
            warnings.append(f"Transfers: bad Date '{date_raw}' for {acct}/{symbol}; skipped")
            continue

        # Cash: single row per event, AssetClass=CASH, Symbol='--'.
        # Positive CashTransfer → DEPOSIT (ACATS in). Negative → WITHDRAWAL
        # (ACATS out); WF WITHDRAWAL uses a positive amount.
        cash_rows = [r for r in group if (r.get("AssetClass") or "").upper() == "CASH"]
        if cash_rows:
            for r in cash_rows:
                try:
                    amount = Decimal(r.get("CashTransfer") or "0")
                except Exception:
                    amount = Decimal(0)
                if amount == 0:
                    continue
                txn_id = (r.get("TransactionID") or "").strip()
                currency = r.get("CurrencyPrimary") or "USD"
                xfer_type = r.get("Type") or "ACATS"
                is_in = amount > 0
                magnitude = amount if is_in else -amount
                output.append(wf_row(
                    date=date_iso,
                    symbol="",
                    instrumentType="",
                    quantity="1",
                    activityType="DEPOSIT" if is_in else "WITHDRAWAL",
                    unitPrice=fmt_amount(magnitude),
                    currency=currency,
                    fee="0",
                    tax="0",
                    amount=fmt_amount(magnitude),
                    accountId=wf_acct,
                    notes=f"IBKR {xfer_type} cash {'transfer' if is_in else 'withdrawal'}",
                    sourceRecordId=synth_id("ibkr_acats", acct, txn_id or f"cash:{date_iso}"),
                ))
            continue

        # Positions: summary row has TransactionID; lot rows have TransferPrice+CostBasis.
        summary = next(
            (r for r in group if (r.get("TransactionID") or "").strip()),
            None,
        )
        txn_id = (summary.get("TransactionID") if summary else "") or ""
        currency = (summary.get("CurrencyPrimary") if summary else None) or "USD"
        xfer_type = (summary.get("Type") if summary else None) or "ACATS"
        asset_class = (
            (summary.get("AssetClass") if summary else "")
            or next((r.get("AssetClass") or "" for r in group if r.get("AssetClass")), "")
        ).upper()

        instrument_type = {
            "STK": "EQUITY",
            "OPT": "OPTION",
            "FUT": "FUTURES",
            "FOP": "FUTURES_OPTION",
        }.get(asset_class)
        if instrument_type is None:
            warnings.append(
                f"Transfers: unsupported AssetClass={asset_class} for {symbol} on {date_iso}; skipped"
            )
            continue

        for r in group:
            try:
                price = Decimal(r.get("TransferPrice") or "0")
                basis = Decimal(r.get("CostBasis") or "0")
            except Exception:
                continue
            if price <= 0 or basis <= 0:
                continue  # summary row or empty lot
            qty = (basis / price).quantize(Decimal("0.000001"))
            open_dt = (r.get("OpenDateTime") or "").strip()
            note = f"IBKR {xfer_type} security transfer"
            if open_dt:
                try:
                    open_iso = datetime.strptime(open_dt[:8], "%Y%m%d").date().isoformat()
                    note += f", originally acquired {open_iso}"
                except ValueError:
                    pass
            output.append(wf_row(
                date=date_iso,
                symbol=symbol,
                instrumentType=instrument_type,
                quantity=fmt_amount(qty),
                activityType="TRANSFER_IN",
                unitPrice=fmt_amount(price),
                currency=currency,
                fee="0",
                tax="0",
                amount=fmt_amount(basis),
                accountId=wf_acct,
                notes=note,
                sourceRecordId=synth_id("ibkr_acats", acct, txn_id or symbol, open_dt, fmt_amount(basis)),
            ))
    return output


# ---------- Corporate actions ----------

_SPLIT_DESC_RE = re.compile(r"SPLIT\s+([\d.]+)\s+FOR\s+([\d.]+)", re.IGNORECASE)


def _parse_split_ratio(description: str) -> Decimal | None:
    """Extract split ratio from IBKR description like 'SPLIT 4 FOR 1'.

    Returns Decimal(new_count / old_count). 4-for-1 forward → 4. 1-for-4
    reverse → 0.25."""
    m = _SPLIT_DESC_RE.search(description or "")
    if not m:
        return None
    try:
        new_n = Decimal(m.group(1))
        old_n = Decimal(m.group(2))
        if old_n == 0:
            return None
        return new_n / old_n
    except Exception:
        return None


def convert_corporate_actions(
    rows: list[dict],
    account_map: dict[str, str],
    warnings: list[str],
) -> list[dict]:
    """Emit SPLIT rows for equity forward/reverse splits.

    Options splits (symbol + strike adjust), spinoffs, mergers, and ticker
    changes are warned and skipped — those need manual close-old/open-new
    entries because they change the underlying asset identity or create
    new positions with allocated basis."""
    output: list[dict] = []
    for r in rows:
        acct = r.get("ClientAccountID") or ""
        # IBKR emits a summary row per action with ClientAccountID='-'; skip it.
        if not acct or acct == "-":
            continue
        wf_acct = account_map.get(acct, acct)
        typ = (r.get("Type") or "").upper()
        asset_class = (r.get("AssetClass") or "").upper()
        symbol = r.get("Symbol") or ""
        currency = r.get("CurrencyPrimary") or "USD"
        action_id = (r.get("ActionID") or "").strip()
        txn_id = (r.get("TransactionID") or "").strip()
        description = r.get("Description") or ""
        date_raw = (r.get("Date/Time") or r.get("Report Date") or "").strip()
        # IBKR Date/Time can be YYYYMMDD or YYYYMMDD;HHMMSS
        date_iso = ""
        for fmt in ("%Y%m%d;%H%M%S", "%Y%m%d"):
            try:
                date_iso = datetime.strptime(date_raw[:15] if ";" in date_raw else date_raw[:8], fmt).date().isoformat()
                break
            except ValueError:
                continue
        if not date_iso:
            warnings.append(f"CorporateAction: unparseable Date/Time '{date_raw}' for {symbol}; skipped")
            continue

        if typ in ("FS", "RS"):
            if asset_class != "STK":
                warnings.append(
                    f"CorporateAction split on {asset_class} {symbol} ({description}) "
                    "skipped — options/futures splits change contract identity, add manual CSV"
                )
                continue
            ratio = _parse_split_ratio(description)
            if ratio is None or ratio <= 0:
                warnings.append(
                    f"CorporateAction: cannot parse split ratio from '{description}' for {symbol}; skipped"
                )
                continue
            # WF SPLIT convention: amount field stores the ratio (matches existing
            # AAPL 2020-08-31 row which stores amount=4 for a 4-for-1 forward).
            output.append(wf_row(
                date=date_iso,
                symbol=symbol,
                instrumentType="EQUITY",
                quantity="",
                activityType="SPLIT",
                unitPrice="",
                currency=currency,
                fee="0",
                tax="0",
                amount=fmt_amount(ratio),
                accountId=wf_acct,
                notes=f"IBKR {typ} {description[:80]}",
                sourceRecordId=synth_id("ibkr_ca", acct, action_id or txn_id or symbol, date_iso),
            ))
            continue

        # Spinoffs, mergers, ticker changes, stock dividends: warn only.
        warnings.append(
            f"CorporateAction {typ} on {symbol} ({description[:80]}) skipped — "
            "unsupported action type; handle with manual CSV entries"
        )
    return output


# ---------- Symbol preflight ----------

_YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/"
_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (ibkr-flex-preflight)"}


def _yahoo_symbol_ok(symbol: str, timeout: float = 5.0) -> tuple[bool, str | None]:
    """Return (resolves, note). `note` is the resolved Yahoo symbol when the
    endpoint's response differs from the requested one — a strong signal of
    a rename (e.g. SQ → XYZ) or a fuzzy fallback."""
    url = f"{_YAHOO_CHART}{urllib.request.quote(symbol)}?range=5d&interval=1d"
    req = urllib.request.Request(url, headers=_YAHOO_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (404,):
            return (False, None)
        return (False, f"http {e.code}")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        return (False, f"error: {type(e).__name__}")

    err = (body.get("chart") or {}).get("error")
    if err:
        return (False, err.get("code") or "unknown")

    result = (body.get("chart") or {}).get("result") or []
    if not result:
        return (False, "empty result")
    meta = result[0].get("meta") or {}
    resolved = meta.get("symbol") or ""
    if resolved and resolved.upper() != symbol.upper():
        return (True, f"resolved as {resolved}")
    return (True, None)


def _collect_stock_symbols(sections: dict[str, list[dict]]) -> set[str]:
    """Unique stock symbols the converter will produce (Trades + OpenPositions).

    Only STK / non-option non-futures assets — option and futures symbols
    aren't Yahoo-lookup-compatible in their raw form."""
    symbols: set[str] = set()
    for r in sections.get("trades") or []:
        if r.get("AssetClass") == "STK":
            sym = (r.get("Symbol") or "").strip()
            if sym:
                symbols.add(sym)
    for r in sections.get("open_positions") or []:
        if r.get("AssetClass") == "STK":
            sym = (r.get("Symbol") or "").strip()
            if sym:
                symbols.add(sym)
    return symbols


def run_symbol_preflight(
    trades_sections: dict[str, list[dict]],
    seed_sections: dict[str, list[dict]] | None,
    warnings: list[str],
    sleep_ms: int = 100,
) -> None:
    """Ping Yahoo for each unique stock symbol and warn on unresolved ones.

    Renamed/delisted tickers (e.g. SQ → XYZ) are a common source of silent
    ghost-asset creation because Wealthfolio's enrichment falls back to a
    fuzzy search that can pick unrelated tickers with the same string
    fragment (e.g. VSQTF instead of SQ)."""
    symbols = _collect_stock_symbols(trades_sections)
    if seed_sections:
        symbols |= _collect_stock_symbols(seed_sections)
    if not symbols:
        print("Symbol preflight: no stock symbols to check.", file=sys.stderr)
        return
    print(f"Symbol preflight: checking {len(symbols)} unique stock symbols on Yahoo…", file=sys.stderr)
    unresolved = 0
    renamed = 0
    for i, sym in enumerate(sorted(symbols)):
        ok, note = _yahoo_symbol_ok(sym)
        if not ok:
            unresolved += 1
            msg = f"Preflight: '{sym}' does not resolve on Yahoo"
            if note:
                msg += f" ({note})"
            msg += ". Wealthfolio may create a ghost asset via fuzzy fallback."
            print(f"  ✗ {msg}", file=sys.stderr)
            warnings.append(msg)
        elif note:  # renamed / different resolution
            renamed += 1
            msg = f"Preflight: '{sym}' {note} — likely a rename or delist. Verify before import."
            print(f"  ⚠ {msg}", file=sys.stderr)
            warnings.append(msg)
        if sleep_ms > 0 and i < len(symbols) - 1:
            time.sleep(sleep_ms / 1000)
    ok_count = len(symbols) - unresolved - renamed
    print(
        f"Symbol preflight: {ok_count} clean, {renamed} renamed/reassigned, "
        f"{unresolved} unresolved.",
        file=sys.stderr,
    )


# ---------- Position preview ----------

def show_open_positions(path: Path) -> None:
    """Print a formatted table of the OpenPositions section from `path`."""
    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        sys.exit(2)
    sections = parse_sections(path)
    rows = sections.get("open_positions") or []
    if not rows:
        print(f"No OpenPositions section found in {path}", file=sys.stderr)
        return

    def fmt_qty(row: dict) -> str:
        raw = row.get("Quantity")
        if raw:
            try:
                return f"{Decimal(raw):,.4f}".rstrip("0").rstrip(".")
            except Exception:
                return raw
        # Derive from cost fields
        try:
            cost_money = Decimal(row.get("CostBasisMoney") or "0")
            cost_price = Decimal(row.get("CostBasisPrice") or "0")
            mult = Decimal(row.get("Multiplier") or "1")
            if cost_price == 0 or mult == 0:
                return "?"
            return f"{cost_money / (cost_price * mult):,.4f}".rstrip("0").rstrip(".")
        except Exception:
            return "?"

    def fmt_money(v: str | None) -> str:
        if not v:
            return "-"
        try:
            return f"{Decimal(v):,.2f}"
        except Exception:
            return v

    display_rows = []
    for r in rows:
        display_rows.append({
            "account": r.get("ClientAccountID") or "",
            "as_of": r.get("ReportDate") or "",
            "class": r.get("AssetClass") or "",
            "symbol": r.get("Symbol") or "",
            "side": r.get("Side") or "",
            "qty": fmt_qty(r),
            "mark": fmt_money(r.get("MarkPrice")),
            "cost_px": fmt_money(r.get("CostBasisPrice")),
            "cost_$": fmt_money(r.get("CostBasisMoney")),
            "unrl_pnl": fmt_money(r.get("FifoPnlUnrealized")),
            "ccy": r.get("CurrencyPrimary") or "",
        })
    display_rows.sort(key=lambda x: (x["account"], x["class"], x["symbol"]))

    headers = ["account", "as_of", "class", "symbol", "side", "qty",
               "mark", "cost_px", "cost_$", "unrl_pnl", "ccy"]
    widths = {h: max(len(h), max((len(r[h]) for r in display_rows), default=0)) for h in headers}
    fmt = "  ".join(f"{{:<{widths[h]}}}" for h in headers)

    print(fmt.format(*headers))
    print("  ".join("-" * widths[h] for h in headers))
    for r in display_rows:
        print(fmt.format(*(r[h] for h in headers)))
    print(f"\n{len(display_rows)} position(s) from {path}")


# ---------- Cash settlement extraction ----------

def collect_cash_settlements(eae_rows: list[dict], warnings: list[str]) -> dict:
    """Extract OptionEAE 'Cash Settlement' rows, keyed for lookup by trade context."""
    out: dict = {}
    for r in eae_rows:
        if r.get("Transaction Type") != "Cash Settlement":
            continue
        try:
            proceeds = Decimal(r.get("Proceeds") or "0")
        except Exception:
            continue
        key = (
            r["ClientAccountID"],
            r["CurrencyPrimary"],
            r["UnderlyingSymbol"],
            r["Expiry"],
            r["Put/Call"],
            r["Strike"],
            r["Date"],
        )
        # Multiple settlements possible for same strike/date? Sum them.
        out[key] = out.get(key, Decimal(0)) + proceeds
    return out


# ---------- Main ----------

def load_account_map(path: Path | None) -> dict[str, str]:
    """Minimal YAML parser for the account-map file: `accounts:` block of `key: value`."""
    if path is None or not path.exists():
        return {}
    result: dict[str, str] = {}
    in_accounts = False
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            in_accounts = stripped == "accounts:"
            continue
        if in_accounts and ":" in stripped:
            k, v = stripped.split(":", 1)
            result[k.strip()] = v.strip().strip('"').strip("'")
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input_csv", type=Path, nargs="?", help="Flex CSV to convert (omit if using --show-positions)")
    ap.add_argument("output_csv", type=Path, nargs="?", help="Output CSV path (omit if using --show-positions)")
    ap.add_argument("--account-map", type=Path, default=Path.home() / ".wealthfolio" / "ibkr_accounts.yml")
    ap.add_argument(
        "--source-tz",
        default=None,
        help="IANA timezone of IBKR DateTime column (e.g. 'America/New_York'). "
             "When set, emitted BUY/SELL rows carry a DST-aware UTC offset. "
             "Default: naive datetime (Wealthfolio interprets as local).",
    )
    ap.add_argument(
        "--seed-from",
        type=Path,
        default=None,
        help="Path to a separate Flex CSV whose OpenPositions section seeds "
             "the account. Typical usage: pass the PRIOR period's export "
             "here — its end-of-period OpenPositions == start-of-period for "
             "the current window. Seed date is taken from each OpenPositions "
             "row's ReportDate field (required). Enables correct per-day "
             "position walks (and thus correct aggregation) when importing "
             "a partial window.",
    )
    ap.add_argument(
        "--seed-walk-only",
        action="store_true",
        help="Use --seed-from's OpenPositions for internal walk classification "
             "(so managed-position round-trips don't get misclassified as day "
             "trades) but DO NOT emit TRANSFER_IN rows to the output CSV. "
             "Use this flag on every import AFTER your first — the DB "
             "already has the seed positions from a prior import's "
             "activities, so emitting them again would double-count.",
    )
    ap.add_argument(
        "--show-positions",
        type=Path,
        default=None,
        metavar="FILE",
        help="Print a formatted table of the OpenPositions section from FILE "
             "and exit. Useful for previewing a seed source or comparing "
             "period-end state to Wealthfolio.",
    )
    ap.add_argument(
        "--preflight-symbols",
        action="store_true",
        help="Before writing the output, ping Yahoo for each unique stock "
             "symbol in Trades + seed OpenPositions. Warns on unresolved or "
             "renamed tickers (e.g. SQ → XYZ) that would otherwise trigger "
             "Wealthfolio's fuzzy-search fallback and create a ghost asset. "
             "Adds ~100ms per unique symbol; skip for tight loops.",
    )
    args = ap.parse_args()

    if args.show_positions:
        show_open_positions(args.show_positions)
        return

    if args.input_csv is None or args.output_csv is None:
        ap.error("input_csv and output_csv are required unless --show-positions is used")

    global _SOURCE_TZ
    if args.source_tz:
        try:
            _SOURCE_TZ = ZoneInfo(args.source_tz)
        except ZoneInfoNotFoundError:
            print(f"Unknown --source-tz '{args.source_tz}'. Use an IANA name like 'America/New_York'.", file=sys.stderr)
            sys.exit(2)
        print(f"Emitting datetimes with timezone: {args.source_tz}", file=sys.stderr)

    sections = parse_sections(args.input_csv)
    print(f"Parsed sections: {{ {', '.join(f'{k}: {len(v)}' for k, v in sections.items())} }}", file=sys.stderr)

    account_map = load_account_map(args.account_map)
    if account_map:
        print(f"Account map: {account_map}", file=sys.stderr)
    else:
        print(f"No account map found at {args.account_map}; using raw IBKR IDs as accountId.", file=sys.stderr)

    warnings: list[str] = []
    cash_settlements = collect_cash_settlements(sections["option_eae"], warnings)
    seed_sections_for_preflight: dict[str, list[dict]] | None = None
    seed_rows: list[dict] = []
    initial_positions: dict[tuple[str, str], Decimal] = {}
    if args.seed_from:
        if not args.seed_from.exists():
            print(f"--seed-from file not found: {args.seed_from}", file=sys.stderr)
            sys.exit(2)
        seed_sections = parse_sections(args.seed_from)
        seed_sections_for_preflight = seed_sections
        seed_positions = seed_sections.get("open_positions", []) or []
        print(
            f"Seed source: {args.seed_from} ({len(seed_positions)} OpenPositions rows)",
            file=sys.stderr,
        )
        initial_positions = build_initial_positions(seed_positions)
        if args.seed_walk_only:
            print(
                f"Seeded walk-only ({len(initial_positions)} position slots); "
                "no TRANSFER_IN rows emitted.",
                file=sys.stderr,
            )
        else:
            seed_rows = convert_open_positions(seed_positions, account_map, warnings)
            print(f"Seeded {len(seed_rows)} positions", file=sys.stderr)

    trade_rows = convert_trades(
        sections["trades"], cash_settlements, account_map, warnings, initial_positions
    )
    cash_rows = convert_cash_txns(sections["cash_txn"], account_map)
    transfer_rows = convert_transfers(sections["transfers"], account_map, warnings)
    if transfer_rows:
        print(f"Transfers: {len(transfer_rows)} ACATS row(s)", file=sys.stderr)
    ca_rows = convert_corporate_actions(sections["corporate_actions"], account_map, warnings)
    if ca_rows:
        print(f"CorporateActions: {len(ca_rows)} split row(s)", file=sys.stderr)

    if args.preflight_symbols:
        run_symbol_preflight(sections, seed_sections_for_preflight, warnings)

    all_rows = seed_rows + trade_rows + cash_rows + transfer_rows + ca_rows
    all_rows.sort(key=lambda r: (r["date"], r["accountId"], r["symbol"]))

    with args.output_csv.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=WF_HEADER)
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"Wrote {len(all_rows)} rows to {args.output_csv}", file=sys.stderr)
    if warnings:
        print(f"\nWarnings ({len(warnings)}):", file=sys.stderr)
        for w in warnings[:20]:
            print(f"  - {w}", file=sys.stderr)
        if len(warnings) > 20:
            print(f"  ... and {len(warnings) - 20} more", file=sys.stderr)


if __name__ == "__main__":
    main()
