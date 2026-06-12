# Investment Advisory Roadmap

Updated: `2026-06-12`

This file is the current roadmap for the live advisory stack. It is not a historical design log.

## Primary operator paths

- Downloaders only: `./all_downloaders.sh`
- Queued downloader refresh: `./all_downloaders_queue.sh` followed by `./all_external_workers.sh`
- Parsers only: `./all_parsers.sh`
- Download + parse: `./complete_data.sh`
- Advisory only: `./all_advisory.sh`
- Fast advisory refresh: `./all_advisory.sh --fast`
- Symbol signal refresh: `python -m advisory.signal_refresh --symbol RELIANCE --reason manual`
- Optional research-only model prep/training: `./all_ml.sh`
- Continuous monitoring: `./all_watchers.sh --loop`

Use `python -m advisory.pipeline` only for stage-level debugging and targeted reruns.

## Current project state

The active stack already has:

- production Screener.in registry sync plus ad hoc Screener.in research queries
- normalized advisory screener universe in `advisory_screener_constituents`
- Dhan daily and intraday OHLCV
- Sharpely fundamentals and peer snapshots
- raw macro data, macro snapshots, base regime classification, and a lightweight news overlay
- normalized NSE exchange-event data and daily exchange-event features
- setup scoring with explicit candidate states such as `PASS_NOW`, `WATCH_*`, `ABSTAIN`, and `REJECT`
- watchlist building with screener, regime, overlay, and theme provenance
- exchange announcement ingest and ET RSS news matching
- LLM-based structured event extraction
- deterministic adversarial review over event tensors
- risk sizing, portfolio ranking, lifecycle, and execution planning
- research ledger logging
- intraday feature persistence and intraday-aware advisory rules
- event-model data prep, train, and score scaffolding retained for research only
- broad research-only training universes from ad hoc Screener queries, disabled from the default operating loop
- continuous watch, live alerting, event routing, consolidated action recommendations, and a Nuxt operator frontend backed by an operator-controlled Python API
- watcher-triggered signal refresh in `advisory_signal_refresh_actions`, which gives fast per-symbol buy/watch/review/exit visibility without rerunning full advisory
- hypothesis/playbook Wait Signals in `advisory_wait_signals` and `advisory_wait_signal_matches`, generated from action plans and matched by watchers/signal refresh
- targeted operator frontend supervision with `./all_frontend.sh --api-only`, `--web-only`, and `--both`
- DB-backed operator snapshots, slow-operation logging, health fix hints, and visible sync-state failure reporting
- guarded Health-page superseded cleanup apply for recovered processing/document failures, audited as metadata cleanup and explicitly blocked from portfolio/action/config/broker mutation
- durable identity issue tracking and guarded identity repair preview/apply flows
- per-symbol feature freshness contracts visible in Action Queue and Symbol Detail, with action decision-time snapshots persisted in `advisory_action_recommendations`
- read-only cron status, operator smoke, signal-quality, prompt-registry, technical-calibration, and research-evidence UI/API paths
- `.env.example` coverage is now checked by `python scripts/env_example_audit.py --strict`, which scans Python, shell, cron, and frontend env usage
- roadmap/docs drift is now checked by `python scripts/docs_state_audit.py --strict`, which flags removed script/static-dashboard references and verifies canonical operator-flow coverage

## What is working now

1. Extraction
   - ingest raw data
   - build snapshots
   - extract structured event tensors from announcements and news

2. Prediction
   - rule engine produces scored candidate states
   - investor playbooks and structured events are interpreted logically before technical entry/exit timing
   - event meta-model train/score path exists, but is not the primary production path
   - intraday confirmation features are available

3. Policy
   - abstain layer is explicit
   - adversarial review can penalize, force manual review, or veto
   - risk and portfolio layers apply caps, sizing, and overlap controls

4. Execution support
   - lifecycle and execution-planning outputs exist
   - continuous watch can trigger fast symbol-level signal refresh
   - hypothesis playbooks can create explicit price/news/announcement conditions to wait for before escalation
   - consolidated action recommendations are the current operator/execution handoff
   - the Nuxt operator frontend replaces the old static HTML dashboard path

## Current bottlenecks

1. The highest-priority gap is now UI-first operations: every normal operator action should be visible, explainable, and auditable from the Nuxt app.
2. Remaining high-risk gaps are observability and correctness gaps, not missing major architecture blocks.
3. Some manual workflows still require CLI/manual edits or copy-paste review: approved config diffs, S3 artifact inspection, and some research-ledger review.
4. Fast signal refresh is intentionally not the authoritative portfolio allocator. Daily `all_advisory.sh` remains the reconciliation path until enough evidence proves incremental advisory is safe.
5. Done: non-home frontend list endpoints now return compact rows or explicit pagination/bounded-list metadata. Actions, Events, Portfolio, trace summary/list payloads, cron logs, prompt registry, hypotheses, artifact manifests, and research review/previews are covered.
6. Done: `/api/health/details` now defaults to bounded parallel fast mode, uses short API caching, and defers heavyweight source/table/log/fallback/API-error-history scans to `mode=full` or CLI diagnostics; 2026-06-09 probe measured ~0.64s cold and ~0.002s warm.
7. Done: Action Queue reads use section snapshots plus `advisory_current_prices`; 2026-06-09 probe improved `/api/actions?...compact=true` to ~0.74s cold and ~0.10s warm, below the generic slowlog threshold.
8. Done: `/api/hypotheses?limit=25` now skips related empty reads, batches promotion-audit lookups, and uses a short API cache; 2026-06-09 probe improved from ~1.45s to ~0.64s cold and ~0.003s warm.
9. Done: trace summaries are materialized, and `scripts/hot_table_retention.py` now provides report-first archive/delete retention for old trace and intraday rows.
10. The serialized external task queue now has concrete NSE/Dhan/Screener handlers plus a queued downloader/worker cron path; direct `all_downloaders.sh` and `complete_data.sh` remain the catch-up/backfill path when a day is missed.
11. `all_advisory.sh` now defaults to bounded local-stage parallelism and skips hidden rule repair; next performance work is stage-budget reporting and moving remaining external repair into queue workers where safe.
12. Continuous watch should add stronger cooldowns, duplicate suppression, and explicit per-source failure counters.
13. Approved technical threshold and signal-quality reviews now generate reviewed diffs, but actual production config application remains manual.
14. Legacy “promotion audit” naming should be migrated to “reliability check” once DB migration is safe.
15. Done: `.env.example` has been reconciled against runtime env usage and guarded by `scripts/env_example_audit.py --strict`.
16. Done: README/docs/todo/analysis current-state coverage is guarded by `scripts/docs_state_audit.py --strict`.

## Highest Priority: UI-First Operations

Goal:

- manage the whole project from the operator UI for normal workflows
- keep CLI commands available for debugging, cron, and emergency repair
- make every write action explicit, versioned, auditable, and non-trading by default
- never hide failures, fallbacks, stale data, or skipped stages

Required operator UI coverage:

1. System health and fix hints
   - show current status for Postgres, Redis, Dhan token/cache, API, Nuxt, cron jobs, source freshness, slow operations, and fallback spikes
   - done: add a UI action to run the read-only smoke check and display the resulting fix hints
   - expose recent cron logs with latest-run status, recovered/manual-interrupt state, and traceback snippets
   - done: show per-symbol feature freshness contracts in Action Queue and Symbol Detail so stale/missing required inputs are visible before trusting a decision
   - done: persist decision-time feature freshness snapshots on consolidated action rows
   - done: add the first explicit feature dependency graph entry for the `actions` stage and publish its gate summary in pipeline output
   - done: extend feature dependency graph visibility to `rules`, `risk`, `portfolio`, and `lifecycle` stage summaries
   - done: downgrade `BUY` / `BUY_MORE` action-consolidation winners to `MANUAL_REVIEW` when required decision-time freshness inputs are blocked
   - done: move blocked `risk` allocated rows to `review_manual` and zero suggested allocation before persistence
   - done: move blocked `portfolio` approved/trimmed rows to `deferred` and zero approved allocation before persistence
   - done: move blocked `rules` `PASS_NOW` candidates to `WATCH_EVENT` before persistence without hiding the candidate
   - done: annotate blocked `lifecycle` and rebalance rows with freshness warnings while preserving exit/risk-reduction actions
   - done: surface stage-level gate outcomes in Operator Health, fix hints, current blockers, and trust gate so blocked rules/risk/portfolio/lifecycle/actions inputs are visible without reading pipeline JSON
   - done: add richer per-symbol UI drill-down for stage-level gate effects, especially which candidate/allocation/action was changed and why
   - done: add guarded Health-page apply for superseded recovered failures; requires operator reason, writes only superseded metadata, and audits `superseded_failure_cleanup_apply`

2. Hypothesis and investor playbook management
   - create, preview, edit, version, activate/deactivate, and mark trusted-overlay playbooks from UI
   - run deterministic/LLM scans from UI with clear dry-run versus write mode
   - show reliability checks, matched evidence, action plans, safe action boundaries, and operator questions
   - rename operator-facing “promotion audit” language to “reliability check” everywhere once backend migration is safe

3. Manual-review workbench
   - one action-required queue for `MANUAL_REVIEW`, event-policy review rows, unresolved/manual-required action conflicts, technical threshold reviews, failed extraction rows, and execution blockers
   - resolved action conflicts must stay audit-only in Decision Trace, symbol detail, and Conflict Rules pages, not in Manual Review
   - allow operator decisions such as `approve_for_manual_config`, `needs_more_data`, `ignore`, `downgrade_to_no_action`, and `watch_for_event`
   - persist every decision with user, timestamp, rationale, before/after payload, and trace links
   - done: decision writes return the same `manual_review_state` and `decision_effect` contract used by the active queue, so the UI can show the saved next state immediately after recording a decision

4. Model/research evidence UI
   - show `advisory.event_model_promotion_check` output in UI: passed gates, failed gates, label coverage, precision lift, ROC-AUC, score freshness, and weekly run count
   - done: event-model promotion output includes a research-only scorecard with usable/not-usable status, operator action, key metrics, failed gates, and explicit no-broker/no-auto-promotion boundaries
   - done: adversarial review ignores persisted event-model scores by default and only consumes them in `promoted` mode when the promotion-check scorecard is usable
   - show S3 artifact upload status and latest artifact keys for event-model runs
   - show TS forecast evaluation, event-policy evaluation, technical threshold calibration, and research ledger runs in one research evidence area
   - no model or threshold should become live policy from UI without an explicit manual review/audit trail

5. Config-change assistant, not auto-config
   - generate reviewed diffs for approved technical threshold changes and playbook/config changes
   - show copyable patch, expected impact, affected setup ids, and rollback notes
   - do not auto-apply production YAML changes until the review workflow is proven safe over multiple runs

6. Operations dashboard
   - done: show the full cron schedule, next-run estimate, latest log marker, current lock status, stale lock flag, and log snippets
   - done: add a read-only cron preflight command that validates generated crontab setup, required env, referenced scripts, stale locks, Python resolution, and operator ports before starting go-crond
   - done: every top-level shell wrapper emits `[stockey.script]` lifecycle markers, including the long-running frontend supervisor and Codex-supervised manual wrappers
   - done: scheduled Python cron jobs now run through named shell wrappers instead of inline `python -m ...` commands, so logs, docs, and Operations labels stay consistent
   - show safe dry-run buttons for selected jobs: health, hypothesis scan, event-model promotion check, technical calibration review, S3 artifact dry-run
   - block or warn on expensive/long-running jobs from UI unless explicitly confirmed

7. Data/debug visibility
   - paginate and compact events/actions/portfolio/trace APIs so the UI remains fast
   - materialize trace summaries after advisory/watchers instead of rebuilding large traces live
   - show source-to-output lineage: raw event -> OCR/summary -> tensor -> policy/review -> action -> lifecycle/execution plan
   - done: consolidate `/api/hypotheses` reads so table initialization and empty-list queries do not cost ~1.45s per page load

Verified current state on `2026-06-12`:

1. Docs and env drift checks pass:
   - `python scripts/docs_state_audit.py --strict`
   - `python scripts/env_example_audit.py --strict`
2. Central downloader/parser state coverage is in place for configured modules, and latest `download_runner:*` rows are visible through Health/Data Health.
3. High-risk schema creation for active advisory/runtime tables is now routed through `utils.schema_migrations`; remaining direct DDL is limited to generic table/upsert helpers, temporary/staging tables, and narrow backfill utilities.
4. The fallback telemetry scanner baseline is `{'records_fallback': 468, 'reraises': 52, 'silent_handler': 2}` with zero `silent_fallback` and zero `logs_then_falls_through` rows. The remaining scanner-visible silent handlers are fallback-telemetry self-protection paths, where emitting telemetry would risk recursive noise.

Next implementation slices:

1. Finish source-specific degraded-state semantics where it still matters operationally: no-data versus source-unavailable versus auth-unavailable versus parser-bug for sources that can mislead advisory trust.
2. Keep fallback telemetry coverage from regressing as new source/API paths are added. The current remaining scanner-visible `silent_handler` rows are intentionally self-protecting telemetry internals, so future work should focus on newly introduced operator-trust, source-freshness, broker-safety, or advisory-correctness fallbacks.
3. Continue UI-first operations for remaining manual/research/config workflows, especially S3 artifact inspection, reviewed config application safety, and operator-visible “what happens next” on write actions.
4. Use `python scripts/api_performance_report.py --limit 20` after normal cron/advisory runs to pick the next frontend/API latency fix from evidence.
5. Keep docs current with `python scripts/docs_state_audit.py --strict` after changing top-level scripts, cron, UI paths, or roadmap status.

## Next development set

This is the recommended current implementation order.

1. Recommendation reason contract
   - done: define and persist `recommendation_reason_json` plus `reason_contract_status` for final consolidated action rows
   - done: fail-soft incomplete broker-action reason contracts into `MANUAL_REVIEW` before execution can see them
   - done: expose reason-contract status in action traces and dashboard detail views
   - done: enrich action-candidate raw context from latest `advisory_candidates` and `advisory_market_regime` snapshots before reason-contract validation
   - done: enrich action-candidate raw context from latest `advisory_event_evaluations`, `advisory_event_reviews`, and `advisory_playbook_action_plans` snapshots before reason-contract validation
   - done: add a frontend detail view that expands `recommendation_reason_json` into separate screener, technical, event, playbook, macro/regime, risk, and competing-candidate panels

2. Top 50% market context tracker
   - done: create a daily market-context universe table for the top 50% of investable Indian equities by configurable ranking, initially market cap plus traded value
   - done: track announcements, news, exchange events, technical leadership, sector movement, and macro sensitivity for this universe even when names are not current recommendations
   - done: summarize this universe into sector breadth, leadership rotation, event clusters, and risk-on/risk-off context
   - done: feed this context into event/playbook interpretation and macro/regime gates, not directly into trade execution
   - done: expose “Market Context: Top 50%” in the operator frontend
   - next: add a lower-priority watcher queue for top-context names so event coverage improves without evaluating every broad-market event with LLMs

3. Investor playbook action framework
   - done: treat these as investor playbooks in the operator UI instead of statistically validated trading policies
   - done: generate playbook action plans for matched news/announcements, including urgency, checks, safe action boundary, and trusted-overlay flag
   - done: use Codex as the action planner with deterministic fallback when LLM planning is disabled or unavailable
   - done: wire trusted playbook action plans into action consolidation as review/risk-overlay candidates, not direct trades
   - done: show playbook overlays as a first-class trace domain in the Operator trace UI
   - done: seed `config/hypotheses.yaml` with 8 starter playbooks, including the austerity/risk-off example
   - done: add YAML importer for versioned playbooks
   - done: add Operator UI support to preview normalized/generated playbooks before saving
   - done: add edit/status controls for existing playbooks in the Operator UI
   - done: replace statistical promotion language with `active_review` and `trusted_overlay` lifecycle statuses
   - done: add reliability checks that show historical coverage, point-in-time forward returns, benchmark excess returns, and sector excess returns without treating sparse playbooks as statistically validated
   - done: use `master_sharpely_equity.sector_code` for sector excess comparisons because live `sharpely_stock_meta` and peer snapshots are currently contaminated
   - done: sync canonical `NIFTY` benchmark rows from `nseindia_indices` into `dhan_ohlcv_daily` after index parsing, and prefer NSE index history for NIFTY technical/regime benchmark loaders
   - next: rename legacy backend table/API names from promotion-audit to reliability-check once data migration is safe

