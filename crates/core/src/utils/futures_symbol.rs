//! CME-style futures symbol parser and per-root contract specifications.
//!
//! Futures ticker format: `<ROOT><MONTH_CODE><YEAR>` (e.g. `ESH26`).
//!
//! - **Root**: 1–4 uppercase letters/digits (`ES`, `NQ`, `6E`, `BTC`).
//! - **Month code**: single letter per CME convention:
//!   `F=Jan G=Feb H=Mar J=Apr K=May M=Jun N=Jul Q=Aug U=Sep V=Oct X=Nov Z=Dec`.
//! - **Year**: 1 or 2 digits. Two-digit years are pinned to 2000–2099; single
//!   digits are resolved to the nearest future decade relative to today (so `H6`
//!   at ingest in 2026 → 2026, not 2016).
//!
//! Expiration date is looked up from `CONTRACT_SPECS`. When a root isn't in the
//! table we fall back to a conservative estimate (last business day of the month
//! before delivery) and warn the caller via `ParsedFuturesSymbol::estimated_expiration`.

use chrono::{Datelike, NaiveDate};
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use thiserror::Error;

#[derive(Debug, Error, PartialEq)]
pub enum FuturesSymbolError {
    #[error("Symbol too short: {0:?}")]
    TooShort(String),
    #[error("Invalid month code {0:?}: expected one of F G H J K M N Q U V X Z")]
    InvalidMonthCode(char),
    #[error("Invalid year digits {0:?}")]
    InvalidYear(String),
    #[error("Empty root symbol")]
    EmptyRoot,
}

/// Expiration rule for a given root. Encoded so we can compute the actual
/// expiration date for any contract month without a per-contract table.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum ExpirationRule {
    /// Third Friday of the delivery month (equity-index E-mini family).
    ThirdFriday,
    /// Three US business days before the first business day of the delivery
    /// month (crude oil, natural gas, most energy contracts).
    ThreeBDaysBeforeDeliveryMonth,
    /// Last US business day of the month preceding the delivery month
    /// (COMEX metals like GC, SI, HG).
    LastBDayBeforeDeliveryMonth,
    /// Second US business day preceding the third Wednesday of the delivery
    /// month (CME FX futures like 6E, 6J, 6B).
    TwoBDaysBeforeThirdWednesday,
    /// Last US business day of the delivery month (Treasury futures ZB/ZN).
    LastBDayOfDeliveryMonth,
}

/// Static contract specification for a futures root.
#[derive(Clone, Debug)]
pub struct ContractSpec {
    pub root: &'static str,
    pub multiplier: Decimal,
    pub tick_size: Decimal,
    pub tick_value: Decimal,
    pub exchange_mic: &'static str,
    pub expiration_rule: ExpirationRule,
}

