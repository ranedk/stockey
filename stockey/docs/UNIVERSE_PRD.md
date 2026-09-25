# Universe PRD: a wider, cleaner stock universe (Layer 1 + Layer 2)

Status: **agreed with the operator 2026-09-25, not built.** Replaces the L1 screen
in `fundamentals/screens/l1_universe.py` (`L1_QUERY`, version 1). Evidence: systrader
`research/LEDGER.md` row 44 and `docs/FUNDAMENTAL_SCREENER_PRD.md` section 14.

## 1. Why

The current screen returns ~196 stocks and misses much of where money was made. Two
"neglect" filters (institutional holding < 20%, shareholders < 50,000) and a Rs 5,000 cr
ceiling keep out almost every stock the market is paying attention to, and its two
quality tests (cash conversion, debtor days) cannot be applied to lenders, so all banks
and NBFCs are silently excluded.

The operator's brief: keep pump-and-dump stocks out, keep fundamentally bad businesses
out, include everything else where money can be made. Widen first; tighten downstream
(signals and price reads), not at the gate.

## 2. Design

Two layers, both used only to EXCLUDE. Nothing is ranked here.

- **Layer 1** is general: the same rules for every stock. It removes stocks that are too
  small, too thinly traded, too new, or flagged by the exchange. It also tags each
  surviving stock with a group.
- **Layer 2** depends on the group: what counts as "fundamentally bad" differs by
  business model (debt is raw material for a bank and a warning sign for an IT firm).

Groups are by business model, not by industry (22 sectors x their own thresholds is too
many knobs to set honestly) and not by company size (size changes risk, not what good
fundamentals look like).

## 3. Layer 1 rules (all must pass)

| # | Rule | Source (already collected) |
|---|---|---|
| 1 | NSE main board only: series EQ. No SME (SM/ST), not trade-to-trade (BE) | `nseindia_ohlcv` |
| 2 | Not under serious NSE surveillance: no GSM stage, no long- or short-term ASM stage >= 2 | NSE daily surveillance-indicator file (`nseindia_surveillance_indicator`, from 2025-01) |
| 3 | Market cap >= Rs 300 cr | `nseindia_mcap` |
| 4 | Median daily traded value over the last 63 sessions >= Rs 50 L | `nseindia_ohlcv.total_value` |
| 5 | Listed >= 252 sessions | `nseindia_ohlcv` |
| ~~6~~ | ~~Promoter pledge < 50%~~ -- moved to Layer 2 (see below) | |

About 1,350 stocks pass rules 1, 3, 4 and 5 today (2026-09-24); rules 2 and 6 will
remove some.

Why these values (LEDGER 44 and its follow-up): of the 1,052 stocks that rose 5x or
more since 2019 and were tradeable at their peak, this Layer 1 would have held 95%
before the peak, typically with 6.2x of the rise still ahead. Rs 300 cr instead of
Rs 500 cr catches the biggest risers earlier (65% of their rise ahead vs 58%) for only
~29 extra stocks today; going lower adds nothing, because the traded-value rule already
removes most small companies. A 6-month listing age adds ~89 very new listings and
almost no recall, so it stays at a year. Every winner the rules missed was SME,
trade-to-trade or newly listed -- mostly the intended exclusions.

Not testable on history (none held): the surveillance list and pledge rules.

Rule 2 as decided (operator, 2026-09-25): ASM stage 1 stays IN. NSE puts a stock on it
mechanically after a large price move; excluding it removed 87 stocks including WELCORP
(Rs 71,000 cr), KIRLOSENG, TATACHEM and SANSERA -- the runners this universe is for. GSM
(any stage) and ASM stage 2+ remove 4. Dhan's `asm_gsm_flag` was the planned source but
marks only ~20 mostly-suspended names; NSE's own file is used instead.

Rule 6 moved to Layer 2 (operator, 2026-09-25): NSE's "> 50% encumbered" flag also counts
parent and PE non-disposal undertakings (it removed VEDL, HINDZINC, OBEROIRLTY, AFFLE,
EUREKAFORB), and screener.in's pledge % covers only the old ~190-name universe. Pledge
becomes a Layer 2 check once screener.in pledge is fetched for the whole universe.

## 4. Groups (assigned in Layer 1)

| Group | Rule |
|---|---|
| Lenders | Financial Services sector AND a lending business (bank, NBFC, housing finance) |
| Other financials | Financial Services, not lending (insurers, fund houses, brokers, exchanges) |
| Real estate and holding companies | Realty sector, or a pure investment/holding company |
| Asset-heavy | everything else with fixed assets (gross block) >= X times annual sales |
| Asset-light | everything else below X |

