# IBKR Import Workflow

Operational steps to pull an IBKR Flex Query CSV, convert it to Wealthfolio
format, and import. For design/roadmap see `ibkr_import_plan.md`.

## One-time setup

### 1. Configure IBKR Flex Query

IBKR Client Portal → Reports → Flex Queries → Create new Activity Flex Query.

**Sections to include** (all four required by the converter):

| Section                        | Required fields                                                                                                                                                                                                                                                                                   |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Trades**                     | `ClientAccountID, CurrencyPrimary, AssetClass, Symbol, Description, UnderlyingSymbol, Multiplier, Strike, Expiry, Put/Call, TradeDate, TransactionType, Quantity, TradePrice, IBCommission, IBCommissionCurrency, NetCash, Notes/Codes, OrigOrderID, Buy/Sell, OrderTime, TradeID, DateTime`      |
| **OptionEAE**                  | `ClientAccountID, CurrencyPrimary, UnderlyingSymbol, Multiplier, Strike, Expiry, Put/Call, Date, Transaction Type, Quantity, Trade Price, Close Price, Proceeds, Comm/Tax, TradeID`                                                                                                               |
| **Cash Transactions**          | `ClientAccountID, CurrencyPrimary, Description, Date/Time, Amount, Type`                                                                                                                                                                                                                          |
| **OpenPositions**              | `ClientAccountID, AssetClass, Symbol, UnderlyingSymbol, Multiplier, Strike, Expiry, Put/Call, MarkPrice, OpenPrice, CostBasisPrice, CostBasisMoney, FifoPnlUnrealized, Side, OpenDateTime, CurrencyPrimary, ReportDate, Quantity` — required to seed the _next_ period's import via `--seed-from` |
| **Mark-to-Market Performance** | (any subset; converter ignores this section but IBKR requires it to be non-empty for the query to save)                                                                                                                                                                                           |

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
  --account-map ~/.wealthfolio/ibkr_accounts.yml \
  --source-tz America/New_York
```

- `--source-tz` is optional. When set (IANA name), BUY/SELL rows include a
  DST-aware UTC offset so Wealthfolio stores the correct instant. Without it,
  emitted datetimes are naive and interpreted as local by Wealthfolio.
- The Flex Query needs the `DateTime` column enabled on Trades for time to
  actually appear; otherwise falls back to date-only.

Output to stderr summarizes parsed sections and row counts. Expect a large
collapse — one test file went from 12,541 raw rows → 99 output rows because 0DTE
round-trips aggregate to a single daily CREDIT per account.

### 3. Sanity-check the output

```bash
awk -F, 'NR>1 {print $5}' /tmp/wf_2026-06.csv | sort | uniq -c
```

Typical distribution:

- `CREDIT` — daily P&L aggregates (positive for winning days, negative for
  losing days). Buckets: `SPXW` (0DTE index options), `OPT` (multi-day
  round-tripped equity/index options), `FOP` (0DTE futures options like ES),
  `FUT` (futures round-trips, including forced FUT round-trips from FOP
  exercise), `STK` (equity round-trips).
- `BUY / SELL` — multi-day holds only (round-trips aggregated away)
- `ADJUSTMENT` — option assignments/expiries + orphan cash settlements
- `FEE` — daily market-data fees + margin interest paid
- `INTEREST` — Broker Interest Received
- `DIVIDEND` — cash dividends on stock holdings
- `DEPOSIT / WITHDRAWAL` — ACH/wire cash movements (IBKR type
  `Deposits/Withdrawals`)

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
6. **Multi-day FOPs** (futures options, IBKR symbols like `E1DK6 C7355`) —
   emitted as `instrumentType=FUTURES_OPTION` (native Wealthfolio type) with an
   OCC-composed symbol built from the futures root + Expiry + Put/Call + Strike
   (e.g. `ESH1` underlying → `ES    210104P03615000`). The asset's multiplier
   (50 for ES, 5 for MES, etc.) is inherited from the futures `CONTRACT_SPECS`
   lookup at asset creation, so cost basis and displayed unit price both match
   IBKR's raw values. `quoteMode` is set to `MANUAL` since no market-data
   provider covers FOPs — mid-hold MV falls back to cost basis. 0DTE FOPs
   continue to aggregate via the `FOP` bucket.

## Recurring imports

### Why seeding matters

The converter walks each `(account, asset)` position chronologically from the
start of the file. Without a seed, the walk assumes flat-zero at time zero,
which misclassifies any position opened before the window (e.g. a SELL of a
pre-existing long looks like opening a short) and breaks daily-aggregation
correctness for stocks/options/futures held across the window boundary.

Seeding fixes this: `--seed-from <prior_file.csv>` reads OpenPositions from the
earlier file and emits one `TRANSFER_IN` per position on that snapshot's
`ReportDate` before processing the current window's trades.

### Preflight symbols against Yahoo

Renamed / delisted tickers (e.g. `SQ` → `XYZ`, `WORK` → delisted after Slack was
acquired) silently map to unrelated tickers via Wealthfolio's fuzzy-search
fallback (Yahoo has no verbatim result → the resolver picks whatever partial
match came back). Flag them before import:

```bash
python3 scripts/import/ibkr_flex_to_wf.py \
  ~/Downloads/wealthfolio_2020.csv /tmp/wf_2020.csv \
  --seed-from ~/Downloads/wealthfolio_2019.csv \
  --preflight-symbols