4. Event-to-action interpretation framework
   - done: add deterministic event-class policies for earnings beats, growth acceleration, margin expansion, order wins, promoter buying/selling, pledge reduction/increase, regulatory notices, management resignations, buybacks, dividends, dilution, analyst meets, and neutral corporate actions
   - done: persist why each evaluated event became `BUY_WATCH`, `MANUAL_REVIEW`, `REDUCE_EXPOSURE_REVIEW`, or `NO_ACTION` in `advisory_event_policy_actions`
   - done: bridge event-policy actions into consolidated action recommendations as review/risk overlays only, never direct broker actions
   - done: add bounded LLM manual-review refinement so review rows get operator notes, possible actions, future events to wait for, and questions; non-actionable rows are downgraded to `NO_ACTION`
   - done: expose event-policy action counts, policy checks, LLM notes, wait-for events, and operator questions in the Nuxt Event Inbox
   - done: persist and expose event-policy actionability context: materiality, freshness, calibrated source quality for exchange filings/news/OCR-document evidence, affected sectors/peers, point-in-time Dhan daily price reaction when available, latest portfolio exposure/allocation when available, suggested next evidence, review priority, and review-only deterministic boundary
   - done: make macro/regime state adjust positive action strength and sizing before action consolidation reaches portfolio/execution policy
   - done: add research-only realized forward-return evaluation for event-policy action types, classes, score buckets, confidence buckets, source-quality buckets, and affected market-scope buckets
   - done: show source-quality and affected market-scope calibration buckets in the Nuxt Event Inbox
   - done: add manual-only `advisory.event_policy_promotion` review/decision audit rows and copyable event-policy review-rule guidance from evaluation summary groups
   - done: wire event-policy promotion review create/list/decision flows into Operator API and frontend API client methods
   - done: add dedicated Event Inbox UI controls to create manual-only event-policy promotion reviews and record approve/reject/needs-more-data decisions
   - done: incorporate approved event-policy promotion decisions into reviewed config/rule-change previews for disabled `event_policy_review_rules`
   - done: expose the event-policy reviewed-diff preview from approved Event Inbox promotion reviews
   - next: use repeated operator feedback to decide whether any generated event-policy review-rule diff should be manually applied

5. Technical/lifecycle calibration
   - keep `advisory.technical_engine` as the single swing-technical state machine
   - done: add research-only technical threshold calibration against realized forward OHLCV returns
   - done: add operator API/page for reviewing latest technical threshold calibration summaries and copying candidate configs
   - done: add LLM-assisted manual promotion review that writes review evidence and a pending patch but does not apply thresholds automatically
   - done: add explicit manual approval/rejection audit records for reviewed threshold patches, with copyable final patch guidance and no automatic config edits
   - done: add reviewed-diff generation against `config/advisory_setups.yaml` for approved threshold and signal-quality overlay decisions
   - calibrate target multiples, stop distances, partial-exit rules, and time-stop defaults using realized lifecycle outcomes
   - next: keep actual production config application manual until reviewed-diff workflow remains clean across multiple runs
   - surface technical sub-scores, trigger archetype, stop, invalidation, and exit reason as first-class operator UI fields