- Sector comes from `fundamentals_company_sector`. Asset intensity is measured from the
  company's own numbers, so new kinds of business land in the right group without a
  label (e.g. "Capital Goods" holds both plant-heavy makers and asset-light electronics
  assemblers).
- **X starts at 0.5** (fixed assets worth half a year's sales) and is set after looking
  at how stocks spread around it; shown to the operator before it is fixed.
- **Lenders vs other financials** needs NSE's finer industry label per stock (we store
  only the broad sector). One small new collector.

## 5. Layer 2 checks by group (lenient; exclusion only)

| Group | Excluded if |
|---|---|
| Lenders | gross/net NPA above a ceiling, capital adequacy below the regulatory minimum, return on assets <= 0, or negative net worth |
| Other financials | loss-making in 2 of the last 3 years, or negative net worth |
| Asset-heavy | operating cash flow negative in 2 of the last 3 years, debt/equity >= 2, or interest cover < 1.5 |
| Asset-light | operating cash flow negative in 2 of the last 3 years, loss-making in both of the last 2 years, or debt/equity >= 1 |
| Real estate and holding | debt/equity >= 1.5, or negative net worth |

Every group also: contingent liabilities >= 25% of net worth excludes. Debtor days is
no longer a gate; it becomes a scoring signal (rising debtor days counts against a
company). Exact lender thresholds and field availability on screener.in are confirmed
during the build.

Layer 2 cannot be tested on history until point-in-time snapshots exist (TODO C4). Before
switching over, report how many of today's 5x winners it would reject, and why.

## 6. Downstream changes that come with it

- **OCR runs as its own continuous job**, not a time-boxed step inside the nightly
  pipeline. The nightly run scores whatever has been extracted; a filing is scored when
  its OCR finishes (detection time is stored, so this is not hindsight). The first
  catch-up will be heavy and will shrink as the backlog clears. The universe size must
  never be limited by OCR throughput.
- **Coverage for the wider set** (was TODO B4): L2 crawl state, BSE/NSE announcement
  crawl, price history and technicals for every new stock, or a listed reason why not.
- **Cost that grows:** screener.in crawling, announcement volume, OCR, LLM triage.
  Watchlist narratives stay limited to watchlist stocks. Confluence and the portfolio's
  100-position cap are unaffected.
- **BSE-only companies leave the universe** (Layer 1 is NSE main board). Names already
  on the watchlist or held in the portfolio keep being tracked until they exit by the
  normal rules; nothing is force-closed.
- **Versioning:** a new `L1_QUERY_VERSION` (never edit the old query in place), so the
  old universe stays reconstructable.

## 7. Build order

1. Layer 1 rules 1-5 from our own tables; report the count. **Done 2026-09-25**
   (`fundamentals/screens/universe.py`, `python -m fundamentals.screens.universe`): rules 1, 3,
   4, 5 pass **1,399** of 2,664 EQ stocks (session 2026-09-24). Failing only one rule: market
   cap 151, traded value 173, listing age 139 (mostly genuine 2025 IPOs). Two fixes on the way:
   listing age counts sessions under the current ISIN OR symbol (a face-value split issues a
   new ISIN and made ADANIPOWER, NAZARA look newly listed; +34 stocks), and `parse_mcap`
   dropped the first row of every NSE market-cap file since 2024-02 (20MICRONS; fixed, today's
   file reloaded, history not backfilled).
2. Surveillance-list rule (Dhan flag) and pledge rule; report the count. **Done 2026-09-25:**
   collector `data/nseindia/surveillance_indicator.py` (nightly, after bhavcopy_parser;
   history backfilled from 2025-01). Layer 1 = **1,395** stocks (session 2026-09-24): rule 2
   removes 4 more; 83 passing stocks are on ASM stage 1, 14 carry the encumbrance flag.
3. NSE industry-label collector; assign groups; measure asset intensity and set X with
   the operator.
4. Layer 2 per group via screener.in; report survivors per group and the 5x winners it
   would reject.
5. Operator sign-off, then switch `l1_universe` to the new definition (new version).
6. Move OCR to its own job; extend coverage (section 6).

## 8. Decided / open

Decided (operator, 2026-09-25): traded-value floor Rs 50 L; size floor Rs 300 cr (moved
from 500 after the recall test); lenders included with their own checks; two layers,
groups by business model; OCR must not limit the universe.

Open: SME stays out for now (revisit with a stricter bar once C4 gives history); X for
the asset-heavy line; lender thresholds.
