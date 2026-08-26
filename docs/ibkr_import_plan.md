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
   pseudo-assets. ✓ shipped
2. **IBKR daily-aggregate importer** — collapses ~99 % of the trade volume
   (round-trip day trades) into daily P&L rows. ✓ shipped in
   `ibkr_flex_to_wf.py`
3. **IBKR full-trades importer** — persistent positions get proper BUY/SELL with
   cost basis. ✓ shipped (same script)
4. **FOP support** — native `InstrumentType::FuturesOption` with OCC-composed
   symbols and CONTRACT_SPECS-derived multipliers. ✓ shipped
5. **Automation via Flex Web Service** — recurring import triggered by a
   cron/loop. Deferred until manual workflow is stable across a few real monthly
   imports.

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

## Native futures Phase 1 — DONE

Shipped. See `docs/futures_support_plan.md` for design + status. Phases 2–4
(broker sync, dedicated UI, continuous back-adjusted charts, margin tracking)
remain open.

## IBKR Flex Query setup

### Sections to enable

- **Trades** — every fill
- **OptionEAE** — **critical**. Option Exercise/Assignment/Expiration + Cash
  Settlement rows. Without it, ITM cash-settled index-option P&L (SPX/SPXW/NDX
  etc.) is silently missing — observed ~$300K/month realized loss unaccounted
  for on a single test file.
- **CashTransactions** — deposits, withdrawals, interest, dividends, fees
- **CorporateActions** — warn-only pass (optional; not required for v1)
- **OpenPositions** — **strongly recommended for recurring imports**. Consumed
  by `--seed-from` to seed each period's position walk from the prior period's
  end-of-period snapshot. Without it, positions crossing a period boundary are
  misclassified (SELL of a pre-existing long looks like opening a short, etc.).
- **PriorPeriodPositions** — superseded by OpenPositions for our purposes;
  IBKR's field selection is more limited than OpenPositions.

### Fields per section

**Trades**:

```
AccountId, TradeDate, TradeTime, Symbol, Description, AssetClass,
Multiplier, Strike, Expiry, Put/Call, Underlying, Buy/Sell, Quantity,
TradePrice, IBCommission, IBCommissionCurrency, NetCash, CurrencyPrimary,
TransactionType, NotesCodes, OrderID, TradeID
```

`TradeID` doubles as idempotency key for recurring imports. Field-selection
gotcha: `OrigOrderID` in current samples returns `0` on most rows — needs Flex
Query re-config if we ever want multi-leg spread grouping via `OrderID`.

**OptionEAE** (required):

```
ClientAccountID, CurrencyPrimary, UnderlyingSymbol, Multiplier, Strike,
Expiry, Put/Call, Date, Transaction Type, Quantity, Trade Price,
Close Price, Proceeds, Comm/Tax, TradeID
```

`Transaction Type` values used: `Cash Settlement` (ITM cash-settled P&L),
`Assignment` (position doc), `Exercise` (position doc), `Buy`/`Sell` (STK
delivery from assignment — deduped against Trades by `TradeID`).

**CashTransactions**:

```
AccountId, DateTime, Type, Description, Amount, CurrencyPrimary
```

Observed `Type` values so far: `Dividends`, `Broker Interest Received`,
`Other Fees`. Deposits/Withdrawals not yet seen in test samples — verify they
emit as `Deposits & Withdrawals` when they occur.

**OpenPositions** (strongly recommended for recurring imports):

```
ClientAccountID, AssetClass, Symbol, UnderlyingSymbol, Multiplier, Strike,
Expiry, Put/Call, MarkPrice, OpenPrice, CostBasisPrice, CostBasisMoney,
FifoPnlUnrealized, Side, OpenDateTime, CurrencyPrimary, ReportDate, Quantity
```

`ReportDate` is required — that's the date each `TRANSFER_IN` seed row is
booked. `Quantity` is preferred; without it the converter derives from
`CostBasisMoney / (CostBasisPrice × Multiplier)`. Short positions (`Side=Short`)
are skipped with a warning — Wealthfolio's TRANSFER_IN doesn't accept negative
quantity yet.

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

## Importer scripts

### `scripts/import/ibkr_flex_to_wf.py` — SHIPPED

Single script does both aggregation and per-trade in one pass. ~360 lines;
splitting was not worth it.

