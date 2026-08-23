# Futures Support — Design Sketch

**Status (2026-08-23)**: Phase 1 shipped. Phases 2–4 and open questions below
remain future work.

## Goal

First-class support for futures contracts: correct valuation (contract
multiplier), expiry-based sync skip (mirroring options), broker/CSV ingest, and
chart display. Out of scope: automated rollover, margin tracking, physical
delivery.

## Assumptions

- Per-contract assets (like options). Each contract month is its own asset
  (`ESH26`, `ESM26`, `ESU26`). Rollover = user closes old / opens new.
- Daily OHLCV only. No intraday, no continuous back-adjustment.
- Yahoo as default provider; other providers gated by capability flag.

## Model changes

`crates/core/src/assets/assets_model.rs`

- `InstrumentType::Futures` variant.
- `FuturesSpec`:
  ```rust
  pub struct FuturesSpec {
      pub root: String,                // "ES", "CL", "GC"
      pub contract_month: NaiveDate,   // 1st of the delivery month
      pub expiration: NaiveDate,       // last trading day
      pub multiplier: Decimal,         // $/point (50 for ES, 1000 for CL)
      pub tick_size: Decimal,          // 0.25 for ES
      pub tick_value: Decimal,         // 12.50 for ES
      pub exchange_mic: Option<String>,// XCME, XNYM, XCEC
  }
  ```
- Serialized under `metadata.futures` (mirror of `metadata.option`).
- Helpers: `is_futures()`, `futures_spec()`, `contract_multiplier()` extended.
- `as_db_str()` / `from_db_str()` / `from_external_str()` updated. Remove the
  `"FUTURE"|"FUTURES" => Equity` coercion in `from_external_str`.

## Symbol parsing

New `crates/core/src/utils/futures_symbol.rs`

- CME format: `<ROOT><MONTH><YY>` — month code `F G H J K M N Q U V X Z` =
  Jan..Dec, year 1 or 2 digits.
- Parser: `ESH26` → `{ root: "ES", contract_month: 2026-03-01, year: 2026 }`.
- Expiration table (per root): equities third Friday of month, energy 3 business
  days before delivery month, etc. Ship v1 with a lookup table for top ~30
  contracts; unknowns fall back to last business day of month before delivery.
- Yahoo suffix helper: `to_yahoo_symbol` — most futures tickers on Yahoo are
  `<ROOT><MONTH><Y>.CME` or `=F` continuous. Add mapping table.

Built-in contract-spec table (v1 hardcoded, later movable to a config file):

```rust
// (root, multiplier, tick_size, tick_value, exchange_mic)
ES  50   0.25  12.50 XCME
NQ  20   0.25   5.00 XCME
YM   5   1.00   5.00 XCBT
RTY 50   0.10   5.00 XCME
CL 1000  0.01  10.00 XNYM
GC 100   0.10  10.00 XCEC
SI 5000  0.005 25.00 XCEC
HG 25000 0.0005 12.50 XCEC
ZB 1000  1/32   31.25 XCBT
ZN 1000  1/64   15.625 XCBT
6E 125000 0.00005 6.25 XCME
6J 12500000 0.0000005 6.25 XCME
BTC 5    5.00  25.00 XCME
```

Anything not in the table → multiplier=1, tick=0.01, warn on ingest.

## Sync loop

`crates/core/src/quotes/sync.rs`

- `AssetSkipReason::ExpiredFutures`.
- In `get_skip_reason`, mirror `is_option()` block:
  ```rust
  if asset.is_futures() {
      if let Some(spec) = asset.futures_spec() {
          if spec.expiration < Utc::now().date_naive() {
              return Some(AssetSkipReason::ExpiredFutures);
          }
      }
  }
  ```
- After skip, reset error state like `ExpiredOption` so expired contracts don't
  clutter the health page.

## Provider capabilities

`crates/market-data/src/provider/`