/// Hardcoded table for the top ~30 contracts most retail traders touch.
/// Values sourced from each exchange's contract specification page.
pub const CONTRACT_SPECS: &[ContractSpec] = &[
    // Equity index — E-mini
    spec("ES",  dec!(50),  dec!(0.25),   dec!(12.50),  "XCME", ExpirationRule::ThirdFriday),
    spec("NQ",  dec!(20),  dec!(0.25),   dec!(5.00),   "XCME", ExpirationRule::ThirdFriday),
    spec("YM",  dec!(5),   dec!(1.00),   dec!(5.00),   "XCBT", ExpirationRule::ThirdFriday),
    spec("RTY", dec!(50),  dec!(0.10),   dec!(5.00),   "XCME", ExpirationRule::ThirdFriday),
    // Equity index — Micro
    spec("MES", dec!(5),   dec!(0.25),   dec!(1.25),   "XCME", ExpirationRule::ThirdFriday),
    spec("MNQ", dec!(2),   dec!(0.25),   dec!(0.50),   "XCME", ExpirationRule::ThirdFriday),
    spec("MYM", dec!(0.5), dec!(1.00),   dec!(0.50),   "XCBT", ExpirationRule::ThirdFriday),
    spec("M2K", dec!(5),   dec!(0.10),   dec!(0.50),   "XCME", ExpirationRule::ThirdFriday),
    // Energy
    spec("CL",  dec!(1000),dec!(0.01),   dec!(10.00),  "XNYM", ExpirationRule::ThreeBDaysBeforeDeliveryMonth),
    spec("NG",  dec!(10000),dec!(0.001), dec!(10.00),  "XNYM", ExpirationRule::ThreeBDaysBeforeDeliveryMonth),
    spec("HO",  dec!(42000),dec!(0.0001),dec!(4.20),   "XNYM", ExpirationRule::ThreeBDaysBeforeDeliveryMonth),
    spec("RB",  dec!(42000),dec!(0.0001),dec!(4.20),   "XNYM", ExpirationRule::ThreeBDaysBeforeDeliveryMonth),
    // Metals
    spec("GC",  dec!(100), dec!(0.10),   dec!(10.00),  "XCEC", ExpirationRule::LastBDayBeforeDeliveryMonth),
    spec("SI",  dec!(5000),dec!(0.005),  dec!(25.00),  "XCEC", ExpirationRule::LastBDayBeforeDeliveryMonth),
    spec("HG",  dec!(25000),dec!(0.0005),dec!(12.50),  "XCEC", ExpirationRule::LastBDayBeforeDeliveryMonth),
    spec("PL",  dec!(50),  dec!(0.10),   dec!(5.00),   "XNYM", ExpirationRule::LastBDayBeforeDeliveryMonth),
    // Micro metals
    spec("MGC", dec!(10),  dec!(0.10),   dec!(1.00),   "XCEC", ExpirationRule::LastBDayBeforeDeliveryMonth),
    spec("SIL", dec!(1000),dec!(0.005),  dec!(5.00),   "XCEC", ExpirationRule::LastBDayBeforeDeliveryMonth),
    // Rates (Treasury futures)
    spec("ZB",  dec!(1000),dec!(0.03125),dec!(31.25),  "XCBT", ExpirationRule::LastBDayOfDeliveryMonth),
    spec("ZN",  dec!(1000),dec!(0.015625),dec!(15.625),"XCBT", ExpirationRule::LastBDayOfDeliveryMonth),
    spec("ZF",  dec!(1000),dec!(0.0078125),dec!(7.8125),"XCBT",ExpirationRule::LastBDayOfDeliveryMonth),
    spec("ZT",  dec!(2000),dec!(0.00390625),dec!(7.8125),"XCBT",ExpirationRule::LastBDayOfDeliveryMonth),
    // FX
    spec("6E",  dec!(125000),   dec!(0.00005),  dec!(6.25),   "XCME", ExpirationRule::TwoBDaysBeforeThirdWednesday),
    spec("6J",  dec!(12500000), dec!(0.0000005),dec!(6.25),   "XCME", ExpirationRule::TwoBDaysBeforeThirdWednesday),
    spec("6B",  dec!(62500),    dec!(0.0001),   dec!(6.25),   "XCME", ExpirationRule::TwoBDaysBeforeThirdWednesday),
    spec("6A",  dec!(100000),   dec!(0.0001),   dec!(10.00),  "XCME", ExpirationRule::TwoBDaysBeforeThirdWednesday),
    spec("6C",  dec!(100000),   dec!(0.0001),   dec!(10.00),  "XCME", ExpirationRule::TwoBDaysBeforeThirdWednesday),
    // Crypto
    spec("BTC", dec!(5),   dec!(5.00),   dec!(25.00),  "XCME", ExpirationRule::LastBDayOfDeliveryMonth),
    spec("MBT", dec!(0.1), dec!(5.00),   dec!(0.50),   "XCME", ExpirationRule::LastBDayOfDeliveryMonth),
    spec("ETH", dec!(50),  dec!(0.25),   dec!(12.50),  "XCME", ExpirationRule::LastBDayOfDeliveryMonth),
];

