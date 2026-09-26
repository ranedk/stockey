# Frontend Completion Plan — promotion funnel + system health

Written 2026-08-31, after an audit of what stockey actually exposes versus what the
`screener/` Nuxt frontend actually renders.

Two unrelated bodies of work turned out to have the same shape: the backend was
built, tested and live, and nobody closed the loop to the UI. This plan covers both.

- **Part A — the promotion funnel.** Watchlist → confluence → L4 thesis works. The
  last step, *how much to buy*, and the check on *whether confluence predicts
  anything*, exist as API and are invisible.
- **Part B — system health.** The four tables that implement `CLAUDE.md`'s "no
  silent fallback" principle have no reader except a nightly log. The 2026-08-26..31
  Dhan outage stayed invisible for five days for exactly this reason.

Neither part needs new data collection. Every table and every endpoint below already
exists and is populated; this is a surfacing exercise, not a pipeline one.

## Status — ALL ITEMS SHIPPED 2026-08-31

Every item below is built, wired, and verified against the live stack (real DB, live
API, server-rendered pages), not just unit-tested.

| item | status | verification |
|---|---|---|
| A0 open L4 thesis | **done** | two theses seeded via the real API, used to verify A1/A2, then **deleted** — the calibration sample is back to 0, no fabricated outcome left behind |
| A1 L5 sizing panel | **done** | `equal_weight` (JINDRILL ₹200,000) and `adv_liquidity_cap` (ALPA ₹119,137) both bound live; 404/400 paths distinct; panel correctly absent on a company with no thesis |
| A2 calibration breakdown | **done** | null hit rate renders "too few to say (n=1)", never 0% — verified with a resolved thesis before deletion |
| A3 draft theses | **done** (surfaced, not deleted) | 75 real candidates on the watchlist index, labelled proposals-not-recommendations |
| B1 collectors | **done** | reconciliation splits 9 error rows into 4 failing / 2 frozen / 3 orphaned |
| B2 issues + fallbacks | **done** | 49 open identity issues by type; 33 error / 34 warn in 24h; read-only path asserted by test |
| B3 scheduler health | **done** | `is_cron_running.sh --json` is the single source of truth; endpoint shells out to it |
| B4 coverage trend | **done** | 20 tables, 1 not-ok, staleness sparklines from 16 nightly runs |

New endpoints: `/api/collectors`, `/api/scheduler-health`, `/api/platform-issues`,
`/api/coverage-report`. New components: `PositionSizingPanel`, `CalibrationBreakdown`,
`CollectorsPanel`, `SchedulerPanel`, `PlatformIssuesPanel`, `CoveragePanel`,
`DraftThesesPanel`. 1,325 tests pass.

**Two things found while building, worth keeping:**

1. **`summarize_fallback_events` merges DB rows with a local spool file**, so its
   `warn_count` (34) is not the DB's warn count (9) and is NOT
   `active_count - error_count` in any load-bearing sense. Deriving it happened to
   match; the panel now reads `warn_count` directly. Do not re-derive it.
2. **The orphan problem is larger than the audit found.** Reconciling against the live
   registry showed **23** orphaned rows, not 3 — most of the archived advisory stack
   still has state in `advisory_sync_state`. Nothing prunes it. The page separates them
   correctly, but a one-off cleanup is still worth doing (see non-goals).

## Audit result (the evidence this plan is built on)

Every fundamentals API route has a frontend consumer **except** `/api/drafts` (orphan)
and `/api/health` (infra, correctly not in the UI). The gaps are not missing
endpoints — they are endpoints and columns nothing renders.

