//! Progress reporting for quote synchronization.
//!
//! `SyncProgressReporter` is invoked by `QuoteSyncService::execute_sync_plans`
//! once when the plan set is finalized, then again after each asset completes.
//! Cumulative counters are always reported so the receiver does not need to
//! accumulate state itself.

use std::sync::Arc;

#[derive(Clone, Copy, Debug, Default)]
pub struct SyncProgress {
    /// Total assets the sync loop plans to touch.
    pub total: usize,
    /// Assets whose sync completed successfully.
    pub synced: usize,
    /// Assets whose sync failed.
    pub failed: usize,
    /// Assets skipped during execution (e.g. lock contention, no data).
    pub skipped: usize,
}

pub trait SyncProgressReporter: Send + Sync {
    fn report(&self, progress: SyncProgress);
}

/// Convenience alias for the boxed reporter passed through service layers.
pub type SharedSyncProgressReporter = Arc<dyn SyncProgressReporter>;
