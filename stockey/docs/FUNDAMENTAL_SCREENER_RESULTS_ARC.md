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

Nothing queued yet. Add the next set of work here when it's scoped.

## Explicitly not now

- Annual report collection/OCR + per-company custom-metric discovery -- real
  idea, but scoped out given 0/43 real filings (checked live) have anything to
  track it against quarterly; revisit as a separate *annual*-cadence build if the
  narrative/triage layer needs richer per-company context later.
- More rating agencies (Acuité etc.) -- self-clearing Todos board, zero real
  occurrences seen live so far.
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
