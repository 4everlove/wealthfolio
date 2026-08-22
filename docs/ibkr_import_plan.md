# IBKR Import Plan

Working doc. Drives the IBKR import workstream and gates the futures support
work it depends on.

## Motivation

Primary user workflow: 200–400 SPXW 0DTE trades/day + a small long-term book of
equities, longer-dated options, futures, and FOPs on IBKR. Goal is to bring this
into Wealthfolio without drowning the app in per-trade contract clutter.

## Priority order

1. **Native futures Phase 1** (see `docs/futures_support_plan.md`) — blocker for
   anything else, because long-term futures/FOPs would otherwise land as manual
   pseudo-assets.
2. **IBKR daily-aggregate importer** — collapses ~99 % of the trade volume
   (round-trip day trades) into daily P&L rows.
3. **IBKR full-trades importer** — persistent positions get proper BUY/SELL with
   cost basis.
4. **Automation via Flex Web Service** — recurring import triggered by a
   cron/loop.
5. **FOP support** — separate design pass after futures Phase 1 lands.

## Design constraints (from user answers)

| Constraint                                                                       | Decision                                                                                     |
| -------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| Multiple IBKR accounts                                                           | Map each `AccountId` → a separate Wealthfolio account via config file (`.ibkr_accounts.yml`) |
| Existing IBKR data in Wealthfolio                                                | None; fresh slate — no cutover needed                                                        |
| First-import scope                                                               | YTD (2026 to today), then backfill by adding earlier Flex windows                            |
| Cadence                                                                          | Recurring, ideally automated via IBKR Flex Web Service (token + QueryID → CSV/XML)           |
| Cash movements (ACH, wires)                                                      | Import as DEPOSIT/WITHDRAWAL                                                                 |
| Interest earned                                                                  | Import as INTEREST                                                                           |
| Margin interest, payment-in-lieu, market data fees, misc fees                    | Import as FEE (or type-tagged where useful)                                                  |
| Dividends on stock positions                                                     | Import as DIVIDEND                                                                           |
| Commission line items (IBCommission, ExchangeFee, RegulatoryFee, TransactionTax) | Sum into single `fee` field                                                                  |
| Assignment volume                                                                | Few per year — handle in importer, don't over-engineer                                       |
| Corporate actions (splits, mergers, spinoffs)                                    | Warn only; user handles in-app                                                               |
| Currency                                                                         | Assume USD only for v1 (revisit if needed)                                                   |
| Timezone                                                                         | Use `TradeDate` as reported by IBKR                                                          |
| Wash sale / tax lots                                                             | IBKR is tax record; Wealthfolio uses FIFO                                                    |

## Aggregation rules

**Round-trip predicate**: for each `(account, asset, date)`, if position was
**zero at both start and end of day** → aggregate all that asset's trades on
that date into daily P&L.

Naturally captures: SPXW 0DTE, day-traded stocks, day-traded futures, day-traded
options that don't expire same day.

Correctly excludes: partial adds/reduces of pre-existing positions, overnight
holds, multi-day scalps.

**Granularity** (middle option chosen): one `CREDIT` row per
`(date, currency, asset_class)` per account. Buckets:

- `SPXW` — 0DTE index options
- `FUT_0DTE` — futures round-tripped intraday
- `STK_DAYTRADE` — equity round-trips
- `OPT_DAYTRADE` — non-0DTE options round-tripped intraday
- `OTHER_0DTE` — anything else (FOP, etc.)

## Native futures Phase 1 — checklist

Extracted from `docs/futures_support_plan.md`. Everything below is a
prerequisite for the trades importer to handle futures correctly.

- [ ] `InstrumentType::Futures` enum variant + serde + `as_db_str`/`from_db_str`
- [ ] `FuturesSpec` model (root, contract_month, expiration, multiplier,
      tick_size, tick_value, exchange_mic)
- [ ] `is_futures()`, `futures_spec()` helpers; `contract_multiplier()` extended
- [ ] `crates/core/src/utils/futures_symbol.rs` with `parse_futures_symbol` (CME
      format)
- [ ] Expiration lookup table for top ~30 contracts (equities third Friday,
      energy 3-BD-before-delivery-month, etc.)
- [ ] Hardcoded contract-spec table (see `futures_support_plan.md` for the
      table)
- [ ] `build_asset_metadata` Futures branch (populates `metadata.futures`)
- [ ] Sync-loop `AssetSkipReason::ExpiredFutures`
- [ ] Yahoo provider capability: add `InstrumentKind::Futures`;
      `MarketDataClient` symbol resolution `root+month+year → Yahoo ticker`
- [ ] Migration: add `"FUTURES"` to `assets.instrument_type` CHECK constraint
- [ ] Tests: symbol parser, expired-futures skip, spec serde round-trip,
      valuation math

Rough estimate: 3–4 days focused work. `docs/futures_support_plan.md` has the
design details.

## IBKR Flex Query setup

### Sections to enable

- **Trades** — every fill
- **CashTransactions** — deposits, withdrawals, interest, dividends, tax
  adjustments
- **CashReport** — daily interest accrual, margin interest
- **PriorPeriodPositions** — positions at query start (required for round-trip
  detection when import scope is partial)
- **OpenPositions** — positions at query end (sanity check)
- **ChangeInDividendAccruals** — dividend accruals (optional, helps time
  DIVIDEND rows)