const fn spec(
    root: &'static str,
    multiplier: Decimal,
    tick_size: Decimal,
    tick_value: Decimal,
    exchange_mic: &'static str,
    expiration_rule: ExpirationRule,
) -> ContractSpec {
    ContractSpec {
        root,
        multiplier,
        tick_size,
        tick_value,
        exchange_mic,
        expiration_rule,
    }
}

/// Look up the spec for a root symbol (case-insensitive).
pub fn lookup_spec(root: &str) -> Option<&'static ContractSpec> {
    let up = root.to_uppercase();
    CONTRACT_SPECS.iter().find(|s| s.root == up)
}

/// Parsed components of a futures symbol.
#[derive(Clone, Debug, PartialEq)]
pub struct ParsedFuturesSymbol {
    pub root: String,
    /// First day of the delivery month.
    pub contract_month: NaiveDate,
    /// Last trading day. Estimated when the root isn't in `CONTRACT_SPECS`.
    pub expiration: NaiveDate,
    pub multiplier: Decimal,
    pub tick_size: Decimal,
    pub tick_value: Decimal,
    pub exchange_mic: String,
    /// True when we didn't have a spec and fell back to a conservative estimate.
    pub estimated_expiration: bool,
}

/// Parse a CME-style futures symbol (e.g. `ESH26`).
pub fn parse_futures_symbol(
    symbol: &str,
) -> std::result::Result<ParsedFuturesSymbol, FuturesSymbolError> {
    let s = symbol.trim().to_uppercase();
    if s.len() < 3 {
        return Err(FuturesSymbolError::TooShort(s));
    }

    // Year is the trailing 1 or 2 digits; month code sits immediately before.
    let bytes = s.as_bytes();
    let mut year_start = bytes.len();
    while year_start > 0 && bytes[year_start - 1].is_ascii_digit() {
        year_start -= 1;
    }
    let year_str = &s[year_start..];
    if year_str.is_empty() || year_str.len() > 2 {
        return Err(FuturesSymbolError::InvalidYear(year_str.to_string()));
    }
    if year_start == 0 {
        return Err(FuturesSymbolError::EmptyRoot);
    }
    let month_char = bytes[year_start - 1] as char;
    let month = month_from_code(month_char).ok_or(FuturesSymbolError::InvalidMonthCode(month_char))?;

    let root = &s[..year_start - 1];
    if root.is_empty() {
        return Err(FuturesSymbolError::EmptyRoot);
    }

    let year_num: i32 = year_str
        .parse()
        .map_err(|_| FuturesSymbolError::InvalidYear(year_str.to_string()))?;
    let year = resolve_year(year_num);
    let contract_month = NaiveDate::from_ymd_opt(year, month, 1)
        .ok_or_else(|| FuturesSymbolError::InvalidYear(year_str.to_string()))?;

    let spec = lookup_spec(root);
    let (expiration, estimated) = match spec {
        Some(s) => (compute_expiration(s.expiration_rule, contract_month), false),
        None => (
            // Fallback: last calendar day of the month preceding delivery.
            last_day_of_prior_month(contract_month),
            true,
        ),
    };

    Ok(ParsedFuturesSymbol {
        root: root.to_string(),
        contract_month,
        expiration,
        multiplier: spec.map(|s| s.multiplier).unwrap_or(Decimal::ONE),
        tick_size: spec.map(|s| s.tick_size).unwrap_or(dec!(0.01)),
        tick_value: spec.map(|s| s.tick_value).unwrap_or(dec!(0.01)),
        exchange_mic: spec
            .map(|s| s.exchange_mic.to_string())
            .unwrap_or_default(),
        estimated_expiration: estimated,
    })
}

