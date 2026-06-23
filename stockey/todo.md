# Stockey Priority Todo

Updated: `2026-06-23`

This is the single active planning document for Stockey. Treat this file as the
priority-ordered backlog and current-state summary.

## Decision Rules

- Do not create BUY recommendations by weakening all rules globally.
- Do not use one broad regime label as the sole BUY/SELL gate.
- Prefer layered context: technical setup, market breadth, macro stress,
  sector/symbol leadership, source-family overlays, exact event-class
  reliability, and benchmark-excess evidence.
- LLMs can extract, summarize, critique, and propose watch/de-risk plans.
  Deterministic technical, risk, lifecycle, reason-contract, identity, and
  execution gates own trade authority.
- Context overlays, causal memory, event policy, and fast signal refresh are
  review/watch/de-risk evidence until full advisory validates them.
- Every fallback, stale source, degraded ingest, skipped symbol, source outage,
  parser failure, and auth issue must be visible in logs, sync state, fallback
  telemetry, Operator Health, or diagnostics.
- Keep point-in-time discipline. Do not backdate recommendations or use future
  data for features, labels, or portfolio assumptions.

## Primary Operator Commands

- Downloaders only: `./all_downloaders.sh`
- Queued downloader refresh: `./all_downloaders_queue.sh` then `./all_external_workers.sh`
- Parsers only: `./all_parsers.sh`
- Download + parse + compact/context refresh: `./complete_data.sh`
- Continuous monitoring: `./all_watchers.sh --loop`
- Context-to-entry repair: `./all_context_to_entry_repair.sh`
- Advisory only: `./all_advisory.sh`
- Fast advisory refresh: `./all_advisory.sh --fast`
- Research evidence refresh: `./all_research_evidence.sh`
- Optional ML/research training: `./all_ml.sh`
- Frontend/API: `./all_frontend.sh`
- Recommendation diagnostics: `python -m advisory.recommendation_diagnostics --format text`
- Operator Health: `python -m advisory.operator_health --skip-dhan`

Use `python -m advisory.pipeline` only for stage-level debugging and targeted
reruns.

## P0 - Must Fix Before Trusting New Recommendations

1. **Verify current no-BUY state after a fresh advisory run.**
   - Run `./all_context_to_entry_repair.sh`.
   - Run `./all_advisory.sh --date <latest-trading-day>`.
   - Run `python -m advisory.recommendation_diagnostics --asof-date <latest-trading-day> --format text`.
   - Expected result: diagnostics must say whether missing BUY rows are caused
     by candidate freshness, technical confirmation, context conversion,
     downstream consolidation, or hard macro/context gates.
   - Do not tune thresholds or context policy from stale candidate/action rows.

2. **Resolve BUY underparticipation using technical evidence.**
   - Current evidence points to technical confirmation blockers, not broad
     regime suppression: `technical_total_below_buy_min`,
     `participation_below_minimum`, `entry_trigger_missing`,
     `breakout_volume_below_min`, `pivot_not_cleared`, and weak close quality.
   - Run bounded calibration first:
     `python -m advisory.technical_threshold_calibration --dry-run --horizons 5 10 20 --max-configs 512 --progress-every 128`.
   - Review blocker and near-miss outcomes before changing any production
     threshold.
   - Candidate relaxations must be archetype-specific, reviewed, and justified
     by after-cost and benchmark-aware outcomes.

3. **Confirm technical threshold DB repair on live calibration.**
   - Re-run technical-threshold calibration after the new Timescale repair
     migration.
   - Confirm no unique-index error occurs for
     `advisory_technical_threshold_eval_summary`.
   - If it fails, repair stale unique indexes that omit `evaluated_at`.

4. **Fix any active script failures that block evidence generation.**
   - Re-check recent `logs/cron/*` after the next cron cycle.
   - Known classes to watch: Dhan auth/CDP, Dhan identity mapping, NSE source
     timeout, sync-state type mismatches, malformed JSON output from long
     scripts, and Timescale unique-key errors.
   - Failures should be recorded as source degradation, not hidden as no-data.

## P1 - Investment Decision Quality

1. **Replace single-regime thinking with layered context everywhere.**
   - Broad regime should remain diagnostic/annotation unless a narrow hard-risk
     gate is explicitly justified.
   - Recommendation diagnostics should continue to separate breadth, macro
     stress, sector/symbol leadership, technical confirmation, source-family
     overlays, and exact event-class reliability.
   - Run `python scripts/context_gate_policy_audit.py --fail-on-single-regime`
     after env/config changes.

