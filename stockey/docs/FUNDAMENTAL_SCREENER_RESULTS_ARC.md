# Fundamental Screener — Current Development Arc

Living backlog, not a spec — update in place as items ship (delete them), don't
accumulate a done-list here. `fundamental_basic_goal.md`/`docs/FUNDAMENTAL_SCREENER_PRD.md`
stay the stable spec; this tracks what's actually next.

## Where we are

L1, L2, and all four of `fundamental_basic_goal.md`'s original L3 triggers are
built (see git history for the results-trigger arc's own detail — bug fixes to
`classify_announcement`, `RESULTS_SCHEMA`'s qoq/yoy split, Python-side growth/
margin computation, the crawl-state table). Two more things shipped 2026-08-13
after an end-to-end pipeline audit:

- **auditor_change/related_party_transaction are now real L3 triggers**, not just
  an L1 pre-filter gate — they were fully collected+extracted (who/what/when/
  amount) but never became an alert for an already-watchlisted company, a real gap
  found auditing the pipeline. Also got their own `signal_pointers.py` types, and
  `exceptional_items_rs_lakh` (extracted since `RESULTS_SCHEMA` was built, never
  read) now caveats PAT-driven growth reasoning.
- **Watchlist exit signals** (`fundamentals/screens/watchlist_exit.py`) — the
  watchlist was purely additive, no company ever left it no matter how stale.
  Three soft-status conditions (never deletes, full history stays queryable):
  `invalidated` (a later alert directly contradicts the trigger_type that got the
  company watchlisted), `price_flagged` (price moved >=50%/<=-30% since
  first_seen_price), `stale` (the LLM's own `suggested_watch_until` passed with
  nothing new since — this field existed and was computed the whole time, just
  never acted on). `get_watchlist()`/the daily digest default to `status=active`
  only; nothing is hidden permanently, `?status=all` or a specific status shows
  everything. Frontend has status tabs on the watchlist list + a badge on the
  detail page.

859 tests passing. All of the above live-verified against the real DB/API, not
just unit tests.

Design principles from this arc, worth carrying into whatever's next:

- Two independent data streams (screener.in cross-sectional, OCR'd filing data) --
  don't force them to reconcile; log divergence as quality telemetry once
  screener.in catches up, don't merge.
- Compute in Python from LLM-extracted numbers, never let the LLM judge arithmetic
  -- versioned per the PRD's own "version every signal definition" rule.
- Confirm live before building -- every field/threshold/keyword in this arc was
  checked against real S3-stored OCR text or a live BSE/screener.in crawl first.
- Extracted data with no consumer is a real gap, not a someday-maybe -- both major
  items this round (auditor/RPT triggers, watchlist exit) were exactly that:
  data or a field that existed and was silently unused.

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
