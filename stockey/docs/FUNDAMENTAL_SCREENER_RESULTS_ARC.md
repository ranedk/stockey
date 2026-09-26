# Fundamental Screener — Backlog

Living backlog, not a spec — update in place as items ship (delete them), don't
accumulate a done-list here. `fundamental_basic_goal.md`/`docs/FUNDAMENTAL_SCREENER_PRD.md`
stay the stable spec; this tracks what's actually next.

## Design principles

- Two independent data streams (screener.in cross-sectional, OCR'd filing data) --
  don't force them to reconcile; log divergence as quality telemetry once
  screener.in catches up, don't merge.
- Compute in Python from LLM-extracted numbers, never let the LLM judge arithmetic
  -- versioned per the PRD's own "version every signal definition" rule.
- Confirm live before building -- check every field/threshold/keyword against
  real S3-stored OCR text or a live BSE/screener.in crawl first.
- Extracted data with no consumer is a real gap, not a someday-maybe.

## Next up

Ranked by what actually blocks knowing if this system works, per a
2026-08-15 comprehensive audit (doc accuracy + storage correctness +
manipulation correctness + pipeline data-utilization + a strategic gap
review, five independent passes):

1. ~~**`fundamentals_l4_thesis` has zero rows, ever.**~~ ✅ **RESOLVED 2026-09-04,
   by deletion.** The operator removed the human forecast entirely ("only machine
   created portfolio and forecast"), so the register, its create/resolve endpoints
   and its frontend forms are gone. The blocker was real and this is the answer to
   it: a gate nobody ever passed through was not discipline waiting to happen, it
   was a design that could not run unattended. Forecasts now live on
   `fundamentals_portfolio_position`, are written by the entry adjudicator, and are
   resolved nightly by `portfolio_resolution.py` — mechanically where the prediction
   reduces to an L2 comparison, judged otherwise, with the two hit rates always
   reported separately so a model cannot flatter its own record. See
   `docs/PORTFOLIO_RULESET_PRD.md`. **The `POST /api/portfolio` endpoint referenced
   below no longer exists.**

   Original text, for the record: The human-committed,
   falsifiable, dated prediction is the only gate capital is supposed to
   pass through (`fundamental_basic_goal.md` §1/§4) and the only input to
   `compute_quarterly_scoring()` (forecast hit rate, `thesis_wrong` vs
   `thesis_right_market_hasnt_paid` attribution — the system's actual
   measure of whether it's any good). The UI and API already work
   (`screener/app/pages/portfolio.vue`, `POST /api/portfolio`); this is a
   discipline gap, not an engineering one. Until it's used on some of the
   38 current watchlist companies, there is no way to know whether any of
   this system's 64 alerts or `rating_confirms_deleveraging`/
  `results_confirm_turnaround` corroborations (fired 5 times, ever) are
   actually good calls. **Start here.**
2. **Structured-extraction throughput is the real bottleneck, not
   collection.** 1,175 raw events collected, but only ~53 have completed
   structured extraction (7/620 pit_sast, 43/388 results, 1/59
   rating_action, 0/18 auditor_change, 0/2 RPT). `nse_pit.py` — the
   structured NSE insider-trading collector — has contributed **zero** live
   rows despite being scheduled; all 620 `pit_sast` events today come from
   BSE's text-only detection (no transaction type/quantity). This is why
   the auditor-change/RPT/insider-trade L3 triggers have functionally never
   fired: not wrong logic, starved inputs.
3. **BSE-only companies (10/192 in one L1 run) get filings coverage but
   zero price/technicals/market-cap.** Already flagged below as a real,
   acknowledged gap (not scoped out by mistake) — worth prioritizing sooner
   given it's the one item in this doc's own "Explicitly not now" section
   that isn't claimed to be fine to leave.
4. **`llm_triage.py`'s volume-ratio self-inclusion bias — FIXED 2026-08-15.**
   Was averaging the event day into its own baseline, understating spikes
   up to ~10x (confirmed live: nse:ONWARDTEC stored 18.1x vs a true 180.9x).
5. **Unguarded LLM-response field access — FIXED 2026-08-15.**
   `investor_classification.py`/`watch_summary.py` indexed
   `judgment["tier"]`/`summary["narrative"]` outside their own try/except;
   a non-conforming LLM response could abort the entire remaining batch
   with no fallback-telemetry record. Now handled the same way every other
   error from those calls already was.
6. **`events_store.py`'s cross-source dedup key is looser than the PRD's
   own spec** — matches on `(isin, filing_type, disclosure_date)`, dropping
   `quantity` from the documented key (§7). Confirmed live: 60 groups of
   `pit_sast` rows already share the narrower key (e.g. 4 distinct
   Regulation 29 disclosures for two different named insiders, same
   isin/date) — once `nse_pit.py` starts contributing real rows (see #2),
   a structured quantity will attach to whichever of those rows happens to
   be oldest, not the one it actually corresponds to. Not fixed yet —
   changing the dedup key or merge logic on a live, actively-read table
   needs more care than a same-session patch.
7. **`ocr_pipeline.py` has no output-quality gate.** A blank/near-empty OCR
   result (an expected, documented return value per
   `utils/ocr/llm_ocr.py`'s own prompt) is marked `ocr_status="done"`
   exactly like a real extraction, contradicting the PRD §6 requirement
   that a failed OCR "never silently drops." `structured_extraction.py`
   then confidently produces an all-null structured row from near-empty
   text, indistinguishable from a genuinely uninteresting filing. No live
   0-char `done` row exists yet — a real, currently-latent risk. Needs a
   threshold decision (what counts as "too little text"), not a blind fix.
8. **`dhan_ohlcv_daily` has confirmed silent mid-series price-scale
   discontinuities** for at least 2 symbols (GOLDIAM, VMARCIND — fake
   ~25-83% single-day "crashes" where `has_recent_adjustment()` didn't
   trigger a full re-fetch before Dhan's own re-adjustment landed
   mid-series). This is the documented **fallback** series
   (`advisory_adjusted_ohlcv_daily`, built from NSE bhavcopy +
   `price_adjustment.py`, is unaffected and remains the PRIMARY series) —
   lower urgency than it would be otherwise, but the 2 known-bad symbols'
   history should be corrected, and the detection window needs
   strengthening to catch future occurrences.
9. **`data/nseindia/indices_parser.py` doesn't case-normalize
   `index_name`** — years of index history are silently split across case
   variants that are the same index (confirmed live: "NIFTY Midcap 100" vs
   "Nifty Midcap 100", similarly for Midcap 50/Smallcap 100/TR series).
   `docs/DATA_CONTRACT.md` documents mixed-case display names as
   intentional ("Nifty 50"), so this isn't a blind uppercase fix — it needs
   a reconciliation decision (which casing is canonical per index, and
   whether/how to merge the already-split historical rows) before touching
   the writer.
10. **`security_history.py`/`security_dimension.py` aren't scheduled
    anywhere** — not in `download_runner.py`'s STEPS, not in the crontab.
    `dim_security_history.company_master_id` is 0.86% populated today vs.
    ~61% if rebuilt with current code — a real, currently-realized
    staleness gap for anyone joining on that column. Their
    `build_dim_security()` merge bug (`price_row_count` silently splitting
    into `price_row_count_x`/`_y`) was fixed 2026-08-15 regardless, since
    it's a real bug whenever this does run.
11. Minor, confirmed, not yet fixed: `fbil_gsec_par` carries 37,818 legacy
    all-NULL-tenor rows (2019-02-13→2023-02-10) from before the current
    `dropna` guard was added — historical junk, safe to delete, not
    growing. `data/dhanlive/ohlcv_pull.py --help`'s own documented
    `--from-datetime` example crashes against the CLI's actual (date-only)
    parser. `data/nseindia/earnings_events.py` stores a naive IST
    timestamp mislabeled as UTC (module is frozen/unscheduled, so inert
    today).

## Explicitly not now

- Annual report collection/OCR + per-company custom-metric discovery -- real
  idea, but scoped out given 0/43 real filings (checked live) have anything to
  track it against quarterly; revisit as a separate *annual*-cadence build if the
  narrative/triage layer needs richer per-company context later.
- More rating agencies (Acuité, Brickwork, Infomerics) -- still a
  self-clearing Todos board with zero real occurrences; re-confirmed live
  2026-08-15 (a prior pass this session briefly misread this as having
  expired -- it hasn't). The 43 `unsupported_agency` rows are entirely
  `agency_name = "unnamed"`: generic BSE boilerplate announcements
  ("Please refer attached file", "Intimation of Credit Rating") with no
  agency name in the announcement text at all, and none have gone through
  OCR/structured extraction yet (all have a real attachment). No new
  agency-specific scraper would help here -- `structured_extraction.py`'s
  `RATING_ACTION_SCHEMA` already extracts `rating_agency` from the OCR'd
  PDF text itself, and the OCR/extraction batch-size increase (2026-08-15,
  see below) is what will actually clear this backlog.
- Watchlist-exit reactivation logic (a flagged company whose situation later
  resolves isn't auto-reactivated) -- noted as a possible follow-up in
  `watchlist_exit.py`'s own docstring, not built.
- `_mark_crawled` (L2 crawl-state) still resets to the full ~75-day interval even
  right after a pull-forward -- one prompt re-check, not a sustained retry loop.
- `classify_announcement` doesn't catch the `"Revision of outcome"` BSE
  subcategory (re-submitted/corrected results) -- seen once live, low volume.
- BSE-exclusive companies (no NSE listing -- confirmed live: 10 of 192 in one
  L1 run, via `map_company_master_ids_nse_or_bse`) get full event coverage
  (results, PIT/SAST, rating actions, capital raises, auditor changes, RPTs --
  `bse_announcements.py` crawls BSE directly by scrip code, no NSE dependency,
  verified live) but zero price/technicals/market-cap (`advisory_adjusted_
  ohlcv_daily` has no BSE-only rows -- no BSE bhavcopy-equivalent collector
  exists) and no structured corporate-action detection (NSE's CA feed doesn't
  cover them; nothing extracts split/bonus/dividend data from BSE
  announcement text either). Degrades gracefully -- a visible fallback event
  in `technicals.py`, not a crash -- these companies just carry a
  filings-only profile. A real gap, not scoped out by mistake; a BSE
  bhavcopy-equivalent collector would close it but is real new scope (new
  source, new rate-limit surface, new adjustment logic for a series that
  currently doesn't exist at all).
