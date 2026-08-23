# Future Work

## Storage optimization for options

Context: active traders accumulate thousands of option-contract assets, each
with its own quote history. Without care, DB grows into hundreds of MB. Ordered
by cost/benefit.

### 1. Verify option ingest health after each big import

Fix in `get_or_create_minimal_asset` populates `metadata.option` for options via
`build_asset_metadata` fallback. Confirm no regression after imports:

```sql
SELECT COUNT(*) FROM assets
WHERE instrument_type='OPTION'
  AND created_at > datetime('now','-1 hour')
  AND json_extract(metadata,'$.option') IS NULL;
```

Should return 0. If not, ingest path still has a gap — investigate the specific
import route.

### 2. Disable device sync when unused

`sync_outbox` accumulates pending events (one per activity/asset upsert) when
device sync is off or broken. 12k+ pending rows already observed = ~11 MB dead
payload. Clear + preventive:

```sql
DELETE FROM sync_outbox WHERE status='pending';
VACUUM;
```

Then either fix device sync or turn it off in Settings before large imports.

### 3. Option-specific pre-activity buffer (code change) — DONE (8b4b2cfcc)

Shipped as `OPTION_HISTORY_BUFFER_DAYS = 5`, threaded through
`SyncPlanningInputs.history_buffer_days`. `history_buffer_days_for(asset)` in
`sync.rs` picks the per-asset value; consumed by `determine_sync_category`,
`calculate_sync_window`, `calculate_date_range_for_mode`, and
`handle_activity_created`.

### 4. Purge quotes for expired options

Once expired, an option's OHLCV bars are historical curiosities that no chart or
valuation touches. Safe to delete:

```sql
DELETE FROM quotes WHERE asset_id IN (
  SELECT id FROM assets
  WHERE instrument_type='OPTION'
    AND date(json_extract(metadata,'$.option.expiration')) < date('now','-30 days')
);
VACUUM;
```

Ship as either a settings-triggered cleanup or a periodic maintenance job.
Expect 20–50% reduction in `quotes` for options-heavy portfolios.

### 5. Archive closed accounts / positions

Closed positions already skipped from sync (via
`quote_sync_state.position_closed_date`) but their historical quotes remain. For
fully closed accounts the user no longer opens, add a purge:

```sql
DELETE FROM quotes WHERE asset_id IN (
  SELECT asset_id FROM quote_sync_state WHERE position_closed_date < date('now','-1 year')
);
```

Guard with UI confirmation — this is destructive for anyone still auditing prior
years.

### 6. VACUUM after big imports

SQLite doesn't reclaim space automatically after DELETE. Run after any bulk
cleanup:

```sql
VACUUM;
```

Reclaims 10–30% of file size in typical cases.

### 7. Selective trade-log import

For historical trade logs going back many years, filter _before_ import:

- Keep all open positions.
- Keep trades from the current tax year.
- Keep trades tied to lots that still influence cost basis.
- Drop long-expired round-trip options with de minimis P&L.

Avoids adding rows to `activities`, `assets`, `snapshot_positions`, and `quotes`
that never inform valuation. Not a code change — a user workflow / import UI
hint.

### Rough projection (importing ~10k activities, ~2k new option contracts)

- **With fix #1 healthy**: +30–50 MB (mostly quotes for live contracts +
  snapshots).
- **Without fix #1** (regression): +200–400 MB from expired-option fetching
  before sync gives up.

### Recommended sequence around a big import

1. Sample import (100 activities) → verify no `metadata.option` gap.
2. Turn off (or fix) device sync.
3. Full import.
4. Run "purge expired option quotes" cleanup.
5. `VACUUM`.
6. Decide if #3 (option buffer) is worth codifying.

---

## Yahoo symbol cross-contamination for ambiguous tickers

When an asset's stored `instrument_symbol` doesn't exist verbatim on Yahoo
(`/chart/{ticker}` returns 404), the enrichment / resolver path falls back to
Yahoo's search endpoint, which fuzzy-matches to whatever the "best" hit is —
often a European-listed company sharing the same ticker string. Observed in a
production DB:

- **BRKB** (`XNYS`, USD) → matched Frankfurt-listed Berkshire proxy → quotes in
  EUR at ~€440 (approximately-right for BRK, wrong currency)
- **EXL** (`XNAS`, USD) → matched **Exasol AG** (German software) → quotes at
  €1.97 (completely wrong company)
- **MASI** (`XNAS`, USD) → matched **Masi Agricola** (Italian winery) → quotes
  at €4.28 (completely wrong company)

Symptoms in `metadata.profile`: `countries` says Germany/Italy on assets whose
MIC says XNYS/XNAS.

### Detection query

Filter by quote-currency mismatch, not by incorporation country — many
foreign-domiciled companies (Jazz, Atlassian, SolarEdge, Genpact, …) are
legitimately US-listed and would false-positive on a country check.

```sql
SELECT a.display_code, a.instrument_exchange_mic AS mic,
       q.currency AS quote_ccy, ROUND(q.close, 2) AS close, q.day AS latest_day
FROM assets a
LEFT JOIN quotes q ON q.asset_id=a.id AND q.day = (
  SELECT MAX(day) FROM quotes q2 WHERE q2.asset_id=a.id
)
WHERE a.instrument_exchange_mic IN ('XNYS','XNAS','XASE','BATS','ARCX')
  AND a.quote_ccy='USD'
  AND (q.currency != 'USD'                              -- wrong currency
       OR (q.close IS NOT NULL AND q.close < 5))        -- suspiciously low price
ORDER BY a.display_code;
```

