# Announcement Event Taxonomy

A small, proximity-classified taxonomy of Indian exchange announcement events, used as a **relevance
gate** for hypothesis matching (constraint #4), plus the **structured-extraction field schema** per
event class for a future "extract typed data from the document" feature.

Derived from operator domain work (a regex categorizer + per-category extraction models). The
classifier lives in `advisory/announcement_event_classifier.py`; the field schemas below are
preserved as a design reference (not yet built into Stockey).

## Event classes (the classifier output)

`classify_announcement(text)` returns a set of these, using proximity/structural patterns rather than
bare keyword presence (so "rating upgrade" inside an ED-search story does NOT classify as
CREDIT_RATING, and "acquisition" inside SEBI takeover boilerplate does NOT classify as an acquisition):

| Class | Fires on (proximity/structure) | Direction-typical |
|---|---|---|
| `WORK_ORDER_CONTRACT` | received/got/award/bagged … order/contract; order/contract worth ₹…; work order | long |
| `L1_BIDDER` | L1 bid | long |
| `CREDIT_RATING` | agency (CRISIL/ICRA/CARE/…) … rating; rating … change/outlook | up=long / down=reduce |
| `BUYBACK` | buy-back / buyback / buy back | long |
| `BONUS` | bonus issue / bonus … equity share | long (mild) |
| `DIVIDEND` | dividend | low signal |
| `ACQUISITION_OF_COMPANY` | acquisition of … shares (default acquisition) | long (acquirer) |
| `ACQUISITION_OF_PROPERTY` | acquisition of … land/property/premises | neutral |
| `AMALGAMATION` | tribunal/NCLT; scheme of arrangement; demerger; amalgamation | long (value unlock) |
| `ALLOTMENT_OF_SHARES` / `ALLOTMENT_OF_DEBENTURES` | allotment of … shares / debentures/NCD | mixed |
| `PREFERENTIAL_ISSUE` / `QIP` | preferential; QIP/QIB | mixed (dilution vs growth) |
| `LOAN_DEFAULT` | default … payment/interest/loan/repay | reduce |
| `PERSONNEL_CHANGE` | resignation/resigned/appoint | mixed (resignation=risk) |
| `SAST` | SAST | info |
| `FINANCIAL_RESULT` | financial … result/statement/report | depends on beat/miss |
| `EARNINGS_CALL` | investor/analyst call/meet; transcript; earnings call | info |
| `SHAREHOLDING` | shareholding pattern/table | info |
| `INTIMATION_MEETING` | intimation/notice … meeting | info |
| `CHANGE_IN_RTA` | change in RTA | info |
| `IGNORE` | newspaper publication; BRSR/sustainability; book/register closure; nothing material | filter out |

## Using event classes in a hypothesis

Declare `event_classes` under `trigger_patterns` to gate matching on the event's classified type. This
is opt-in and combines with keywords/excludes:

```yaml
trigger_patterns:
  keywords:
    include: [buyback, share buyback, repurchase]
  event_classes: [BUYBACK]        # event must classify as BUYBACK -> drops incidental mentions
```

- **keywords + event_classes** — most precise (event must classify into a declared class AND a keyword
  must appear). On real data this cut additional noise 18-65% (ORDER_WIN 167->59, CREDIT_RATING
  41->32) on top of word-boundary matching.
- **event_classes only** (no keywords) — broad recall within the class (e.g. all real buybacks).
- **keywords only** — current behaviour (word-boundary phrase matching).

## Structured-extraction field schema (future feature, design reference)

Once an event is classified, an LLM could extract typed fields into a schema (Stockey's
`llm_event_evaluator` / structured-output infra would do this). The field taxonomy below is preserved
from operator work; "Also known as" synonyms are extraction hints. Not yet built.

`category_map` (event class -> schemas to extract):

```
FINANCIAL_RESULT (annual)  -> Pnl, BalanceSheetAssets, BalanceSheetLiabilities, BalanceSheetEquity, CashFlow
FINANCIAL_RESULT (quarter) -> Pnl
EARNINGS_CALL              -> Guidance, ArbitrationAward, DebtCapital
WORK_ORDER_CONTRACT        -> OrderBook
QIP / PREFERENTIAL_ISSUE / RIGHTS_ISSUE -> FundRaise
CREDIT_RATING              -> DebtCapital
```

Key schemas (field -> meaning):

- **Pnl** (quarter/annual): total_revenue, total_expenses, employee_expenses, depreciation, interest,
  other_income, profit_before_tax, total_tax, net_profit (a.k.a. PAT), eps_basic, eps_diluted; plus
  `*_breakup` JSON for line items.
- **BalanceSheet** (Assets / Liabilities / Equity): cash_and_cash_equivalents, accounts_receivable,
  inventory, PP&E, intangibles, investments (short/long); accounts_payable, short/long_term_debt,
  deferred_tax; share_capital, retained_earnings, total_equity, treasury_stock.
- **CashFlow**: total_cash_operating/investing/financing_activities, purchase/sale_fixed_assets (capex),
  proceeds/repayment_borrowings, dividends_paid, share_buybacks, acquisition_companies, net_change_cash.
- **Guidance** (from concall): revenue, revenue_growth, ebitda_margin(+trend+reason), pat_margin(+trend),
  operating_margin, current_order_book_value, current_order_breakup, new_orders, execution_timeline_months,
  capacity_utilization(+history), capacity_increase, new_capacity_date, long_term_revenue_guidance.
- **OrderBook**: orders (list of {value, client, scope, timeline}).
- **FundRaise**: fund_raise_amount, fund_raise_type (QIP/Preferential/Rights/Other), fund_raise_parties,
  promoter_participation, fund_raise_timeline, fund_raise_planned.
- **DebtCapital**: long/short_term_borrowing amount + interest_rate + reason, working_capital_change,
  expected_payoff, interest_cost_reduction.
- **ArbitrationAward**: arbitration_amount, arbitration_expected, arbitration_payment_expected, title.

Building this extraction layer would let event-driven hypotheses condition on *magnitude* (e.g. order
value as a share of revenue, margin trend) rather than just event presence -- the natural next step
after event-class relevance.