fn month_from_code(c: char) -> Option<u32> {
    match c {
        'F' => Some(1),
        'G' => Some(2),
        'H' => Some(3),
        'J' => Some(4),
        'K' => Some(5),
        'M' => Some(6),
        'N' => Some(7),
        'Q' => Some(8),
        'U' => Some(9),
        'V' => Some(10),
        'X' => Some(11),
        'Z' => Some(12),
        _ => None,
    }
}

/// Resolve a 1- or 2-digit year to a full 4-digit year.
///
/// Two-digit years pin to 2000–2099. Single-digit years resolve to the nearest
/// non-past year with that last digit (`H6` in 2026 → 2026; `H1` in 2026 → 2031).
fn resolve_year(y: i32) -> i32 {
    resolve_year_with_today(y, chrono::Utc::now().date_naive().year())
}

fn resolve_year_with_today(y: i32, current: i32) -> i32 {
    if y >= 10 {
        return 2000 + y;
    }
    let base_decade = current - (current % 10);
    let candidate = base_decade + y;
    if candidate < current {
        candidate + 10
    } else {
        candidate
    }
}

fn compute_expiration(rule: ExpirationRule, contract_month: NaiveDate) -> NaiveDate {
    match rule {
        ExpirationRule::ThirdFriday => nth_weekday_of_month(contract_month, chrono::Weekday::Fri, 3)
            .unwrap_or_else(|| last_day_of_prior_month(contract_month)),
        ExpirationRule::ThreeBDaysBeforeDeliveryMonth => {
            step_business_days(first_of_month(contract_month), -3)
        }
        ExpirationRule::LastBDayBeforeDeliveryMonth => {
            last_business_day_of_month(prior_month(contract_month))
        }
        ExpirationRule::TwoBDaysBeforeThirdWednesday => {
            let third_wed = nth_weekday_of_month(contract_month, chrono::Weekday::Wed, 3)
                .unwrap_or_else(|| last_day_of_prior_month(contract_month));
            step_business_days(third_wed, -2)
        }
        ExpirationRule::LastBDayOfDeliveryMonth => last_business_day_of_month(contract_month),
    }
}

fn first_of_month(d: NaiveDate) -> NaiveDate {
    NaiveDate::from_ymd_opt(d.year(), d.month(), 1).unwrap()
}

fn prior_month(d: NaiveDate) -> NaiveDate {
    let (y, m) = if d.month() == 1 {
        (d.year() - 1, 12)
    } else {
        (d.year(), d.month() - 1)
    };
    NaiveDate::from_ymd_opt(y, m, 1).unwrap()
}

fn last_day_of_prior_month(d: NaiveDate) -> NaiveDate {
    let first_this = first_of_month(d);
    first_this.pred_opt().unwrap()
}

fn last_business_day_of_month(anchor: NaiveDate) -> NaiveDate {
    let year = anchor.year();
    let month = anchor.month();
    // start from day 28+ and walk back to the last day of the month
    let mut d = NaiveDate::from_ymd_opt(year, month, 28).unwrap();
    while let Some(next) = d.succ_opt().filter(|nd| nd.month() == month) {
        d = next;
    }
    while matches!(d.weekday(), chrono::Weekday::Sat | chrono::Weekday::Sun) {
        d = d.pred_opt().unwrap();
    }
    d
}

fn nth_weekday_of_month(anchor: NaiveDate, weekday: chrono::Weekday, n: u32) -> Option<NaiveDate> {
    let first = first_of_month(anchor);
    let offset = (7 + weekday.num_days_from_monday() - first.weekday().num_days_from_monday()) % 7;
    let day = 1 + offset + (n - 1) * 7;
    NaiveDate::from_ymd_opt(anchor.year(), anchor.month(), day)
}