- Add `InstrumentKind::Futures` variant.
- Yahoo (`yahoo/mod.rs`): declare `Futures` in `instrument_kinds`. Symbol
  resolution in `MarketDataClient` maps
  `FuturesSpec.root + month + year → <yahoo-ticker>`.
- Other providers (Alpha Vantage, Finnhub, etc.): initially exclude from
  `instrument_kinds`. Add later if users request.

## Ingestion

`crates/core/src/activities/activities_service.rs`

- `build_asset_spec`: when `instrument_type == Futures`, populate
  `spec.metadata` via new `build_futures_metadata(symbol)`.
- `build_asset_metadata` in assets_model: add `Futures` branch → calls
  `parse_futures_symbol` → builds `FuturesSpec`.
- Fallback funnel already fixed (`get_or_create_minimal_asset`) — no additional
  change once `build_asset_metadata` handles Futures.

`crates/connect/src/broker/service.rs`

- Broker holdings sync: detect futures via broker payload (each broker uses
  different flags — parse per adapter).
- Build `FuturesSpec` using broker-provided expiration/multiplier when
  available; fall back to parser + table.

## Valuation / P&L

`crates/core/src/portfolio/`

- `contract_multiplier()` on Asset already exists — Futures branch returns
  `FuturesSpec.multiplier`. Existing quantity × price × multiplier logic used
  for options should transparently work.
- Cash flow: futures are marked-to-market daily; positions carry no principal.
  Ignore for v1 — treat like options (P&L = (mark − avg_cost) × qty ×
  multiplier).
- No dividends, splits, or corporate actions.

## Schema / migration

No new tables. Reuse `assets.metadata` JSON blob with `futures` key.

- New CHECK constraint update: add `"FUTURES"` to allowed `instrument_type`
  values.
- Migration file: `NNNN_futures_instrument_type/up.sql`.
- Data backfill: nothing to migrate (`FUTURES` was silently coerced to `EQUITY`;
  user must reclassify manually via a rescue script if they've been importing
  futures). Ship a `wf futures repair` CLI command that scans equity assets with
  recognizable futures tickers and reclassifies.

## UI (`apps/frontend/src/`)

- Symbol search: allow filtering by instrument type — add "Futures" filter.
- Holdings table: show contract month + expiration on futures rows (like options
  show expiry/strike).
- Chart: no changes; existing OHLCV chart works.
- Position editor: new fields when instrument_type = Futures (root,
  contract_month, multiplier).
- i18n: add `futures.*` keys.

## Testing

- `futures_symbol` parser tests: valid CME, invalid month code, year rollover,
  non-standard roots.
- `ExpiredFutures` skip test in sync (mirror expired-option test).
- `FuturesSpec` serde round-trip.
- Contract-multiplier valuation test: 1 ES contract × 10 pts = $500 P&L.
- Broker sync end-to-end test with a futures holding.

## Rollout / phasing

1. **Phase 1 (foundation)**: enum variant, `FuturesSpec`, parser, sync-skip,
   Yahoo capability. Ship without UI changes. Futures work via CSV import +
   manual holding creation. No broker sync yet.
2. **Phase 2 (broker sync)**: adapt each broker connector. Adds meaningful data
   for actively traded users.
3. **Phase 3 (UI polish)**: dedicated futures screens, contract-spec editor,
   rollover helper (offer to close+open on expiry).
4. **Phase 4 (advanced)**: continuous back-adjusted series for charts (Yahoo
   `=F` fallback), margin tracking, tick-value-based P&L attribution.

## Open questions

- Rollover UX: automated create-new-close-old on expiry, or manual? Recommend
  manual for v1 (matches how options work).
- Continuous vs. per-contract charts: users often want a continuous chart across
  rollovers. Store separately? Overlay on demand?
- Custom multipliers: user override for non-standard contracts? Add
  `asset.metadata.contractMultiplier` top-level override (already exists for
  options — reuse).
- Margin: track initial/maintenance margin as an account-level cash constraint,
  or ignore? Ignore for v1.
- Non-US futures (Eurex, ICE, HKEX): symbol formats differ; ship US contracts
  first, expand.