6. Runtime robustness and observability
   - done: update generated cron/template to run data refresh, watchers, hypothesis scans, TS workflow/evaluation, advisory, event-policy evaluation, weekly technical calibration, and frontend supervision
   - done: add read-only operator health smoke test and Nuxt health page for DB, Redis, Dhan token, cron logs, table freshness, and optional dependencies
   - done: add deterministic `[stockey.script]` lifecycle markers to primary shell wrappers and prefer those in cron-log health parsing
   - done: add Data Health fix hints plus filters for errors, warnings, recovered rows, and OK rows
   - done: recover manual `KeyboardInterrupt` logs when mapped output tables have fresher rows than the interrupted log
   - done: add API self-check latency and Dhan cached-token age/expiry to the operator health payload and Data Health page
   - done: surface operator snapshot freshness, slow-operation issues, and failed watcher/router sync-state rows in operator health and the Nuxt Health page
   - done: add Advisory Trust Gate to Health so the UI says whether today’s recommendations are usable, review-only, or blocked by runtime, freshness, event evidence, identity, signal-quality, or degradation issues
   - done: add persisted fallback telemetry plus Health-page fallback spike cards for Redis fail-soft, Dhan identity fallback, NSE retry/session reset, and LLM/Codex deterministic fallbacks
   - done: add `python -m advisory.identity_issues` dry-run/apply lifecycle to recheck and close Dhan/security identity issues after master refresh, with attempt counts and resolution metadata
   - done: add common index/benchmark aliases to Dhan identity resolution so `NIFTY50`, `NIFTY 50`, `BANKNIFTY`, `NIFTY BANK`, `INDIAVIX`, and `INDIA VIX` can be rechecked and closed through the same Identity Issues lifecycle
   - done: add Operator Health coverage for latest broker-capable action rows that do not join to `company_master` or have no Dhan NSE/BSE security id
   - done: downgrade broker-capable action winners with missing company/Dhan identity to `MANUAL_REVIEW` before persistence/execution, with explicit no-broker reason-contract context
   - done: add execution-planning defense for older/non-standard broker-capable action rows so Dhan identity failures are `submit_blocked`, recorded in safety contracts, and emitted as execution fallback telemetry
   - done: record dashboard section-loader failures in payloads instead of only printing them
   - done: make watcher cycle failures persist `advisory_sync_state.status=error` and publish error messages before returning
   - done: add a single smoke-test command for API + DB + frontend dependency checks
   - done: add narrow Identity Issues UI actions to run the dry-run resolver, show rows that would close, and apply only previewed issue keys
   - done: redact common secrets from fallback telemetry, Operator API audit errors, Health cron summaries, and Operations cron-log tails before they reach the UI
   - done: extend fallback telemetry to shared DB retry/exhaustion paths with a file-backed JSONL spool, avoiding circular Postgres writes while still surfacing retry spikes in Operator Health
   - done: add a reusable `utils.db.execute_db_operation` transaction wrapper for source-specific `db_session` blocks and route action-conflict resolver delete/update transactions through it, so deadlocks/timeouts retry the full transaction.
   - done: route Screener.in ad-hoc query result cleanup through `execute_db_operation`, so transient Postgres failures retry the pre-upsert cleanup transaction instead of leaving confusing partial query state.
   - done: route file-level ingestion-state reads, marks, failure lookups, listing, and cleanup through `execute_db_operation`, so parser/downloader state visibility is resilient to transient Postgres failures.
   - done: route external-task queue claim, complete, and fail transitions through `execute_db_operation`, so watcher/advisory worker state changes retry full transactions on transient Postgres failures.
   - done: route superseded-failure cleanup column setup and recovered-failure marking writes through `execute_db_operation`, so stale-error cleanup retries full transactions instead of leaving UI failure lifecycle state half-updated.
   - done: route identity-issue record, resolved, and resolution-failed writes through `execute_db_operation`, so Dhan/security mapping issue state retries full transactions on transient Postgres failures.
   - done: route decision-trace conflict-rule seeding through `execute_db_operation`, so action-conflict policy bootstrap retries full transactions on transient Postgres failures.
   - done: route watchlist rebuild cleanup through `execute_db_operation`, so rebuilding advisory watch state retries the delete transaction before upserting fresh rows.
   - done: route rule-engine rebuild cleanup through `execute_db_operation`, so advisory candidate/rejection rebuilds retry the delete transaction before fresh outputs are written.
   - done: route market-news overlay rebuild cleanup through `execute_db_operation`, so overlay refresh deletes retry before the new daily overlay row is upserted.
   - done: route market-regime rebuild cleanup through `execute_db_operation`, so full regime snapshot rebuilds retry the delete transaction before fresh rows are written.
   - done: route portfolio-order cleanup through `execute_db_operation`, so per-symbol/asof replacement deletes retry before fresh portfolio rows and traces are written.
   - done: route risk-allocation cleanup through `execute_db_operation`, so per-setup/asof replacement deletes retry before fresh risk allocation rows and traces are written.
   - done: route lifecycle/rebalance cleanup through `execute_db_operation`, so position lifecycle and rebalance replacement deletes retry before fresh lifecycle/action rows and traces are written.
   - done: route consolidated-action cleanup through `execute_db_operation`, so action recommendation replacement deletes retry before fresh final action rows, conflict rows, and traces are written.
   - done: route technical-feature rebuild cleanup through `execute_db_operation`, so symbol-scoped and full technical feature rebuild deletes retry before fresh daily technical rows are written.
   - done: route intraday-feature rebuild cleanup through `execute_db_operation`, so date/interval-scoped intraday feature rebuild deletes retry before fresh daily intraday rows are written.
   - done: route exchange-event missing-type repair updates through `execute_db_operation`, so legacy NSE deal/short/unclassified backfills retry the full classification repair transaction.
   - done: route trace-summary cleanup through `execute_db_operation`, so materialized operator trace cache retention deletes retry the full cleanup transaction.
   - done: route wait-signal matched-status updates through `execute_db_operation`, so persisted wait-signal matches retry the status transition after match rows are written.
   - done: route NSE offmarket parser schema-maintenance transactions through `execute_db_operation`, so constraint/text-column repair retries the full DDL transaction.
   - done: route Screener.in registered screener registry/snapshot writes through `execute_db_operation`, so recurring screener parser writes and removals retry transient Postgres failures.
   - done: route event-risk cleanup through `execute_db_operation`, so stale LLM event-risk rows are deleted with transient DB retry before fresh risks are inserted.
   - done: route advisory screener-constituent replacement cleanup through `execute_db_operation`, so normalized Screener.in constituent refreshes retry stale-row deletes before upsert.
   - done: route company-master-id backfill transactions through `execute_db_operation`, so each table-level historical identity backfill retries the full update transaction without duplicating in-memory status rows.
   - done: route Dhan scrip-master bulk update transactions through `execute_db_operation`, so schema sync, staging COPY, and versioning SQL reconnect/retry together on transient Postgres failures.
   - done: route deprecated-table cleanup DDL through `execute_db_operation`, so maintenance cleanup retries the full DROP transaction on transient Postgres failures.
   - done: route hot-table retention archive/delete transactions through `execute_db_operation`, so monthly archive/delete chunks retry the DB block and only publish chunk summaries after success.
   - done: route announcement text S3 offload metadata updates through `execute_db_operation`, so document/report pointer writes retry the DB block and rebuild manifest counts per successful attempt.
   - done: route announcement S3 pointer validator fetches through `execute_db_operation`, so read-only offload QA reconnects/retries the DB scan before S3 validation.
   - done: route operator API conflict-rule promotion/update writes through `execute_db_operation`, so manual conflict-rule changes retry the full DB write before returning the updated rule.
   - done: route schema-dump metadata fetches through `execute_db_operation`, so `python -m utils.db_schema_dump --schemas public` reconnects/retries the full catalog read used for QA.
   - done: route schema-migration registry writes through `execute_db_operation`, so registry creation, migration apply, and failure recording retry transient Postgres failures.
   - done: route lifecycle tightened-stop baseline updates through `execute_db_operation`, so automatic stop ratchets retry the portfolio update transaction and preserve the returned update count.
   - done: add `scripts/db_retry_coverage_report.py`, a static QA report that classifies `db_session()` blocks as retry-wrapped, direct, core helper, or parse errors so remaining DB retry gaps can be targeted instead of found manually with `rg`.
   - done: run `scripts/db_retry_coverage_report.py` on 2026-06-12 and verify scanned `advisory`, `data`, `scripts`, and `utils` code has no direct unwrapped `db_session()` blocks (`core_helper=5`, `wrapped=54`).
   - done: add `scripts/fallback_telemetry_coverage_report.py`, a static QA report that classifies exception handlers as telemetry-recorded, re-raised, logs-only, or silent so fallback visibility gaps can be targeted without repeating manual `rg` sweeps.
   - done: run `scripts/fallback_telemetry_coverage_report.py` on 2026-06-12 and capture the initial fallback telemetry baseline across `advisory`, `data`, `scripts`, and `utils`: `logs_only=19`, `logs_then_falls_through=24`, `records_fallback=157`, `reraises=56`, `silent_fallback=142`, `silent_handler=123`.
   - done: add local fallback telemetry for announcement-watch active-watchlist load failures, announcement-watch table lookup failures, continuous-watch open-position source failures, and continuous-watch cycle failures; the 2026-06-12 coverage baseline moved to `records_fallback=161` and `silent_fallback=138`.
   - done: add local fallback telemetry for LLM event-evaluator existing-evaluation lookup failures, exchange/bhavcopy context load failures, and broad-market context load failures; the 2026-06-12 coverage baseline moved to `records_fallback=164`, `silent_fallback=136`, and `silent_handler=122`.
   - done: add local fallback telemetry for operator API company-memory enrichment lookup failures, generic Manual Review source query failures, threshold-review load failures, and identity-issue load failures; the 2026-06-12 coverage baseline moved to `records_fallback=169` and `silent_fallback=131`.
   - done: add local fallback telemetry for legacy live-dashboard safe frame/list section loaders; the 2026-06-12 coverage baseline moved to `records_fallback=171` and `logs_only=17`.
   - done: add local fallback telemetry for Operator Health snapshot and standardized downloader/parser run-state check failures; the 2026-06-12 coverage baseline moved to `records_fallback=173` and `silent_fallback=129`.
   - done: add local fallback telemetry for central download-runner sync-state persistence failures, module nonzero exits, and module exceptions; the 2026-06-12 coverage baseline moved to `records_fallback=176` and `silent_fallback=126`.
   - done: add local fallback telemetry for advisory daily OHLCV symbol sync failures, so Dhan mapping/token/price-repair issues are operator-visible; the 2026-06-12 coverage baseline moved to `records_fallback=177` and `logs_only=16`.
   - done: add local fallback telemetry for non-fatal decision trace write failures, so missing audit/trace evidence is operator-visible; the 2026-06-12 coverage baseline moved to `records_fallback=178` and `logs_only=15`.
   - done: add local fallback telemetry for Economic Times RSS per-feed source-unavailable and parse failures, so degraded market-news ingestion is operator-visible; the 2026-06-12 coverage baseline moved to `records_fallback=180` and `logs_only=13`.
   - done: add local fallback telemetry for NSE bhavcopy per-date archive download failures, so missing market-wide evidence is operator-visible while catch-up can retry; the 2026-06-12 coverage baseline moved to `records_fallback=181` and `logs_only=12`.
   - done: add local fallback telemetry for NSE indices per-date archive download failures, so missing index/benchmark evidence is operator-visible while catch-up can retry; the 2026-06-12 coverage baseline moved to `records_fallback=182` and `logs_only=11`.
   - done: add local fallback telemetry for NSE holiday-calendar source-unavailable and generic download/parse failures, so stale trading-calendar context is operator-visible; the 2026-06-12 coverage baseline moved to `records_fallback=184` and `logs_only=9`.
   - done: add local fallback telemetry for NSE off-market/deals block download failures, so missing block/bulk/short-selling evidence is operator-visible while catch-up can retry; the 2026-06-12 coverage baseline moved to `records_fallback=185` and `logs_only=8`.
   - done: add local fallback telemetry for NSE off-market/deals parser failures, so missing parsed block/bulk/short-selling evidence is operator-visible with parser classification and stored file name; the 2026-06-12 coverage baseline moved to `records_fallback=186` and `logs_only=7`.
   - done: add local fallback telemetry for Redis set-member state read failures, so safe reprocessing caused by missing Redis processed-state is operator-visible; the 2026-06-12 coverage baseline moved to `records_fallback=187` and `logs_only=6`.
   - done: add local fallback telemetry for FRED macro latest-date fallback and per-series fetch failures, so partial/lookback-based US macro context is operator-visible; the 2026-06-12 coverage baseline moved to `records_fallback=189` and `logs_only=5`.
   - done: add local fallback telemetry for MOSPI CPI month download retry failures, so stale/partial India inflation context is operator-visible; the 2026-06-12 coverage baseline moved to `records_fallback=190` and `logs_only=4`.
   - done: add local fallback telemetry for RBI bank-rates browser/source-unavailable and generic download/parse failures, so stale policy-rate context is operator-visible; the 2026-06-12 coverage baseline moved to `records_fallback=192` and `logs_only=2`.
   - done: add local fallback telemetry for manual Dhan OHLCV pull utility failures, so operator price-inspection failures are visible with ticker/mode/source context; the 2026-06-12 coverage baseline moved to `records_fallback=193` and `logs_only=1`.
   - done: add local fallback telemetry for event meta-model train/score CLI command failures, so missing model train/score output is operator-visible; the 2026-06-12 coverage baseline moved to `records_fallback=194` and `logs_only=0`.
   - done: add local fallback telemetry for announcement first-page OCR/transcription, full-document OCR, and earnings-call audio transcription failures, so degraded announcement evidence is operator-visible while ingestion continues; the 2026-06-12 coverage baseline moved to `records_fallback=197` and `logs_then_falls_through=20`.
   - done: add local fallback telemetry for NSE announcement cookie-bootstrap URL failures, so NSE session degradation is operator-visible while retry behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=198` and `logs_then_falls_through=19`.
   - done: add local fallback telemetry for per-announcement managed-ingest failures, so incomplete OCR/report/summary output is operator-visible while batch ingestion continues; the 2026-06-12 coverage baseline moved to `records_fallback=199` and `logs_then_falls_through=18`.
   - done: add local fallback telemetry for Dhan daily/intraday bulk OHLCV per-symbol failures, so stale/missing price data is operator-visible while symbol batches continue; the 2026-06-12 coverage baseline moved to `records_fallback=201` and `logs_then_falls_through=16`.
   - done: add local fallback telemetry for Dhan identity fallback/issue telemetry persistence failures, so unresolved security-id evidence remains visible when DB-backed telemetry or identity-issue persistence fails; the 2026-06-12 coverage baseline moved to `records_fallback=205`, `logs_then_falls_through=14`, and `silent_handler=120`.
   - done: add local fallback telemetry for canonical benchmark sync per-symbol failures, so stale benchmark/regime/relative-strength context is operator-visible while partial run-state behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=206` and `logs_then_falls_through=13`.
   - done: add local fallback telemetry for WPI per-item download retry failures, so stale India producer-price context is operator-visible while retry/resume behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=207` and `logs_then_falls_through=12`.
   - done: add local fallback telemetry for FRED/ISM macro leg failures, so degraded US macro context is operator-visible while partial run-state behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=209` and `logs_then_falls_through=10`.
   - done: add local fallback telemetry for Operator API latest daily/intraday OHLCV price lookup failures, so degraded Action Queue and symbol-page price context is operator-visible while partial UI/API responses continue; the 2026-06-12 coverage baseline moved to `records_fallback=211` and `logs_then_falls_through=8`.
   - done: add local fallback telemetry for TS forecast workflow Screener.in fallback failures, so degraded TS Watch candidate sourcing is operator-visible while the tracked Dhan/OHLCV symbol fallback continues; the 2026-06-12 coverage baseline moved to `records_fallback=212` and `logs_then_falls_through=7`.
   - done: add local fallback telemetry for Redis backup/restore per-key and backup-file failures, so Redis state maintenance issues are operator-visible while existing skip-or-exit behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=215` and `logs_then_falls_through=4`.
   - done: add local fallback telemetry self-spooling when DB-backed fallback telemetry writes fail, so telemetry storage degradation remains operator-visible; the 2026-06-12 coverage baseline moved to `records_fallback=216` and `logs_then_falls_through=3`.
   - done: add local fallback telemetry for agent tool runner, SQL query runner, and duplicate-index cleanup failures, so maintenance-tool failures are operator-visible while existing JSON-error or skip behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=219` and zero `logs_then_falls_through` rows.
   - done: add local fallback telemetry for peer OHLCV, peer fundamentals, and NSDL FPI failed-month sync failures, so degraded peer/relative-strength and foreign-flow context is operator-visible while existing partial result/run-state behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=223`, `silent_fallback=124`, and `silent_handler=118`.
   - done: add local fallback telemetry for Dhan token validation failures, corrupt cached-token payloads, invalid cached-token expiry strings, and Dhan scrip-master download/short-response failures, so broker-auth and security-master degradation is operator-visible before it cascades; the 2026-06-12 coverage baseline moved to `records_fallback=227`, `silent_fallback=121`, and `silent_handler=117`.
   - done: add local fallback telemetry for NSE announcement session bootstrap-after-reset failures, session-build retry failures, and unsupported NSE/BSE announcement timestamp formats, so source session degradation and timestamp parse failures are operator-visible; the 2026-06-12 coverage baseline moved to `records_fallback=231`, `silent_fallback=119`, and `silent_handler=115`.
   - done: add local fallback telemetry for NSE bhavcopy parser invalid-key dates, top-level parse failures, circuit-hit date fallback, zip-stat failures, file-level parser failures, and nested corrupt-zip failures while preserving parser-state semantics; the 2026-06-12 coverage baseline moved to `records_fallback=239`, `silent_fallback=113`, and `silent_handler=113`.
   - done: add local fallback telemetry for NSE indices parser invalid-key dates, zip-stat failures, date-format fallback, file-level parse failures, and archive parse failures while preserving parser-state semantics; the 2026-06-12 coverage baseline moved to `records_fallback=244`, `silent_fallback=110`, and `silent_handler=112`.
   - done: add local fallback telemetry for malformed NSE bhavcopy/indices archive keys and malformed indices downloaded-date members, so catch-up re-download decisions caused by bad stored metadata are operator-visible; the 2026-06-12 coverage baseline moved to `records_fallback=247`, `silent_fallback=107`, and `silent_handler=112`.
   - done: add local fallback telemetry for NSE security-history corporate-action context and persisted-history load failures, so degraded identity review/fallback mapping is operator-visible; the 2026-06-12 coverage baseline moved to `records_fallback=249`, `silent_fallback=106`, and `silent_handler=111`.
   - done: add local fallback telemetry for NSE off-market malformed downloaded-file keys and recent-events mixed-date parser fallback, clearing all silent `data/nseindia/*` scanner rows; the 2026-06-12 coverage baseline moved to `records_fallback=251`, `silent_fallback=105`, and `silent_handler=110`.
   - done: add local fallback telemetry for company-memory review symbol-discovery failures, so missing review-only memory rows caused by source query failures are operator-visible; the 2026-06-12 coverage baseline moved to `records_fallback=252`, `silent_fallback=104`, and `silent_handler=110`.
   - done: add local fallback telemetry for hypothesis/playbook market-context load failures, so generated action plans keep an explicit error payload and the degraded context is operator-visible; the 2026-06-12 coverage baseline moved to `records_fallback=253`, `silent_fallback=103`, and `silent_handler=110`.
   - done: add local fallback telemetry for identity-issue resolution failures, so unresolved Dhan/security mapping repair attempts stay open and become operator-visible degraded operations; the 2026-06-12 coverage baseline moved to `records_fallback=254`, `silent_fallback=102`, and `silent_handler=110`.
   - done: add local fallback telemetry for news-watch recent-news source load failures and active news-theme screener mapping lookup failures, so degraded market/news source context is visible while safe-empty behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=256`, `silent_fallback=100`, and `silent_handler=110`.
   - done: add local fallback telemetry for signal-refresh table-existence checks, trace-summary cache refresh failures, and per-router item refresh failures, so watcher-triggered refresh degradation is operator-visible while safe-unavailable/partial-batch behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=259`, `silent_fallback=99`, and `silent_handler=108`.
   - done: add local fallback telemetry for core Operator Health DB/API/latency-probe/Redis/sync-state check failures, so Health-page diagnostic outages are themselves visible as degraded operations; the 2026-06-12 coverage baseline moved to `records_fallback=264`, `silent_fallback=94`, and `silent_handler=108`.
   - done: add local fallback telemetry for Operator Health schema registry, Dhan cached-token inspection, Dhan token validation, and Dhan cache inspection failures, so broker-readiness diagnostics are themselves visible as degraded operations; the 2026-06-12 coverage baseline moved to `records_fallback=268`, `silent_fallback=90`, and `silent_handler=108`.
   - done: add local fallback telemetry for Operator Health API-error history, trace summary cache, identity issue, active action identity coverage, and Screener.in failure inspection paths, so source-inspection outages are visible as degraded operations; the 2026-06-12 coverage baseline moved to `records_fallback=273`, `silent_fallback=85`, and `silent_handler=108`.
   - done: add local fallback telemetry for Operator Health announcement document failure inspection, frontend dependency checks, signal-quality freshness/rows/coverage checks, and feature stage-gate checks, so remaining runtime health-check outages are visible as degraded operations; the 2026-06-12 coverage baseline moved to `records_fallback=280`, `silent_fallback=80`, and `silent_handler=107`.
   - done: add local fallback telemetry for Operator API table-column lookup failures, so schema metadata outages are visible while safe-empty column behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=281`, `silent_fallback=79`, and `silent_handler=107`.
   - done: add local fallback telemetry for Operator API event-model artifact manifest/S3 inspection failures, so missing local artifacts and unavailable S3 artifact metadata are visible while partial research-evidence payloads remain readable; the 2026-06-12 coverage baseline moved to `records_fallback=284`, `silent_fallback=79`, and `silent_handler=104`.
   - done: add local fallback telemetry for execution-engine legacy portfolio/exit Dhan identity resolution failures, so broker-planning blockers are visible while affected rows remain `submit_blocked`; the 2026-06-12 coverage baseline moved to `records_fallback=286`, `silent_fallback=79`, and `silent_handler=102`.
   - done: add local fallback telemetry for Operator API whitelisted command timeouts, so timed-out maintenance/smoke commands are visible in fallback telemetry while command-run audit rows still persist `timeout`; the 2026-06-12 coverage baseline moved to `records_fallback=287`, `silent_fallback=79`, and `silent_handler=101`.
   - done: add local fallback telemetry for Manual Review queue latest-decision load failures and invalid legacy decision states, so degraded queue filtering/suppression is visible while the queue still renders available items; the 2026-06-12 coverage baseline moved to `records_fallback=290`, `silent_fallback=79`, and `silent_handler=98`.
   - done: add local fallback telemetry for decision-trace table existence lookup failures, so missing trace/manual-review link visibility is observable while trace reads still fail closed; the 2026-06-12 coverage baseline moved to `records_fallback=291`, `silent_fallback=78`, and `silent_handler=98`.
   - done: add local fallback telemetry for sync-state bus publish failures, so Redis/pub-sub outages are visible while the database sync state remains authoritative; the 2026-06-12 coverage baseline moved to `records_fallback=315`, `silent_fallback=66`, and `silent_handler=86`.
   - done: add local fallback telemetry for malformed action-recommender JSON context, so corrupt stored recommendation/context payloads are visible while existing default fallback behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=316`, `silent_fallback=65`, and `silent_handler=86`.
   - done: add local fallback telemetry for portfolio CLI database/build failures, so empty or failed portfolio output is visible in Health while existing non-zero exit behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=318`, `silent_fallback=63`, and `silent_handler=86`.
   - done: add local fallback telemetry for invalid execution-engine integer/float environment values, so broker-execution safety-limit misconfiguration is visible while default fallback behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=320`, `silent_fallback=61`, and `silent_handler=86`.
   - done: add local fallback telemetry for Operator API slow-request middleware response-size parse and slowlog write failures, so frontend/API latency telemetry degradation is visible while successful API responses remain non-fatal; the 2026-06-12 coverage baseline moved to `records_fallback=322`, `silent_fallback=61`, and `silent_handler=84`.
   - done: add local fallback telemetry for non-zero `SystemExit` raised inside central downloader module entrypoints, so imported downloader/parser failures remain visible while exported run state is preserved; the 2026-06-12 coverage baseline moved to `records_fallback=323`, `silent_fallback=60`, and `silent_handler=84`.
   - done: add local fallback telemetry for cron lock PID parsing and liveness-check failures, so Operations UI stale-lock status is visibly degraded when PID files or process checks are unreliable; the 2026-06-12 coverage baseline moved to `records_fallback=325`, `silent_fallback=58`, and `silent_handler=84`.
   - done: add local fallback telemetry for performance slowlog JSON/state/context-manager failures, so latency triage degradation is visible while safe empty/string fallback behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=328`, `silent_fallback=56`, and `silent_handler=83`.
   - done: add local fallback telemetry for Operator Health log-tail read failures, so missing/unreadable cron/API logs are visible while Health still returns a bounded empty tail; the 2026-06-12 coverage baseline moved to `records_fallback=329`, `silent_fallback=55`, and `silent_handler=83`.
   - done: add local fallback telemetry for Dhan cached-token clear misses and missing browser launcher attempts, so broker-auth recovery degradation is visible while existing retry/raise behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=331`, `silent_fallback=53`, and `silent_handler=83`.
   - done: add local fallback telemetry for malformed risk-engine allocation context snapshots, so allocation trace explainability degradation is visible while empty-context fallback behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=332`, `silent_fallback=52`, and `silent_handler=83`.
   - done: add local fallback telemetry for Operator API runtime git/source mtime metadata failures, so stale-code and version-display degradation is visible while `/api/runtime` remains resilient; the 2026-06-12 coverage baseline moved to `records_fallback=335`, `silent_fallback=50`, and `silent_handler=82`.
   - done: add local fallback telemetry for malformed Operator Health JSON payloads and API-error request contexts, so Health-page diagnostic context degradation is visible while safe empty-object fallback remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=336`, `silent_fallback=49`, and `silent_handler=81`.
   - done: add local fallback telemetry for corrupt local fallback JSONL rows, unreadable fallback spool files, and fallback-telemetry table inspection/query failures, so the Health fallback system is itself observable while spool-only summary behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=340`, `silent_fallback=45`, and `silent_handler=81`.
   - done: add local fallback telemetry for Dhan automated web-login cleanup, forced Proceed-click fallback, and missing-token timeout paths, so broker-auth automation degradation is visible while login behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=345`, `silent_fallback=45`, and `silent_handler=76`.
   - done: add local fallback telemetry for Screener.in automated-auth cleanup, login-status timeout, and dashboard wait-timeout paths, so screener/session degradation is visible while login behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=350`, `silent_fallback=45`, and `silent_handler=71`.
   - done: add DB retry-spool telemetry coverage for pool disposal, retry-spool read/write, session cleanup, temp-table cleanup, retry wrappers, SQL read retries, and first-run max-date lookup failures, so Postgres degradation remains visible even when Postgres itself is unstable; the 2026-06-12 coverage baseline moved to `records_fallback=363`, `silent_fallback=44`, and `silent_handler=63`.
   - done: add Redis retry, reconnect-cooldown, fail-soft, and close-cleanup fallback telemetry, so watcher/pub-sub state degradation is visible instead of only returning safe defaults; the 2026-06-12 coverage baseline moved to `records_fallback=365`, `silent_fallback=43`, and `silent_handler=62`.
   - done: add local fallback telemetry for malformed Dhan API JSON responses before raw-text fallback or `DhanAPIError`, so broker/API degradation is visible in Health while response behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=366`, `silent_fallback=43`, and `silent_handler=61`.
   - done: add local fallback telemetry for malformed HTTP cache date keys and unreadable cache files, so shared source-cache degradation is visible before falling back to file mtime or network fetch; the 2026-06-12 coverage baseline moved to `records_fallback=368`, `silent_fallback=43`, and `silent_handler=60`.
   - done: add local fallback telemetry for RBI FBIL G-sec trade-date parse fallback, Par-Yield sheet-name fallback, and per-date download/parse failure, so macro/rates source degradation is visible before run-state partial status; the 2026-06-12 coverage baseline moved to `records_fallback=371`, `silent_fallback=42`, and `silent_handler=58`.
   - done: add local fallback telemetry for Operator API latency probe HTTP errors and request failures, so performance evidence gaps are visible in Health instead of only as failed probe rows; the 2026-06-12 coverage baseline moved to `records_fallback=373`, `silent_fallback=42`, and `silent_handler=56`.
   - done: add local fallback telemetry for corrupt cached Sharpely stock metadata JSON, so peer/fundamental context degradation is visible while the existing row-field fallback remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=374`, `silent_fallback=41`, and `silent_handler=56`.
   - done: add local fallback telemetry for corrupt API performance probe JSON and malformed slowlog/probe numeric fields, so performance triage evidence gaps are visible while report generation remains tolerant; the 2026-06-12 coverage baseline moved to `records_fallback=376`, `silent_fallback=39`, and `silent_handler=55`.
   - done: add local fallback telemetry for malformed `advisory_sync_state.state_json` payloads, so corrupt downloader/watcher state rows are visible in Health while `load_sync_state()` still falls back to an empty state; the 2026-06-12 coverage baseline moved to `records_fallback=377`, `silent_fallback=39`, and `silent_handler=54`.
   - done: add local fallback telemetry for malformed screener coverage JSON fields, so corrupt screener/action context payloads are visible while coverage metrics keep their safe empty-list/object fallback; the 2026-06-12 coverage baseline moved to `records_fallback=379`, `silent_fallback=37`, and `silent_handler=54`.
   - done: add local fallback telemetry for malformed event-policy evaluator raw context JSON, so policy calibration grouping degradation is visible while the evaluator keeps its existing empty-context fallback; the 2026-06-12 coverage baseline moved to `records_fallback=380`, `silent_fallback=36`, and `silent_handler=54`.
   - done: add local fallback telemetry for malformed event-policy promotion stored JSON, so review/decision payload degradation is visible while promotion review APIs keep their existing default fallback; the 2026-06-12 coverage baseline moved to `records_fallback=381`, `silent_fallback=35`, and `silent_handler=54`.
   - done: add local fallback telemetry for malformed signal-quality promotion stored JSON, so overlay-review payload degradation is visible while promotion review APIs keep their existing default fallback; the 2026-06-12 coverage baseline moved to `records_fallback=382`, `silent_fallback=34`, and `silent_handler=54`.
   - done: add local fallback telemetry for malformed technical-threshold promotion stored JSON, so threshold-review payload degradation is visible while promotion review APIs keep their existing default fallback; the 2026-06-12 coverage baseline moved to `records_fallback=383`, `silent_fallback=33`, and `silent_handler=54`.
   - done: add local fallback telemetry for malformed config-change assistant JSON, so reviewed config-preview/decision payload degradation is visible while preview APIs keep their existing default fallback; the 2026-06-12 coverage baseline moved to `records_fallback=384`, `silent_fallback=32`, and `silent_handler=54`.
   - done: add local fallback telemetry for malformed decision-trace stored JSON, so corrupt conflict-rule conditions are visible while trace/rule readers keep their existing default fallback; the 2026-06-12 coverage baseline moved to `records_fallback=385`, `silent_fallback=31`, and `silent_handler=54`.
   - done: add local fallback telemetry for malformed event-policy stored JSON context, so corrupt event tensors/source traces/affected-peer payloads are visible while policy readers keep their existing empty/default fallback; the 2026-06-12 coverage baseline moved to `records_fallback=386`, `silent_fallback=30`, and `silent_handler=54`.
   - done: add local fallback telemetry for malformed signal-refresh wait-signal evidence JSON, so corrupt incremental-refresh context is visible while refresh decisions keep their review-safe default fallback; the 2026-06-12 coverage baseline moved to `records_fallback=387`, `silent_fallback=29`, and `silent_handler=54`.
   - done: add local fallback telemetry for malformed materialized trace-summary cache JSON, so corrupt symbol/event explanation cache rows are visible while trace readers keep their live-fallback behavior; the 2026-06-12 coverage baseline moved to `records_fallback=388`, `silent_fallback=28`, and `silent_handler=54`.
   - done: add local fallback telemetry for malformed execution-engine broker/safety JSON context, so degraded broker handoff explainability is visible while execution remains safely blocked/defaulted; the 2026-06-12 coverage baseline moved to `records_fallback=389`, `silent_fallback=27`, and `silent_handler=54`.
   - done: add local fallback telemetry for malformed hypothesis/playbook stored JSON, so corrupt trigger, policy, match, or expected-effect payloads are visible while hypothesis scans keep their configured default fallback; the 2026-06-12 coverage baseline moved to `records_fallback=390`, `silent_fallback=26`, and `silent_handler=54`.
   - done: add local fallback telemetry for malformed LLM event-evaluator JSON context, so corrupt category/watch/document/event-tensor payloads are visible while prompt/classification code keeps its text fallback; the 2026-06-12 coverage baseline moved to `records_fallback=391`, `silent_fallback=25`, and `silent_handler=54`.
   - done: add local fallback telemetry for malformed advisory-pipeline JSON context, so feature-gate stage annotations are visibly degraded while keeping the existing empty-context fallback; the 2026-06-12 coverage baseline moved to `records_fallback=392`, `silent_fallback=24`, and `silent_handler=54`.
   - done: add local fallback telemetry for market-context numeric parse fallbacks, so malformed breadth/regime/context numeric inputs are visible while market-context builders keep their configured default fallback; the 2026-06-12 coverage baseline moved to `records_fallback=393`, `silent_fallback=23`, and `silent_handler=54`.
   - done: add local fallback telemetry for Operator API JSON byte-size, safe serialization, JSON-like parsing, and count parsing fallbacks, so degraded API payload telemetry/list pagination context is visible while existing response fallbacks remain unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=398`, `silent_fallback=19`, and `silent_handler=53`.
   - done: add local fallback telemetry for live-dashboard table-column lookup, JSON blob parse, and exit-rule parse fallbacks, so degraded static-dashboard/operator snapshot context is visible while existing empty/raw fallback behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=401`, `silent_fallback=16`, and `silent_handler=53`.
   - done: add local fallback telemetry for LLM event-evaluator scalar missing-check fallbacks, so malformed scalar context checks are visible while existing treat-as-present behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=402`, `silent_fallback=15`, and `silent_handler=53`.
   - done: add local fallback telemetry for risk-engine missing-value check fallbacks, so malformed allocation/risk scalar checks are visible while existing treat-as-present behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=403`, `silent_fallback=14`, and `silent_handler=53`.
   - done: add local fallback telemetry for research-ledger git revision lookup failures, so research/provenance rows with null `git_rev` have visible degradation evidence; the 2026-06-12 coverage baseline moved to `records_fallback=404`, `silent_fallback=13`, and `silent_handler=53`.
   - done: add local fallback telemetry for malformed Dhan auth CLI cached-expiry parsing, so broker-token freshness degradation is visible while existing stale/unknown fallback behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=405`, `silent_fallback=12`, and `silent_handler=53`.
   - done: add local fallback telemetry for Economic Times RSS pubDate parser fallback, so source timestamp-format drift is visible while pandas timestamp fallback remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=406`, `silent_fallback=11`, and `silent_handler=53`.
   - done: add local fallback telemetry for MOSPI CPI group-code parse fallback, so malformed CPI group labels are visible while existing default group code `0.0` remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=407`, `silent_fallback=10`, and `silent_handler=53`.
   - done: add local fallback telemetry for Screener.in integer/float parse fallbacks, so malformed numeric-looking screener values are visible while existing keep-original-text behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=409`, `silent_fallback=8`, and `silent_handler=53`.
   - done: add local fallback telemetry for analysis-agent post-check timeouts and cron-preflight Python resolver failures, so automation/preflight degradation is visible while existing stop/error behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=411`, `silent_fallback=6`, and `silent_handler=53`.
   - done: add local fallback telemetry for URL and JSON redaction fallback paths, so security-sanitization degradation is visible while existing redacted/plain-text fallback behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=413`, `silent_fallback=4`, and `silent_handler=53`.
   - done: add local fallback telemetry for syntax-analyzer parse failures in QA scanner scripts, env-audit Unicode decode skips, and corrupt DB retry telemetry spool lines; the 2026-06-12 coverage baseline moved to `records_fallback=417`, zero `silent_fallback` rows, and `silent_handler=53`.
   - done: add local fallback telemetry for Codex structured-output validation retries and direct-JSON parse fallback, so LLM/Codex planning degradation is visible while existing retry/extraction behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=419`, zero `silent_fallback` rows, and `silent_handler=51`.
   - done: add local fallback telemetry for action-recommender context missingness fallbacks, action-conflict serialization missingness fallback, and execution-engine JSON/text missingness fallbacks, so decision/execution explanation degradation is visible while existing fail-soft behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=425`, zero `silent_fallback` rows, and `silent_handler=45`.
   - done: add local fallback telemetry for Manual Review, Wait Signals, Identity Issues, Signal Refresh, Event Policy, Event Policy Evaluator, and Event Policy Promotion normalization fallbacks, so operator-state and event-policy interpretation degradation is visible while existing fail-soft behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=433`, zero `silent_fallback` rows, and `silent_handler=37`.
   - done: add local fallback telemetry for Operator API pagination/text/bool/JSON-ready fallbacks, module app initialization failure, Company Memory normalization fallbacks, Signal Quality evaluator normalization fallbacks, and Signal Quality Promotion JSON missingness fallback, so UI payload and research-evidence degradation is visible while existing fail-soft behavior remains unchanged; the 2026-06-12 coverage baseline moved to `records_fallback=446`, zero `silent_fallback` rows, and `silent_handler=24`.
   - done: add local fallback telemetry for config-change assistant JSON fallbacks, technical-threshold promotion JSON fallbacks, advisory-pipeline feature-gate JSON fallbacks, risk-engine allocation-context JSON fallbacks, NSDL FPI year-inference date parse fallbacks, and RBI FBIL G-sec multi-format date parse fallbacks; the 2026-06-12 coverage baseline moved to `records_fallback=453`, zero `silent_fallback` rows, zero `logs_then_falls_through` rows, and `silent_handler=17`.
   - done: add local fallback telemetry for cron-status invalid step fallback, event-data-quality/event-evidence/hypothesis/operator-health/operator-smoke serialization fallbacks, announcement S3 pointer validation failures, shared date/display formatting fallbacks, maintenance-tool JSON/path fallbacks, and transcription temp-file cleanup misses; the 2026-06-12 coverage baseline moved to `records_fallback=468`, zero `silent_fallback` rows, zero `logs_then_falls_through` rows, and `silent_handler=2`.
   - next: keep the two fallback-telemetry self-protection `silent_handler` rows intentionally uninstrumented unless a non-recursive reporting design is added.
   - done: route operator current-price cache query fallback into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route risk ADV20 liquidity-cap fallback into aggregated local fallback telemetry and a source-specific Operator Health fix hint
   - done: route risk macro/exchange context lookup failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route signal-refresh source-context lookup failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route action conflict-rule lookup/load failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route wait-signal source-evidence lookup/schema/load failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route feature-freshness input lookup failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route company-memory source-context lookup/load failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route news-theme as-of and market-news load failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route event-policy evaluator schema/source-load failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route event-data-quality checker lookup/readiness failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route trace-summary cache lookup/rebuild failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route compact event-evidence lookup/source-query failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route event meta-model required-source and optional-context failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route exchange-feature table/date/event load failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route market-context source/cache lookup failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route fundamental-snapshot universe/peer/date/statement/shareholding source failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route TS forecast feature/evaluator OHLCV/forecast/price source failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route watchlist-builder event-transition/existing-state source failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route symbol-trace table/stage/screener/rejection/event source failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route setup-trace table/asof/regime/overlay/screener/stage source failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route technical-threshold calibration schema/signal/price source failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route signal-quality evaluator schema/event/bhavcopy/company-memory source failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route technical-threshold, signal-quality, and event-policy promotion-review evidence lookup failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route event-model promotion-check label-coverage and score-freshness evidence failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route macro-feature table/source-row failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route regime-engine benchmark/macro table and source-row failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route technical-feature universe/OHLCV/benchmark/sector/peer source failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route intraday-feature universe/coverage/Dhan-sync/history/daily-reference source failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route rule-engine date/regime/overlay/screener/technical/intraday/fundamental source failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route portfolio-engine allocation and overlap-metadata source failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route position-lifecycle open-order/price-identity/price-history/technical-context source failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route execution-engine broker-account/reconciliation-target/live-submit/broker-reconcile failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route operator-snapshot load/section-load/price-cache refresh failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route event-router source-row/watchlist-priority/symbol-refresh failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route external-task-queue status-load/worker-task failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route config-change assistant approved-decision/preview-history lookup failures into local fallback telemetry and a source-specific Operator Health fix hint
   - done: route research-ledger start/finish/list failures into local fallback telemetry and a source-specific Operator Health fix hint
   - next: audit remaining source-specific fallback query paths outside shared DB helpers and route them to fallback telemetry or explicit Health payloads
   - keep cron/frontend logs visible from the operator app without adding write/trading controls

## Next Most Important Tasks

1. Enforce feature freshness and dependencies.
   - Define stage-level required inputs and allowed staleness.
   - Done: `actions` has an explicit dependency gate for `daily_ohlcv` and `technical_daily`, and pipeline output includes the gate result.
   - Done: `rules`, `risk`, `portfolio`, and `lifecycle` now emit explicit feature-gate summaries in pipeline output.
   - Done: positive broker-capable final actions are downgraded to Manual Review when required decision-time inputs are blocked.
   - Done: blocked `risk` positive allocations become `review_manual` with zero allocation, and blocked `portfolio` positive plans become `deferred` with zero approved capital.
   - Done: blocked `rules` `PASS_NOW` candidates become `WATCH_EVENT`, and blocked `lifecycle` outputs receive freshness warnings while preserving exits.
   - Keep current and decision-time freshness panels as the operator explanation layer.

2. Standardize ingestion run state.
   - done: central `data.download_runner` now attaches normalized run-state metadata to every module run and persists latest runner state in `advisory_sync_state`.
   - done: runner-level classification distinguishes `ok`, `no_data`, `source_unavailable`, `auth_unavailable`, `parse_failed`, `failed`, and `skipped`.
   - done: runner supports module-exported `STOCKEY_RUN_STATE` / `RUN_STATE` / `RUN_RESULT` dictionaries and merges exact row/window metadata into persisted state.
   - done: `advisory.event_evidence_store` exports compact evidence rows, rows written/read, bhavcopy/announcement row counts, date window, and dry-run flag.
   - done: `data.dhanlive.ohlcv` exports total rows, daily/intraday rows, date/timestamp windows, failed symbols, symbol count, interval, exchange, and asset type.
   - done: `data.dhanlive.scrip_master` exports rows read/written, column count, source file, load timestamp, and fallback flag.
   - done: `data.sharpelydata.scrip_master` exports filtered fund/equity master counts, raw row counts, instrument type count, load timestamp, and fallback flag.
   - done: `data.sharpelydata.sharpely_data` exports symbol/date-window counts plus meta, peer, statement, shareholding, and historical market-cap row counts.
   - done: Screener registered and ad-hoc query CLIs export row/query/screener counts plus no-data state for the central runner.
   - done: Screener ad hoc queries validate known DMA moving-average syntax locally before opening Chrome/network.
   - done: Screener fetch/parse failures persist to `screenerin_parse_failures` with sanitized URL/query/body excerpts and HTML context flags before re-raising.
   - done: Operator Health surfaces recent Screener query/fetch/parse failures in fix hints, the Advisory Trust Gate, and Fallbacks & Degradation so they are treated as operational issues before they create confusing downstream symptoms.
   - done: add the Nuxt `/screeners` workbench plus `/api/screeners/preview` so operators can validate Screener.in query syntax locally and fetch bounded authenticated preview rows with `persist=false` before registering a production screener.
   - done: add read-only screener coverage metrics in `advisory.screener_coverage`, `/api/screeners/coverage`, and the `/screeners` workbench to show constituent, candidate, final-action, positive-action, manual-review, and exit attribution by screener.
   - done: NSE bhavcopy and indices parsers export file-level parse state with date windows, skipped/already-processed/empty/failed counts, and failure samples.
   - done: NSE bhavcopy parser distinguishes `empty_valid_source`, `bad_file_retryable`, `schema_changed`, and `parser_bug`; empty valid archives advance state without retry loops, while bad/corrupt files remain failed and retryable.
   - done: NSE indices parser now uses the same parser-state contract as bhavcopy: empty/no-row archives advance as `empty_valid_source`, and bad/schema/parser failures remain failed with explicit classification.
   - done: NSE off-market parser now persists failed ingestion state with explicit parser classification and treats valid zero-row files as `empty_valid_source`.
   - done: `scripts/ingestion_state_runner.py summary` now reports ingestion state counts by status, source, and parser failure classification so operators can distinguish retryable bad files, schema drift, parser bugs, and valid empty files without manual SQL.
   - done: `/api/operations/ingestion-state` and the Nuxt Health page now expose the same file-level ingestion state summary with read-only cleanup guidance.
   - done: add a shared regression contract for bhavcopy, indices, and off-market parser failure classifications so parser-state rows keep using the machine-readable `classification=` vocabulary.
   - done: NSE sparse event crawlers for corporate actions, earnings events, insider deals, and recent event calendar export symbol/date-window rows plus queried/skipped counts.
   - done: surface latest standardized `download_runner:*` rows in Operator Health/Data Health, fix hints, and degradation feed so old log-only failures do not become investment Manual Review noise.
   - done: central runner now preserves explicit module `state_advanced` and normalizes retry, attempt, failed-attempt, fallback, source-unavailable, and no-data counters for Health/Data Health.
   - done: central runner now imports modules and calls `main()` directly when available, preserving exported `STOCKEY_RUN_STATE` even when modules end with `SystemExit`, including non-zero exits.
   - done: NSE off-market downloader exports block/date attempts, retries, failed attempts, skipped blocks, downloaded blocks, and source-unavailable counts.
   - done: NSE bhavcopy and indices archive downloaders export candidate/missing/downloaded date counts, failed attempts, source-unavailable counts, and consecutive-failure stop flags.
   - done: WPI exports year/item counts, retry counts, failed attempts, failed item samples, and source-unavailable counts for the central runner.
   - done: CPI exports month counts, retry counts, failed attempts, failed month samples, and source-unavailable counts for the central runner.
   - done: NSDL FPI exports monthly catch-up counts, downloaded/skipped/failed month counts, failed month samples, and source-unavailable counts for the central runner.
   - done: RBI FBIL G-sec exports date counts, downloaded/skipped-weekend/failed date counts, failed date samples, and source-unavailable counts for the central runner.
   - done: RBI bank rates exports browser/CDP source-unavailable state plus read/write row counts for the central runner.
   - done: Economic Times RSS exports feed-level success, empty-feed, failed-feed, and source-unavailable counters for the central runner.
   - done: FRED/ISM macro exports series/leg-level row counts, fallback counts, failed series/legs, and source-unavailable counters for the central runner.
   - done: NSE holiday-calendar downloader exports browser/CDP source-unavailable state plus row/product counts for the central runner.
   - done: canonical benchmark sync exports symbol-level row, no-data, dry-run, and failure counters for the central runner.
   - done: NSE off-market parser exports file-level skipped/parsed/failed counts and failure samples for the central runner.
   - done: company master exports identity row and identifier coverage counts for the central runner.
   - done: company-master lookup failures now emit source-specific local fallback telemetry and still re-raise, so identity lookup outages are visible without silently using partial data.
   - done: every module configured in `data.download_runner.DOWNLOAD_STEPS` and `PARSER_STEPS` now exports standardized runner state.
   - next: refine deeper per-source semantics, especially stricter no-data/source-unavailable handling and source-specific session reset counters where useful.

3. Add schema migration/version tracking.
   - done: add `utils.schema_migrations` with `stockey_schema_migrations`, checksum-protected idempotent apply, durable failure recording, dry-run, list, and ensure-table CLI modes.
   - done: add regression tests for dry-run, already-applied skip, checksum mismatch, successful apply, and failure recording.
   - done: surface missing, running, and failed schema migration registry rows in Operator Health, fix hints, current blockers, and the existing Data Health UI payload.
   - done: route Dhan daily/intraday OHLCV table setup through the named `20260611_dhan_ohlcv_base_asset_type` migration.
   - done: route Dhan scrip master table/index setup through the named `20260611_dhan_scrip_master_base` migration while keeping dynamic broker-column sync.
   - done: route consolidated action recommendations through the named `20260611_advisory_action_recommendations_base` migration.
   - done: route decision trace, event-processing, action-conflict, and action-conflict-rule audit tables through the named `20260611_advisory_decision_trace_base` migration.
   - done: route event-evaluation and event-risk output tables through the named `20260611_advisory_event_evaluation_outputs_base` migration.
   - done: route portfolio-order table setup through the named `20260611_advisory_portfolio_orders_base` migration.
   - done: route lifecycle and rebalance action tables through the named `20260611_advisory_position_lifecycle_base` migration.
   - done: route incremental signal-refresh action table setup through the named `20260611_advisory_signal_refresh_actions_base` migration.
   - done: route shared sync-state cursor/status table setup through the named `20260611_advisory_sync_state_base` migration.
   - done: route continuous-watch live alert table setup through the named `20260611_advisory_live_watch_alerts_base` migration.
   - done: route file-level ingestion state table setup through the named `20260611_ingestion_file_state_base` migration.
   - done: route NSE security identity history/review table setup through the named `20260611_nse_security_history_base` migration.
   - done: route market regime snapshot table setup through the named `20260611_advisory_market_regime_base` migration.
   - done: route research ledger table setup through the named `20260611_advisory_research_runs_base` migration.
   - done: route market news overlay output table setup through the named `20260611_advisory_market_overlay_daily_base` migration.
   - done: route TS forecast evaluation and summary table setup through the named `20260611_advisory_ts_forecast_evaluator_base` migration.
   - done: route event-policy evaluation and summary table setup through the named `20260611_advisory_event_policy_evaluator_base` migration.
   - done: route event-policy promotion review/decision tables through the named `20260611_advisory_event_policy_promotion_base` migration.
   - done: route technical-threshold calibration evaluation and summary table setup through the named `20260611_advisory_technical_threshold_calibration_base` migration.
   - done: route signal-quality evaluation and summary table setup through the named `20260611_advisory_signal_quality_evaluator_base` migration.
   - done: route identity issue tracking table setup through the named `20260611_advisory_identity_issues_base` migration.
   - done: route fallback/degraded-path telemetry table setup through the named `20260611_advisory_fallback_telemetry_base` migration.
   - done: route serialized external task queue table setup through the named `20260611_advisory_external_task_queue_base` migration.
   - done: route compact operator current-price cache table setup through the named `20260611_advisory_current_prices_base` migration.
   - done: route wait-signal and wait-signal-match table setup through the named `20260611_advisory_wait_signals_base` migration.
   - done: route live event-router action table setup through the named `20260611_advisory_event_router_actions_base` migration.
   - done: route materialized operator trace-summary cache setup through the named `20260611_advisory_trace_summaries_base` migration.
   - done: route execution order and fill handoff table setup through the named `20260611_advisory_execution_orders_base` migration.
   - done: route review-only company-memory review table setup through the named `20260611_advisory_company_memory_reviews_base` migration.
   - done: route rule-engine candidate/rejection output table setup through the named `20260611_advisory_rule_outputs_base` migration.
   - done: route event-policy action overlay table setup through the named `20260611_advisory_event_policy_actions_base` migration.
   - done: route risk allocation table setup through the named `20260611_advisory_allocations_base` migration.
   - done: route adversarial event-review table setup through the named `20260611_advisory_event_reviews_base` migration.
   - done: route announcement watchlist/event output table setup through the named `20260611_advisory_announcement_watch_outputs_base` migration.
   - done: route news watch event output table setup through the named `20260611_advisory_news_events_base` migration.
   - done: route config-change preview table setup through the named `20260611_advisory_config_change_previews_base` migration.
   - done: route intraday feature cache table and supporting Dhan OHLCV indexes through the named `20260611_advisory_intraday_features_base` migration.
   - done: route watchlist builder output table setup through the named `20260611_advisory_watchlist_base` migration.
   - done: route operator API error, command-run, and manual-review decision audit tables through the named `20260611_advisory_operator_api_audit_base` migration.
   - done: route technical-threshold promotion review/decision tables through the named `20260611_advisory_technical_threshold_promotion_base` migration.
   - done: route signal-quality promotion review/decision tables through the named `20260611_advisory_signal_quality_promotion_base` migration.
   - done: route Screener.in registered screener registry/snapshot tables through the named `20260611_screenerin_registered_screeners_base` migration.
   - done: route Screener.in ad hoc query run/result tables through the named `20260611_screenerin_ad_hoc_query_base` migration.
   - done: route Screener.in parse/fetch failure audit table through the named `20260611_screenerin_parse_failures_base` migration.
   - done: route Economic Times RSS item table setup through the named `20260611_economictimes_rss_items_base` migration.
   - done: route hypothesis/playbook, match, action-plan, and promotion-audit tables through the named `20260611_advisory_hypothesis_engine_base` migration.
   - done: route event meta-model score output table setup through the named `20260611_advisory_event_meta_model_scores_base` migration.
   - done: route news theme screener mapping table setup through the named `20260611_advisory_news_theme_screeners_base` migration.
   - done: route TS forecast feature table setup through the named `20260611_advisory_ts_forecast_features_base` migration.
   - done: route TS forecast workflow watchlist table setup through the named `20260611_advisory_ts_forecast_workflow_base` migration.
   - done: route announcement text S3 offload metadata columns through the named `20260611_announcement_text_offload_storage_columns` migration.
   - done: route dynamic optional-table `company_master_id` backfill columns through per-table named migrations.
   - note: remaining direct DDL is limited to generic `utils.db` create/alter helpers and source-specific temporary/staging tables.

4. Done: implement hot/cold retention for intraday and trace rows.
   - done: keep recent rows in hot Postgres tables via configurable `TRACE_RETENTION_DAYS` and `INTRADAY_RETENTION_DAYS`.
   - done: archive old rows to S3-compatible storage with `scripts/hot_table_retention.py --archive-s3`.
   - done: add retention reports before delete/archive; deletes are blocked unless S3 archive is requested or `--allow-delete-without-archive` is explicitly supplied.

5. Continue UI-first operations.
   - Finish remaining operator flows for S3 artifact inspection, research-ledger review, and safe reviewed-config application.
   - Keep broker execution disabled until approval/reconciliation workflows are proven separately.

### 0A. Build a proper Nuxt operator app

Problem:

- The deprecated static dashboard was useful, but it is not enough for debugging a live advisory system.
- It shows final state more than process state.
- It is hard to answer:
  - what happened when this news or announcement arrived?
  - did OCR/transcription/summarization/classification run?
  - what did the event evaluator extract?
  - what rules, model features, technical state, regime, and reviewer checks changed?
  - why did the system recommend buy, sell, hold, watch, or manual review?
  - whether duplicate screeners or conflicting theses were consolidated correctly

Target:

- create a first-class operator frontend in `apps/operator-web`
- stack:
  - Nuxt 3
  - Vue 3
  - TypeScript
  - Tailwind CSS
  - Pinia for client state
  - TanStack Query or Nuxt data fetching for API caching
  - Charting: ECharts or lightweight-charts for price, forecast, and regime views
  - Table/grid: TanStack Table or AG Grid community if needed
- keep the backend as Python-owned initially; the Nuxt app should consume JSON/API endpoints rather than reimplementing domain logic
- static `live_dashboard/` generation is deprecated; the Nuxt app is now the operator UI target

Core pages:

- Overview
  - current portfolio
  - today's recommendations
  - action queue at the top
  - current watchlist
  - exited recommendations
  - TS Watch / TimesFM research cards
  - live alerts
- Event Inbox
  - all latest news and announcements
  - processing status for each item: downloaded, OCR/transcribed, summarized, categorized, parsed, evaluated, reviewed, routed
  - raw source links and stored document artifacts
  - event tensor output
  - investor playbook matches
  - event-class policy interpretation
  - final action impact
- Decision Trace
  - symbol-level timeline of recommendations, action changes, watch updates, exits, and live alerts
  - one expandable trace per decision showing:
    - screener provenance
    - technical state
    - regime/macro context
    - exchange-event context
    - news/announcement context
    - event model score
    - adversarial review
    - risk sizing
    - lifecycle/exit policy
    - consolidated action winner and losers
- Investor Playbooks
  - manage investor playbooks as versioned reliability overlays
  - link each playbook to observable event patterns
  - run historical scans over news/announcements/macro data
  - generate action plans with checks, urgency, safe action boundary, and trusted-overlay permission
  - feed only trusted overlays into review/risk-overlay candidates
- Research Ledger
  - list experiments
  - show configs, date ranges, validation protocol, result metrics, and data snapshots
  - compare runs side by side
- Data Health
  - ingestion freshness by source
  - failed announcements/OCR/summarization/LLM/Codex runs
  - stale Dhan/OHLCV symbols
  - playbook/action-plan freshness
  - optional event-model label coverage for research-only runs
  - Redis/Postgres/cron status
- Admin / Runbook
  - show operator commands
  - trigger safe dry-run jobs where appropriate
  - show cron schedule and last run logs

Backend/API target:

- add a small Python API service, likely FastAPI, under `advisory/api` or `server/api`
- initial endpoints should be mostly read-only, with explicit operator/audit write controls:
  - `/api/summary`
  - `/api/actions`
  - `/api/portfolio`
  - `/api/watchlist`
  - `/api/events`
  - `/api/events/{unique_id}/trace`
  - `/api/symbols/{symbol}/trace`
  - `/api/hypotheses`
  - `/api/hypotheses/{id}/backtests`
  - `/api/research-runs`
  - `/api/data-health`
- expose Server-Sent Events or WebSocket later for live watcher updates
- keep write endpoints gated and explicit; no accidental live trading from the frontend

Implementation slices:

1. Done: add `docs/operator_app_prd.md` with UX, data contracts, and safety boundaries.
2. Done: scaffold `apps/operator-web` with Nuxt 3, Vue 3, TypeScript, Tailwind, Pinia, and starter pages.
3. Done: add FastAPI operator service with read endpoints plus controlled operator/audit write endpoints.
4. Done: reuse the existing `advisory.live_dashboard` payload builder behind an API-compatible JSON contract so the Nuxt app can reuse current data quickly.
5. Started: build the Overview page first with better hierarchy and filtering; it now shows the action queue and symbol trace loading, but trace rendering is still raw JSON.
6. Started: build Event Inbox and Decision Trace as readable stage/timeline pages; these are the main debugging gaps.
7. Started: richer Data Health now has fix hints, cron latest-run parsing, recovered/manual-interrupt handling, and status filters. Add frontend views for TS Watch and research ledger next.
8. Add SSE/WebSocket updates from Redis pub-sub once the operator app is stable.
9. Done: remove static `live_dashboard/` generation from cron/watch paths; serve operator state through API + Nuxt.
10. Done: add a single operator smoke-test command that validates API, DB reads, Node/npm, and Nuxt dependency health.
11. Done: add read-only `/api/operator-journey` plus frontend API typing/client method for issue -> decision -> wait signal -> match -> signal refresh -> action -> portfolio/execution timeline data.
12. Done: add the Nuxt Operator Journey page with filters, stage buckets, skipped sources, read-only safety boundary, and newest-first timeline backed by `/api/operator-journey`.

Do not:

- put trading submit buttons in V1
- make the frontend compute investment decisions
- let the frontend mutate production hypotheses or setup rules without versioning and validation
- hide failed OCR/LLM/Codex/event-router steps

### 0B. Add event decision traces and explainability tables

Problem:

- The pipeline has many useful intermediate outputs, but they are not tied together into one operator-visible trace.
- Debugging currently requires reading several tables and logs manually.

Target:

- create a durable decision trace layer that records every meaningful state transition and evidence bundle.
- every news/announcement that touches a watched or invested symbol should produce a trace record, even if the final action is `NO_CHANGE`.

Suggested tables:

- `advisory_event_processing_runs`
  - `unique_id`, `symbol`, `source_type`, `stage`, `status`, `started_at`, `completed_at`, `error`, `input_hash`, `output_hash`
- `advisory_decision_traces`
  - `trace_id`, `asof_date`, `symbol`, `unique_id`, `trigger_type`, `previous_action`, `new_action`, `action_changed`, `final_action`, `final_reason`
- `advisory_decision_trace_steps`
  - one row per stage:
    - source ingest
    - OCR/transcription
    - summary
    - categorization
    - structured extraction
    - event evaluation
    - hypothesis match
    - adversarial review
    - rule engine
    - technical state
    - risk sizing
    - lifecycle
    - action consolidation
- `advisory_action_conflicts`
  - losing action candidates per symbol/date with reason they lost to the winner

Trace payload should include:

- input row identifiers
- model names and prompt versions
- rule IDs and threshold values
- key feature values before/after
- reason text fit for UI display
- raw JSON payload path or compact JSON snapshot
- error details, retries, and fallback behavior

Implementation slices:

1. Done: add trace writer utility with append-only semantics.
2. Done: trace event ingest, event evaluation, adversarial review, lifecycle, rebalance actions, and final action consolidation.
3. Done: add `python -m advisory.symbol_trace` support for symbol-level timelines.
4. Done: add API endpoint `/api/events/{unique_id}/trace`.
5. Started: render traces in the Nuxt Event Inbox.
6. Done: add symbol-level action timeline endpoint so action consolidation traces without `unique_id` are visible from portfolio/action views.
7. Started: render symbol traces from the Nuxt Overview action and recommendation cards.
8. Done: normalize trace API responses into stage summaries so the frontend does not show raw JSON blobs by default.
9. Started: add domain-specific trace cards for technical state, event tensor, adversarial review, lifecycle, action consolidation, and execution eligibility.
10. Done: trace risk sizing, portfolio allocation, macro context, and exchange-event context so the UI can explain sizing and vetoes end to end.
11. Done: trace execution-order planning, live-safety checks, submission, reconciliation, broker ids/status, and skipped/blocked execution reasons.
12. Done: add frontend trace filters by domain, status, problems, execution blockers, action changes, and event-driven changes.
13. Done: persist trace filters in the URL and add deep links to specific trace decisions.
14. Done: create playbook action-plan framework over investor reliability overlays.
15. Done: wire trusted playbook action plans into action consolidation as review/risk-overlay candidates.

### 0B.1. Make every recommendation explainable

Problem:

- A stock can enter the advisory flow from screeners, events, technicals, playbooks, watchlists, or lifecycle state.
- If the final recommendation does not say why it was screened and selected, the operator cannot trust it.
- A recommendation without a clear reason chain should not appear as a clean investable idea.

Target:

- every final action row must carry a complete reason contract
- the reason contract should be readable by the frontend and auditable through traces
- missing reason fields should downgrade the action to `MANUAL_REVIEW`

Suggested `recommendation_reason_json` fields:

- `screened_by`
  - setup id
  - screener name/query
  - screener date
  - setup state before policy
- `selection_reason`
  - concise reason the name survived screening
  - key fundamental/technical/event features
  - whether it is a new entry, watch continuation, add-on, hold, reduce, or exit
- `event_reason`
  - event ids
  - event class
  - what happened
  - expected impact
  - materiality/confidence
- `playbook_reason`
  - matched playbook ids
  - matched evidence
  - action-plan checks
  - production-allowed flag
- `technical_reason`
  - technical state
  - score
  - trigger archetype
  - pivot/support/stop/invalidation
- `macro_reason`
  - regime
  - macro risk state
  - whether macro amplified, neutralized, or blocked the idea
- `risk_reason`
  - position-size logic
  - liquidity constraints
  - sector/exposure caps
  - abstain/manual-review reasons
- `action_consolidation`
  - winning action
  - losing actions
  - why winner won

Implementation slices:

1. Done: add reason-contract builder utility in advisory code.
2. Done: enrich final action reason contracts from candidates, portfolio/lifecycle candidates, event evaluations, playbook action plans, and macro/regime context.
3. Done: enforce reason completeness before a broker-action row can remain `BUY`, `BUY_MORE`, `SELL`, or `PARTIAL_SELL`; incomplete rows are downgraded to `MANUAL_REVIEW`.
4. Done: show a compact reason summary and expandable full reason contract in the Nuxt Overview and Decision Trace pages.
5. Done: add regression tests that assert clean broker actions cannot ship without a reason contract.
6. Done: add execution-layer defense so stale action rows with missing/incomplete contracts or unresolved Dhan identity are blocked before broker handoff.

Do not:

- allow generic reasons such as “score passed” without feature/evidence detail
- let LLM-generated prose replace persisted deterministic fields
- hide rejected/losing reasons when multiple strategies disagree

### 0B.2. Track top 50% market context

Problem:

- The current pipeline focuses mostly on watched/recommended symbols.
- Many useful signals come from broader market leaders: announcement clusters, sector rotation, macro sensitivity, breadth, and leadership breakdowns.
- The system needs market context without trying to trade the entire market.

Target:

- maintain a daily “top 50% market” universe for context
- track news, announcements, exchange events, technical leadership, and sector movement for that universe
- summarize this into market/sector context that informs playbook/event interpretation and risk policy

Initial definition:

- start from the investable equity universe with valid Dhan identity and recent OHLCV
- rank by configurable blend of:
  - latest market cap when available
  - 20-day average traded value
  - liquidity/spread filters
- keep the top 50% by rank percentile
- persist membership point-in-time

Suggested tables:

- `advisory_market_context_universe`
  - `asof_date`
  - `symbol`
  - `rank_pct`
  - `market_cap`
  - `avg_traded_value_20d`
  - `sector`
  - `included_reason`
- `advisory_market_context_events`
  - mapped announcements/news/exchange events for top-50% names
- `advisory_market_context_daily`
  - sector breadth
  - event clusters
  - leadership rotation
  - macro-risk interpretation
  - risk-on/risk-off summary

Implementation slices:

1. Done: build `advisory.market_context` to persist top-50% membership and summary rows daily.
2. Done: extend announcement/news watchers to also ingest top-50% market-context names with lower priority than active watchlist names.
3. Done: build daily context features: sector breadth, leader breakdowns, event clusters, exchange-event context, and macro/regime summary.
4. Done: feed market context into event/playbook action planning as background evidence.
5. Done: show “Market Context: Top 50%” in the operator frontend.
6. Done: add a lower-priority watcher queue for top-context names so event coverage improves without evaluating every broad-market event with LLMs.
7. Done: add regression checks that positive playbook actions remain non-executable in weak/risk-off broad-market context.
8. Done: tune deterministic materiality keywords and add dashboard counts for `triggered` versus `context_observed` top-context events.

Do not:

- treat top-50% membership as a buy signal
- run full LLM evaluation on every top-50% event without prioritization
- let broad context override direct stock-level vetoes, liquidity rules, or exits

### 0C. Build an investor playbook action framework

Problem:

- There are many qualitative market playbooks from experienced investors.
- They should not be hardcoded directly into trades or treated as statistically validated because the data is sparse and high-dimensional.
- Example: if the Prime Minister or top authority calls for austerity, the market may fall and the system should go cash or short.

Target:

- represent playbooks as versioned operator rules
- scan news, announcements, macro releases, policy events, and market context for hypothesis triggers
- generate an action plan that decides what else to check and how to act safely
- use `trusted_overlay` status only to allow review/risk-overlay candidates, never direct automatic trades

Playbook object fields:

- `hypothesis_id`
- `title`
- `description`
- `source`
- `status`: `draft`, `active_review`, `trusted_overlay`, `retired`
- `trigger_scope`: market, sector, symbol, portfolio
- `trigger_patterns`
  - keywords
  - entity types
  - authority/person/role
  - source reliability
  - contradiction filters
  - required evidence count
- `expected_effect`
  - index down
  - sector down/up
  - volatility up
  - liquidity stress
  - go cash
  - short candidate
  - reduce exposure
- `holding_window`
- `test_universe`
- `validation_protocol`
- `reliability_rule`

Suggested tables/config:

- `config/hypotheses.yaml` for authored hypotheses
- `advisory_hypothesis_matches` for point-in-time trigger matches
- `advisory_playbook_action_plans` for LLM/deterministic action plans
- `advisory_action_recommendations` receives trusted-overlay playbook action plans as non-executable review/risk-overlay candidates

Implementation slices:

1. Done: add `docs/hypothesis_lab.md` with schema and examples.
2. Done: add `config/hypotheses.yaml` with the first 5-10 starter playbooks, including the austerity example.
3. Done: add `advisory/hypothesis_engine.py` to save hypotheses and match them against persisted news/announcements.
4. Done: persist hypotheses and matches in `advisory_hypotheses` and `advisory_hypothesis_matches`.
5. Done: add `advisory_playbook_action_plans` and generate action plans with Codex/fallback.
6. Done: surface playbook creation, scans, matches, and latest action plans in the Nuxt Playbooks page.
7. Done: bridge trusted-overlay action plans into `advisory.action_recommender` as review/risk-overlay candidates.
8. Only when status is `trusted_overlay`, allow use as:
   - market overlay
   - sector risk cap
   - portfolio de-risking flag
   - manual-review reason

Do not:

- treat a hypothesis match as automatic proof
- let one headline trigger portfolio liquidation without confirmation and validation
- enable shorting in production until separate broker/risk controls exist

### 0. Add an experimental time-series forecast layer

Problem:

- Dhan OHLCV is now available, but the system does not yet evaluate whether time-series foundation models add incremental predictive value.
- Models such as Google TimesFM, Amazon Chronos, and Salesforce Moirai may be useful, but they should not directly create trades without proof.
- OHLCV-only forecasts are noisy, so this must start as a research/paper layer rather than another live action source.

Target:

- create a separate forecast feature table, `advisory_ts_forecasts_daily`
- feed existing Dhan daily OHLCV into forecast adapters
- forecast multiple horizons, initially `5d`, `10d`, and `20d`
- store forecast outputs as features:
  - expected return
  - forecast price
  - downside / upside quantiles
  - probability of positive return
  - signal quality
  - explicit experimental action hint
- compare against simple baselines before trusting any foundation model

Design rules:

- do not let TimesFM / Chronos / Moirai directly issue `BUY` or `SELL`
- keep outputs out of Dhan execution until paper results are validated
- forecast returns or relative returns where possible, not just raw price levels
- use walk-forward validation with costs and slippage
- compare against naive momentum, technical engine, and XGBoost/event meta-model baselines
- preserve point-in-time discipline and never train/evaluate with future OHLCV leakage

Integration path:

- `advisory.ts_forecast_features` builds forecast features from `dhan_ohlcv_daily`
- event/risk/meta-model layers may later consume forecast features as one input
- dashboard shows TS forecast context under an experimental, non-execution section
- action recommender may consume TS forecasts only after paper validation shows incremental value

Implementation slices:

1. Done: add `advisory/ts_forecast_features.py` with a dependency-free `naive_momentum_v1` baseline adapter.
2. Done: add `advisory_ts_forecasts_daily` persistence contract.
3. Done: add regression coverage for experimental forecast row generation.
4. Done: add optional TimesFM adapter behind the same output schema.
5. Done: add `advisory/ts_forecast_evaluator.py` for matured forecast evaluation against future Dhan OHLCV returns after costs.
6. Done: add `advisory/ts_forecast_workflow.py` for Screener.in selection, Dhan OHLCV refresh, TimesFM forecast generation, and experimental TS watchlist persistence.
7. Add optional Chronos / Moirai adapters behind the same output schema.
8. Done: add `advisory/ts_forecast_paper_portfolio.py` for forecast-only `PAPER_BUY` / `PAPER_SKIP` decisions, matured after-cost paper outcomes, naive-momentum baseline comparison, and current advisory-action alignment.
9. Done: add optional research-ledger entries for TS forecast paper-portfolio runs via `--log-research-ledger`.
10. Done: surface forecast context in the dashboard as experimental, non-execution evidence.
11. Done: surface TS forecast paper-portfolio summaries on the Operator home page and legacy dashboard as research-only evidence.
12. Done: define read-only TS forecast promotion gates for minimum matured paper rows, after-cost win/return thresholds, baseline outperformance, advisory-conflict limits, breadth, and manual-review-only authority.
13. Done: add manual-only `advisory.ts_forecast_promotion` review/decision audit rows and copyable TS forecast review-rule guidance when the read-only gate returns `review_candidate`.
14. Done: add reviewed config-change preview support for approved TS forecast promotion decisions; the preview emits disabled `ts_forecast_review_rules` guidance and still does not alter live policy.
15. Done: add config validation/visibility for manually applied disabled `ts_forecast_review_rules`; `/api/research/ts-forecast-review-rules` and the Operator home TS section now show loaded rules, issues, and no-broker/no-auto-policy boundaries.
16. Next: add an operator-reviewed application workflow for approved config previews, still disabled-by-default, before any low-weight consumption.
17. Only after validation and reviewed config application, add TS forecast features to the event meta-model / risk model as low-weight inputs.

## Active roadmap

### 1. Deferred: event-model dataset training

Priority:

- keep the code available for research-only experiments
- do not make event-model training a blocker for live advisory
- run `all_ml.sh` weekly as evidence generation only; do not let it change live policy automatically

Done already:

- `advisory.event_model_data_prep`
- `advisory.training_universe`
- `EVENT_MODEL_TRAINING_V1`
- `all_ml.sh`
- weekly Sunday cron for `all_ml.sh`
- `advisory.event_model_promotion_check` read-only gate for deciding whether weekly ML evidence is ready for manual operator review
- S3 artifact upload after successful training via `advisory.event_model_artifact_store`

Still needed:

- more matured labeled rows across dates, sectors, event classes, and regimes
- statistically usable walk-forward validation after costs
- baseline comparison against passive benchmark, deterministic event policy, and naive momentum
- research-ledger records for config, validation protocol, costs, baselines, and known failure cases
- promotion only as a low-weight review/risk/action input after repeated encouraging weekly runs

Pick this up when:

- `python -m advisory.event_model_promotion_check` returns `Decision: review_candidate`
- no failed gates are shown for label coverage, diversity, score freshness, successful weekly runs, precision lift, or ROC-AUC
- the operator review still confirms no leakage, no obvious event-class overfit, and no concentration in one symbol/date cluster

### 2. Improve the regime stack

Target direction:

- add explicit shock detection
- move toward a more persistent regime estimate
- keep regime separate from setup policy

Do not:

- invent new regime names every week
- let regime labels become an uncontrolled news bucket

### 3. Build a proper macro feature layer

Problem:

- CPI, WPI, FPI, RBI rates, FBIL/G-sec curves, US macro/FRED, ISM, and related inputs are ingested.
- Current usage is partial and too raw.
- The event model and risk engine should not join directly to many raw macro tables.

Target:

- create one normalized point-in-time table, for example `advisory_macro_features_daily`
- make it the only macro feature input for regime, event ML, setup policy, and risk sizing
- preserve publication lag and causal availability dates

Source mapping:

- `mospi_cpi`: inflation level, month-over-month change, rolling acceleration, food/core proxies where available
- `eaindustry_wpi`: WPI headline/category momentum, commodity and manufacturing inflation pressure
- `fii_investments` and `fii_derivatives`: FPI equity/debt flow, 1d/5d/20d rolling flow, derivative risk appetite
- `rbi_bank_rates`: policy-rate regime, last rate change, days since change
- `fbil_gsec_quote` and `fbil_gsec_par`: short/long yield levels, curve slope, yield shock
- `macro_usa`: US 10Y, dollar/liquidity proxy fields, global risk pressure
- `macro_usa_ism`: US demand/manufacturing cycle context
- `macro_india_gdp`: slower growth backdrop, used with explicit publication lag only

Feature groups:

- inflation pressure: CPI/WPI levels and momentum
- rates pressure: RBI policy, G-sec yield changes, curve slope
- foreign-flow pressure: FPI rolling flows and derivatives positioning
- global pressure: US yields, ISM, global risk indicators
- macro stress score: combined normalized signal for policy/risk sizing
- sector sensitivity tags: optional WPI/CPI category features for chemicals, pharma, metals, textiles, autos, energy, FMCG

Integration points:

- `advisory.regime_engine`: use macro features as inputs to base regime and shock/stress detection
- `advisory.news_overlay_engine`: use macro stress as context, not as a replacement for news overlay
- `advisory.event_meta_model`: join macro features on event anchor date as point-in-time predictors
- `advisory.risk_engine`: reduce sizing/caps when macro stress is high
- `advisory.portfolio_engine`: optionally tighten sector/overlap caps during adverse macro regimes

Implementation slices:

1. Done: add `advisory/macro_features.py`.
2. Done: create/persist `advisory_macro_features_daily`.
3. Done: add tests for macro feature ordering and null-tolerant feature generation.
4. Done: wire macro features into the regime engine.
5. Done: add macro feature columns to the event-model dataset.
6. Done: add a simple macro stress multiplier to risk sizing.
7. Done: document current raw-to-feature mappings in the advisory manual.

Do not:

- wire every raw macro table directly into every model
- use revised macro data as if it was known earlier
- make macro features a hard trade selector by themselves

### 4. Plug NSE deal and exchange-event data into advisory

Problem:

- NSE off-market and corporate-event data is already downloaded and parsed.
- Current advisory still relies mostly on screeners, OHLCV, fundamentals, announcements, news, macro, and intraday features.
- Block/bulk deals, insider deals, short selling, corporate actions, and earnings calendar rows should become point-in-time event/features instead of unused raw tables.

Target:

- create one normalized point-in-time exchange-event table, for example `advisory_exchange_events`
- create one daily per-symbol feature table, for example `advisory_exchange_features_daily`
- use exchange-event data as structured evidence for event model, adversarial review, setup scoring, risk sizing, and watch prioritization
- pass recent relevant exchange events/features into the LLM as bounded structured context when evaluating a symbol event
- preserve event date and disclosure date separately wherever available

Source mapping:

- `nseindia_block_deals`: institutional large trade signal, buyer/seller concentration, block-value shock
- `nseindia_bulk_deals`: unusual accumulation/distribution signal, repeated buyer/seller patterns
- `nseindia_short_selling`: bearish positioning pressure, repeated short interest pressure
- `nseindia_insider_deals`: promoter/director/insider buying or selling, holding change, confidence/governance signal
- `nseindia_corporate_actions`: split/bonus/dividend/rights/actions as technical adjustment and event context
- `nseindia_earnings_events`: upcoming/results calendar context and post-result event anchors
- `nseindia_events`: generic NSE event feed where available

Feature groups:

- block/bulk pressure:
  - block deal value as percent of market cap and ADV20
  - net buyer/seller side if inferable
  - repeated named buyer/seller in last 5/20 trading days
  - deal cluster count by symbol and counterparty
- insider pressure:
  - insider buy/sell count and traded value over 30/90 days
  - promoter/director category flag where inferable
  - holding percent change after transaction
  - insider accumulation/distribution score
- short-selling pressure:
  - short-selling value/quantity over 1/5/20 days
  - short pressure versus ADV20
  - repeated short-selling streak flag
- corporate-event context:
  - upcoming earnings flag
  - days to earnings/results
  - corporate-action adjustment flag
  - recent split/bonus/dividend context
- exchange event score:
  - combined normalized signal for event-model features and risk/reviewer context
  - should not directly force buy/sell decisions

Integration points:

- `advisory.event_meta_model`: join exchange features on event anchor date as point-in-time predictors
- `advisory.llm_event_evaluator`: include recent structured exchange events and exchange features in context so the LLM can classify event significance, materiality, surprise, direction, and expected decay
- `advisory.adversarial_review`: penalize or veto when insider selling, short pressure, or adverse exchange events contradict a setup

### 5. Add thesis buckets, horizon policy, and exit-event policy

Problem:

- `advisory_portfolio_orders` currently tells us allocation, priority, and invalidation, but not the intended holding contract.
- Some ideas are target-driven, some are explicitly time-window trades, and some should stay live only while the data continues to support them.
- The dashboard should show not just what was selected, but why it belongs in a given portfolio bucket and what would exit it.

Target:

- divide portfolio ideas into three thesis buckets:
  - `TARGET`
  - `TIME_HORIZON`
  - `DATA_DEPENDENT`
- add explicit exit-event rules that override target/time bucket behavior
- surface bucket rationale, screener provenance, and exit conditions in the dashboard and lifecycle views

Policy model:

- target bucket:
  - valuation or rerating driven
  - fields: `target_price`, `target_basis`, `target_confidence`, `target_review_date`
- time-horizon bucket:
  - defined event/trade window
  - fields: `expected_horizon_days`, `horizon_type`, `horizon_end_date`, `horizon_basis`
- data-dependent bucket:
  - thesis remains live while evidence remains supportive
  - fields: `continue_while`, `key_monitor_fields`, `recheck_frequency`
- exit-event layer:
  - invalidation hit
  - stop hit
  - thesis contradiction
  - adverse insider / exchange-event cluster
  - regime deterioration
  - liquidity breakdown

Implementation slices:

1. Add bucket and exit-policy columns to `advisory_portfolio_orders`.

### 5A. Add consolidated action recommendations

Problem:

- The system now has portfolio rows, lifecycle rows, rebalance rows, and execution rows.
- Even though portfolio/lifecycle snapshots are mostly deduped to one row per symbol, operator intent is still spread across multiple tables.
- The execution engine still reasons from separate portfolio and rebalance paths instead of one explicit action contract.
- The dashboard can show multiple adjacent concepts for the same stock, which is noisy when the operator just needs the single current action.

Target:

- create one point-in-time table, `advisory_action_recommendations`
- enforce one winning action per `asof_date + symbol`
- make this the single contract for:
  - operator dashboard
  - trading API handoff
  - execution dry-runs

Canonical action set:

- `BUY`
- `BUY_MORE`
- `PARTIAL_SELL`
- `SELL`
- `TIGHTEN_STOP`
- `MANUAL_REVIEW`
- `HOLD`
- `WATCH`

Priority model:

- `SELL` outranks everything
- `PARTIAL_SELL` outranks `BUY_MORE`
- `MANUAL_REVIEW` outranks `BUY` and `WATCH`
- `TIGHTEN_STOP` is lower than liquidation actions but higher than passive `HOLD`
- `BUY_MORE` outranks `BUY`
- `BUY` outranks `WATCH`

Source mapping:

- `advisory_rebalance_actions`
  - `exit_*` -> `SELL`
  - `trim_winner` -> `PARTIAL_SELL`
  - `add_on_pullback` -> `BUY_MORE`
  - `tighten_stop` -> `TIGHTEN_STOP`
  - `review_*` / `review_manual` -> `MANUAL_REVIEW`
- `advisory_portfolio_orders`
  - approved live recommendation -> `BUY`
- `advisory_position_lifecycle`
  - open with no stronger action -> `HOLD`
- `advisory_watchlist`
  - active non-abstain watch row -> `WATCH`

Execution integration:

- `advisory.execution_engine` should plan only from `advisory_action_recommendations`
- only broker-submittable actions should become order intents:
  - `BUY`
  - `BUY_MORE`
  - `PARTIAL_SELL`
  - `SELL`
- review-only actions must remain visible but non-submittable
- live Dhan submission is fail-closed unless `STOCKEY_LIVE_TRADING_ENABLED=true`, approval/reconciliation gates pass, and the operator supplies the current per-run confirmation token through `--live-confirmation` or `STOCKEY_EXECUTION_LIVE_RUN_CONFIRMATION`
- done: action-table execution previews carry `order_intent_lineage` in the safety contract, linking each order intent to action recommendation, reason-contract summary/status, risk sizing, stop/target levels, and approval/reconciliation gates

Dashboard integration:

- show one current `Action Recommendation` per stock
- explain:
  - action code
  - action priority
  - winning reason
  - winning source table
  - action fraction if any
  - execution mode

Implementation slices:

1. Done: add `advisory/action_recommender.py`
2. Done: persist `advisory_action_recommendations`
3. Done: wire `actions` stage into `advisory.pipeline`
4. Done: make `advisory.execution_engine` read the consolidated action table
5. Done: make the dashboard/operator view prefer the consolidated action over raw rebalance rows
6. Done: make action conflicts readable in the Nuxt Decision Trace page instead of only persisting them.
7. Done: add `advisory_action_conflict_rules` and `advisory/action_conflict_resolver.py` so conflict rules can be re-applied after rule edits.
8. Done: add a Nuxt conflict-rules page to inspect enabled rules, recent matched conflicts, and unresolved/manual-required combinations.
9. Done: add edit/disable controls and a promotion workflow that turns unresolved manual decisions into new deterministic rules.
10. Done: allow enabled approved conflict rules to influence `rank_action_candidates()` directly instead of only annotating the selected winner.
11. Done: fix built-in conflict classifier rule-id mapping and add regressions for same-action duplicate collapse plus WATCH-loser classification.
12. Done: add explicit `action_pair` promoted conflict-rule conditions for action-code-only precedence across sources, with API, resolver, ranking, and UI-copy coverage.
13. Next: expand beyond `action_pair_exact` and `action_pair` only when repeated manual resolutions prove a stable semantic rule is useful.

Do not:

- let multiple active actions survive for the same symbol on the same advisory date
- allow both a plain `BUY` and a `BUY_MORE` execution plan for the same symbol in the same run
- hide unresolved/manual-required conflicts; surface them explicitly as the winning action
- show resolved conflicts as audit/debug records in trace and conflict-rule views, not as Manual Review work

### 6. Build a proper swing technical buy/exit engine

Problem:

- Current technical usage is useful but still too mixed between generic rule checks, intraday confirmation, and event/risk overlays.
- We need one explicit swing technical engine for Indian equities that answers:
  - is the stock in a tradable uptrend?
  - is it forming a constructive structure?
  - is there accumulation and leadership?
  - is it executable with sensible risk?
  - has the setup triggered?
  - has the technical thesis broken?
- This is not an intraday trading engine.

Target:

- create a dedicated technical state engine that outputs daily:
  - pre-entry:
    - `REJECT`
    - `IGNORE`
    - `WATCHLIST`
    - `NEAR_PIVOT`
    - `READY`
    - `BUY_TRIGGERED`
  - post-entry:
    - `HOLD`
    - `ADD_ON_PULLBACK`
    - `PARTIAL_EXIT`
    - `FULL_EXIT`
    - `EMERGENCY_EXIT`
- make the engine explicitly prefer:
  - liquid Indian equities
  - structurally strong names
  - breakout and trend-continuation setups
  - clean execution and realistic stop placement

Design rules:

- do not build this around standalone RSI / MACD / stochastic signals
- do not turn Elliott Wave, candlestick patterns, or Fibonacci into primary buy logic
- use indicators as tools, not as the edge
- keep the engine swing-oriented and based on trend, structure, participation, leadership, and tradability

Hard pre-score filters:

- reject before scoring when any of these fail:
  - minimum average daily traded value
  - minimum price
  - minimum median volume
  - maximum spread percent
  - maximum recent gap frequency
  - excessive recent circuit behavior
  - obvious event hazard for short swing holds
  - highly erratic chart with no clean structure
- use config-driven thresholds, not hardcoded literals inside scoring code

Technical score model:

- total score: `100`
- trend regime score: `25`
  - price vs 20 / 50 / 150 DMA
  - slope of moving averages
  - higher-high / higher-low behavior
  - distance from 52-week high
  - 1m / 3m / 6m trend persistence
- structure quality score: `30`
  - flat base / tight range / VCP-like contraction / ascending base / breakout shelf / constructive pullback
  - base duration and depth
  - volatility contraction
  - support respect
  - pivot clarity
  - overhead supply estimate
- participation / volume score: `20`
  - breakout-day volume vs 20-day average
  - accumulation vs distribution balance
  - dry-up on pullbacks and contractions
  - retest behavior
- relative strength score: `15`
  - 1m / 3m / 6m performance vs benchmark
  - sector-relative performance
  - RS improvement before breakout
  - resilience during market weakness
- tradability / risk score: `10`
  - ATR percent
  - spread percent
  - average traded value
  - gap risk
  - circuit frequency
  - realistic stop distance

Suggested V1 thresholds:

- minimum gate before `READY` / `BUY_TRIGGERED`:
  - trend `>= 15/25`
  - structure `>= 18/30`
  - participation `>= 10/20`
  - relative strength `>= 8/15`
  - tradability `>= 6/10`
- state guidance:
  - total `< 55`: `IGNORE` or `REJECT`
  - total `55-69`: `WATCHLIST`
  - total `70-77`: `READY`
  - total `>= 78`: trigger-eligible, but only `BUY_TRIGGERED` after valid entry confirmation

Entry trigger archetypes:

- breakout entry:
  - close above valid pivot
  - meaningful breakout width
  - strong close
  - breakout volume confirmation
- breakout-retest entry:
  - breakout already happened
  - muted selling into retest
  - support hold / reversal confirmation
- trend-pullback entry:
  - controlled pullback into 20 / 50 DMA or prior support shelf
  - volume contraction
  - reversal / hold confirmation
- reclaim entry:
  - reclaim after false breakdown / failed breakout trap
  - strong close and preferably volume support

Post-entry exit framework:

- full exit:
  - pivot failure
  - decisive support break
  - structure invalidation
  - leadership collapse
  - regime deterioration with broken structure
- partial exit:
  - sharp extension away from 20 DMA / base
  - blow-off behavior
  - event-risk reduction
  - reward/risk deterioration without thesis break
- trailing exit:
  - aggressive: below recent swing support
  - balanced: below 20 DMA / pivot support
  - slower: below 50 DMA
  - optional ATR-based trail
- abnormal distribution exit:
  - clustered high-volume down days
  - repeated failed rebounds
  - deteriorating RS
- time-stop:
  - breakout triggered but no follow-through within configurable bars
- emergency exit:
  - severe gap / event shock / execution-risk breach

Conviction and sizing guidance:

- conviction bucket from technical score:
  - `LOW_CONVICTION`: `70-75`
  - `MEDIUM_CONVICTION`: `76-84`
  - `HIGH_CONVICTION`: `85+`
- do not size from score alone
- final sizing must still respect:
  - stop distance
  - liquidity
  - event-risk proximity

Context tags:

- allow only low-weight tags:
  - trend maturity early / middle / late
  - climax extension risk
  - broad market favorable / neutral / hostile
  - earnings proximity
  - sector momentum strong / weak
- these are tags, not primary buy logic

Implementation direction:

- add a dedicated module, for example `advisory/technical_engine.py`
- keep raw daily feature generation in `advisory/technical_features.py`
- do not move this into `advisory/intraday_features.py`
- intraday should stay secondary and timing-oriented, not primary swing logic

Required data/model additions:

- extend daily technical feature set with:
  - 52-week high distance
  - 20 / 50 / 150 DMA slope features
  - base duration / depth
  - range contraction measures
  - support-touch / pivot clarity metrics
  - accumulation / distribution day counts
  - breakout-volume ratios
  - gap frequency
  - circuit frequency
  - spread / tradability metrics where available
  - benchmark and sector-relative performance windows
- add explicit technical state outputs to the advisory layer

Pipeline integration points:

- `advisory.technical_features`:
  - extend feature generation for swing-structure fields
- new `advisory.technical_engine`:
  - compute technical sub-scores, technical state, entry type, invalidation candidate, and exit state
- `advisory.rule_engine`:
  - consume technical state instead of only generic technical rule fragments
  - use `WATCHLIST`, `NEAR_PIVOT`, `READY`, `BUY_TRIGGERED` directly
- `advisory.watchlist_builder`:
  - preserve technical engine state, pivot, support, and trigger archetype
- `advisory.position_lifecycle`:
  - map technical post-entry states to `HOLD`, `ADD_ON_PULLBACK`, `PARTIAL_EXIT`, `FULL_EXIT`, `EMERGENCY_EXIT`
- `advisory.risk_engine`:
  - combine conviction with liquidity and stop realism
- Nuxt operator frontend:
  - show technical state, trigger archetype, structure commentary, and exit reason cleanly

Implementation slices:

1. Done: add missing swing feature fields in `advisory.technical_features`.
2. Done: add config block for hard filters and technical thresholds.
3. Done: add `advisory.technical_engine` scoring buckets and state machine.
4. Done: add `BUY_TRIGGERED` trigger logic for breakout / retest / pullback / reclaim.
5. Done: add post-entry technical exit states and map them into lifecycle.
6. Started: replace scattered technical-only rule fragments in `advisory.rule_engine` with the explicit technical engine output.
7. Done: surface technical state, pivot, trigger type, sub-scores, stop/target, and exit condition in Nuxt operator traces through the shared Technical Decision panel.
8. Done: add regression tests for:
   - clean breakout
   - near-pivot base
   - loose / junk structure reject
   - failed breakout full exit
   - sharp extension partial exit
   - dead-money time stop
9. Done: add research-only technical threshold calibration against realized forward OHLCV outcomes after costs.
10. Done: add operator UI/API views for threshold calibration summaries and copyable configs.
11. Done: add research-only comparison of technical-only signals versus technical plus event-policy, bhavcopy, and company-memory overlays after costs.
12. Done: add operator UI/API views for signal-quality overlay summaries before allowing any overlay to influence production action rules.
13. Done: add review-only signal-quality overlay promotion workflow with operator decisions and copyable patch guidance; no live policy is changed.
14. Done: add reviewed config-change diff generation for approved technical-threshold and signal-quality overlay decisions; no live policy is changed.
15. Done: add read-only LLM/Codex prompt registry with API/UI visibility for prompt ids, schemas, env vars, source files, authority scopes, and fallbacks.
16. Done: persist prompt ids, prompt versions, and response schema versions on core LLM/Codex output rows and migrate callers to registry constants. Covered rows include advisory event evaluations, event-policy LLM manual reviews, playbook action plans, company-memory reviews, manual-revision pointers, technical-threshold promotion reviews, announcement summaries/OCR metadata, and structured announcement reports.

Do not:

- let intraday confirmation become the primary swing buy engine
- make oscillator crosses the core logic
- overfit V1 with too many market-regime branches
- mix technical trigger confirmation with loose event-driven overrides in a way that hides the actual setup quality

### 4A. Rebuild exit policy, stop-loss policy, and profit-booking policy

Problem:

- current lifecycle logic is too shallow
- `stop_price` and `invalidation_price` are effectively the same number
- `trim_winner` and `tighten_stop` exist mostly as labels, not as a real management framework
- post-entry technical states exist in `advisory.technical_engine`, but lifecycle does not use them yet
- execution only handles full `exit_*` actions and does not translate partial exits into broker behavior

Target:

- separate execution stop from thesis invalidation
- route lifecycle through `technical_engine.evaluate_post_entry_state(...)`
- add real profit-booking and stop-tightening rules
- persist explicit exit-policy metadata and action semantics
- allow Dhan execution planning to handle:
  - full exits
  - partial exits
  - stop-based exits

Required semantics:

- `invalidation_price`
  - thesis-level break
  - wider, slower, structural
  - if broken, the original setup is wrong
- `stop_price`
  - execution/risk-control stop
  - tighter than invalidation where possible
  - can be tightened as the trade matures
- `partial exit`
  - reduce risk or lock profit without killing the whole position
- `full exit`
  - stop hit, invalidation hit, technical failure, emergency gap/event damage
- `time stop`
  - setup-family aware
  - should not be one flat `20 day` rule

Implementation slices:

1. Make `risk_engine.compute_invalidation(...)` return distinct `stop_price` and `invalidation_price`.
2. In `position_lifecycle`, load latest technical context and use `evaluate_post_entry_state(...)`.
3. Map post-entry technical states into:
   - `exit_emergency`
   - `exit_technical_failure`
   - `trim_winner`
   - `add_on_pullback`
   - `hold`
4. Keep stop/invalidation exits as highest-priority overrides.
5. Replace the flat stale-review rule with bucket/setup-aware time-stop logic.
6. Add action metadata for exit sizing:
   - full exit
   - partial exit fraction
   - no-execution review-only action
7. Update `execution_engine` so Dhan execution supports:
   - full `SELL`
   - partial `SELL`
   - skip non-executable review-only actions
8. Surface stop, invalidation, technical exit reason, and partial-exit plan in dashboard/lifecycle views.

Current implementation status:

- lifecycle now recomputes a deterministic management plan per open position:
  - target price
  - expected holding days
  - horizon end date
  - target review date
  - recommended stop
- target-hit positions become `trim_winner`
- time-horizon losers after horizon expiry become `exit_time_stop`
- post-entry technical `FULL_EXIT`, `EMERGENCY_EXIT`, `PARTIAL_EXIT`, and `ADD_ON_PULLBACK` states map into lifecycle/rebalance actions with deterministic tests
- technical full exits now persist `TECHNICAL_FULL_EXIT` as the active exit condition so bucket/audit fields explain the exit trigger
- tightened-stop baseline updates now write `advisory_lifecycle_policy_changes` audit rows in the same retryable transaction as the portfolio stop update, and non-improving stop recommendations are tested to skip mutation/audit writes
- missing portfolio target baselines are initialized from lifecycle output with `target_initialized` audit rows; existing targets are not overwritten by this helper
- Symbol Detail and `/api/portfolio/{symbol}/detail` now show recent lifecycle policy-change audit rows so stop changes are visible without direct SQL
- Operator Health now checks recent `tighten_stop` actions for missing lifecycle policy-change audit rows and emits fix hints when coverage is incomplete or unverifiable
- action recommendations and execution planning understand the time-stop full-exit path
- `all_advisory.sh --fast` exists for quick refreshes without slow watch/news/repair work

Remaining:

- calibrate target multiples and horizon defaults against realized trade outcomes
- add target-change audit/history when target policy begins raising/lowering existing portfolio target baselines
- add optional low-worker parallel execution for pure DB/CPU stages only
- do not parallelize Chrome/browser-connected scraping in the same browser session

Do not:

- treat stop and invalidation as synonyms
- trigger partial exits only from arbitrary PnL thresholds without technical context
- let execution auto-submit ambiguous review actions
- let technical post-entry logic live separately from lifecycle
2. Add lifecycle fields so exit-event triggers and bucket state are visible in `advisory_position_lifecycle`.
3. Add first-pass deterministic bucket classification using setup family, holding horizon note, event context, and invalidation guidance.
4. Show bucket, bucket reason, exit-policy summary, and screener provenance in the live dashboard.
5. Later: allow richer target-price and horizon inputs from valuation/event models instead of only deterministic defaults.

Do not:

- force all ideas into target-price logic
- let bucket type replace invalidation/stop/event exits
- let the LLM invent unconstrained bucket policy without deterministic fields persisted downstream
- `advisory.rule_engine`: allow optional setup rules for insider accumulation, block-deal accumulation, or short-pressure avoidance
- `advisory.risk_engine`: reduce sizing when short pressure or insider distribution is elevated; optionally boost review priority for insider accumulation
- `advisory.watchlist_builder` and `advisory.continuous_watch`: prioritize watched symbols with fresh exchange-event activity
- `advisory.dashboard`, `advisory.symbol_trace`, and `advisory.setup_trace`: show latest exchange-event signals and feature provenance

Implementation slices:

1. Done: add `advisory/exchange_events.py` to normalize raw NSE deal/event rows into `advisory_exchange_events`.
2. Done: add `advisory/exchange_features.py` to build `advisory_exchange_features_daily`.
3. Done: add point-in-time joins from exchange features into `advisory.event_meta_model`.
4. Done: add recent exchange-event context to `advisory.llm_event_evaluator`, including compact raw event rows plus derived feature summaries.
5. Done: add exchange contradiction checks to `advisory.adversarial_review`.
6. Add optional setup-rule fields for exchange signals without making them mandatory for all setups.
7. Done: add risk sizing haircuts for elevated insider selling or short pressure.
8. Surface exchange-event data in dashboard and trace utilities.
9. Done: add tests for normalization, feature generation, point-in-time joins, LLM context, review, and risk behavior.
10. Done: document raw-to-feature mappings in the advisory manual and scripts docs.

Do not:

- treat one block/bulk deal as automatic alpha
- send unfiltered raw NSE history to the LLM
- use trade date as known date when disclosure date is later
- make low-quality counterparty inference mandatory
- let exchange-event features override abstention, liquidity, governance, or risk controls
- block all insider selling mechanically without checking materiality and context

### 5. Deferred: shift more prediction into tabular models

Target direction:

- keep LLMs in extraction and adversarial review
- keep tabular predictors as optional research
- prioritize deterministic event/playbook policies, macro gates, and technical timing for production

Likely next slices:

- only if resumed: learned reviewer or meta-reviewer
- only if resumed: richer event-memory and decay features
- only if resumed: more model-driven setup ranking

### 6. Strengthen event memory

Target direction:

- store event persistence and decay by event type, sector, and regime
- separate surprise, credibility, and persistence instead of using one flat event score

This should support:

- better forward-return labels
- better reviewer features
- less prompt-driven heuristics

### 7. Tighten the continuous watch loop

Current continuous watch is live, but can improve with:

- symbol cooldowns
- duplicate alert suppression
- better per-cycle prioritization
- richer dashboard summaries
- clearer operator-visible reasons for reruns

Keep the design:

- incremental
- watchlist-scoped
- fail-soft
- cheap enough to run every few minutes

### 8. Keep research discipline strict

The research ledger is implemented. The next step is using it consistently.

For all serious model work:

- log config
- log horizon
- log as-of date range
- log validation protocol
- keep point-in-time discipline
- prefer abstention over forced opinions

### 9. Long-Term Performance Architecture

Postgres is currently doing too much: OLTP state, time-series storage, text/blob storage, frontend serving, research warehouse, and audit logging. Keep Postgres as the control plane and latest-state store, not the warehouse for every raw artifact.

Current constraints:

- do not use DuckDB as the core store because prior Postgres compatibility issues made it fragile
- do not increase NSE browser concurrency; NSE should stay behind one controlled queue/session
- do not move to a complex lakehouse stack before simpler wins are exhausted
- full daily advisory remains the authoritative path; incremental advisory is research-only until proven safe

Target shape:

- PostgreSQL: current state, latest serving rows, action queue, audit IDs, compact event tensors, trace metadata
- S3-compatible object storage: raw PDFs, OCR text, full transcripts, full document summaries, raw JSON payloads, old logs, cold historical exports
- frontend-serving cache: compact snapshot/latest tables for health, actions, portfolio, watchlist, event inbox, trace summaries, market context, TS watch
- optional analytical store later: ClickHouse or BigQuery only after measuring remaining slow queries after slimming Postgres

Design rules:

- keep hot API paths snapshot-first; never rebuild the full `live_dashboard` payload inside normal `/api/home`, `/api/actions`, or `/api/portfolio` requests
- store large text once in object storage and keep only pointers, hashes, excerpts, parse status, and small classifications in Postgres
- separate latest state from history so frontend routes do not scan raw historical rows
- keep list endpoints compact, paginated, and bounded by latency/response-size tests
- use live builders only through explicit debug/repair commands with slowlog visibility
- add indexes only from slow-operation and slow-query evidence, while keeping duplicate-index reports clean

Completed performance work:

- `advisory_operator_snapshots` stores compact operator payloads
- operator API reads snapshots first and can serve stale snapshots instead of hanging on a live rebuild
- stale snapshot metadata is exposed to the frontend and health checks
- payload size warnings are recorded in the slow-operation log
- process-local API payload cache avoids repeatedly reparsing the same snapshot
- compact `/api/home` keeps the Nuxt landing page fast
- DB duplicate index and heavy-text cleanup scripts exist for periodic maintenance
- Actions, Events, Portfolio, trace summary/list, cron logs, prompt registry, hypotheses, artifact manifests, and research review/previews expose compact rows or stable pagination/bounded-list metadata: `total_count`, `returned_count`, `limit`, `offset`, `has_more`, and `next_offset`
- Health details use bounded parallel fast/default mode plus short API caching for UI responsiveness; full mode performs the expensive event-data, freshness, cron-log, API-error-history, and fallback scans on demand. `OPERATOR_HEALTH_FAST_WORKERS` controls the fast-check worker count for small Postgres instances.
- Latest OHLCV price enrichment uses `advisory_current_prices` plus process-local TTL caching, and `/api/actions` uses section snapshots instead of loading the full operator snapshot. Next measured hotspot should be selected from the slow-operation report after the next normal cron/advisory run.
- API latency probe writes the latest endpoint summary to `logs/performance/latest_api_latency_probe.json`, cron runs it at 07:55, 11:55, 16:55, and 21:55 on weekdays, and Operator Health now surfaces stale, slow, or failed probes in fix hints and the degradation feed.
- `scripts/api_performance_report.py` ranks latest probe rows plus deduped slow-operation state into concrete endpoint priorities and recommendations, so the next API/index optimization can be selected from evidence instead of guessing.
- `/api/actions` and `/api/portfolio` compact responses expose per-section `payload_bytes`, `avg_row_bytes`, and `max_row_bytes` in `meta`, so the next slow API review can separate row bloat from query latency before changing payloads or indexes.
- `/api/manual-review` now defaults to compact source previews instead of full raw rows; bulky raw keys are omitted from the list response and full debug payloads remain available through `include_raw=true`.
- `/api/health/details` now defaults to compact bounded lists and truncated long strings; full raw diagnostics remain available through `mode=full&compact=false`.
- `/api/operations/smoke` and `python -m advisory.operator_smoke` now compact fix hints and blocker details at source, so preflight output cannot balloon from long tracebacks or nested diagnostic rows.
- `/api/event-policy` now defaults to compact rows that preserve parsed checks/operator notes/LLM review while omitting bulky raw JSON source columns; full debug rows remain available through `include_raw=true`.

Next performance backlog:

- split monolithic operator snapshots into section-serving tables when endpoint payload size justifies it: summary, actions, portfolio, health, watchlist, event inbox, trace summaries, market context, TS watch
- add detail-only fetches for full reason contracts, OCR text, raw events, and traces
- add a dedicated Manual Review item-detail endpoint if operators need full source rows without using `include_raw=true` on the whole list
- refresh frontend snapshots after successful advisory/watchers and expose a one-click repair command when stale or missing
- run `python scripts/api_performance_report.py --limit 20` after cron/advisory runs and optimize the top endpoint with a concrete payload/index/query-plan change; for Actions and Portfolio, first inspect the response `meta` payload-byte telemetry
- bound advisory candidate universe per run so expensive stages do not scan unbounded historical/cross-sectional data
- add stage-level freshness checks so unchanged macro, exchange, technical, intraday, trace, and snapshot outputs can be reused safely
- done: move announcement OCR/transcript/report payloads out of hot Postgres rows with dry-run/apply manifests, S3 keys, hashes, byte counts, excerpts, and optional nulling
- done: add read-only object-store pointer validation for offloaded announcement artifacts with dry-run, HEAD/size checks, and optional SHA-256 verification
- done: add `scripts/heavy_payload_inventory.py` to identify non-announcement heavy text/blob payloads by catalog stats, risk, and recommended action
- run heavy payload inventory on live DB when reachable and remediate high-risk non-announcement findings
- add hot/cold retention for trace, intraday, event, alert, and raw crawler rows with dry-run cleanup reports
- evaluate Timescale compression/continuous aggregates for OHLCV and intraday if available
- evaluate ClickHouse as the preferred self-hosted analytical companion for historical OHLCV/features/events/evaluations if Postgres remains slow after offload
- evaluate BigQuery only for guarded research scans over exported Parquet/CSV, not low-latency frontend serving
- consider Go only after profiling shows Python CPU/serving code is the bottleneck rather than DB scans or payload size

## Deprioritized or intentionally avoided

- free-form multi-agent debate systems
- LLM-led trade selection
- automatic mutation of production screeners from research ideas
- full-market LLM polling loops
- backtest theater without leakage control

## Working principle

Keep the stack separated into:

1. extraction
2. prediction
3. policy
4. execution

LLMs belong mainly in extraction and adversarial review. Execution authority stays in deterministic policy and risk code.
