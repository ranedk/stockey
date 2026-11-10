# Announcement catalogue

Generated from `stockey/infra/catalog.py` -- edit that file, not this one. Version 2. **Expected to change** as new kinds of filings turn up; each change bumps the version and the records extracted under an older one are read again.

The exchange's category is not trusted (NSE's tags are often wrong); the kind is decided from the filing's content.

## Changes

- 1 (2026-11-10): first fact list, flat, no per-type fields.
- 2 (2026-11-10): filing kinds with summary notes; per-type detail fields; kinds decided from content; added shareholding_pattern, fund_utilisation, scheme, disruption, clarification.

## Filing kinds and how to summarise them

| Kind | What it is | The summary says | Routine |
|---|---|---|---|
| `financial_results` | quarterly / annual results, standalone and/or consolidated | period; revenue, EBITDA and profit with year-on-year change; consolidated vs standalone; exceptional items; any auditor qualification or emphasis of matter |  |
| `board_outcome` | outcome of a board meeting | every decision taken (results, dividend, fund raise, appointments, acquisitions, capex), each with its number and date |  |
| `investor_presentation` | investor / analyst presentation | order book and inflow, guidance, segment mix, capacity and capex, anything new versus the last presentation |  |
| `earnings_call` | earnings call transcript or summary | management's guidance, order book, margin and demand commentary, capex, and the risks or doubts analysts pressed on -- with the numbers said |  |
| `press_release` | press / media release | the event announced, its size, counterparty and timing |  |
| `order` | order, contract, LoA/LoI, work order, or lowest-bidder (L1) status | customer, value, scope, execution period, domestic or export; say if it repeats an earlier announcement |  |
| `capital_raise` | preferential issue, warrants, QIP, rights, IPO/OFS, ESOP allotment, debt raise | instrument, size, price, who gets it (promoter or not), stage (proposed / approved / allotted), lock-in and purpose |  |
| `shareholding_change` | promoter / insider / substantial-holder disclosures (SAST, PIT), pledges, shareholding pattern | who, how many shares, holding before and after, mode (market, off-market, gift, pledge) and price |  |
| `corporate_action` | dividend, bonus, split, buyback, record date | the action, amount or ratio, record and payment dates |  |
| `acquisition_investment` | acquisition, investment in or formation of a subsidiary / JV / associate, divestment, scheme of arrangement | target, stake, consideration and valuation, funding, completion status and date |  |
| `capex_capacity` | new plant, capacity expansion, commissioning, disruption (fire, shutdown, strike) | project, capacity, cost, location, timeline, and effect on operations |  |
| `management_change` | director or key managerial person appointed, re-appointed, resigned or ceased; auditor change | who, which role, effective date, term, and the stated reason (quote it for a resignation) |  |
| `credit_rating` | credit rating action | agency, instrument and amount, rating and outlook, previous rating, upgrade / downgrade / reaffirm |  |
| `meeting_and_voting` | AGM / EGM / postal ballot notice, scrutiniser report, voting results | the resolutions and, for results, whether each passed and with what margin if close |  |
| `annual_report` | annual report or business responsibility report | the year's key numbers, order book, capex plans, related-party dealings, contingent liabilities, and what management says changed |  |
| `fund_utilisation` | monitoring agency report, statement of deviation or variation | money raised, used so far, and any deviation from the stated purpose |  |
| `legal_regulatory` | litigation, tax demand, penalty, regulator or exchange action | forum, counterparty, amount, what is claimed, and the company's stated impact |  |
| `clarification` | reply to an exchange query or news verification, price-movement clarification | what was asked and what the company said, including any denial |  |
| `analyst_meet_intimation` | schedule of analyst / investor meetings (not the content) | one sentence: dates and organisers | yes |
| `newspaper_copy` | copy of a newspaper advertisement (results ad, AGM notice, etc.) | one sentence naming what was advertised | yes |
| `trading_window` | trading window closure / opening | one sentence with the dates | yes |
| `registrar_compliance` | depository / RTA certificates (Reg 74(5)), lost or duplicate share certificates, investor complaints statement | one sentence | yes |
| `other` | anything else | what the filing discloses, with numbers and dates |  |

Routine kinds get a one-sentence summary and no facts.

## Fact types

Every fact has: type, one-sentence summary, event date, period, amount (as printed, with unit and currency; stored in Rs crore), pct, shares, price (Rs), counterparty, person, role, direction, details, and a verbatim quote. It is **verified** only when the quote is in the filing and every number given is in the quote.