2. **Make context-to-entry reliable.**
   - Fresh macro/news/announcement/bhavcopy/exchange/theme context should become
     durable `CONTEXT_OVERLAY_WATCH` rows.
   - Durable context-watch rows should carry technical confirmation plans and
     point-in-time technical prechecks.
   - Rows with hard OHLCV/technical-build issues must stay inactive/rejected
     audit rows until data is repaired.
   - Full advisory must be the only path that grants portfolio/risk/lifecycle
     authority.

3. **Build stronger sector/exact-class reliability.**
   - Source-family aggregate labels are not enough.
   - Use exact `context_class`, sector split, horizon, direction, and
     benchmark-excess evidence before allowing a source to influence watch or
     de-risk pressure.
   - Suppress harmful/no-lift, negative-after-cost, horizon-inconsistent, and
     benchmark-unattributed classes.

4. **Improve causal event memory as the core event layer.**
   - Continue compact symbol/sector memory over announcements, exchange events,
     bhavcopy, macro, and themes.
   - Evaluate memory classes against forward return and benchmark-excess return.
   - Design bounded deterministic consumers only for repeated helpful groups.
   - Explicitly suppress harmful/no-lift, benchmark-beta-only, and
     benchmark-unattributed classes.

5. **Keep LLM decisioning bounded but useful.**
   - LLMs should reduce Manual Review by classifying ambiguous evidence into
     `NO_ACTION`, review-only `WATCH`, or review-only `REDUCE_EXPOSURE_REVIEW`
     where possible.
   - LLM output must carry no-portfolio/no-broker authority unless deterministic
     gates later validate it.
   - Prompt/skill contracts should remain inspectable in backend config, not
     hidden in UI-only workflows.

6. **Clarify action authority and portfolio transition semantics.**
   - Action Queue must distinguish review-only rows from executable rows.
   - Recommendations page should show only the most recommended operator action
     per symbol: buy, sell, buy_50%, sell_50%, watch, hold, or no action.
   - Reset portfolio semantics should not create SELL recommendations for
     positions that no longer exist.
   - BUY-looking review-only signal-refresh rows should not appear as approved
     executable BUYs.

7. **Validate action-transition stability before promoting it.**
   - Continue action-transition evaluation against point-in-time forward returns
     and NIFTY benchmark-excess movement.
   - Keep unstable transition downgrades deterministic and conservative.
   - Do not promote transition policy from raw helpful outcomes that are only
     benchmark beta.

## P2 - Data, Evidence, And Source Reliability

1. **Keep announcement and document intelligence compact but complete.**
   - Continue suppressing routine filings unless stronger material keywords are
     present.
   - Ensure material filings become compact structured/unstructured evidence
     with source pointers and event class.
   - Surface unresolved company mappings, OCR failures, source fetch failures,
     document lookup failures, and ingest backlog in Operator Health.

2. **Use bhavcopy and exchange data more directly.**
   - Maintain compact accumulation, distribution, circuit risk, abnormal
     turnover, short/off-market, insider, block/bulk, and corporate-action
     evidence.
   - Feed these as queryable context to LLM/company-memory/event-policy paths.
   - Evaluate them by source family, event class, sector, and benchmark-excess
     outcomes before runtime influence.

3. **Harden corporate actions handling.**
   - Verify stock splits, bonus, symbol changes, and security master changes are
     handled causally in OHLCV, labels, and portfolio history.
   - Do not compare pre/post-split prices without adjustment or explicit event
     context.

4. **Maintain identity issue visibility.**
   - Dhan/security/company-master mapping failures should never silently drop
     context or advisory candidates.
   - Keep `advisory_identity_issues` and source-skip panels current.
   - Prefer current Dhan master fallback for review-only symbol validation when
     company master is stale, but keep that provenance visible.

5. **Preserve fallback telemetry quality.**
   - Run `python scripts/fallback_telemetry_coverage_report.py --fail-on-silent`
     after adding new source/API paths.
   - Source/API errors should record fallback telemetry or re-raise with clear
     operator context.

6. **Keep `.env.example`, docs, and cron current.**
   - Run `python scripts/env_example_audit.py --strict`.
   - Run `python scripts/docs_state_audit.py --strict`.
   - Run `python scripts/cron_preflight.py` before restarting `go-crond`.

## P3 - Research, Validation, And Promotion Discipline

1. **Run research evidence refresh regularly.**
   - Use `./all_research_evidence.sh` after post-close data catch-up.
   - Keep event-policy, causal-memory, signal-quality, adversarial-review,
     action-transition, context-watch, and negative-pressure evaluators current.

2. **Treat optional ML as research until proven.**
   - `./all_ml.sh` should not be part of live authority unless it repeatedly
     improves after-cost, benchmark-aware outcomes.
   - Fix noisy stdout / non-JSON child output if it breaks model training runner.
   - Keep model artifacts, training summaries, and promotion checks auditable.

