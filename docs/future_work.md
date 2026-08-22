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