```

Warnings list every unique stock symbol that doesn't resolve verbatim on Yahoo,
plus renames where Yahoo returns a different symbol than requested. Fix by
editing the raw CSV (rename the ticker) or manually creating the correct asset
in Wealthfolio first, then re-run without the flag.

Adds ~100ms per unique symbol; skip for tight recurring loops once your symbol
set is clean.

### Preview OpenPositions before seeding

Quick sanity check on any Flex CSV to confirm the OpenPositions section is
present and looks right:

```bash
python3 scripts/import/ibkr_flex_to_wf.py --show-positions ~/Downloads/wealthfolio_2020.csv
```

Prints a table of every row in the OpenPositions section: account, asset class,
symbol, side, quantity, mark price, cost basis, and unrealized P&L. Useful for
previewing what would seed the next period, or comparing to Wealthfolio's
positions at the validation checkpoint.

### Chain each period from the prior period's OpenPositions

**Annual pattern** (backfills, or if you pull yearly):

```bash
# Year 2020 (seed from end-of-2019 snapshot)
python3 scripts/import/ibkr_flex_to_wf.py \
  ~/Downloads/wealthfolio_2020.csv /tmp/wf_2020.csv \
  --seed-from ~/Downloads/wealthfolio_2019.csv \
  --source-tz America/New_York

# Year 2021 (seed from end-of-2020 snapshot)
python3 scripts/import/ibkr_flex_to_wf.py \
  ~/Downloads/wealthfolio_2021.csv /tmp/wf_2021.csv \
  --seed-from ~/Downloads/wealthfolio_2020.csv \
  --source-tz America/New_York
```

**Monthly pattern** (ongoing / Flex Web Service):

```bash
# Feb 2026 (seed from end-of-Jan snapshot)
python3 scripts/import/ibkr_flex_to_wf.py \
  ~/.wealthfolio/ibkr/2026-02.csv /tmp/wf_2026-02.csv \
  --seed-from ~/.wealthfolio/ibkr/2026-01.csv \
  --source-tz America/New_York
```

Every pull should include the OpenPositions section with `ReportDate` so the
file can seed the _next_ period. Save each raw CSV persistently — you'll need it
as the seed for the following period.

### First-period bootstrapping

For your earliest period (nothing before it), the account cash balance also
needs a seed. Two options:

1. **Genuine account inception**: if the first period includes the initial
   wire/ACH deposit that opened the account, no manual step needed — the
   CashTransactions row handles it.
2. **Partial history**: manually add a `DEPOSIT` row to the output CSV on the
   seed date, matching the account's starting cash balance from IBKR's
   statement.

### Validation checkpoint after each import

After importing period N, compare Wealthfolio's positions on the last day
against `wealthfolio_N.csv`'s OpenPositions section. If they diverge:

- Missing asset in Wealthfolio → a short position wasn't seeded (see
  short-position caveat below); or a trade in the window was misclassified
- Extra asset in Wealthfolio (phantom) → likely a short-close was misinterpreted
  as opening a long (same root cause)
- Wrong quantity → a partial fill got deduped, or an OPTION_EXPIRY row's
  quantity didn't match IBKR's Exercise/Assignment count

Fix any divergence _before_ moving to period N+1, because that period's seed
comes from the current period's IBKR snapshot (which is authoritative), and
persistent Wealthfolio errors won't self-heal.

### Short-position caveat

Seed emission skips short positions with a warning (Wealthfolio's `TRANSFER_IN`
doesn't accept negative quantity yet). Impact:

- Persistent shorts crossing a seed boundary are missing from Wealthfolio until
  the seed handler is enhanced.
- When such a short eventually closes, the closing BUY looks like opening a new
  long → phantom long lot appears.

**Manual workaround** for rare cases: after the converter warns about a skipped
short seed, hand-add a `SELL` row (with `POSITION_OPEN` subtype and a paired
ADJUSTMENT zeroing the proceeds) to the output CSV before import.

Or if your account rarely holds short positions across periods, ignore the
warning and manually delete any phantom long lots that appear.

### Flex Web Service (automated pulls)

Not yet shipped (`ibkr_flex_fetch.py`, see `ibkr_import_plan.md`). When built,
the fetcher will need to:

1. Pull the current period → save to `~/.wealthfolio/ibkr/<YYYY-MM>.csv`
2. Look up the previous period's file (same folder)
3. Invoke the converter with `--seed-from <prior>` automatically
4. Import the resulting Wealthfolio CSV

**Cadence note**: schedule the cron for at least the 2nd of each month if your
Flex Query period is "Last Month" — otherwise you might miss end-of-month
settlement bookings that IBKR posts after 5pm ET on the last business day.

### Idempotency across all patterns

Every emitted row's `sourceRecordId` is deterministic:

- Trades → hash of `TradeID`
- Aggregates → hash of `(account, date, currency, bucket)`
- Seed rows → hash of `(account, symbol, ReportDate)`
- Cash txns → hash of `(account, date, description, amount)`

So re-importing the same file (or overlapping windows) is safe — Wealthfolio
dedupes by `sourceRecordId`. You can rebuild the entire chain from scratch
without duplicating rows.