| Type | What it is | Details |
|---|---|---|
| `order_won` | new order, contract, LoA/LoI, work order (counterparty = customer; amount = value) | `scope` (what is to be supplied or built), `execution_by` (completion date or period), `market` (domestic / export), `segment` (business segment), `repeat` (yes if announced before) |
| `order_book` | order book / unexecuted orders as of a date (period = as-of date; amount = total) | `by_segment` (segment: amount, ...), `inflow_period` (order inflow in the period, with period) |
| `l1_bid` | declared lowest bidder (L1), not yet awarded (counterparty = client; amount = bid value) | `tender` (tender name or number) |
| `results` | reported numbers for one period (period; amount = revenue) | `basis` (consolidated / standalone), `ebitda_cr`, `pat_cr` (profit after tax), `eps`, `revenue_yoy_pct`, `pat_yoy_pct`, `exceptional` (exceptional items) |
| `guidance` | management forecast (period = horizon) | `metric` (revenue / margin / order inflow / capex ...), `target` (the stated number or range) |
| `preferential_issue` | shares or CCPS on a preferential basis (counterparty = allottee; shares, price_inr, amount) | `allottee_category` (promoter / promoter group / investor / strategic), `instrument` (equity / CCPS), `stage` (proposed / approved / allotted), `lock_in`, `purpose` |
| `warrants` | convertible warrants (counterparty = allottee; shares, price_inr, amount) | `allottee_category`, `upfront_pct` (share paid upfront), `stage` (proposed / approved / allotted / converted), `conversion_by` |
| `public_issue` | QIP, IPO, FPO, OFS or rights issue (amount = size; price_inr) | `route` (QIP / IPO / OFS / rights), `stage`, `floor_price`, `discount_pct` |
| `debt_raise` | loans, NCDs, CP, or other borrowing raised or repaid (amount) | `instrument`, `rate` (interest / coupon), `tenor`, `lender`, `action` (raised / repaid) |
| `esop` | employee options granted, vested, exercised or allotted (shares, price_inr = exercise price) | `scheme`, `action` (grant / vest / allot) |
| `buyback` | share buyback (amount = size; price_inr; shares) | `route` (tender / open market), `stage` |
| `dividend` | dividend (price_inr = per share) | `kind` (interim / final / special), `record_date` |
| `bonus_split` | bonus issue or split | `ratio`, `record_date` |
| `promoter_stake` | promoter / founder holding change (person; shares; pct = holding after; price_inr) | `before_pct`, `mode` (market / off-market / gift / inter-se / conversion / creeping), `action` (buy / sell / transfer) |
| `pledge` | promoter pledge (person; shares; pct = share of holding pledged) | `action` (create / release / invoke), `lender`, `purpose` |
| `insider_trade` | director / KMP / designated person trade (person, role; shares; amount; direction) | `mode` |
| `major_holder` | non-promoter crossing or changing a disclosed holding (counterparty = holder; pct = after) | `before_pct`, `mode` |
| `shareholding_pattern` | quarterly holding mix (period = quarter end; pct = promoter %) | `fii_pct`, `dii_pct`, `public_pct`, `pledged_pct` |
| `investment` | money put into another entity or a stake taken (counterparty = investee; amount; pct) | `instrument`, `purpose`, `relation` (subsidiary / associate / third party) |
| `acquisition` | buying a company, business or asset (counterparty = target; amount = consideration; pct) | `valuation`, `funding` (cash / shares / debt), `stage` (signed / completed), `country` |
| `divestment` | selling a company, business, stake or asset (counterparty = buyer; amount; pct) | `asset`, `stage` |
| `subsidiary_jv` | subsidiary / JV / associate formed, struck off or merged (counterparty = entity) | `partner`, `country`, `action` (formed / closed / merged) |
| `scheme` | merger, demerger, amalgamation or other scheme of arrangement | `entities`, `ratio` (share swap ratio), `stage` (board / NCLT / effective) |
| `capex` | capacity expansion, new plant, capex approved or commissioned (amount) | `project`, `capacity`, `location`, `timeline`, `funding`, `stage` |
| `disruption` | fire, accident, shutdown, strike, force majeure | `site`, `impact`, `duration` |
| `partnership` | MoU, tie-up, licence, distribution or supply agreement without an order value (counterparty) | `scope` |
| `product_approval` | product launch, certification, licence or regulatory approval | `product`, `authority` |
| `credit_rating` | rating action (counterparty = agency; amount = rated facility) | `instrument`, `rating`, `previous`, `outlook`, `action` (upgrade / downgrade / reaffirm / assign / withdraw) |
| `director_joined` | director appointed or re-appointed (person, role) | `category` (independent / executive / non-executive / nominee), `term` |
| `director_left` | director resigned, retired, ceased or term ended (person, role) | `category`, `reason` (the stated reason, quoted) |
| `kmp_joined` | CEO, CFO, CS or other senior manager appointed (person, role) |  |
| `kmp_left` | CEO, CFO, CS or other senior manager left (person, role) | `reason` |
| `auditor_change` | auditor appointed, resigned or rotated (counterparty = new auditor) | `auditor_type` (statutory / secretarial / internal / cost), `previous`, `reason` |
| `fund_utilisation` | use of money raised (amount = used so far) | `issue`, `raised_cr`, `deviation` (none or what changed) |
| `litigation` | legal case, arbitration, tax demand (counterparty; amount) | `forum`, `status`, `impact` (company's stated impact) |
| `regulatory_action` | regulator or exchange action, penalty, show-cause (counterparty = regulator; amount) | `action`, `status` |
| `related_party` | material related-party transaction (counterparty; amount) | `nature` |
| `clarification` | company's reply to a query or news report | `asked`, `answer` |
| `other_material` | any other fact an investor would care about |  |