Any row where the latest quote is non-USD or the price is implausibly low for a
US listing is a candidate for cross-contamination. Cross-check the
`metadata.profile` for the company name to distinguish real bugs from
legitimately-cheap US penny stocks.

### Ad-hoc remediation

1. Correct the `instrument_symbol` to Yahoo's canonical form (e.g. `BRKB` →
   `BRK.B`; `EXL` → `EXLS`).
2. Clear `metadata` so enrichment reruns.
3. Purge the wrong quotes.
4. Trigger a manual sync.

Example: [committed as ad-hoc SQL block in support conversation on 2026-08-23].

### Proper fix (code)

Two guardrails in the resolver / enrichment path:

- **Reject cross-currency search hits.** If asset has a declared MIC in a known
  currency (e.g. XNYS → USD), any provider search result returning a different
  currency should be treated as a non-match, not accepted silently.
- **Don't substitute unresolved tickers.** When `/chart/{ticker}` returns 404
  and the resolver has no explicit provider mapping, mark the asset as
  unresolvable (surface in health page) rather than swallowing Yahoo's fuzzy
  search suggestion.

Likely touches `RulesResolver::resolve_equity`, `QuoteService::search_symbol`,
and the enrichment code in `assets_service.rs`. Add a coverage check comparing
`asset.instrument_exchange_mic → mic_to_currency()` against the resolved
`ProviderInstrument`'s expected currency.

---

## Historical futures valuation gap

Yahoo's futures resolver maps CME tickers to the continuous back-adjusted series
(`ESH27 → ES=F`). For **recent** history this is a reasonable approximation of
the specific contract's price during its front-month period. For **older**
history the back-adjustment factor grows, so continuous prices drift from actual
per-contract prices. A 9-year historical import will show increasing accuracy
loss the further back you go.

### Symptoms

- Historical NLV chart during an old futures holding period shows a value that
  differs from what the position was actually worth at the time.
- Realized P&L (from BUY/SELL trades) is unaffected — it's captured from the
  trade prices, not from marks during hold.

### Mitigations already shipped

- Cost-basis fallback in `valuation_calculator.rs` and `net_worth_service.rs`:
  for contract instruments (multiplier > 1) with no quote at all, valuation
  falls back to book value so the NLV chart stays flat during unknown-price
  windows rather than dropping to zero.

### Follow-up options

- **Detect stale continuous data.** If the difference between a fetched
  continuous quote and the position's cost basis exceeds some threshold on the
  BUY date, log a warning ("continuous series may not match this specific
  contract's historical price").
- **Ingest per-contract futures history from Barchart / CQG / broker export.**
  Real per-contract series would resolve this entirely; requires paid feed or
  broker cooperation.
- **Trust broker's own historical marks.** IBKR Flex reports can include
  positions with daily marks — a broker-sourced quote path would bypass
  continuous approximation for imported historical data.

## Futures cash-flow model

Wealthfolio books BUY/SELL as `qty × price × multiplier` cash outflow/inflow,
treating futures like options. For options that matches reality (premium × 100
is real cash cost). For futures it's wrong (you post margin ~5% of notional, not
the full notional). Symptoms in the NLV chart during a futures holding period:

- Cash goes deeply negative on BUY (by full notional)
- Investment MV rises to full notional (matches, so total NLV stays flat if
  quotes track cost basis)
- On SELL: both reverse, realized P&L nets out correctly

Net NLV is arithmetically correct at start and end of the position. The
individual cash / investment components look weird mid-position but the total
line ties out.

### Follow-up

- Add "margin-traded" flag distinguishing futures/FOPs from premium-traded
  (options) or full-notional (equity).
- Book cash flow as initial margin on BUY + realized P&L on SELL for
  margin-traded instruments; keep the existing model for premium-traded.
- Track margin usage as a separate line item on the account (so users see how
  much of their cash is tied up in margin vs available).

Non-trivial change; touches holdings calculator, cash accounting, and
account-level margin tracking. Prioritize when futures usage becomes real enough
that the cash-swing visualization becomes a pain point.

## Futures expiry handling

Options expiries are covered by IBKR's OptionEAE section (Assignment / Exercise
/ Cash Settlement rows), which the IBKR importer folds into the correct
ADJUSTMENT + cash rows. Futures have no equivalent section — a MES short opened
on 06-09 that expires on 06-18 shows only the opening SELL in Trades, no closing
entry. Position stays open in Wealthfolio indefinitely.

### Options

- **Manual close before expiry**: workflow-only fix. Requires user discipline;
  won't help for backfilled history.
- **Synthesize closing trade from settlement**: on futures expiry date,
  auto-emit a BUY/SELL at the settlement price. Needs a settlement-price source
  (Yahoo's continuous series is approximate; IBKR sometimes reports in the
  CashReport section).
- **Explicit "position close on expiry" activity type**: like
  `ADJUSTMENT(OPTION_EXPIRY)` but for futures. Removes qty, books cash from a
  supplied settlement price.

Not urgent if user closes futures manually before expiry. Becomes relevant when
backfilling historical futures held to expiry, or if IBKR eventually starts
auto-rolling contracts.