3. **Strengthen false-discovery controls.**
   - Every threshold/config/source-family experiment should record configs,
     dates, horizons, costs, data windows, and results in research evidence or
     research ledger.
   - Avoid acting on one good-looking sample.
   - Prefer repeated windows, benchmark-excess checks, and negative controls.

4. **Reviewed config diffs remain manual.**
   - Technical threshold, signal-quality overlay, event-policy rule, and
     causal-memory rule changes may generate reviewed diffs.
   - Actual production config application remains manual until several clean
     cycles prove the workflow.

5. **Keep TS forecasts experimental.**
   - TS forecasts and paper portfolios are research-only.
   - Promotion requires matured paper rows, after-cost returns, baseline
     outperformance, breadth, advisory alignment checks, and manual review.

## P4 - Performance And Operations

1. **Use evidence before optimizing.**
   - Run `python scripts/api_performance_report.py --limit 20`.
   - Prefer fresh probe rows over historical slowlog rows.
   - Add snapshots, caching, pagination, or indexes only for proven slow paths.

2. **Keep full advisory from becoming the only fast path.**
   - Use bounded context refresh, context-watchlist reconciliation, targeted
     technical refresh, and rules-through-actions reruns where appropriate.
   - Do not rerun browser-bound downloads or expensive source ingest when the
     blocker is already isolated downstream.

3. **Respect single-client external sources.**
   - NSE, Dhan, and Screener should use serialized queues/workers where needed.
   - Avoid aggressive parallel Chrome/browser sessions that trigger source
     blocking.

4. **Continue hot/cold data management.**
   - Use retention/archive/report-first scripts for trace, intraday, large
     textual, and slowlog data.
   - Keep frontend endpoints compact by default and put full payloads behind
     explicit detail/debug flags.

## P5 - UI Only When It Improves Trust

Compatibility phrase for docs audit: Highest Priority: UI-First Operations.

The UI is currently good enough. Do UI work only when it exposes critical
backend truth or reduces operator mistakes.

This keeps the previous UI-first operations priority but narrows it: operator
UI work is priority work only when it makes backend state, action authority,
staleness, source failure, or fix guidance safer to act on.

Nuxt operator frontend replaces the old static HTML dashboard path. The active
operator surface is the Nuxt frontend backed by the FastAPI operator API.

Allowed UI work:

- clearer action authority and stale-data badges
- showing why a BUY/SELL is not executable
- showing source degradation and fix hints
- surfacing exact technical blockers and context source-family blockers
- operator-safe reviewed-diff and research evidence visibility

Avoid UI work:

- visual polish without backend trust improvement
- hiding complex backend state behind ambiguous tags
- turning review-only evidence into apparently executable recommendations

## Completed Foundations

These are already implemented and should generally be preserved:

- Dhan daily/intraday OHLCV and technical feature pipeline.
- Screener.in production and ad hoc research queries.
- Sharpely fundamentals and peer snapshots.
- Macro, regime, announcement, bhavcopy, exchange-event, and theme/news context
  overlays with review-only authority.
- Context-watchlist and signal-refresh path with no-portfolio/no-broker
  boundaries.
- Technical setup explainability via `technical_setup_archetype` and
  `technical_setup_quality_json`.
- Recommendation diagnostics for stale candidates, no-BUY causes, technical
  blockers, market/context attribution, and source-family attribution.
- Technical threshold calibration with blocker and near-miss research.
- Operator Health, fallback telemetry, source degradation, failed-symbol/source
  skip visibility, and fix hints.
- Reason contracts and single consolidated action recommendation path.
- Event-policy, adversarial review, company-memory, causal-memory, hypothesis,
  wait-signal, and playbook scaffolding.
- Nuxt/FastAPI operator frontend, compact API paths, trace summaries, and
  Operations tooling.
- Research-only TS forecast, signal-quality, event-policy, causal-memory,
  adversarial-review, and action-transition evaluators.
- Cron wrappers, script groups, preflight checks, and current generated
  go-crond flow.

## How To Pick The Next Slice

Use this order:

1. Bugs causing scripts, diagnostics, health, downloads, advisory, or API to
   fail.
2. Decision correctness: BUY underparticipation, action authority, stale
   evidence, context-to-entry blockers, or portfolio transition errors.
3. Failure/fallback visibility that affects operator trust.
4. Research evidence needed before safe policy/config review.
5. Performance fixes backed by current slowlog/probe evidence.
6. UI changes only if they expose critical backend state.

Every slice should include:

- a narrow code change
- a focused regression test or script validation
- docs/todo update only if project state changes
- `python -m py_compile <changed modules>`
- focused `pytest` where practical
- `python scripts/docs_state_audit.py --strict`
- `git diff --check`
