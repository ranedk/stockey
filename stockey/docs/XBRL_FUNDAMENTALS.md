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
