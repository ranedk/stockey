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