fn step_business_days(mut d: NaiveDate, delta: i32) -> NaiveDate {
    let step = if delta >= 0 { 1 } else { -1 };
    let mut remaining = delta.unsigned_abs();
    while remaining > 0 {
        d = if step > 0 { d.succ_opt().unwrap() } else { d.pred_opt().unwrap() };
        if !matches!(d.weekday(), chrono::Weekday::Sat | chrono::Weekday::Sun) {
            remaining -= 1;
        }
    }
    d
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_esh26() {
        let p = parse_futures_symbol("ESH26").unwrap();
        assert_eq!(p.root, "ES");
        assert_eq!(p.contract_month, NaiveDate::from_ymd_opt(2026, 3, 1).unwrap());
        // 3rd Friday of March 2026 = March 20
        assert_eq!(p.expiration, NaiveDate::from_ymd_opt(2026, 3, 20).unwrap());
        assert_eq!(p.multiplier, dec!(50));
        assert_eq!(p.exchange_mic, "XCME");
        assert!(!p.estimated_expiration);
    }

    #[test]
    fn parses_micro_es() {
        let p = parse_futures_symbol("MESU25").unwrap();
        assert_eq!(p.root, "MES");
        assert_eq!(p.multiplier, dec!(5));
    }

    #[test]
    fn parses_fx_future() {
        let p = parse_futures_symbol("6EM26").unwrap();
        assert_eq!(p.root, "6E");
        assert_eq!(p.multiplier, dec!(125000));
        // 3rd Wed of June 2026 = June 17; two business days before = June 15
        assert_eq!(p.expiration, NaiveDate::from_ymd_opt(2026, 6, 15).unwrap());
    }

    #[test]
    fn parses_crude() {
        let p = parse_futures_symbol("CLZ26").unwrap();
        assert_eq!(p.root, "CL");
        // Dec 1, 2026 is Tue. Three business days before: Mon Nov 30, Fri Nov 27,
        // Thu Nov 26. Ignores CME holidays — good enough for the sync-skip filter.
        assert_eq!(p.expiration, NaiveDate::from_ymd_opt(2026, 11, 26).unwrap());
    }

    #[test]
    fn parses_gold() {
        let p = parse_futures_symbol("GCM26").unwrap();
        assert_eq!(p.root, "GC");
        // Last business day of May 2026 = Fri May 29
        assert_eq!(p.expiration, NaiveDate::from_ymd_opt(2026, 5, 29).unwrap());
    }

    #[test]
    fn parses_bond_future() {
        let p = parse_futures_symbol("ZBH26").unwrap();
        assert_eq!(p.root, "ZB");
        // Last business day of Mar 2026 = Tue Mar 31
        assert_eq!(p.expiration, NaiveDate::from_ymd_opt(2026, 3, 31).unwrap());
    }

    #[test]
    fn unknown_root_falls_back() {
        let p = parse_futures_symbol("XYZH26").unwrap();
        assert_eq!(p.root, "XYZ");
        assert!(p.estimated_expiration);
        // Fallback: last calendar day of prior month = Feb 28, 2026
        assert_eq!(p.expiration, NaiveDate::from_ymd_opt(2026, 2, 28).unwrap());
    }

    #[test]
    fn single_digit_year_resolves_forward() {
        // Assuming today = 2026. `resolve_year_with_today` is pure so we can
        // pin the reference year and exercise every branch deterministically.
        assert_eq!(resolve_year_with_today(6, 2026), 2026); // same year
        assert_eq!(resolve_year_with_today(1, 2026), 2031); // was 2021, push forward
        assert_eq!(resolve_year_with_today(7, 2026), 2027); // future, keep
        assert_eq!(resolve_year_with_today(26, 2020), 2026); // two-digit pinned
    }

    #[test]
    fn rejects_invalid_month_code() {
        assert!(matches!(
            parse_futures_symbol("ESA26"),
            Err(FuturesSymbolError::InvalidMonthCode('A'))
        ));
    }

    #[test]
    fn rejects_too_short() {
        assert!(matches!(
            parse_futures_symbol("H6"),
            Err(FuturesSymbolError::TooShort(_)) | Err(FuturesSymbolError::EmptyRoot)
        ));
    }
}
