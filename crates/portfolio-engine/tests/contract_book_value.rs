//! A contract instrument (option / futures) with no quote is valued at its
//! book cost, so historical NLV stays flat; an unquoted equity stays unpriced.
#![allow(clippy::unwrap_used, clippy::panic, reason = "test code")]

mod support;

use rust_decimal_macros::dec;
use support::*;
use wealthfolio_portfolio_engine::model::*;

#[test]
fn an_unquoted_contract_is_valued_at_book_cost() {
    let scenario: Scenario = serde_yaml::from_str(
        r#"
id: CONTRACT-BOOK-01
policy: { base_currency: USD, timezone: UTC, as_of: 2025-01-08 }
accounts:
  - { id: acc, currency: USD }
assets:
  - { id: opt, quote_ccy: USD, instrument_type: OPTION, contract_multiplier: 100 }
  - { id: stk, quote_ccy: USD }
activities:
  - { id: dep-1, account: acc, type: DEPOSIT, date: 2025-01-02T10:00:00Z, amount: 10000 }
  - { id: buy-opt, account: acc, type: BUY, date: 2025-01-03T10:00:00Z, asset: opt, quantity: 1, unit_price: 5, amount: 500 }
  - { id: buy-stk, account: acc, type: BUY, date: 2025-01-03T10:00:00Z, asset: stk, quantity: 10, unit_price: 20, amount: 200 }
"#,
    )
    .expect("scenario");
    let pipeline = Pipeline::run(scenario.raw_facts()).expect("pipeline");
    let day = pipeline.series[&AccountId::new("acc")]
        .days
        .last()
        .expect("valuation day");
    assert_eq!(day.cash_balance, dec!(9300));
    // The option counts at 1 × 5 × 100; the stock contributes nothing.
    assert_eq!(day.investment_market_value, dec!(500));
    assert_eq!(day.total_value, dec!(9800));
    assert_eq!(day.value_status, ValueStatus::PartialUnpriced);
}

#[test]
fn a_futures_sell_without_a_long_opens_a_short() {
    let scenario: Scenario = serde_yaml::from_str(
        r#"
id: FUTURES-SHORT-01
policy: { base_currency: USD, timezone: UTC, as_of: 2025-01-08 }
accounts:
  - { id: acc, currency: USD }
assets:
  - { id: es, quote_ccy: USD, instrument_type: FUTURES, contract_multiplier: 50 }
activities:
  - { id: dep-1, account: acc, type: DEPOSIT, date: 2025-01-02T10:00:00Z, amount: 10000 }
  - { id: sell-es, account: acc, type: SELL, date: 2025-01-03T10:00:00Z, asset: es, quantity: 1, unit_price: 6000, amount: 0 }
quotes:
  - { asset: es, day: 2025-01-03, close: 6000 }
"#,
    )
    .expect("scenario");
    let pipeline = Pipeline::run(scenario.raw_facts()).expect("pipeline");
    let frame = pipeline.bundle.keyframes[&AccountId::new("acc")]
        .last()
        .expect("keyframe");
    assert_eq!(
        frame.state.positions[&AssetId::new("es")].quantity,
        dec!(-1)
    );
}