- **CorporateActions** — for the warn-only pass

### Fields per section

**Trades**:

```
AccountId, TradeDate, TradeTime, Symbol, Description, AssetClass,
Multiplier, Strike, Expiry, Put/Call, Underlying, Buy/Sell, Quantity,
TradePrice, IBCommission, IBCommissionCurrency, NetCash, CurrencyPrimary,
TransactionType, NotesCodes, OrderID, TradeID
```

`TradeID` doubles as idempotency key for recurring imports.

**CashTransactions**:

```
AccountId, DateTime, Type, Description, Amount, CurrencyPrimary
```

**PriorPeriodPositions**:

```
AccountId, Symbol, Underlying, AssetClass, Multiplier, Expiry,
Strike, Put/Call, Quantity, MarkPrice, CostBasisPrice, CostBasisMoney,
CurrencyPrimary
```

Format: **CSV** with header row (simpler to parse than XML; equivalent for our
needs).

Period: rolling last N days for cadence; whole YTD for first backfill.

### Flex Web Service (automation)

- Two-step HTTP flow:
  1. `POST /AccountManagement/FlexWebService/SendRequest?t=<token>&q=<query_id>&v=3`
     → returns reference code
  2. `GET /AccountManagement/FlexWebService/GetStatement?t=<token>&q=<reference>&v=3`
     (poll until ready) → returns CSV or XML
- **Token**: IBKR Client Portal → Reports → Settings → Flex Web Service →
  generate token (long-lived, treat as secret)
- **Query ID**: shown after saving the Flex Query in the Client Portal
- Base URL:
  `https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/`
- Rate limits: ~1 request per Flex Query per few minutes (per IBKR docs)
- Wrapper script: `scripts/import/ibkr_flex_fetch.py` — fetch → save CSV →
  invoke converters

## Importer scripts (built on top of native futures)

### `scripts/import/ibkr_shared.py`

- Flex parser (CSV; XML fallback if needed)
- `PriorPeriodPositions` loader → seeds holdings map
- Round-trip detector (per-account, per-asset, per-date walker)
- Account mapping loader (reads `.ibkr_accounts.yml`)
- OCC / futures symbol composers
- Fee summer (across all commission line items)

### `scripts/import/ibkr_daily_aggregate.py`

- Filters trades where round-trip detector returns `AGGREGATE`
- Emits one `CREDIT` per `(date, currency, asset_class, account)`
- Amount = sum of `NetCash` for that bucket
- No asset creation, no sync overhead

### `scripts/import/ibkr_flex_to_wf_trades.py`

- Filters trades where round-trip detector returns `FULL`
- Per-trade BUY/SELL rows
- Multi-leg spread grouping:
  `notes = "spread_id=<OrderID>|strategy=<inferred_shape>"`
- Assignment expansion: `Assignment` row → option close at $0 + underlying
  BUY/SELL at strike
- Corporate action row → warn log entry, skip import (user handles in-app)
- Cash transactions from `CashTransactions` section →
  DEPOSIT/WITHDRAWAL/INTEREST/FEE/DIVIDEND
- Uses `TradeID` as `sourceRecordId` for idempotent re-runs

### `scripts/import/ibkr_flex_fetch.py`

- Runs the Flex Web Service dance
- Writes raw CSV to `~/.wealthfolio/ibkr/<date>.csv`
- Optionally invokes both converter scripts and merges output CSV
- Suitable for cron: `0 6 * * 1 ibkr_flex_fetch.py --run-converters`

## Open questions (parking lot)

- **OrderID field name** in Flex output — `orderID` vs `ibOrderID` vs `orderId`.
  Confirm from first sample.
- **Multi-day spread lifecycle**: opened Monday, closed Friday → each leg is
  full-import. How should Wealthfolio display grouped legs? For now, `OrderID`
  in notes; future enhancement could be a "strategy" pseudo-asset.
- **FOP symbology**: not covered by futures Phase 1. Interim: MANUAL-priced
  pseudo-asset. Design pass needed.
- **Flex Web Service secret storage**: token in
  `~/.wealthfolio/ibkr/credentials.toml` with 0600 perms. Or macOS Keychain
  integration.
- **First-run YTD 2026 volume estimate**: ~250 trading days × 300 SPXW
  trades/day ≈ 75k trade rows. Aggregator collapses to ~250 CREDIT rows; trades
  script sees the residual (probably a few hundred).
- **Prior period seed accuracy**: if user's IBKR account has weird
  transfers-in-kind or corporate action history, PriorPeriodPositions cost basis
  may not match reality. Trades script should log discrepancies loudly.
- **Cross-account transfers**: internal transfers between IBKR sub-accounts.
  Flex reports them; converter should classify as TRANSFER_OUT + TRANSFER_IN
  (Wealthfolio has both types).

## Next concrete step

Ship futures Phase 1 (per `docs/futures_support_plan.md` checklist above), then:

1. Set up Flex Query with the sections/fields listed here.
2. Pull a small sample (last 3 trading days) — send to iterate on shared parser.
3. Draft `ibkr_shared.py` (parser + round-trip detector + account map).
4. Draft `ibkr_daily_aggregate.py`.
5. Trial-import into a fresh dev DB (`DATABASE_URL=/tmp/wf_ibkr_trial.db`).
6. Draft `ibkr_flex_to_wf_trades.py`.
7. Add Flex Web Service fetcher + cron docs.
