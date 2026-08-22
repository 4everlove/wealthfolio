# Beancount ↔ Wealthfolio Integration

Working doc. Ideas here will get iterated as the personal-finance workflow
settles.

## Guiding decision

**Beancount is the source of truth. Wealthfolio is the portfolio-analytics
view.**

Rationale:

- Beancount enforces double-entry and reconciliation — that's what makes it
  audit-quality for tax and net-worth truth.
- Wealthfolio is strong where beancount is weak: cost-basis with corporate
  actions, DRIP synthesis, MTM valuation, live charts, broker sync.
- Making Wealthfolio authoritative would erode beancount's balancing invariants.

## Domain boundary

| Domain                                    | Master                                                        | Notes                                                                                        |
| ----------------------------------------- | ------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| Cash accounts, checking, credit cards     | beancount                                                     | daily transactions, bill pay                                                                 |
| Spending categorization                   | beancount                                                     | beancount's category tree is richer; Wealthfolio spending is a nice UI but shallow           |
| Liabilities (mortgage, student loans)     | beancount                                                     | amortization + interest split needs balancing                                                |
| Salary, tax withholdings, refunds         | beancount                                                     | ditto                                                                                        |
| Investment activities (BUY/SELL/DIVIDEND) | Wealthfolio (via broker sync) → replicate to beancount        | Wealthfolio's compiler handles cost basis, DRIP, splits; beancount just records the postings |
| Market prices / quote history             | Wealthfolio's SQLite → export to beancount `price` directives | Wealthfolio already syncs Yahoo — no need to duplicate                                       |
| Portfolio charts, YTD returns, TWR        | Wealthfolio (view-only)                                       | beancount can compute these but the UI is fava, less polished                                |

## Proposed workflow

Weekly/monthly script `scripts/export/wealthfolio_to_beancount.py`:

1. Read activities from Wealthfolio SQLite since last export high-water-mark.
2. Emit `.bean` transactions to `investments.bean`.
3. Emit `price` directives from `quotes` to `prices.bean`.
4. Track high-water-mark so re-runs are idempotent.

Beancount side:

```
include "investments.bean"
include "prices.bean"
```

Generated posting example:

```
2026-08-14 * "HealthEquity" "BUY VIIIX"
  Assets:HSA:VIIIX          0.33997 VIIIX {380.50 USD}
  Assets:HSA:Cash          -129.43 USD
```

## What to avoid

- **Dual entry**: pick one direction per domain and stick with it. Broker sync
  goes to Wealthfolio → then to beancount, not both by hand.
- **Bidirectional sync**: eventually double-books when the two disagree about
  what "changed" means.
- **Cross-porting spending categorization**: beancount and Wealthfolio use
  different mental models. Trying to unify creates fights with both tools.

## Wealthfolio spending/liabilities: two stances

Given beancount already handles these:

1. **Ignore them entirely** — cleanest. Wealthfolio = investments only;
   beancount = everything else.
2. **Populate selectively** for the dashboard — e.g., a single "monthly credit
   card summary" liability so net-worth is complete. Don't itemize.

## Open questions / iteration parking lot

- **Account naming convention**: How to map `Assets:HSA:VIIIX` vs
  `Assets:Investments:Broker:Ticker`. Config file per account?
- **Cash sweep funds** (TTTXX etc.): treat as commodity in beancount or as USD
  cash?
- **DRIP entries synthesized in Wealthfolio**: emit as normal beancount
  transactions, or as `padding` + reconciliation?
- **Corporate actions** (splits, mergers, spinoffs): Wealthfolio understands
  them, but the export needs to produce beancount-idiomatic postings.
- **Multi-currency**: FX rates already in Wealthfolio's `fx` table — export to
  beancount `price` for the conversion currencies.
- **Selective exports**: only investment accounts (skip anything that beancount
  tracks directly)?
- **Round-tripping tax lots**: Wealthfolio's cost basis engine is richer than
  beancount's `{cost}` syntax; how much lot detail to carry over?
- **Reconciliation checks**: at export time, assert Wealthfolio's per-account
  snapshot matches beancount's balance for those accounts. Useful drift
  detector.
- **Reverse direction (edge case)**: manual beancount edits (e.g. adjustment for
  a stock split we backdated) — how do they get reflected in Wealthfolio, or
  should they?
- **fava vs Wealthfolio dashboards**: which is better for what? Fava for
  tax/reporting drill-down, Wealthfolio for chart-heavy portfolio view.

## Next concrete step

Draft `scripts/export/wealthfolio_to_beancount.py` — reads SQLite, writes
idempotent `.bean` output. Structure mirrors
`scripts/import/healthequity_to_wf_csv.py`. Needs upstream decision on account
naming convention.
