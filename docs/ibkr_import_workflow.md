# IBKR Import Workflow

Operational steps to pull an IBKR Flex Query CSV, convert it to Wealthfolio
format, and import. For design/roadmap see `ibkr_import_plan.md`.

## One-time setup

### 1. Configure IBKR Flex Query

IBKR Client Portal → Reports → Flex Queries → Create new Activity Flex Query.

**Sections to include** (all four required by the converter):

| Section                        | Required fields                                                                                                                                                                                                                                                                    |
| ------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Trades**                     | `ClientAccountID, CurrencyPrimary, AssetClass, Symbol, Description, UnderlyingSymbol, Multiplier, Strike, Expiry, Put/Call, TradeDate, TransactionType, Quantity, TradePrice, IBCommission, IBCommissionCurrency, NetCash, Notes/Codes, OrigOrderID, Buy/Sell, OrderTime, TradeID` |
| **OptionEAE**                  | `ClientAccountID, CurrencyPrimary, UnderlyingSymbol, Multiplier, Strike, Expiry, Put/Call, Date, Transaction Type, Quantity, Trade Price, Close Price, Proceeds, Comm/Tax, TradeID`                                                                                                |
| **Cash Transactions**          | `ClientAccountID, CurrencyPrimary, Description, Date/Time, Amount, Type`                                                                                                                                                                                                           |
| **Mark-to-Market Performance** | (any subset; converter ignores this section but IBKR requires it to be non-empty for the query to save)                                                                                                                                                                            |

**Format**: CSV, no header/trailer rows.

**Period**: pick a rolling window (e.g. last 60 days) for recurring imports, or
a specific month for backfills.

**Critical**: OptionEAE is not optional. Without it, ITM cash-settled index
option expiries lose their realized P&L (observed ~$300K/month missing on a
sample file). If you skipped it in an earlier query, edit the query and add.

### 2. Configure account mapping

Copy the example to your home config:

```bash
cp scripts/import/ibkr_accounts.example.yml ~/.wealthfolio/ibkr_accounts.yml
```

Edit to map each IBKR `ClientAccountID` (starts with `U`) to a Wealthfolio
account you've already created:

```yaml
accounts:
  U1896004: IBKR-Main
  U2858775: IBKR-Trading
```

The right-hand side is the Wealthfolio account **name** as shown in the app, not
the UUID. Create those accounts in Wealthfolio first.

### 3. Create the Wealthfolio accounts

For each account in your map, create a Wealthfolio account with:

- Type: `Investment`
- Currency: USD (all IBKR flow assumed USD in v1)
- Optional: seed with a big DEPOSIT for the first cutover, or import a
  `TRANSFER_IN` for opening positions.

## Per-import workflow

### 1. Pull the Flex query CSV

Client Portal → Reports → Flex Queries → run the query → download CSV. Save to a
stable location (e.g. `~/Downloads/ibkr_<month>.csv`).

### 2. Convert to Wealthfolio format

```bash
python3 scripts/import/ibkr_flex_to_wf.py \
  ~/Downloads/ibkr_2026-06.csv \
  /tmp/wf_2026-06.csv \
  --account-map ~/.wealthfolio/ibkr_accounts.yml
```

Output to stderr summarizes parsed sections and row counts. Expect a large
collapse — one test file went from 12,541 raw rows → 99 output rows because 0DTE
round-trips aggregate to a single daily CREDIT per account.

### 3. Sanity-check the output

```bash
awk -F, 'NR>1 {print $5}' /tmp/wf_2026-06.csv | sort | uniq -c
```

Typical distribution:

- `CREDIT` — daily P&L aggregates (positive for winning days, negative for
  losing days)
- `BUY / SELL` — multi-day holds only (round-trips aggregated away)
- `ADJUSTMENT` — option assignments/expiries + orphan cash settlements
- `FEE` — daily market-data fees + margin interest paid
- `INTEREST` — Broker Interest Received
- `DIVIDEND` — cash dividends on stock holdings

### 4. Reconcile a sample day

Pick one day, look it up in your IBKR daily statement, and compare against the
CREDIT row for that account/day:

```bash
grep "2026-06-09.*IBKR-Main.*CREDIT" /tmp/wf_2026-06.csv
```

Expect the CREDIT amount to match your IBKR trading P&L for that day within a
few dollars (rounding + fees on separately-emitted rows like MES futures
per-trade SELLs).

### 5. Import into Wealthfolio

**Recommended: use a throwaway DB for the first import.**

```bash
# In .env (or launch env):
DATABASE_URL=/tmp/wf_ibkr_test.db
```

Restart Wealthfolio. Create the mapped accounts. Then:

1. Activities → Import
2. Select `/tmp/wf_2026-06.csv`
3. Map columns (should auto-detect for the standard header)
4. Review the preview — spot-check that:
   - CREDIT rows on losing days show negative amounts (script requires the
     signed-CREDIT fix, shipped in commit `46ccba6ee`)
   - Multi-day option positions have proper OCC symbols (e.g.
     `IBIT  270115P00064000`)
   - Futures positions have CME symbols (e.g. `MESM6`, `ESH27`)
5. Confirm import
6. Verify NLV chart drops on the day you reconciled

If numbers look right, switch `DATABASE_URL` back to your real DB and re-run.

### 6. Re-import safety

Every emitted row has a `sourceRecordId` derived from `TradeID` (Trades) or a
content hash (aggregates + cash txns). Re-importing the same window is
idempotent — Wealthfolio dedupes by `(account, source_record_id)`.

## Known caveats

Not blockers, but be aware:

1. **Futures held to expiry** — MES/ES shorts that expire don't auto-close. No
   OptionEAE equivalent for futures. Workaround: close manually before expiry,
   or wait for the futures-expiry follow-up (see `future_work.md`).
2. **Futures cash-flow model** — BUY of a MES contract books cash outflow of
   `qty × price × 5` (notional), not the actual ~5% margin posted. Mid-hold NLV
   components look weird but net NLV ties at endpoints. Ongoing issue — see
   `future_work.md`.
3. **Partial fills** — IBKR reports a single order that fills across two venues
   as two rows with distinct `TradeID`s and `P` (partial) notes. The script
   preserves them (idempotency). Position math is identical.
4. **Cross-currency** — v1 assumes USD only. If you trade non-USD instruments on
   IBKR the script will emit currency codes as-is but the reconciliation flow
   isn't validated.
5. **Prior positions** — if importing a partial window (not from account
   inception), any short options assigned during the window will emit
   `ADJUSTMENT(OPTION_EXPIRY)` rows that reference positions Wealthfolio doesn't
   know about. Rust handler logs a warning and skips — no data corruption. Cash
   impact from paired stock assignment / cash settlement still books correctly.

## Recurring imports

For monthly imports:

1. Set the Flex Query period to "Last N Days" (60+ recommended to catch trades
   that opened before but closed in the current period).
2. Pull → run script → import.
3. Re-import safety (`sourceRecordId`) means overlap with a previous window just
   no-ops the duplicate rows.

Once Flex Web Service automation is built (`ibkr_flex_fetch.py`, not yet shipped
— see `ibkr_import_plan.md`), this becomes a cron job.