- Multi-section CSV parser (auto-detects Trades / OptionEAE / MtM Prices /
  CashTransactions header rows, in any order)
- Round-trip detector: for each (account, asset), if
  `pos=0 at start of day AND pos=0 at end of day` → aggregate into one CREDIT
  per `(date, currency, bucket)` where bucket ∈ {SPXW, STK, FUT, OPT}. Otherwise
  emit per-trade BUY/SELL rows.
- Zero-price OPT book trades (`A`/`Ep`/`Ex`) → ADJUSTMENT with subtype
  `OPTION_EXPIRY` (removes qty via FIFO, no cash impact)
- Paired STK BUY/SELL from physical assignment kept as-is (Trades row is
  authoritative; OptionEAE STK duplicates are deduped by `TradeID`)
- OptionEAE Cash Settlement rows → folded into daily aggregate for round-tripped
  assets; standalone ADJUSTMENT cash row for multi-day holds
- CashTransactions section → DIVIDEND / INTEREST / FEE (Deposits/Withdrawals not
  yet observed in samples)
- Account map via YAML at `~/.wealthfolio/ibkr_accounts.yml` (example at
  `scripts/import/ibkr_accounts.example.yml`)
- `sourceRecordId` = `ibkr_trade:<hash(TradeID)>` for idempotent re-runs
- FUT rows leave `amount` blank so Wealthfolio computes
  `qty × price × multiplier` itself (IBKR NetCash on FUT is variation margin,
  not notional)

Test collapse observed: 12,541 raw Trades rows → 114 output rows (June 2026).

### `scripts/import/ibkr_flex_fetch.py` — NOT YET BUILT

- Runs the Flex Web Service two-step dance
- Writes raw CSV to `~/.wealthfolio/ibkr/<date>.csv`
- Optionally invokes `ibkr_flex_to_wf.py` and appends output to a running CSV
- Suitable for cron: `0 6 * * 1 ibkr_flex_fetch.py --run-converter`

## Open questions (parking lot)

- **OrderID field name** in Flex output — `orderID` vs `ibOrderID` vs `orderId`.
  Confirm from first sample.
- **Multi-day spread lifecycle**: opened Monday, closed Friday → each leg is
  full-import. How should Wealthfolio display grouped legs? For now, `OrderID`
  in notes; future enhancement could be a "strategy" pseudo-asset.
- **FOP symbology**: RESOLVED — native `InstrumentType::FuturesOption` shipped.
  OCC-composed symbols use the futures root as underlying
  (`ES    210104P03615000`). Multiplier inherited from `CONTRACT_SPECS` lookup
  at asset creation. `quote_mode=MANUAL` since no provider covers FOP quotes
  yet.
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

- [x] Ship futures Phase 1
- [x] Set up Flex Query with Trades + OptionEAE + CashTransactions + MtM Prices
- [x] Pull small samples (May 2026, June 2026)
- [x] Draft `ibkr_flex_to_wf.py` (parser + aggregator + per-trade + cash)
- [x] Trial-import into fresh dev DB (`DATABASE_URL=/tmp/wf_ibkr_trial.db`);
      diff NLV against IBKR statement — reconciled 06/09/2026 IBKR-Main to
      within $81 of stated -$90,219 loss
- [x] Iterate on discrepancies surfaced by trial import — see commit `bdccbbfc8`
      (cash-settlement dedup, fee gross-up, daily fee aggregation) and
      `46ccba6ee` (signed CREDIT support in core)
- [x] Document import workflow — see `docs/ibkr_import_workflow.md`
- [x] Add FOP (futures options) 0DTE aggregation support (see commit
      `d45d9eb61`)
- [x] Native `InstrumentType::FuturesOption` — multi-day FOPs now supported with
      correct per-asset multiplier inherited from underlying futures'
      `CONTRACT_SPECS`; `quote_mode=MANUAL` since no provider covers FOP quotes
- [x] OpenPositions-based seeding via `--seed-from` for correct cross-period
      position walks
- [ ] Short-position support in seed (currently skipped with warning; manual
      workaround documented)
- [ ] Add FUT expiry handling (see `docs/future_work.md`) — non-urgent
      workaround: close futures manually before expiry
- [ ] Add Flex Web Service fetcher (`ibkr_flex_fetch.py`) + cron docs — last
      item; wait until manual workflow is stable across a few real monthly
      imports
