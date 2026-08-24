use crate::assets::Asset;

/// Central policy for assets that may carry negative lots.
///
/// Options and futures are naturally short-able and use signed lots directly.
/// Stock/ETF-style assets may also carry signed lots, but only when the
/// activity has explicit short intent (POSITION_OPEN subtype) — otherwise a
/// SELL without a long position is treated as an error rather than a short.
pub struct ShortabilityPolicy;

impl ShortabilityPolicy {
    pub fn allows_negative_lots(asset: &Asset) -> bool {
        asset.is_option() || asset.is_futures() || asset.is_equity_like()
    }

    pub fn requires_explicit_short_intent(asset: &Asset) -> bool {
        asset.is_equity_like()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::assets::{AssetKind, InstrumentType};

    fn asset_with(kind: AssetKind, instrument_type: Option<InstrumentType>) -> Asset {
        Asset {
            id: "test".to_string(),
            kind,
            instrument_type,
            ..Default::default()
        }
    }

    #[test]
    fn futures_allow_negative_lots_without_explicit_intent() {
        // Regression: a SELL on a futures asset without a prior long position
        // used to be dropped silently because allows_negative_lots was false
        // for futures. Result was a phantom long position when a later BUY
        // (intended to close the short) opened a new long lot instead.
        let fut = asset_with(AssetKind::Investment, Some(InstrumentType::Futures));
        assert!(ShortabilityPolicy::allows_negative_lots(&fut));
        assert!(!ShortabilityPolicy::requires_explicit_short_intent(&fut));
    }

    #[test]
    fn options_allow_negative_lots_without_explicit_intent() {
        let opt = asset_with(AssetKind::Investment, Some(InstrumentType::Option));
        assert!(ShortabilityPolicy::allows_negative_lots(&opt));
        assert!(!ShortabilityPolicy::requires_explicit_short_intent(&opt));
    }

    #[test]
    fn equities_allow_negative_lots_only_with_explicit_intent() {
        let eq = asset_with(AssetKind::Investment, Some(InstrumentType::Equity));
        assert!(ShortabilityPolicy::allows_negative_lots(&eq));
        assert!(ShortabilityPolicy::requires_explicit_short_intent(&eq));
    }
}