| capability | backend | frontend |
|---|---|---|
| L3 alert → watchlist | live | ✅ `/watchlist`, 73 active |
| Confluence score (PRD §12 #6) | live | ✅ badge + min-confluence filter |
| L4 thesis create/resolve | live | ✅ `PortfolioAddForm` on `watchlist/[id]` |
| **L5 sizing** (PRD §12 #8) | `GET /api/watchlist/{id}/sizing` | ❌ **zero consumers** |
| **Confluence calibration** (PRD §12 #7) | in `/api/portfolio/scoring` | ❌ **dropped by `portfolio.vue`** |
| Draft theses | `GET /api/drafts` | ❌ orphan |
| Collector status | `advisory_sync_state`, 47 rows, 9 `error` (**3 real**, 3 phantom, 1 frozen) | ❌ none |
| Identity issues | `advisory_identity_issues`, **49 open** | ❌ none |
| Fallback telemetry | `advisory_fallback_events`, 19.8k rows | ❌ none |
| Coverage trend | `data_coverage_report`, 392 rows | ❌ none |
| Scheduler health | `scripts/is_cron_running.sh` | ❌ CLI only |
| Data completeness | `scripts/data_completeness.py` | ✅ `/data-health` |

To be fair to the PRD: §12 #6 explicitly claimed the watchlist page and delivered it.
#7 and #8 only ever claimed the module and the endpoint — they were backend-scoped by
design, not overclaimed. The loop was simply never closed.

## Part A — complete the promotion funnel

### A0. Unblock verification: there are zero L4 theses — DO THIS FIRST

`/api/portfolio/scoring` currently returns `total_theses: 0`, all three breakdowns
empty, and `/api/watchlist/{id}/sizing` would 404 for **every** company today.

This is a hard blocker, not a footnote: A1 and A2 cannot be verified end-to-end
against real data without at least one open thesis, and shipping them unexercised is
precisely how §12 #6 shipped two `UndefinedTable`/`UndefinedColumn` bugs that only a
live run caught (unit tests mock `sql_to_df`, so they cannot catch this class).

Options, in preference order:

1. Create one real thesis through the existing UI on a company the operator actually
   believes in. Best — it exercises the true path and leaves real data behind.
2. Seed a throwaway thesis against a liquid name, verify, then resolve it. Acceptable
   but pollutes `compute_quarterly_scoring()`'s sample; resolve honestly or delete.

Do not stub the frontend against a fabricated payload — that hides exactly the
integration failures this step exists to surface.

### A1. L5 sizing panel — `watchlist/[id].vue`

**The gap.** The detail page already computes `openThesis`, which is the exact
precondition the endpoint needs, renders the thesis, and offers to resolve it — then
never asks how much to buy. The funnel tells you *what* and *why*, and stops one call
short of *how much*.

**Contract.** `GET /api/watchlist/{company_master_id}/sizing` requires
`total_capital_rs` and `target_position_count` as query params. Both are deliberately
required with no server-side default (`app.py`'s own comment: the API must not
"silently assume a sleeve size or position count on the caller's behalf"), so the UI
has to collect them. Returns:

```json
{ "equal_weight_capital_rs": 0.0, "adv_cap_rs": 0.0,
  "recommended_size_rs": 0.0, "binding_constraint": "equal_weight" }
```

`binding_constraint` is one of `equal_weight`, `adv_liquidity_cap`,
`equal_weight_no_adv_data`.

**Build.**

- Render the panel only when `openThesis` is non-null — mirror the endpoint's own
  404-without-a-thesis rule in the UI rather than showing a dead form.
- Two inputs (capital, target position count). Persist them in `localStorage` so the
  operator is not retyping their sleeve size on every company; never send a default
  the user did not choose.
- Show all three numbers, not just `recommended_size_rs`. The whole point of the
  calculator is that the operator can see *which constraint bound* — an ADV-capped
  size on a thin name is a materially different message from an equal-weight one.
  Make `binding_constraint` the headline, not a footnote.
- Handle `equal_weight_no_adv_data` explicitly ("no ADV data — size is uncapped, treat
  with caution"), not as a silent equal-weight result.
- Handle 400 (`target_position_count <= 0`) and 404 distinctly from a network error.

**Label it a calculator, not a recommendation.** `l5_sizing.py` and the route comment
are both emphatic that this is arithmetic downstream of a human gate, not a decision.
The UI copy must not turn "recommended_size_rs" into an instruction to buy.

**Verify.** With a real open thesis from A0: a liquid large-cap should bind on
`equal_weight`; a thin small-cap at the same capital should bind on
`adv_liquidity_cap`. If both return `equal_weight`, the ADV path is untested — pick a
thinner name until you see the cap bind.

### A2. Confluence calibration — `portfolio.vue`

**The gap.** `compute_quarterly_scoring()` returns `hit_rate_by_origin_tag`,
`hit_rate_by_trigger_type` and `hit_rate_by_confluence_count`. All three are typed in
`app/types/api.ts` with explanatory comments. `portfolio.vue` renders only headline
Scoring / Open / Resolved and drops every one of them.

So the watchlist lets you filter on a confluence score whose predictive value the UI
cannot show. That inverts the intent of §12, which exists precisely so confidence
comes "from something checkable after the fact".

**Build.**

- A breakdown section with three small tables, `hit_rate_by_confluence_count` first —
  it is the one that answers the design question.
- Each entry is `{"hit_rate": pct-or-None, "count": n}`. **`hit_rate: null` means the
  sample is below `MIN_SAMPLE_SIZE_FOR_BREAKDOWN` (5) and must render as "too few to
  say (n=3)" — never as 0%, never as a blank cell.** The backend deliberately returns
  `None` rather than a misleading percentage; the UI must not undo that.
- Show `count` always, even when `hit_rate` is null — the sample size is the honest
  signal while the sample is thin.
- With `total_theses: 0` the page must read as "not enough history yet", not as an
  empty or broken panel.

**Verify.** Assert the null-hit-rate rendering explicitly. This is the single most
likely place to accidentally ship a fake number.

### A3. Draft theses — close the orphan

`GET /api/drafts` returns every CANDIDATE-ONLY draft thesis for an active watchlist
company — per its docstring, "the same data the digest email already sends,
previously not reachable via the API at all". It is now reachable and still unread.

Drafts are the *entry* to this funnel, so surfacing them completes the loop at both
ends. Smallest useful version: a count + list on the watchlist index, linking through
to each company's detail page. Decide explicitly whether this earns a page or a
section — if the answer is neither, delete the endpoint rather than leave it orphaned.

## Part B — system health visibility

Extend the existing `/data-health` page rather than adding new top-level pages. It
already establishes the pattern: one shared implementation behind both the cron gate
and the API, so the dashboard and the gate cannot disagree.

**Boundary note.** These are data-platform tables, not fundamentals. They ride the
fundamentals API only because it is stockey's sole HTTP surface. This is monitoring of
stockey's own data, not research/signals/LLM, so it stays inside the pure-TA boundary
(`docs/DATA_INVENTORY.md`) — the same reasoning already recorded for
`/api/data-health` in `CLAUDE.md`.

### B1. Collectors — `advisory_sync_state` (highest value)

47 rows, one per collector: `status`, `last_success_at`, `error_text`, `updated_at`.
Nine currently read `error`, and nothing in the UI shows it. But the raw count
**overstates the problem**, and that distinction is the whole design of this section:

| source | module on disk? | verdict |
|---|---|---|
| `download_runner:data.rbi.download_bank_rates` | yes | **real failure** — live pure-TA collector |
| `download_runner:data.rbi.download_fbil_gsec` | yes | **real failure** — `partial_failed` |
| `download_runner:data.nseindia.offmarket` | yes | **real** — the §12 #1 caveat: its NSE navigation fix was never live-smoke-tested |
| `data.nseindia.recent_events` (×2 rows) | yes | **frozen by design** — deliberately unscheduled (`docs/DATA_INVENTORY.md` BORDERLINE) |
| `continuous_watch:announcements` | **no** | phantom — advisory stack archived |
| `download_runner:data.mospi.cpi` | **no** | phantom — removed in the pure-TA cut (legacy scope, retained here only as an example of stale state) |
| `download_runner:data.sharpelydata.sharpely_data` | **no** | phantom — module renamed; the registry now uses `data.sharpelydata.scrip_master` |

So: **3 real failures, 1 frozen-by-design, 3 phantom rows** for modules that no longer
exist. `advisory_sync_state` is append/update-only and nothing ever prunes a source
that was removed or renamed, so those three will sit in `error` forever.

`rf = repo/T-bill from rbi_bank_rates` feeds systrader, so the RBI pair is not
cosmetic.

**This is the actual design requirement, not a detail.** A collectors page that shows
9 red rows when 3 are real and 3 can never go green trains the operator to ignore it —
the precise alert-fatigue failure this whole plan exists to prevent. So:

- Reconcile `advisory_sync_state` against the live registry
  (`data/download_runner.py`'s `DOWNLOADER_STEPS`/`PARSER_STEPS`) and render three
  distinct states: **failing**, **frozen by design**, **stale/orphaned row**.
- Prune or tombstone the orphans as part of this work — a one-off cleanup plus
  whatever keeps it true (either the reconcile at read time, or a delete when a source
  leaves the registry). Prefer reconcile-at-read: it cannot drift.
- Show error text verbatim; never summarize a collector failure into a status word.

This section alone would have shown the Dhan outage on day one.

New endpoint: `GET /api/collectors`, failures first.

### B2. Issues and fallback telemetry

- `advisory_identity_issues` — 73 rows, **49 open** (40 `dhan_security_id_missing`,
  9 `dhan_ohlcv_history_unavailable`). This is the same population as the 199/273
  uncollected symbols the completeness check reports as a bare number; surfacing it
  turns "273 symbols missing" into a list with reasons.
- `advisory_fallback_events` — 19,806 rows, **32 errors + 9 warns in the last 24h**.
  Show a 24h severity rollup grouped by `fallback_type`, not a raw feed.

`scripts/issue_digest.py` already computes both rollups for its nightly log. Reuse its
logic rather than writing a second definition — the same one-implementation discipline
as `run_all_checks()`.

### B3. Scheduler and host health

`scripts/is_cron_running.sh` proves go-crond is *firing*, not merely alive — the
distinction that cost five days of silence in August and 2.5 hours in a separate
`builder.py` incident. It is CLI-only, so the one signal whose absence caused the
outage still cannot be seen from a browser.

Expose its checks as `GET /api/scheduler-health`: go-crond alive + firing, parent-is-
init, stale job locks, crontab drift, Chrome CDP up.

**Design constraint.** The shell script must stay the source of truth for the *logic*,
or the two will drift — which is the failure this whole plan is about. Either shell out
to it with `--quiet` and parse the exit code plus a `--json` mode added for the
purpose, or port the checks to Python and have the script call *that*. Do not
reimplement the checks twice.

**Caveat worth stating:** an API served by a process the scheduler manages cannot
fully report on the scheduler. If go-crond dies it may take the API's restarter with
it. This section improves visibility; it does not replace the OS-crontab watchdog.

### B4. Coverage trend — `data_coverage_report`

392 rows of per-table coverage/staleness history, 20 tables on the latest run with
**1 in `error`**. The nightly job writes `docs/DATA_COVERAGE.md` and this table; only
the markdown is ever read. A small table view plus a sparkline of `staleness_days` per
table is the cheapest way to see a feed degrading *before* it fails outright.

## Sequencing

Part A first: it completes a capability the operator actively uses, and A0 has to
happen before anything in A can be verified honestly.

| # | item | depends on | rough size |
|---|---|---|---|
| 1 | **A0** create/seed an open L4 thesis | — | operator decision |
| 2 | **A1** L5 sizing panel | A0 | 1 endpoint consumer + panel |
| 3 | **A2** calibration breakdown | A0 | page section, no backend |
| 4 | **B1** collectors | — | 1 query + endpoint + section |
| 5 | **B3** scheduler health | B1 (shared page section) | endpoint + `--json` on the script |
| 6 | **B2** issues + fallback rollup | B1 | 2 queries, reuse `issue_digest` |
| 7 | **A3** drafts — surface or delete | — | small, or a deletion |
| 8 | **B4** coverage trend | B1 | 1 query + table |

B1 is deliberately early despite Part A's priority: three live collectors are failing
right now (both RBI feeds, which supply systrader's risk-free rate, plus `offmarket`)
and nobody can see it without a psql prompt.

## Cross-cutting rules for this work

1. **Live-verify every read against the real DB, not just unit tests.** Every test in
   `tests/test_data_platform.py` mocks `sql_to_df`, so an entire class of failure is
   invisible to them. §12 #6 shipped two production 500s this way.
2. **New table or new column read by a different module needs a defensive
   `_ensure_*()` in the READER.** `upsert_to_db`'s `CREATE TABLE IF NOT EXISTS` /
   `ADD COLUMN IF NOT EXISTS` only fire on a *write*, so a reader that runs first gets
   `UndefinedTable`/`UndefinedColumn`. This has now bitten three times.
3. **Never render a null metric as a number.** The backend returns `None` for a
   below-threshold hit rate on purpose. A UI that shows 0% destroys that care.
4. **The fundamentals API does not hot-reload.** `all_fundamentals_api.sh` no-ops when
   the port is healthy, so it will keep serving old code after a change — kill the
   listener (`ss -tlnp | grep :8000`) and relaunch. This cost a debugging cycle adding
   `/api/data-health`.
5. **Keep one implementation per question.** `run_all_checks()` is shared by the cron
   gate and the API for exactly this reason; B2 and B3 must follow it.

## Explicit non-goals

- No new collectors, tables, or scheduled jobs. Everything here reads what exists.
- No LLM anywhere in the promotion path — §1/§3.3 and §12's whole rationale forbid a
  capital decision synthesized by a model. L5 stays arithmetic behind a human gate.
- Not fixing the 9 erroring collectors as part of this work. Surfacing them is the
  goal; each root cause is its own task (`offmarket`'s untested NSE nav fix, the RBI
  `source_unavailable` pair, `sharpely_data`).
- Not fixing the 199/273 structurally-uncollectable symbols (ETFs, rights
  entitlements, delisted names). Separate backlog: needs a `company_master` refresh
  and a Dhan security-id re-map.
