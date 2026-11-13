# Fundamentals from exchange XBRL filings

Written 2026-11-10 after a 97-filing sample (13 companies: 7 ordinary companies of all
sizes, 2 banks, 2 NBFCs, 2 insurers; June, September and March quarters; 2023-24 and 2026).
Goal (SAAS_PRD.md section 2.2): replace screener.in as our fundamentals source with numbers
built from the companies' own filings. **Scope: results from 2020 on** (operator, 2026-11-10);
nothing before 2019 is parsed. **Tolerance against screener.in: 2-5%** -- screener.in has its
own errors, so it is a check, not the truth.

## What the sample showed

**Sources.** Two NSE listings, both reached through `nse_request_gate`:
- `api/corporates-financial-results?index=equities&symbol=X&period=Quarterly`: XBRL from the
  June 2020 quarter to December 2024. Basis labels "Consolidated" / **"Non-Consolidated"**.
- `api/integrated-filing-results?index=equities&symbol=X&type=Integrated Filing- Financials`:
  2025 on. Basis labels "Consolidated" / "Standalone"; revisions appear as extra rows
  (`type_Sub`, `revised_Date`).
Insurers appear only in the integrated listing.

**Formats (taxonomies).** Old: `Ind-AS_entry_point` (companies), `in-bse-fin` (NBFCs),
`banking_entry_point` (banks), plus insurers. New: one `in-capmkt-ent` taxonomy for all,
versioned (2025-01-31, 2026-01-31). **No company used its own tags**: filers fill the
exchange's template, and their own line items arrive as numbered "other" members
(`OtherExpenses01..N`) with a text description.

**What each filing holds.**
- Every quarter: profit and loss for the quarter (`OneD`) and the year to date (`FourD`),
  segment results.
- September and March: also the balance sheet (`OneI`) and the cash-flow statement
  (year to date: `FourD`). June and December: profit and loss only.
- Old format: no prior-year columns. New format: also the prior year-end balance (`PY_I`).
- Values are in rupees; `decimals` is precision only. `LevelOfRounding` ("Crores") is what
  the company printed, not the unit of the value.

**Traps found.**
1. **Context periods lie.** In old filings the `FourD` context declares the quarter's dates
   while its facts say year-to-date. The period must come from the `DateOfStartOfReportingPeriod`
   / `DateOfEndOfReportingPeriod` facts inside each context, never from the context element.
2. **Missing contexts.** An HDFC Bank 2023 filing refers to `OneD` without defining it; a
   strict XBRL parser would drop the whole filing. Read facts by context id.
3. **Same name, different meaning per format.** `Income` is total income; banks report
   revenue as `InterestEarned`; insurers as premium tags; profit is `ProfitLossForPeriod`
   (companies, NBFCs), `ProfitLossAfterTaxesMinorityInterest...` (banks),
   `ProfitLossAfterTax...` (insurers). One mapping table per format family.
4. **Ratios as fractions** (bank CET1, NPA %) beside rupee amounts.
5. **Revisions** come as a second filing for the same quarter; keep both with publish times.
6. **Standalone vs consolidated.** Our stored screener.in quarters are standalone. Both bases
   are filed; we keep both and say which.

**Accuracy (standalone, like for like, 42 quarters).** Ordinary companies and NBFCs: revenue
within 0.2% in 37 of 42, all within 2.8%; profit within 1% in 40 of 42. Exceptions to look at:
Reliance revenue 1-3% (likely duties/taxes treatment), Muthoot Finance Sep-2026 profit 7%,
Safari Sep-2026 profit 2.5%. Bank revenue differs 10-25% only because the quick check used
total income instead of interest earned.

## The parser to build

1. **Fetch**: both listings per company; keep every filing (both bases, revisions) with its
   publish time; raw XBRL stored in S3 (`fundamentals/xbrl/...`). Runs on the Hetzner crawl
   pool as orchestrator tasks.
2. **Read**: every fact with its context id, the context's real period (from the reporting
   period facts), dimensions and their member descriptions, unit and decimals.
3. **Map** to our own line items per format family (company, NBFC, bank, insurer) -- one
   mapping table, versioned; a tag never seen before goes to a review list (Claude proposes
   the mapping, it is checked once and saved).
4. **Derive** quarter values: Q1 = YTD; later quarters = this YTD - previous YTD when only YTD
   is filed (cash flow); balance sheet at half-years.
5. **Check**: accounting identities (income - expenses = profit before tax; profit before tax
   - tax = profit; assets = equity + liabilities; the cash-flow sections sum to the change in
   cash), jumps against the company's own history (wrong scale), and screener.in within 2-5%.
   Failures are flagged, not dropped.
6. **Store** point-in-time: every value keeps the filing it came from and when it became public;
   a revision adds a version, never overwrites.

## Pilot: 50 companies, 2026-11-10

Run through the crawl fleet (7 Hetzner workers): 50 listings, 1,919 filings since 2020, all
parsed; 4 internal checks failed out of all identities tested. Compared with screener.in's
standalone quarters (582 quarters, 46 companies):

| | within 2% | within 5% |
|---|---|---|
| sales | 533 / 567 | 540 / 567 (95%) |
| profit | 519 / 582 | 554 / 582 (95%); 568 (98%) counting gaps under Rs 0.5 cr (screener.in rounds to whole crores) |

What the gaps were:
- **Duty inside revenue** (SULA excise, PREMIERPOL GST, 7-14% every quarter). Filed inside
  revenue from operations and itemised as an "other expenses" line. Mapping v2 adds
  `excise_duty` (the itemised lines described as excise or GST) and `revenue_net`; the
  screener.in comparison uses `revenue_net`. Fixed.
- **screener.in shows restated history, we show figures as filed.** RAYMOND (demergers
  2023-25), JKLAKSHMI, HONASA, JASH, RSYSTEMS, ADANIPORTS some quarters: our values equal the
  filing; screener.in carries the restated comparatives printed in later results. The XBRL
  files hold only the current period, so a restated series needs the next year's results
  (PDF) -- later work if needed.
- **One-off screener.in gaps**: AWHCL 2024-12 and 2025-09 (filing says Rs 8.5 cr, screener.in
  Rs 156 cr), 63MOONS 2023-24 sales (we include all operating revenue).

## Full run: all companies, 2026-11-12

3,546 listings, 86,003 fetches (12 Hetzner workers, about a day): 87,747 filings from 2,478
companies parsed; 175 files gone from NSE, 34 unreadable. Mapping v3 adds `regulatory_deferral`:
power companies add it after tax, and it was behind all 722 files failing `pat = pbt - tax`
(54 of 60 sampled; the rest failed in another period). After v3, 1 file fails a check.

Against screener.in (standalone quarters, 1,476 companies), counting a gap as fine when it is
within 5% or under Rs 0.5 cr (screener.in rounds):

| | quarters | fine |
|---|---|---|
| sales | 18,105 | 97.7% |
| profit | 18,191 | 98.1% |

Misses are spread over ~160 companies; the most frequent (HAPPSTMNDS, MASTEK, QUESS, SIEMENS,
RAYMOND, ABREL) have had demergers or restatements, the same as-filed vs restated split as the pilot.
