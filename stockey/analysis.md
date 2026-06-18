# Stockey Pipeline Analysis

Date: 2026-06-12

Purpose: create one working audit of the codebase and pipeline so we can decide what to harden next. This is intentionally direct. `Done` means the code path exists and is wired. `Partial` means it exists but has confidence, UX, testing, or operational gaps. `To do` means the missing work can affect correctness, trust, or production safety.

## Executive Summary

The project has a broad end-to-end pipeline:

1. raw data ingestion
2. parsing and identity mapping
3. feature generation
4. screeners and technical/rule scoring
5. event extraction and event-policy overlays
6. risk, portfolio, lifecycle, and action consolidation
7. manual-review and wait-signal intervention
8. execution planning
9. operator UI, traces, health, and research evaluation

The main architectural idea is sound: LLMs extract/review text, while deterministic/statistical systems own action policy and execution gates. The confidence problem is not the idea. The confidence problem is that several side paths were added incrementally and not every operator decision, failure, fallback, and follow-up has been fully wired into the main deterministic state machine.

Highest-priority gaps:

- `Partial`: formal state machine for manual interventions and their downstream effects. Decision effects, matched wait-signal reopen/suppression, matched follow-up closure suppression, stale operator-decision reopening when source rows change after `decided_at`, per-item Manual Review visibility lifecycle contracts, durable review-only action-candidate linkage, explicit matched-wait reason-contract boundaries, and a mocked end-to-end operator journey are enforced; broader superseded/downstream state coverage still needs work.
- `Done`: regression tests for every manual-review decision type and the core watch-signal downstream journey now cover decision recording, wait-signal creation, matching, Manual Review reopening, and review-only action-candidate creation.
- `Partial`: action/reason contract tests covering event-policy, playbook, lifecycle, execution, and wait-signal paths. Matched Manual Review wait-signal candidates now carry explicit wait-signal evidence, same-symbol source-precedence contracts expose losing candidates including market-adjustment context, enabled promoted exact-match conflict rules affect candidate ranking and preserve rule attribution in reason contracts, incomplete broker-capable rows downgrade with explicit Manual Review/no-broker boundary evidence, event-policy/playbook/lifecycle/generic Manual Review rows carry explicit review-only/no-broker boundary evidence, and compact API rows preserve execution-safety contracts even when only persisted in raw broker JSON; duplicate BUY screener collapse, portfolio BUY-over-WATCH, lifecycle HOLD-over-WATCH, market-gated portfolio BUY and BUY_MORE Manual Review-over-WATCH, cautious-market portfolio BUY_MORE-over-WATCH sizing reduction, event-policy Manual Review-over-positive portfolio BUY, event-policy Manual Review-over-market-adjusted BUY_MORE plus WATCH, adversarial-veto Manual Review-over-market-gated BUY_MORE plus WATCH, rebalance SELL-over-market-gated BUY_MORE plus WATCH, portfolio BUY_MORE-over-portfolio BUY, portfolio BUY_MORE-over-WATCH, portfolio SELL-over-portfolio BUY, SELL-over-WATCH, generic MANUAL_REVIEW-over-portfolio BUY, PARTIAL_SELL-over-portfolio BUY, PARTIAL_SELL-over-WATCH, review-only TIGHTEN_STOP-over-portfolio BUY, review-only TIGHTEN_STOP-over-WATCH, MANUAL_REVIEW-over-WATCH, and event-policy/playbook/lifecycle matrix permutations now have focused coverage, but broader contract coverage remains.
- `Done`: adversarial-review veto reasons are visible in compact Action Queue reason contracts/UI, not only trace/detail views.
- `Done`: compact Action Queue/API reason fields now humanize internal status/reason codes before display while preserving machine action codes.
- `Done`: stale operator snapshot warnings now use a shared API warning payload and render consistently on snapshot-backed operator pages, including symbol detail. Non-snapshot Manual Review, Wait Signals, and Identity Issues pages now expose source-specific freshness/unavailable-source warnings derived from their visible rows, and home/portfolio/events read APIs now publish explicit broker-disabled schema contracts.
- `Done`: Overview and Symbol Detail now render a reusable source-specific payload freshness strip, so Home/Actions/Portfolio/Signal Refresh and symbol-scoped Actions/Portfolio/Events/Trace/Data Inputs/Identity generated-at/status/stale-warning metadata is visible before operators trust final actions, P&L, targets, stops, or evidence.
- `Done`: better cleanup and de-duplication of old processing failures after the underlying bug is fixed. Manual Review and Health suppress recovered/superseded failure rows at read time, Health groups active/recovered/superseded-ready degradations, Health shows superseded candidate samples, `advisory/superseded_failures.py` can dry-run or mark durable superseded metadata, cron/Operations can audit the dry-run path without applying cleanup, and Health now has a guarded operator apply workflow that writes only superseded metadata with an audit row.
- `Done`: Operator Health now treats deduped slow-operation rows as active only when a fresh API latency probe reproduces a slow or failing route. Historical unprobed slowlog rows remain visible as recovered/contextual degradation evidence without keeping the current blocker list red.
- `Done`: the operator paper portfolio is now isolated from advisory and broker state. `/recommendations` can manually Buy/Sell latest consolidated recommendations into `advisory_operator_portfolio_ledger`, `/paper-portfolio` shows entry price, exit price, current price, and percentage P&L, and `scripts/reset_operator_portfolio.py --confirm` resets only that paper ledger.
- `Partial`: explicit “what happens next” visibility everywhere an operator clicks a decision. Manual Review now distinguishes item impact category and selected decision boundary before save, renders the saved backend decision-effect contract after a decision write, and has frontend write-flow smoke coverage; config-change application decisions now return and reload structured `decision_effect` and `operator_boundary` contracts that state no config, policy, portfolio, action, or broker state is mutated by the API, and the Technical Calibration, Signal Quality, and Event Policy UIs can record/display those application audit boundaries for reviewed config previews; the standalone identity issues page surfaces read-only repair boundaries and now renders preview/apply result boundaries showing whether the repair flow mutates issue status, identity mappings, or broker execution; other operator write flows still need the same treatment.
- `Partial`: wait signals now have typed condition contracts, matching, Manual Review reopening, closed-original and closed-follow-up suppression, review-only action-candidate linkage with explicit `manual_review_wait_signal_matched` reason-contract boundaries, conservative signal-refresh escalation, and mocked end-to-end operator journey coverage; broader production journey/UI coverage remains.
- `Partial`: watchers can trigger fast signal refresh with operator-visible effect labels for action change, wait-signal match, or evidence-only refresh, plus first-class previous-action/action-changed fields in refresh rows and decision traces. Signal-refresh rows and compact API payloads now carry explicit authority fields: `authority_scope=review_input_only`, `portfolio_authority=none`, `broker_execution_allowed=false`, and `full_advisory_required=true`, and the home UI renders those badges. Watchers are still not equivalent to a full advisory state transition.
- `Partial`: execution planning exists, but live broker execution should remain disabled until dry-run approval, reconciliation, evidence, live-allowance, and manual CLI-token flows are proven over real runs. Action Queue surfaces dry-run approval/reconciliation safety contracts, compact API rows preserve persisted raw broker safety contracts, mocked broker account/reconciliation coverage exercises nested account payload parsing/order/fill status mapping/persisted reconciliation safety-contract updates, execution previews now persist structured identity-resolution blockers and fallback telemetry when Dhan security identity fails, stale persisted dry-run rows are blocked by `load_ts` age before live submission/preflight, the `/execution-approvals` workbench is linked from global navigation and tested, every execution approval row now exposes a structured live-readiness checklist for dry-run row status, broker identity, quantity, reference price, operator approval, reconciliation, evidence, live allowance, and post-allowance approval, Action Queue rows now expose a read-only Final State Trust contract that combines reason status, final queue classification, transition preconditions, feature freshness, and execution boundary into one operator-visible checklist, and the operator header shows the global live-trading disabled/enabled state from `/api/runtime`; the UI still does not submit broker orders.
- `Partial`: feature freshness is visible in Action Queue and Symbol Detail and is persisted at action-decision time in `advisory_action_recommendations`. Explicit feature-gate summaries now exist for `rules`, `risk`, `portfolio`, `lifecycle`, and `actions`. Blocked rules gates move `PASS_NOW` candidates to `WATCH_EVENT`, blocked risk gates move positive allocations to `review_manual`, blocked portfolio gates defer positive approved capital, lifecycle gates annotate outputs without hiding exits, and positive broker-capable action winners downgrade to `MANUAL_REVIEW`. Operator Health surfaces stage gate blockers in fix hints, current blockers, and trust gate. Symbol Detail now shows per-symbol stage gate effects derived from persisted decision context and feature-freshness snapshots, plus a read-only current stage-gate drill-down from `/api/symbols/{symbol}/feature-freshness` that states blocked stages, blocked inputs, and the no-mutation/no-broker boundary. Action context enrichment and feature-freshness latest-row lookups now use explicit point-in-time cutoffs: date-only advisory runs include the full trading date, while exact-time watcher/ad-hoc runs cannot see later same-day event, playbook, wait-signal, or feature rows.

## Status Legend

- `Done`: implemented and wired into normal scripts/API.
- `Partial`: implemented but incomplete, under-tested, too implicit, or not yet trusted.
- `To do`: missing work or known shortcoming.

## 1. Runtime And Environment

Status: `Partial`

Done:

- `scripts/resolve_python.sh` lets shell scripts use the active venv or configured interpreter.
- `.env.example` exists and documents many runtime variables.
- `scripts/env_example_audit.py --strict` scans Python, shell, cron templates, and frontend code for runtime env usage and fails when `.env.example` is missing entries.
- `builder.py` prepares environment pieces including logs and local `go-crond`.
- `all_frontend.sh` uses `nvm` when configured and runs FastAPI plus Nuxt. It now supports `--api-only`, `--web-only`, and `--both` for targeted local restarts.
- `operator_health` now separates frontend dependency readiness from frontend runtime reachability (`frontend_runtime`), so smoke/Health can block trust when the Nuxt UI is down even if Node/npm dependencies are installed.
- `operator_health` fast mode now checks operator snapshot freshness directly, so compact smoke/Health blocks trust when the UI is reachable but serving stale advisory cache rows.
- Stale operator snapshot repair is now available as the audited Operations `operator_snapshot_refresh` command. Snapshot warnings point to this UI command and the direct `python -m advisory.operator_snapshot` fallback; the command writes only operator snapshot/cache rows and does not change recommendations, portfolio state, config, or broker orders.
- `scripts/start_chrome_cdp.sh` starts a foreground Chrome/Chromium CDP session with a separate user-data directory so Screener.in and Dhan browser automation have a documented local startup path.
- Cron template and generated crontab exist under `config/`.

Gaps:

- `Done`: `.env.example` has been reconciled against `env(...)`, `os.getenv(...)`, `os.environ`, shell defaults, cron templates, and frontend public env usage with the strict audit script.
- `Partial`: split frontend supervisor into a more robust process manager. `all_frontend.sh` now has explicit component modes, startup checks, and per-component health checks; remaining work would be moving this to a real daemon/process supervisor if local shell supervision proves insufficient.
- `Done`: document one canonical way to restart only API, only Nuxt, and both together.
- `Done`: add a health check for “running code hash/version” so the UI can tell when the API is stale after edits. `/api/runtime` reports git/process/source metadata, and the operator header warns when source files are newer than the API process.

## 2. Cron And Orchestration

Status: `Partial`

Done:

- `config/stockey.crontab.template` and `config/stockey.generated.crontab` schedule recurring jobs.
- Recurring scripts are split into `all_watchers.sh`, `all_downloaders_queue.sh`, `all_external_workers.sh`, `complete_data.sh`, `all_advisory.sh`, `all_frontend.sh`, and weekly `all_ml.sh`.
- `scripts/with_lock.sh` and `scripts/wait_for_locks.sh` prevent obvious overlaps.
- `all_watchers.sh` self-locks.
- `all_advisory.sh` waits for catch-up/data worker locks before running.
- `advisory.pipeline` now records reporting-only stage budgets in stderr and final JSON (`stage_timings`, `slow_stages`, `stage_budget`) so long `all_advisory.sh` runs can be diagnosed by stage without changing execution behavior.
- `scripts/advisory_stage_report.py` extracts the latest stage-budget summary from mixed cron/stdout logs and ranks stages by elapsed time, so operators can triage slow `all_advisory.sh` runs without manually locating the final JSON block.
- Operations UI/API now exposes the same read-only advisory stage report as the audited `advisory_stage_report` command, so slow-run triage is available without shell access and appears in operator command history.
- Operator Health now also treats failed or incomplete marker-only advisory runs as actionable evidence. If `all_advisory.sh` fails before the final JSON summary, Health reports the nearest failed stage from log markers instead of saying the timing summary is missing.
- Dhan auto-login now avoids Playwright sync-API failures inside advisory/API asyncio contexts by running the existing CDP/TOTP login helper in a separate Python subprocess when a running event loop is detected. The normal direct sync Playwright path is unchanged for CLI contexts. The Dhan page flow also uses a configurable `DHAN_AUTO_LOGIN_STEP_TIMEOUT_MS` and explicitly advances from TOTP to PIN when the Dhan page does not auto-transition; Chrome/CDP remains a hard dependency, and `python -m data.dhanlive.auth_cli ensure --auto-login --min-fresh-minutes 10` was verified successfully after Chrome CDP was started.
- Codex-backed OCR/summarization/event/manual-review flows now resolve the Codex CLI binary before execution, including common nvm, local npm, Homebrew, and `/usr/local/bin` paths, and record fallback telemetry with searched paths if cron or a non-interactive shell cannot find `codex`.
- Announcement attachment downloads now reject placeholder or non-HTTP attachment URLs such as `-` before making an HTTP request, record `announcement_attachment_url_invalid` fallback telemetry, and mark the attachment unavailable instead of surfacing a misleading OCR/download exception.
- Operator Health now checks the advisory stage report and emits a fix hint when the latest `all_advisory.log` has no parseable stage summary or contains over-budget stages.
- Fast Operator Health now keeps heavy DB diagnostics deferred by default and lowers default parallelism, so the normal Health page does not overload a small Postgres while full Health remains the explicit deep-diagnostics path.
- Fast Operator Health now returns a top-level `deferred_diagnostics` contract, and the Health page renders which deep checks were skipped plus the full-health command, so speed does not get mistaken for complete diagnostics.
- Current Health blockers now dedupe common fix-hint/section/degradation duplicates such as Dhan token cache, API latency probe, and slow-operation issues, and summary-only trust-gate rows no longer appear as self-referential blockers.
- Config-change application audit list reads now select only compact list fields by default, exclude bulky preview snapshots unless explicitly requested, and use a statement timeout, reducing `/api/config-change/applications` list payload/query cost while preserving decision-effect and operator-boundary evidence.
- Operations UI/API now exposes a scheduled-job status card parsed from `config/stockey.generated.crontab`, including schedule, next-run estimate, latest log marker, bounded tail, lock state, and stale-lock detection.
- `scripts/cron_preflight.py` now provides a read-only go-crond setup preflight for generated crontab parsing, required environment lines, referenced script executability, stale locks, Python resolution, and operator API/web ports. It is also registered as the safe `cron_preflight` Operations command.
- All top-level shell wrappers now emit deterministic `[stockey.script]` lifecycle markers either through `scripts/run_with_markers.sh` or, for the long-running frontend supervisor, an internal cleanup trap.
- Cron scheduled Python jobs now route through named marker-enabled wrappers: `all_api_latency_probe.sh`, `all_operator_health.sh`, `all_hypothesis_scan.sh`, `all_ts_forecast_workflow.sh`, `all_ts_forecast_evaluator.sh`, `all_ts_forecast_paper_portfolio.sh`, `all_event_policy_evaluator.sh`, and `all_technical_threshold_calibration.sh`.

Gaps:

- `Done`: add a single operator page/card that shows next cron run estimate, last log marker, status, lock state, and latest log tail for every scheduled job.
- `Done`: detect stale cron processes and stale lock files.
- `Done`: move all one-off direct Python cron commands into named shell wrappers or command registry entries so the UI/docs stay consistent.
- `Done`: add script-level success markers to all wrappers, not just logs.
- `Done`: add cron simulation/dry-run command that verifies paths, locks, venv, ports, and required env before starting go-crond.

## 3. Raw Data Downloaders

Status: `Partial`

Done:

- Dhan scrip master and OHLCV loaders exist.
- NSE bhavcopy, indices, corporate actions, earnings, insider, off-market, and recent events loaders/parsers exist.
- Screener.in login and ad hoc query flows exist.
- Sharpely, FRED, CPI, WPI, NSDL FPI, RBI, and ET/news loaders exist.
- External task queue exists for serialized single-client work.
- NSE/Dhan/Screener flows have retry and auth improvements.

Gaps:

- `Partial`: centralize downloader state. The central `data.download_runner` now writes a latest run-state row into `advisory_sync_state` for every module it runs, including queued worker module execution. Some individual sources still use DB max date, Redis/sync state, file state, and source-specific logic internally.
- `Done`: every configured central downloader/parser module now emits or exports a standard run row: source, from/to where relevant, rows, skipped/no-data, retries or attempts where available, fallback used, error/failure counters, and state advanced. The central runner now emits source/module/purpose/phase/status/classification/elapsed/returncode/skipped/error/state-advanced, preserves explicit module `state_advanced`, normalizes retry/attempt/failure/fallback/no-data/source-unavailable counters, preserves source-specific classification counters and failed-symbol lists for Health, supports module-exported run-state dictionaries, `advisory.event_evidence_store` exports exact compact evidence row counts/date windows, `data.company_master` exports identity row and identifier coverage counts, `data.dhanlive.ohlcv` exports daily/intraday rows plus price windows and failed symbols, Dhan and Sharpely master scripts export rows/columns/source-file/load timestamp or filtered master counts, Sharpely fundamentals exports symbol/date-window and table-family row counts, Screener registered/ad-hoc query CLIs export row/query/screener counts plus no-data state, NSE holiday-calendar downloads export browser/CDP source-unavailable state plus row/product counts, NSE bhavcopy/indices parsers export file-level parse state with date windows, skipped/already-processed/empty/failed counts, and failure samples, NSE off-market parser exports file-level skipped/parsed/failed counts, NSE sparse event crawlers export symbol/date-window rows plus queried/skipped counts, NSE off-market downloads export block/date attempt, retry, failure, skip, and success counters, NSE bhavcopy/indices archive downloaders export candidate/missing/downloaded date counts plus failure-stop counters, canonical benchmark sync exports symbol-level row, no-data, dry-run, and failure counters, WPI exports year/item retry and failure counters, CPI exports month retry/failure counters, NSDL FPI exports monthly catch-up/failure counters, RBI FBIL G-sec exports date-level download, weekend-skip, failure, and source-unavailable counters, RBI bank rates exports browser/CDP source-unavailable state plus row counts, Economic Times RSS exports feed-level success, empty-feed, failed-feed, and FRED/ISM macro exports series/leg-level row, fallback, failure, and source-unavailable counters. Operator Health/Data Health now summarizes latest `download_runner:*` rows, counters, source-specific classification counts, failed symbols, fix hints, degradation entries, and a first-class Failed Symbols / Source Skips panel so Dhan mapping misses and partial source failures are visible without opening raw JSON. Remaining work is deeper per-source semantics, not missing central runner coverage.
- `Done`: sync-state timestamp persistence now normalizes nullable `last_success_at`, `last_item_ts`, `updated_at`, and `load_ts` columns to UTC datetime dtype before upsert, preventing downloader runs with no item cursor from writing timestamptz columns as text. The Sharpely master precheck was verified through `data.download_runner` with `sync_state_persist.status=ok`.
- `Done`: every central downloader/parser run now has a “no data vs source unavailable vs auth unavailable vs parse failed” classification.
- `Done`: Dhan identity failures are stored in first-class identity-issue rows with Health fix hints, Manual Review/Identity Issues UI visibility, dry-run/apply repair lifecycle, and registry-managed schema setup.
- `Done`: NSE stale cookie/session resets should be counted and visible in Health. Announcement NSE HTTP retry/reset paths record durable `nse_retry` / `nse_session_reset` telemetry, fallback summaries expose explicit NSE counts, and Operator Health emits a source-specific fix hint plus trust-gate details.
- `Partial`: parser failure semantics are now explicit for NSE bhavcopy, indices, and off-market parser paths. Empty valid/no-row sources are stored as `empty_valid_source` so they do not loop; failed parse rows include `bad_file_retryable`, `schema_changed`, or `parser_bug` classification in state. `scripts/ingestion_state_runner.py summary`, `/api/operations/ingestion-state`, and the Health page expose status/source/classification counts for operator inspection.
- `Done`: ingestion-state and centralized downloader/parser run classifications now have a shared operator-facing catalog. CLI/API summaries expose labels, meaning, trust impact, and operator action for classes such as `ok`, `no_data`, `empty_valid_source`, `bad_file_retryable`, `schema_changed`, `parser_bug`, `source_unavailable`, `auth_unavailable`, `reference_mapping_missing`, and `partial_failed`; Health degradation rows and the Health page render those explanations instead of raw enum-only output. The download runner now lets exported classifications win and refines successful zero-row runs into `no_data` or `source_unavailable` when counters show the source did not advance. Dhan OHLCV run-state now classifies missing security-id mappings, source outages, auth failures, no-data windows, and partial symbol failures with counters so Health can tell operators whether to refresh credentials, repair the master mapping, retry later, or accept no new data. Dhan scrip-master sync now exports classified run-state for download/source failures, parser failures, and schema changes such as broker-added columns before failing closed, so stale security-id mappings have a visible reason. Screener.in registered-screener sync now exports `ok`, `no_data`, `auth_unavailable`, `source_unavailable`, `parse_failed`, or `partial_failed` run-state plus failed/empty screener counters, and returns non-zero when any configured screener fails so broken candidate sourcing is visible. NSE recent-events calendar sync now fail-closes with exported `ok`, `no_data`, `source_unavailable`, or `parse_failed` run-state, sync-state payload, and fallback telemetry so CDP/NSE/parser failures do not disappear as generic script crashes. Sharpely fundamentals now continues through per-symbol failures, records fallback telemetry, exports failed-symbol/classification counters, and returns non-zero for `partial_failed`, `auth_unavailable`, `source_unavailable`, or `parse_failed` so degraded peer/fundamental context is visible while successful symbol rows are retained.
- `Done`: Symbol Detail now loads symbol-scoped identity issues from `/api/identity-issues?symbol=...` and renders open Dhan/company-master mapping problems as Identity / Source Blockers before price/action interpretation, with repair hints and a link to the Identity Issues workbench. Action Queue also loads open identity issues and renders matching symbol-level source blockers directly on action cards, including the source error, repair hint, Identity Issues link, and portfolio/readiness blocker text before operators treat a row as broker-ready.
- `To do`: use one queue abstraction for all single-client/browser-bound sources, including Screener and Dhan auth-sensitive calls.

## 4. Parsing And Ingestion State

Status: `Partial`

Done:

- Bhavcopy parser no longer silently eats many parse failures.
- Empty/processed file state improvements exist.
- Ingestion file state scripts exist.
- Announcement pipeline tracks documents/reports and processing failures.
- Heavy announcement text offload tooling exists and now emits bounded JSON samples plus optional manifest files for dry-run/apply review before inline OCR/transcript/report fields are nulled.
- Manual Review suppresses old `advisory_event_processing_runs` failures when a later successful run exists for the same `unique_id` and `stage`.
- Manual Review and Health suppress announcement document rows where stale `last_error` remains after OCR and parse statuses have recovered.
- `advisory/superseded_failures.py` previews by default and can explicitly mark superseded `advisory_event_processing_runs` rows plus recovered announcement `last_error` rows with durable metadata.
- `all_superseded_cleanup_audit.sh`, cron, and the audited Operations command registry expose the superseded cleanup dry-run path without passing `--apply`.
- File-level ingestion-state summaries now split failed parser/source files into active versus stale historical lifecycle buckets. Operator Health treats recent actionable parser failures as blockers, while old historical failed files stay visible as recovered/suppressed audit context instead of polluting current blockers.

Gaps:

- `Done`: old processing failures no longer need to keep polluting Manual Review after bugs are fixed. Read-time suppression, Health active/recovered/superseded-ready grouping, explicit dry-run/apply command visibility, a durable marking helper, scheduled/audited dry-run visibility, and guarded operator-facing apply now exist for event-processing and recovered announcement-document errors.
- `Partial`: every parser should distinguish `empty_valid_source`, `bad_file_retryable`, `schema_changed`, and `parser_bug`. NSE bhavcopy, indices, and off-market parser paths now do this and have focused tests, and the shared classification catalog explains operator action/trust impact; remaining parser paths still need the same contract.
- `Done`: stale historical parse failures should not keep polluting active operator blockers unless still current and actionable. File-level ingestion-state failures now carry active/stale lifecycle counts; Health marks old historical failures as recovered/suppressed context and only treats recent failures inside `OPERATOR_HEALTH_INGESTION_FAILURE_ACTIVE_DAYS` as active blockers.
- `Partial`: add parser contract tests for the most important source files. Bhavcopy, indices, and off-market file-state parser classification contracts now have shared regression coverage; remaining non-file-state parsers still need source-specific contracts where failures affect operator trust.
- `Done`: make ingestion state summaries visible from the operator UI. The Health page now uses the same summary contract as the CLI via `/api/operations/ingestion-state`.

## Manual Review Semantics Update

- `Done`: Manual Review rows now expose a backend operator-boundary contract with `investment_judgment_review`, `technical_or_operational_issue`, or `research_or_config_review` categories, and the Manual Review payload/UI now show backend-authored queue-level category, lane, and item-impact summaries so operators can separate investment judgment, technical/data repair, research/config review, and execution-blocking work before opening individual cards.
- `Done`: Manual Review rows include per-decision effect metadata, so the UI can explain close/annotate/wait-signal effects from the API contract instead of local-only inference.
- `Done`: the Manual Review page shows the primary operator task and explicit portfolio/action/broker boundaries for each item, making technical issues visibly separate from investment judgment. It also includes an always-visible decision guide for every dropdown choice plus selected-choice effect facts for active-queue, wait-signal, portfolio, action-row, and broker outcomes before save.
- `Done`: the Manual Review page now surfaces stale operator-decision reopen state from `manual_review_state`, including an aggregate reopened count and per-item explanation when newer source evidence arrived after the latest decision.
- `Done`: the Manual Review page now renders the saved decision response beside the item, including next state, close/annotate effect, wait-signal creation, portfolio boundary, and broker boundary, with a frontend test that saves a decision and checks the backend effect contract.
- `Remaining`: add a bulk close/apply workflow only after per-item semantics remain stable over several runs; for now each decision remains explicit and individually audited.

## Watcher Reliability Update

- `Done`: continuous-watch price-alert persistence now stores a deterministic `alert_fingerprint` and suppresses duplicate setup/symbol/type/source/state alerts within `WATCHER_ALERT_COOLDOWN_SECONDS`.
- `Done`: watcher OHLCV cycle output and sync-state payloads include alert input, persisted, suppressed, and cooldown counts, so frequent cron runs are auditable instead of silently duplicating rows.
- `Done`: alert dedupe lookup failures record local fallback telemetry and fail open by persisting alerts, preserving evidence when Postgres is degraded.
- `Done`: OHLCV, news, and announcement watcher cycles now publish normalized `source_counters` in both run results and `advisory_sync_state`, covering symbols, sync results, latest prices, alert persistence/suppression, news items, matched/persisted events, feeds, ingest targets, discovered/parsed/failed documents, and watch updates.
- `Done`: Operator Health and the Health page now surface watcher source counters, stale/missing/error status, and a fix hint that points operators to rerun `./all_watchers.sh` and refresh Health.
- `Remaining`: add richer source-specific diagnostics for nested producer substeps once a real Health/UI view needs more than the normalized counters.

## 5. Identity And Symbol Mapping

Status: `Partial`

Done:

- `company_master` exists.
- `dim_security`, `dim_security_history`, and review events exist.
- Dhan identity resolver supports NSE/BSE fallback behavior.
- Company master ID backfill exists.
- Missing Dhan stock security IDs are recorded in `advisory_identity_issues` with symbol, requested exchange, fallback attempts, company-master context, error text, and suggested repair steps.
- Manual Review surfaces open identity issues as technical items with fix instructions.
- Operator API/frontend now expose a read-only standalone Identity Issues page with open skipped-symbol rows, repair hints, attempted exchanges/fallbacks, Manual Review item linkage, and explicit no-broker/no-mutation boundaries.
- `python -m advisory.identity_issues` can dry-run rechecks of open Dhan/security identity issues, and `python -m advisory.identity_issues --apply` closes rows that now resolve after Dhan/company-master refresh.
- Dhan index/benchmark identity resolution now expands common aliases such as `NIFTY50`, `NIFTY 50`, `BANKNIFTY`, `NIFTY BANK`, `INDIAVIX`, and `INDIA VIX`, records unresolved index aliases as identity issues, and lets the existing dry-run/apply repair lifecycle close them when mappings are refreshed.
- Operator Health now checks the latest broker-capable action rows against `company_master` and Dhan NSE/BSE ids, so hidden active-action identity gaps are visible even if no explicit identity issue row was recorded yet.
- Action consolidation now downgrades broker-capable winners with missing company/Dhan identity to `MANUAL_REVIEW`, review-only/no-broker-execution, with explicit reason-contract context before persistence or execution can see them.

Gaps:

- `Done`: unresolved Dhan identity issues now have a durable table, Manual Review queue, standalone skipped-symbol/identity page, Health fix hints, attempt counts, resolution metadata, CLI close lifecycle, and a guarded operator UI dry-run/apply workflow that only closes previewed rows.
- `Done`: store every “No Dhan security id mapped” stock case in a durable table with symbol, source, exchange tried, fallback tried, and suggested action.
- `Partial`: add daily auto-repair for NIFTY/index mappings and common symbol aliases. Common index aliases now resolve and open alias failures can be closed by the existing Identity Issues dry-run/apply lifecycle; next work is scheduling an automatic safe dry-run/apply if desired.
- `Partial`: assert no active advisory/action row lacks a resolvable company/security identity unless explicitly marked non-tradable. Operator Health flags latest broker-capable action rows with missing company-master rows or missing Dhan ids, action consolidation downgrades broker-capable winners with missing identity before persistence, and execution planning blocks older/non-standard action rows with structured identity safety-contract evidence plus fallback telemetry. Remaining work is broader coverage for any non-action-table broker handoff path if legacy fallback is deliberately enabled.

## 6. Storage, DB Reliability, And Performance

Status: `Partial`

Done:

- DB retry wrappers exist for many query/upsert paths.
- DB retry/exhaustion paths now write local JSONL fallback telemetry without touching Postgres, so Operator Health can show retry spikes even when Postgres is unstable.
- Operator current-price cache query failures now write local fallback telemetry and Health emits a source-specific fix hint when the API may have fallen back to OHLCV history.
- Risk sizing now emits aggregated local fallback telemetry when missing ADV20 forces liquidity-cap fallback, and Operator Health shows a specific fix hint.
- Risk sizing macro/exchange context lookup failures now emit local fallback telemetry, and Operator Health distinguishes this from normal no-data.
- Signal refresh lookup failures for latest action/lifecycle/rebalance rows, event-policy rows, and router actions now emit local fallback telemetry so watcher-triggered refreshes do not silently lose context.
- Action consolidation conflict-rule lookup/load failures now emit local fallback telemetry, and Operator Health distinguishes default deterministic-rule fallback from dynamic promoted-rule fallback.
- Wait-signal source table lookup, column-check, missing-column, and event-evidence load failures now emit local fallback telemetry, so missed event/news/announcement wait-signal matching is visible in Operator Health.
- Feature freshness input lookup failures now emit local fallback telemetry before marking the input `error`, so action downgrades caused by freshness lookup failures are visible in Operator Health.
- Company-memory source table lookup, source row load, and wait-signal row load failures now emit local fallback telemetry, so review-only memory summaries based on partial evidence are visible in Operator Health.
- News-theme as-of resolution and recent-market-news load failures now emit local fallback telemetry, so empty theme-to-screener routing output is visible in Operator Health.
- Event-policy evaluator schema lookup, missing required-column, and policy-row load failures now emit local fallback telemetry, so empty research-calibration output is visible in Operator Health.
- Event-data-quality table/schema/count/sync-state/readiness query failures now emit local fallback telemetry, so the Health trust gate can distinguish source quality issues from degraded quality-check execution.
- Trace summary cache lookup, recent-entity discovery, and per-entity rebuild failures now emit local fallback telemetry, so symbol/event trace pages falling back to live or partial summaries are visible in Operator Health.
- Compact event-evidence table/schema/max-date lookup failures and bhavcopy/announcement source-query failures now emit local fallback telemetry, so stale or failed compact evidence refreshes are visible in Operator Health.
- Event meta-model event-row, price-history, intraday, macro, and exchange-context lookup failures now emit local fallback telemetry, so missing labels or degraded model context are visible in Operator Health.
- Exchange feature table/date/event load failures now emit local fallback telemetry, so missing deal/insider/short/corporate-action feature context is visible in Operator Health.
- Market-context source/cache lookup failures now emit local fallback telemetry, so stale or partial market-gating context is visible in Operator Health.
- Fundamental snapshot universe, peer, target-date, release-calendar, statement, balance-sheet, cashflow, shareholding, and company-master mapping failures now emit local fallback telemetry, so missing peer/fundamental context is visible in Operator Health.
- TS forecast feature/evaluator universe, OHLCV, refresh, forecast-row, and future-price lookup failures now emit local fallback telemetry, so missing TS Watch rows or validation summaries are visible in Operator Health.
- Watchlist builder event-transition and existing-state lookup failures now emit local fallback telemetry, so missed event upgrades/downgrades or reset watch metadata are visible in Operator Health.
- Symbol trace source table lookup, stage-row, screener-row, rejection-row, and aggregated-event lookup failures now emit local fallback telemetry, so incomplete or failed symbol detail pages are visible in Operator Health.
- Setup trace source table lookup, as-of, regime, overlay, screener-row, column-inspection, and stage-row lookup failures now emit local fallback telemetry, so incomplete or failed setup funnel pages are visible in Operator Health.
- Technical-threshold calibration schema, signal-row, and price-history lookup failures now emit local fallback telemetry, so missing or degraded threshold review output is visible in Operator Health.
- Technical-threshold calibration persistence now has focused regression coverage for Timescale-safe unique keys: evaluation upserts use `evaluated_at, config_id, horizon_days`, and summary upserts use `evaluated_at, horizon_days`, both with `evaluated_at` as the hypertable column. Old cron logs showing `cannot create a unique index without evaluated_at` are stale relative to current code; rerun `./all_technical_threshold_calibration.sh` to refresh the log.
- Technical-threshold calibration now fail-closes source outages explicitly. Schema, signal-row, or OHLCV source read failures raise `SourceUnavailableError`; the CLI returns JSON with `status=source_unavailable`, `source_unavailable=true`, and exit code 2 instead of reporting `status=ok` with zero rows.
- Signal-quality evaluator schema, event-policy, bhavcopy-evidence, and company-memory overlay lookup failures now emit local fallback telemetry, so degraded overlay-promotion evidence is visible in Operator Health.
- Technical-threshold, signal-quality, and event-policy promotion-review evidence lookup failures now emit local fallback telemetry, so missing manual promotion-review rows are visible in Operator Health.
- Event-model promotion-check label-coverage and score-freshness evidence failures now emit local fallback telemetry, so research-to-review readiness gaps are visible in Operator Health.
- Macro feature table lookup and source-row load failures now emit local fallback telemetry, so stale or unavailable macro/regime context is visible in Operator Health.
- Regime-engine table lookup plus NSE/Dhan benchmark and macro source load failures now emit local fallback telemetry, so stale or unavailable market-regime snapshots are visible in Operator Health.
- Technical-feature universe, stock OHLCV, benchmark, sector, peer-membership, and peer-OHLCV source failures now emit local fallback telemetry, so degraded or unavailable rule-scoring inputs are visible in Operator Health.
- Intraday-feature table lookup, screener-universe, coverage, Dhan sync, intraday-history, and daily-reference failures now emit local fallback telemetry, so degraded or unavailable intraday rule context is visible in Operator Health.
- Rule-engine effective-date, regime, overlay, screener-universe, technical, intraday, and fundamental source failures now emit local fallback telemetry, so candidate pass/watch/rejection generation failures are visible in Operator Health.
- Portfolio-engine allocation and sector metadata lookup failures now emit local fallback telemetry, so missing/degraded portfolio approval, defer, and overlap decisions are visible in Operator Health.
- Position-lifecycle open-order, price-identity, price-history, and technical-context failures now emit local fallback telemetry, so degraded or unavailable hold/exit/partial-exit decisions are visible in Operator Health.
- Execution-engine broker account, reconciliation target, live-submit, and broker-reconciliation failures now emit local fallback telemetry, so degraded broker handoff and reconciliation paths are visible in Operator Health without reading stderr or row-level execution text.
- Operator snapshot load, section-snapshot load, and current-price refresh failures now emit local fallback telemetry, so stale/missing snapshot fallback and degraded UI price context are visible in Operator Health.
- Event-router source-row, watchlist-priority, and per-symbol refresh failures now emit local fallback telemetry, so missed or degraded watcher-triggered fast refreshes are visible in Operator Health.
- Advisory daily OHLCV symbol sync failures now emit local fallback telemetry, so missing Dhan mappings, expired Dhan tokens, or failed price repairs for advisory symbols are visible in Operator Health instead of only stderr.
- Decision trace write failures now emit local fallback telemetry while preserving non-fatal pipeline behavior, so incomplete trace/audit evidence is visible in Operator Health instead of only logs.
- Economic Times RSS per-feed source-unavailable and parse failures now emit local fallback telemetry, so degraded market-news ingestion is visible in Operator Health while the remaining feeds can still be used.
- NSE bhavcopy archive per-date download failures now emit local fallback telemetry, so missing market-wide bhavcopy evidence is visible in Operator Health while later catch-up can still retry the date.
- NSE indices archive per-date download failures now emit local fallback telemetry, so missing index/benchmark archive evidence is visible in Operator Health while later catch-up can still retry the date.
- NSE holiday-calendar source-unavailable and generic download/parse failures now emit local fallback telemetry, so stale trading-calendar context is visible in Operator Health.
- NSE off-market/deals block download failures now emit local fallback telemetry, so missing block/bulk/short-selling evidence is visible in Operator Health while later catch-up can still retry the block.
- NSE off-market/deals parser failures now emit local fallback telemetry with parser classification and stored file name, so missing parsed block/bulk/short-selling evidence is visible in Operator Health while ingestion state still controls retry.
- External task queue status-load and worker task failures now emit local fallback telemetry, so delayed or failed serialized NSE/Dhan/Screener work is visible in Operator Health.
- Config-change assistant approved-decision and preview-history lookup failures now emit local fallback telemetry while failing closed, so reviewed config diff generation issues are visible in Operator Health without applying any config.
- Research-ledger start, finish, and list failures now emit local fallback telemetry while preserving exceptions, so missing false-discovery or model-validation audit rows are visible in Operator Health.
- Redis set-member state read failures now emit local fallback telemetry before returning the safe empty-set fallback, so possible safe reprocessing caused by missing Redis state is visible in Operator Health.
- FRED macro latest-date fallback and per-series fetch failures now emit local fallback telemetry, so partial or lookback-based US macro context is visible in Operator Health.
- MOSPI CPI month download failures now emit local fallback telemetry on each failed retry attempt, so stale or partial India inflation context is visible in Operator Health while the month-by-month resume behavior remains unchanged.
- RBI bank-rates browser/source-unavailable and generic download/parse failures now emit local fallback telemetry, so stale policy-rate context is visible in Operator Health while the job still returns structured run state.
- Manual Dhan OHLCV pull utility failures now emit local fallback telemetry with ticker/mode/source metadata before returning the existing error payload, so operator price-inspection failures are visible in Health.
- Event meta-model CLI train/score command failures now emit local fallback telemetry before returning the existing JSON error payload, so missing train/score output is visible in Operator Health.
- Announcement first-page OCR/transcription, full-document OCR, and earnings-call audio transcription failures now emit local fallback telemetry while preserving the existing continue-on-error ingestion behavior, so degraded announcement evidence is visible in Operator Health.
- NSE announcement cookie-bootstrap URL failures now emit local fallback telemetry before trying the next bootstrap URL or returning empty cookies, so NSE session degradation is visible without changing retry behavior.
- Per-announcement managed-ingest failures now emit local fallback telemetry while preserving batch continuation and failed-document status persistence, so incomplete OCR/report/summary output is visible in Operator Health.
- Dhan daily/intraday bulk OHLCV per-symbol failures now emit local fallback telemetry while preserving batch continuation, so missing/stale prices for individual failed symbols are visible in Operator Health.
- Dhan identity fallback/issue telemetry persistence failures now emit local fallback telemetry, so unresolved security-id evidence is still visible when the DB-backed telemetry or identity-issue table is unavailable.
- Canonical benchmark sync per-symbol failures now emit local fallback telemetry while preserving partial run-state behavior, so stale benchmark/regime/relative-strength context is visible in Operator Health.
- WPI per-item download retry failures now emit local fallback telemetry while preserving retry/resume behavior, so stale India inflation/producer-price context is visible in Operator Health.
- FRED/ISM macro leg failures now emit local fallback telemetry while preserving partial macro run-state behavior, so degraded US macro context is visible in Operator Health.
- Operator API latest daily/intraday OHLCV price lookup failures now emit local fallback telemetry while preserving partial UI/API responses, so degraded Action Queue or symbol-page latest-price context is visible in Operator Health.
- TS forecast workflow Screener.in fallback failures now emit local fallback telemetry before falling back to the tracked Dhan/OHLCV symbol universe, so degraded TS Watch candidate sourcing is visible in Operator Health.
- Redis backup/restore per-key and backup-file failures now emit local fallback telemetry while preserving the existing skip-or-exit behavior, so Redis state maintenance issues are visible in Operator Health.
- DB-backed fallback telemetry write failures now spool a local meta-event, so telemetry storage degradation itself is visible even when Postgres-backed telemetry cannot be written.
- Agent tool runner, SQL query runner, and duplicate-index cleanup failures now emit local fallback telemetry while preserving their JSON-error or skip behavior, so operator maintenance tooling failures are visible in Operator Health.
- Peer OHLCV and peer-fundamentals sync failures now emit local fallback telemetry while preserving partial peer-sync results, so degraded relative-strength/peer context is visible in Operator Health.
- NSDL FPI failed-month catch-up now emits local fallback telemetry while preserving failed-month run-state counters and retry-on-later-run behavior, so stale foreign-flow context is visible in Operator Health.
- Dhan auth validation failures, corrupt cached-token payloads, invalid cached-token expiry strings, and Dhan scrip-master download/short-response failures now emit local fallback telemetry, so broker-auth and security-master degradation is visible in Operator Health before it cascades into 401s or missing security ids.
- NSE announcement session bootstrap-after-reset failures, session-build retry failures, and unsupported NSE/BSE announcement timestamp formats now emit local fallback telemetry, so source session degradation and point-in-time timestamp parse failures are visible in Operator Health.
- NSE bhavcopy parser invalid-key date parsing, archive parse failures, circuit-hit date fallback, zip-stat failures, file-level parser failures, and nested corrupt-zip failures now emit local fallback telemetry while preserving empty-valid, retryable-bad-file, schema-changed, and parser-bug state semantics.
- NSE indices parser invalid-key date parsing, zip-stat failures, index-close date fallback, file-level parse failures, and archive parse failures now emit local fallback telemetry while preserving benchmark parser state semantics.
- NSE bhavcopy and indices archive downloaders now emit local fallback telemetry when malformed object keys or malformed downloaded-date members are ignored, so catch-up re-download decisions caused by bad stored metadata are visible in Operator Health.
- NSE security-history corporate-action context and persisted-history load failures now emit local fallback telemetry while preserving safe-empty and raw-symbol/ISIN fallback identity behavior.
- NSE off-market/deals malformed downloaded-file keys and recent-events mixed-date parser fallbacks now emit local fallback telemetry while preserving the existing safe ignore/dayfirst fallback behavior.
- Company-memory review symbol-discovery failures now emit local fallback telemetry, so missing review-only LLM/deterministic memory rows caused by candidate-source lookup failures are visible in Operator Health.
- Hypothesis/playbook market-context load failures now emit local fallback telemetry while preserving explicit error payloads in generated playbook action plans.
- Identity-issue resolution failures now emit local fallback telemetry before keeping the issue open, so unresolved Dhan/security mapping repairs are visible as degraded operation rather than only as a resolver result row.
- News-watch recent-news source load failures and active news-theme screener mapping lookup failures now emit local fallback telemetry while preserving existing safe-empty/no-slug behavior.
- Signal-refresh table-existence checks, trace-summary cache refresh failures, and per-router item refresh failures now emit local fallback telemetry while preserving existing safe-unavailable, persisted-row, and partial-batch behavior.
- Core Operator Health DB/API/latency-probe/Redis/sync-state check failures now emit local fallback telemetry while preserving existing Health status payloads.
- Operator Health schema-migration registry, Dhan cached-token inspection, Dhan token validation, and Dhan token-cache inspection failures now emit local fallback telemetry while preserving existing Health status payloads.
- Operator Health API-error history, trace summary cache, identity issue, active action identity coverage, and Screener.in failure inspection paths now emit local fallback telemetry while preserving existing Health status payloads.
- Operator Health announcement document failure inspection, frontend dependency checks, signal-quality freshness/rows/coverage checks, and feature stage-gate checks now emit local fallback telemetry while preserving existing Health status payloads or safe partial rows.
- Operator API table-column lookup failures now emit local fallback telemetry while preserving the existing safe-empty column-set behavior.
- A static fallback telemetry QA utility, `scripts/fallback_telemetry_coverage_report.py`, now classifies exception handlers as telemetry-recorded, re-raised, logs-only, silent, or explicit telemetry self-protection so fallback coverage can be audited repeatably instead of with one-off `rg` sweeps. The 2026-06-12 baseline across `advisory`, `data`, `scripts`, and `utils` is now `{'records_fallback': 495, 'reraises': 53, 'self_protection': 2}` and `--fail-on-silent` passes. Normal source/API/helper paths now have zero `logs_only`, zero `logs_then_falls_through`, zero `silent_fallback`, zero `silent_handler`, and zero silent `data/nseindia/*` rows. The only remaining non-recorded handlers are explicit fallback-telemetry self-protection paths where emitting telemetry would risk recursion.
- Upserts use temp files for COPY payloads.
- Duplicate index, size, retention, slowlog, archive, and S3 offload scripts exist.
- `scripts/hot_table_retention.py` provides report-first hot/cold retention for Dhan intraday candles, daily intraday feature rows, raw decision traces, trace steps, event-processing runs, action conflicts, and trace-summary cache rows. It is dry-run by default, chunks archive/delete by month, supports S3/object-store archive, and blocks delete unless archive or explicit unsafe override is supplied.
- Operator snapshot and trace summary materialization exist.
- API latency probe and slow-operation state exist. The probe now writes a latest JSON summary to `logs/performance/latest_api_latency_probe.json`, cron runs it several times per market day, and Operator Health surfaces stale, slow, or failed probes in the degradation feed and fix hints.
- The default API latency probe now measures the same compact, paginated list contracts used by the operator UI for Actions, Portfolio, and Watchlist (`limit=25&compact=true`), while full/unbounded payload checks remain explicit debug probes. This keeps `scripts/api_performance_report.py` focused on current operator pain instead of historical or deliberately verbose endpoints.
- API performance triage now has a read-only report script, `scripts/api_performance_report.py`, that combines latest probe rows and deduped slow-operation state into ranked endpoint priorities and endpoint-specific optimization guidance. The report now includes an evidence-freshness contract and operator action when probe data is missing or stale, so old slowlog rows do not silently drive new optimization work.
- Operator Health API-latency fix hints now include both the probe command and the ranked `scripts/api_performance_report.py --limit 20` command, so the UI/debug path points to evidence-backed endpoint prioritization instead of only the raw slowlog.
- The Health page runtime section now renders an API latency probe card with probe age, slow/error counts, and copyable probe/report commands, so the performance triage workflow is visible even before opening Fix Hints.
- Manual Review list payloads now default to compact source previews. Full raw rows remain available through `include_raw=true`, while every item also carries a compact `source_evidence` contract with read-only source kind, headline, and key facts for event policy, action conflicts, execution blockers, processing/announcement failures, identity issues, thresholds, action reviews, and wait-signal follow-ups.
- Manual Review compact payloads now strip full previous `item_snapshot` data from `latest_operator_decision`, cap nested raw previews more aggressively, and expose payload byte telemetry in the summary/queue contract; `include_raw=true` remains the explicit debug escape hatch for full raw state.
- The Manual Review page renders that payload telemetry in the Active Queue Contract panel, so operators can tell whether the current queue is compact or raw and how large the response is without reading API JSON.
- Operator Health details now default to compact bounded lists and truncated long strings. Full raw diagnostics remain available through `mode=full&compact=false`, while the Health page labels compact responses and reports omitted/truncated counts.
- Operator smoke/preflight output now compacts fix hints and current blockers at source, bounding nested lists and truncating long strings before both the CLI and `/api/operations/smoke` return payloads.
- Event-policy list rows now default to compact payloads that keep parsed checks, operator notes, LLM review, and compact raw context while omitting bulky source JSON columns. Full raw rows remain available through `include_raw=true`.
- Actions and Portfolio compact responses now include per-section payload byte telemetry in `meta` (`payload_bytes`, `avg_row_bytes`, `max_row_bytes`) so slowlog/probe follow-up can distinguish slow queries from oversized rows without opening full payloads.
- `/api/actions` and `/api/portfolio` now default to compact rows at the HTTP route layer while preserving `?compact=false`, `/api/actions/detail`, and `/api/portfolio/{symbol}/detail` for short debugging/full-evidence workflows, so normal Action Queue, Portfolio, and latency-probe reads do not pull bulky raw evidence by accident.
- Operator health now has explicit fast/full modes. The default API path runs bounded parallel checks, uses short API caching, defers heavyweight event-data/table-freshness/cron/fallback/API-error-history scans, and points to full diagnostic commands, so `/api/health/details` no longer times out on large source tables.
- Latest OHLCV price enrichment in the operator API now uses a short process-local cache, cutting repeated Action Queue reads from roughly 1.2s to roughly 0.2s in the 2026-06-09 probe.
- Central downloader/parser runner state is now captured from imported module entrypoints, including modules that end with `SystemExit`; exported `STOCKEY_RUN_STATE` counters survive both successful and non-zero exits, so Health/Data Health can use module-level row/retry/source-unavailable/no-data details instead of only wrapper-level failures.

Gaps:

- `Done`: standardize retry policy for direct `db_session()` access paths in scanned runtime code. Shared `sql_to_df`, `get_sql`, metadata helpers, upsert wrappers, schema-dump metadata fetches, schema-migration registry writes, and the reusable `execute_db_operation` transaction wrapper retry transient failures and emit file-backed retry telemetry. A static coverage utility, `scripts/db_retry_coverage_report.py`, now reports direct versus retry-wrapped `db_session()` blocks; the 2026-06-12 scan reported `{'core_helper': 5, 'wrapped': 54}` and no direct unwrapped blocks across `advisory`, `data`, `scripts`, and `utils`. Action-conflict resolver delete/update transactions, operator API conflict-rule promotion/update writes, Screener.in ad-hoc result cleanup, Screener.in registered screener registry/snapshot writes, advisory screener-constituent replacement cleanup, company-master-id backfill transactions, Dhan scrip-master bulk update transactions, deprecated-table cleanup DDL, hot-table retention archive/delete transactions, announcement text S3 offload metadata updates, announcement S3 pointer validator fetches, file-level ingestion-state reads/writes/cleanup, external-task queue claim/complete/fail transitions, superseded-failure cleanup DDL/marking writes, identity-issue record/resolution writes, decision-trace conflict-rule seeding, watchlist rebuild cleanup, rule-engine rebuild cleanup, market-news overlay rebuild cleanup, market-regime rebuild cleanup, portfolio-order cleanup, risk-allocation cleanup, lifecycle/rebalance cleanup, tightened-stop baseline updates, consolidated-action cleanup, event-risk cleanup, technical-feature rebuild cleanup, intraday-feature rebuild cleanup, exchange-event repair updates, trace-summary cleanup, wait-signal matched-status updates, and NSE offmarket parser schema-maintenance transactions now use the wrapper.
- `Partial`: every fallback query path should log “fallback used” into a durable telemetry table or health payload. Runtime fallback events use `advisory_fallback_events`; DB retry events and selected hot-path source fallbacks use local JSONL spools to avoid circular DB writes and are folded into Health. Current-price cache fallback, operator snapshot fallback, risk ADV20 liquidity-cap fallback, risk macro/exchange context lookup fallback, event-router fallback, external task queue fallback, config-change assistant fallback, research-ledger fallback, signal-refresh source-context lookup fallback, action conflict-rule fallback, wait-signal source-evidence fallback, feature-freshness lookup fallback, company-memory source-context fallback, news-theme market-news fallback, event-policy evaluator source fallback, event-data-quality checker fallback, company-master lookup fallback, execution broker/reconciliation fallback, source-date parser fallback, operator diagnostic serialization fallback, and maintenance/date/display fallback are covered. The only scanner-visible silent handlers are intentionally self-protecting fallback-telemetry internals where adding telemetry would create recursive noise; future work should focus on new source/API fallbacks as they are introduced.
- `To do`: add indexes, section snapshots, detail endpoints, or additional payload compaction for remaining API hot paths after reviewing `scripts/api_performance_report.py` output, Actions/Portfolio payload-byte telemetry, and Postgres query plans.
- `Done`: enforce compact/paginated API payloads for frontend list pages. Actions, Events, Portfolio, trace summary/list payloads, cron logs, prompt registry, hypotheses, artifact manifests, research ledger, and research review/previews now expose stable pagination or bounded-list metadata with focused regression coverage. Event-model artifact inspection now also exposes a read-only operator boundary plus local file hash/key metadata and S3 HEAD evidence in Operations; research-ledger inspection now exposes recent run provenance, validation protocol, result metrics, config snippets, and no-policy/no-broker boundaries in Operations; TS forecast review/application state is visible on the home TS workflow panel.
- `Partial`: reduce operator API latency. 2026-06-09 probe: `/api/health/details` improved from timeout to ~0.64s cold / ~0.002s warm by using bounded parallel fast checks and short API caching; `/api/hypotheses?limit=25` improved from ~1.45s to ~0.64s cold / ~0.003s warm; `/api/actions?...compact=true` improved to ~0.74s cold / ~0.10s warm by using section snapshots, current-price cache, one company-memory review lookup, and short API caches. On 2026-06-12, `/api/actions` and `/api/portfolio` route defaults changed to compact so direct/probe reads use bounded payloads unless `compact=false` is explicit; `/api/actions?include_feature_freshness=true` now uses persisted decision-time freshness by default and only runs current live freshness checks when `refresh_feature_freshness=true` is explicit. In-process `/api/actions` builder measurement after compaction is ~0.10-0.12s warm with ~184 KB JSON for the default frontend query; after tightening compact action trust/manual-review fields and keeping full details on detail routes, the live compact probe measured `/api/actions?limit=25&compact=true` at ~104-451 ms / ~159 KB depending on cold DB/cache state, down from ~406 ms / ~177 KB, while preserving the execution-safety contract and prioritizing the execution-boundary check. `/api/home` now omits duplicated action/today cards by default, loads summary/runtime/cron/sync through partial section snapshots instead of the full dashboard snapshot, tolerates missing optional `ts_forecast_paper_summary`, and measured ~315 ms cold / ~2 ms warm with ~16 KB JSON; `/api/summary` now uses the same section-snapshot path and measured ~193 ms cold / ~2 ms warm with ~16 KB JSON. `/api/portfolio` now supports selected-section `bucket` reads, and the landing page uses that to reduce measured portfolio JSON from ~160 KB for all sections to ~72-74 KB for recommendation tabs and ~8 KB for the raw portfolio tab; `/api/watchlist` now supports selected-section reads and compact TS forecast rows, reducing all-section JSON from ~253 KB to ~91 KB and selected sections to ~6-34 KB. Current fresh probe status: Operator API is reachable, TS forecast promotion check returns a research-only blocked payload instead of 500 when the paper table is absent, and all default probe endpoints are below the 750 ms slow threshold.
- `Done`: add hot/cold retention for old trace and intraday rows. The utility has unit coverage and fails loudly when Postgres is unavailable; live dry-run verification on 2026-06-11 was blocked because configured Postgres refused connections after retries.
- `Partial`: finalize S3/object storage offload for heavy text and OCR payloads. Announcement OCR/transcript/report offload now has dry-run/apply manifests with keys, hashes, byte counts, excerpts, and nulling flags, plus `scripts/validate_announcement_s3_pointers.py` for read-only dry-run, HEAD/size, and optional SHA-256 validation. `scripts/heavy_payload_inventory.py` now identifies non-announcement text/json/blob-like columns by catalog stats, risk, and recommended action; remaining work is running it on live DB when Postgres is reachable and remediating any high-risk findings.
- `Partial`: add DB schema migration/version tracking. `utils.schema_migrations` now provides a `stockey_schema_migrations` registry, checksum-protected idempotent apply, durable failure recording, dry-run/list/ensure-table CLI modes, focused regression tests, and Operator Health/fix-hint visibility for missing, running, or failed migration rows. The high-risk Dhan daily/intraday OHLCV, Dhan scrip master, file-level ingestion state, NSE security identity history/review output, market regime snapshot output, Screener.in registered screener snapshot output, Screener.in ad hoc query output, Screener.in parse/fetch failure audit, Economic Times RSS item output, market news overlay output, hypothesis/playbook output, external task queue, identity-issue tracking, fallback telemetry, current-price cache, wait-signal outputs, live event-router action output, trace-summary cache, execution order/fill handoff, company-memory review output, event-policy action overlay, adversarial-review output, announcement-watch output, news-watch output, news theme screener mapping, config-change preview output, intraday feature cache, watchlist builder output, rule-engine candidate/rejection output, operator API write-audit output, technical-threshold promotion review output, signal-quality promotion review output, risk allocation output, consolidated advisory action-recommendation, decision trace/action-conflict audit, event-evaluation/risk output, event meta-model score output, event-policy evaluator output, technical-threshold calibration output, signal-quality evaluator output, portfolio-order, lifecycle/rebalance, signal-refresh, shared sync-state, continuous-watch alert, research-ledger, TS forecast feature output, TS forecast workflow watchlist, TS forecast evaluator, TS forecast paper-portfolio output, announcement text S3 offload metadata columns, and dynamic company-master-id backfill columns now route through named registry migrations. Remaining direct DDL is limited to generic `utils.db` table/upsert helpers and source-specific temporary/staging tables.

## 7. Feature Generation

Status: `Partial`

Done:

- Macro snapshots/features exist.
- Regime snapshots exist.
- `advisory.regime_overlay` now creates review-only dynamic regime overlay proposals from base regime, market context, macro features, recent market news, and announcement-event counts. It can use Codex structured output when explicitly enabled, but defaults to a deterministic fallback and persists `authority_scope=review_input_only`, `production_status=proposed`, prompt version, evidence, proposed rules, expiry, and operator questions in `advisory_regime_overlay_proposals`. `/api/regime-overlays` and the Nuxt `/regime-overlays` page let the operator approve for testing, promote to review-rule candidate, reject, or request more evidence; all decisions are audit/review state only and do not mutate action policy, portfolio, or broker state.
- Market context exists.
- Technical features and technical engine exist.
- Intraday features exist.
- Fundamental snapshots and peer sync exist.
- Exchange event/features exist.

Gaps:

- `Partial`: add feature freshness contract per feature table. `advisory.feature_freshness` now classifies core symbol inputs as fresh, stale, missing, error, or intentionally skipped, exposes `/api/symbols/{symbol}/feature-freshness`, persists decision-time freshness snapshots on action rows, shows required-input summaries in the Action Queue, shows current plus decision-time Data Inputs Used panels on symbol detail pages, provides explicit stage dependency gates for `rules`, `risk`, `portfolio`, `lifecycle`, and `actions`, downgrades blocked rules `PASS_NOW` candidates to watch, moves blocked risk allocations to review-only, defers blocked portfolio capital, annotates lifecycle warnings without suppressing exits, downgrades positive broker-capable action winners to `MANUAL_REVIEW` when required decision-time inputs are blocked, surfaces stage-level blockers through Operator Health, and shows persisted plus current per-symbol gate effects in Symbol Detail.
- `Partial`: add explicit feature dependency graph so a stage cannot silently use stale upstream rows. Gate summaries are now emitted for the core decision stages, every core stage now has conservative behavior or annotation, Health exposes blockers, and symbol pages show persisted decision-time gate effects plus current read-only gate checks. Remaining work is broader point-in-time feature testing and materialized/current snapshots.
- `Partial`: explain in the UI which feature was missing or stale when an action became Manual Review. Required stale/missing core inputs are now visible beside action rows and on symbol detail pages, including persisted decision-time snapshots for action rows, current stage-gate blockers, and positive action downgrades preserve `feature_freshness_blockers` plus `blocked_original_action_code` in action context; remaining work is broader Manual Review source coverage.
- `To do`: reduce expensive full-table feature queries with materialized/current snapshots.
- `To do`: add feature-level tests for point-in-time behavior and no lookahead.
- `Partial`: add an operator-reviewed promotion path for dynamic regime overlay rules before action policy can consume them. Proposal review/decision audit exists in API/UI, but a separate reviewed config/rule implementation is still required before promoted review-rule candidates can affect market gates or sizing.

## 7A. Announcement, Bhavcopy, And LLM Signal Intelligence

Status: `Partial`

This is the next quality-upgrade program. The current Dhan OHLCV technical spine is the strongest part of the system. The weaker part is turning corporate announcements and NSE/bhavcopy-derived data into compact, point-in-time evidence that can be used by deterministic rules, LLM reviewers, action consolidation, and the operator UI.

Done:

- Dhan OHLCV is the canonical price source for active technical analysis.
- Exchange event and exchange feature tables exist and are consumed by event/risk/model paths.
- Announcement ingestion, OCR/transcription, summarization, structured reports, event evaluation, event policy, wait signals, and action consolidation exist.
- LLM/Codex usage is bounded to extraction/review/signal explanation, not direct broker authority.
- `advisory.event_evidence_store` builds compact V1 tables: `advisory_bhavcopy_evidence_daily` for liquidity/deals/short/circuit/volatility/margin evidence and `advisory_announcement_evidence` for announcement metadata plus latest LLM evaluation fields.
- `advisory.llm_event_evaluator` now consumes `advisory_announcement_evidence` before raw announcement documents, marks raw-document fallback explicitly, and adds latest point-in-time `advisory_bhavcopy_evidence_daily` into exchange context.
- `advisory.company_memory_review` writes review-only company-memory summaries into `advisory_company_memory_reviews` from compact announcement evidence, compact bhavcopy evidence, technical/candidate state, event policy, wait signals, and latest action rows. It is deterministic by default and Codex is opt-in.
- Company-memory review payloads now include an explicit compact evidence-source contract showing announcement, bhavcopy, technical, event-policy, action, and wait-signal coverage. Action Queue and Symbol Detail surface `complete` versus `partial` coverage and list missing required compact sources, so operators do not mistake a partial review for full historical evidence.
- `advisory.signal_quality_evaluator` compares realized outcomes for `technical_only`, `technical_plus_event`, `technical_plus_bhavcopy`, `technical_plus_company_memory`, and `technical_plus_all` variants using point-in-time overlays and future Dhan OHLCV returns after costs. It is research-only and does not change live action policy.
- `/api/signal-quality` and the Nuxt `/signal-quality` page expose the latest signal-quality run with overlay coverage, lift versus technical-only, and selected matured examples for manual review.
- `advisory.exchange_events` now reads normalized and bhavcopy corporate-action sources, maps corporate-action `action_type` into stable event types, and exposes `--repair-missing-types` for legacy null-typed deal/short-selling/unclassified rows.
- Sparse NSE crawlers for corporate actions, earnings, and insider deals now persist `advisory_sync_state`, and `advisory.event_data_quality` uses that state to avoid confusing “checked recently, no new sparse events” with failed ingestion.

Current findings:

- Legacy `advisory_exchange_events` null typing is now repairable by command. A first live repair classified legacy deal and short-selling rows, but the final catch-all repair still needs to be rerun after Postgres is stable because the DB was shutting down during verification.
- NSE block deals, bulk deals, short selling, circuit hits, CMVOLT, indices, and OHLCV have useful recent coverage, but they are not yet prepared as a broad queryable evidence layer.
- Earnings source coverage is stale in the current DB. Corporate-action and insider false positives are resolved by correct table mapping plus sparse-source sync-state support. Corporate-action feature influence is now proven nonzero on a rebuilt 2025-10-28 smoke day; broader historical/current rebuild coverage remains.
- Announcement documents and reports are recent and rich, but event-policy output is still too coarse for some cases; many rows become Manual Review without enough actionability context.
- Raw announcement text and large bhavcopy tables are too heavy for direct UI/advisory querying. V1 compact cached evidence tables now exist, and the event LLM path has switched to compact announcement/bhavcopy context. Remaining downstream consumers still need to switch from raw scans to these stores consistently.

Gaps:

- `To do`: add more OHLCV technical algorithms on top of Dhan data: breakout/retest/pullback state detection, volatility compression, relative strength, stop/target/time-horizon logic, partial-exit rules, and realized-outcome validation.
- `To do`: create a complete corporate-announcement taxonomy that maps every announcement into one of three storage forms: compact structured event, structured event plus summary text, or archived raw/unstructured evidence with metadata and source pointers.
- `Partial`: create an announcement evidence/actionability tensor with materiality, direction, surprise, novelty, contradiction, confidence, expected decay, source reliability, affected peers/sectors, price reaction, current exposure, and suggested next evidence. V1 event-policy rows now persist `actionability_json` with materiality, freshness, calibrated source quality, affected market scope, current exposure, price reaction, suggested next evidence, review priority, and deterministic authority boundary. Remaining work is broader downstream use outside Event Inbox/action policy.
- `Partial`: create a bhavcopy evidence store that the LLM and deterministic rules can query without scanning raw tables. V1 exposes liquidity, turnover, circuit behavior, block/bulk accumulation, short-selling pressure, volatility, margins, and abnormal participation; the event LLM path consumes the latest row. Market-context summaries now split broad top-context evidence into material `triggered` and context-only `context_observed` counts. Corporate actions, index/sector context, broader deterministic rules, and UI explainability consumption remain.
- `Partial`: add an LLM company-memory review flow that can inspect historical company events, prior decisions, current technical state, bhavcopy evidence, latest announcements, and wait signals to propose `BUY`, `SELL`, `HOLD`, `SELL_PARTIAL`, `BUY_MORE`, `WATCH`, or `NO_ACTION`. V1 is implemented as `review_input_only`, deterministic by default, persisted to `advisory_company_memory_reviews`, wired as the `company_memory` pipeline stage, attached to action API rows, and visible in Action Queue plus Symbol Detail. Rows carry a compact evidence-source contract and UI warnings for missing required compact announcement/bhavcopy/technical sources. Outcome comparison is available through `advisory.signal_quality_evaluator`; review-only overlay promotion decisions are available through `advisory.signal_quality_promotion`; approved decisions can generate reviewed config diffs through `advisory.config_change_assistant`. Remaining work is action-consolidation influence rules after evidence proves lift and an operator manually applies a reviewed config/rule change.
- `To do`: keep final broker-executable action authority in deterministic action consolidation. LLM signal output must be an input with rationale/confidence, not the final execution decision.
- `Done`: centralize LLM/Codex prompt contracts in `advisory.prompt_registry` with prompt ids, versions, schema names, model env vars, source files, authority scopes, output tables, fallbacks, and migration status; expose it through `/api/research/prompt-registry` and the Nuxt `/prompt-registry` page.
- `Done`: migrate current core prompt callers to registry-backed prompt ids/version constants and persist prompt/schema metadata on current LLM/Codex output rows, including advisory event evaluations, event-policy manual review notes, playbook action plans, company-memory reviews, manual-revision pointers, technical-threshold promotion reviews, announcement summaries/OCR metadata, and structured announcement reports. Future LLM/Codex callers must add registry contracts plus row-level prompt/schema metadata before they are considered production-auditable.
- `Partial`: surface this evidence in the operator UI: what happened, what data was used, what the LLM concluded, what deterministic rules accepted/rejected, what evidence is stale/missing, and why the final action differs from raw signals. Event Inbox now shows event-policy actionability, affected sectors/peers, and research calibration buckets for source quality and market scope; broader action-consolidation and historical company-memory deltas remain.
- `Done`: add an evaluator that compares technical-only outcomes against technical-plus-event-policy, bhavcopy, and company-memory outcomes after costs, using point-in-time data.
- `Done`: add operator UI/API summaries for `advisory_signal_quality_eval_summary` so overlay lift/worsening can be reviewed before any production rule change.
- `Done`: add a manual signal-quality overlay-promotion review workflow that writes review/decision audit rows, shows Trust Gate context in the Nuxt `/signal-quality` page, and emits copyable patch guidance without changing live policy.
- `Done`: add reviewed config-change diff generation for approved threshold and signal-quality overlay decisions without applying the change.
- `To do`: add deterministic action-consolidation influence rules only after a generated diff is manually reviewed and applied.

## 8. Screener And Candidate Universe

Status: `Partial`

Done:

- Production and ad hoc Screener.in flows exist.
- Training universes can be synced from ad hoc screeners.
- Screener snapshots/constituents feed the advisory universe.
- Query syntax fixes such as `DMA 50` / `DMA 200` are known.
- Screener fetch/parse failures now persist to `screenerin_parse_failures` with URL, query hash/text, query name, sanitized body excerpt, HTML context flags, error class/message, and optional HTTP/final URL context before the original exception is re-raised.
- Ad hoc Screener queries now run through a local syntax validator before Chrome/network access. The validator catches known bad moving-average aliases such as `50 Day Moving Average` and compact `DMA200`, and suggests Screener.in syntax like `DMA 50` / `DMA 200`.
- Operator Health now summarizes recent `screenerin_parse_failures` rows and surfaces them in fix hints, the Advisory Trust Gate, and the degradation feed so Screener query/login/parse failures are visible as operational issues instead of only appearing as generic downstream symptoms.
- The Nuxt `/screeners` workbench and `/api/screeners/preview` endpoint let operators validate ad hoc Screener.in queries locally and optionally fetch bounded authenticated preview rows with `persist=false` before registering any production screener.
- `advisory/screener_coverage.py`, `GET /api/screeners/coverage`, and the Nuxt `/screeners` page now summarize how much each screener contributes to constituents, rule candidates, and final consolidated action rows over a bounded lookback window.
- `GET /api/screeners/failures` and the Nuxt `/screeners` page now expose recent Screener.in validation/fetch/parse failures as a read-only operational repair panel with URL/query/error excerpts and an explicit no-Manual-Review/no-broker/no-registration boundary.

Gaps:

- `Partial`: maintain a tested query library for Screener.in syntax so bad fields fail before runtime. V1 covers known DMA moving-average aliases; broader Screener field/operator validation remains.
- `Done`: store screener fetch/parse failures with URL/query/body excerpt for debugging.
- `Done`: prevent repeated generic screener failures from flooding Manual Review. Recent failures are visible in Health/fix hints/trust gate/degradation and in the source-specific `/screeners` failure panel as operational repair work rather than investment Manual Review.
- `Done`: add operator UI to test an ad hoc screener and preview rows before using it.
- `Done`: add coverage metrics: how much of action universe came from each screener and how often each screener produces useful outcomes.

## 9. Technical Engine

Status: `Partial`

Done:

- Swing technical PRD is reflected in code direction.
- Technical score buckets exist.
- Technical threshold calibration exists and writes research-only evidence.
- Technical Decision Panel in UI exposes technical state, trigger type, pivot, stop, target, score buckets, and lifecycle exit context from direct row fields and nested reason-contract evidence.
- Threshold promotion helper is manual-only and does not auto-edit config.
- Post-entry technical states `HOLD`, `ADD_ON_PULLBACK`, `PARTIAL_EXIT`, `FULL_EXIT`, and `EMERGENCY_EXIT` are now covered through lifecycle classification regressions, and technical full exits now persist `TECHNICAL_FULL_EXIT` bucket/audit condition labels in lifecycle outputs.

Gaps:

- `Partial`: finish translating every PRD state into tested code paths. Pre-entry `NEAR_PIVOT` and `BUY_TRIGGERED`, hard-reject behavior, and post-entry `HOLD`, `ADD_ON_PULLBACK`, `PARTIAL_EXIT`, `FULL_EXIT`, and `EMERGENCY_EXIT` have focused tests; remaining work is broader `WATCHLIST`, `READY`, and setup-specific entry archetype coverage.
- `Partial`: add tests for stop, target, time horizon, partial-profit, and technical invalidation behavior. Stop/invalidation, target trim, time-stop loser exit, technical full/partial/emergency/add-on states, and no-backdated paper entry are covered; remaining work is broader trailing-stop and per-setup stop update behavior.
- `Partial`: make technical conflict precedence explicit when event/risk/lifecycle disagree. Exit/risk-management beats entries, market-gated and adversarial Manual Review beat positive/watch candidates, and event-policy Manual Review now has a named default rule over BUY/BUY_MORE; remaining work is broader setup-specific lifecycle/technical semantic rules only after repeated operator resolutions prove them.
- `Done`: Action Queue compact rows now keep reason-contract/manual-revision explanations and show a consolidated “why not approved/executable” blocker summary for blocked or review-only candidates, including original action, final action, reason-contract status, freshness blockers, approval/reconciliation state, and execution-safety issues.
- `Done`: Action Queue cards now show the consolidation summary directly: one final winning action, winning source, candidate/conflict counts, selected conflict rule/reason when present, and the top losing candidates.
- `Done`: Action Queue status filtering now uses an explicit `action_queue_contract`: the `approved` filter means broker-candidate final action only, not upstream text such as `portfolio_status=approved`. Review-only/manual rows are excluded from approved results even if an earlier source candidate was approved, and the UI labels the filter as “Broker candidate.”
- `Done`: Action Queue API rows now include a `final_state_trust` contract and the UI renders it before detailed panels. It states the final action, queue status, whether the row is broker-candidate, reason-contract state, action-transition preconditions, feature-freshness state, execution-boundary status, blockers, and warnings in one place.
- `Done`: Action Queue cards now render a first-read Portfolio Eligibility panel for every row. It classifies rows as manual review, watch-only, hold/context, exit/risk-reduction, broker-candidate blocked, portfolio-handoff candidate, or not portfolio-eligible, with the next operator step and top blockers derived from final-state trust, action-transition, execution, portfolio-handoff evidence, and matching symbol-level identity/source blockers.
- `Done`: Nuxt operator traces now surface technical state, trigger type, pivot, sub-scores, risk levels, and exit condition through the shared Technical Decision panel, including when the data arrives only inside `recommendation_reason.evidence`.
- `To do`: connect calibration evidence to a manual config patch workflow with strong review gates.

## 10. LLM Event Extraction And Event Policy

Status: `Partial`

Done:

- Event evaluator produces structured event rows.
- Event policy maps event classes into bounded overlays.
- Low-information event-policy rows can downgrade to `NO_ACTION`.
- Event-policy manual-review rows now use human-facing text in the API.
- Event-policy evaluator checks realized outcomes after costs.
- LLM is used for extraction/review, not direct broker authority.

Gaps:

- `Partial`: all LLM failures should create a durable, categorized issue with whether deterministic fallback was used. Core Codex/LLM fallback paths now write `advisory_fallback_events` and Health shows counts/samples; event-processing/document failures and event-policy rows derived from superseded LLM-error evaluations now have recovery/supersession handling. Remaining work is broader supersession coverage for any other persisted LLM-derived review rows.
- `Done`: old event-policy Manual Review rows derived from `llm_error` fallback evaluations are suppressed from Manual Review when a later completed, non-LLM-error event evaluation exists for the same setup/symbol/event; matching shadow action-review rows from event policy are suppressed as well.
- `Partial`: event-policy class/actionability rules need more outcome-driven calibration and operator review. `advisory.event_policy_promotion` now creates manual-only review/decision audit rows and copyable review-rule guidance from event-policy evaluation summary groups, the Event Inbox can create reviews plus record approve/reject/needs-more-data decisions, and approved reviews can generate/display preview-only disabled `event_policy_review_rules` diffs through the config-change assistant. Repeated-run/operator-feedback calibration and any manual config application decision remain.
- `Done`: add a V1 source-quality model for news versus exchange announcements versus OCR/document-derived evidence. Event-policy actionability now distinguishes primary exchange filings, secondary market news, derived document/OCR evidence, parse failures, confirmation requirements, and confidence ceilings; event-policy evaluation summaries now group realized outcomes by source quality/family/authority/confirmation requirement.
- `Done`: require “actionability” fields on every event-policy row, including Manual Review: materiality, freshness, current holding exposure when available, price reaction when available, source quality, suggested next evidence, review priority, and no-broker deterministic boundary.
- `Partial`: make event-policy rows explicitly symbol/date/current-state aware, not just event aware. V1 actionability now records symbol/date freshness, affected sectors/peers, and, when source schemas are present, joins point-in-time Dhan daily close reaction plus latest portfolio status/allocation before policy generation. Event-policy evaluator now summarizes outcomes by source-quality and market-scope buckets, Event Inbox shows those calibration groups, and manual-only event-policy promotion reviews can generate review-rule guidance, operator decisions, and preview-only config diffs. Remaining work is operator-feedback calibration of source quality and broader downstream use beyond Event Inbox/action policy.
- `To do`: separate LLM outputs into extraction, company-memory review, proposed signal, adversarial challenge, and final deterministic policy input so prompts and schemas are auditable.
- `To do`: store prompt/skill version, input evidence IDs, output schema version, and deterministic acceptance/rejection reason for every LLM-generated signal.

## 11. Adversarial Review

Status: `Partial`

Done:

- Deterministic adversarial review exists.
- It can clear, penalize, force manual review, or veto.
- Risk engine consumes review context.
- Action ranking now has an explicit adversarial-veto precedence rule so vetoed event-policy manual reviews beat positive broker and watch candidates, even if raw candidate priorities are misleading.
- Compact operator action rows preserve adversarial-review event evidence, and the Action Queue reason-contract panel shows veto status, review action/status, and review reason.

Gaps:

- `Partial`: define exact precedence between adversarial review, event policy, market context, technical signals, and lifecycle exits. Adversarial veto over BUY/WATCH, event-policy Manual Review over positive entry/add-on candidates, and risk-off market-gated BUY_MORE plus WATCH collisions are now pinned; broader cross-system precedence remains.
- `Done`: add regression tests for veto beating buy/watch candidates.
- `Done`: show adversarial-review reason in the action queue, not only trace/details.
- `To do`: evaluate veto precision and false positives over realized outcomes.

## 12. Hypothesis, Playbooks, And Wait Signals

Status: `Partial`

Done:

- Hypothesis/playbook config and UI exist.
- Hypothesis scans create matches/action plans.
- Trusted playbooks can bridge as review-only overlays.
- Wait signals exist and match price/news/announcement conditions.
- `watch_for_event` manual-review decisions now create active `advisory_wait_signals` rows.
- New wait signals store typed `condition_json.condition_type` contracts for `price_level`, `event_keywords`, `clarification_filing`, `result_update`, `management_commentary`, `sector_event`, and `expiry_only`.
- The matcher routes by typed condition instead of only `signal_type`; invalid or unknown condition types return issue rows rather than crashing.
- Wait-signal matches now include originating wait-signal context in `evidence_json.wait_signal`.
- Signal refresh escalates matched waits conservatively: negative waits become reduce-review signals, positive waits become WATCH, and unknown waits become MANUAL_REVIEW. It does not create broker-executable trades.
- Watchers run wait-signal matching.
- Operator frontend now has a first-class `/wait-signals` page with active, matched, expired, and closed buckets, source labels, readable condition summaries, and latest match evidence.
- `/api/wait-signals` now returns compact `summary`, `sections`, and match-enriched signal rows while preserving raw-compatible `signals` and `matches` arrays.
- Matched manual-review wait signals now expose `manual_review_item_id`, source table/key, original wait question, and latest evidence in API/UI payloads.
- Manual Review now surfaces matched manual-review wait signals as follow-up review items without mutating portfolio, action recommendations, or broker execution.
- Manual Review now treats prior operator decisions as stale when the underlying source row updates after `decided_at`, including canonical duplicate/conflict keys. Closing decisions such as `ignore` or `downgrade_to_no_action` no longer hide newer refreshed evidence; the item reopens with `manual_review_state.state=source_updated_after_operator_decision`.
- Regression coverage exists for the wait-signal API payload shape and manual-review/playbook signal visibility.
- A mocked operator journey regression covers Manual Review `watch_for_event` decision recording, wait-signal creation, event matching, stale waiting-item replacement with a reopened follow-up, and review-only action-candidate linkage.

Gaps:

- `Done`: wait-signal matching now has typed conditions for price level, event keywords, clarification filings, result updates, management commentary, sector events, and expiry-only waits.
- `Done`: UI shows wait signals created from manual review separately from playbook-generated wait signals.
- `Done`: when a wait signal matches, the UI/API shows “what changed,” links back to the original Manual Review item, suppresses the stale waiting item, and surfaces the matched wait as a reopened follow-up item.
- `Done`: matched Manual Review wait signals now create durable review-only action-recommendation candidates linked back to the original manual-review item and match evidence.
- `Done`: add tests for manual-review wait signal creation, matching, closure, conservative escalation, and the core operator journey. Creation, API visibility, typed parsing, price/event matching, expiry-only, invalid-condition handling, stale waiting-item replacement, closed-original suppression, review-only signal refresh, and review-only action-candidate linkage are covered.

## 13. Rules, Risk, Portfolio, Lifecycle

Status: `Partial`

Done:

- Rule engine creates candidates/rejections.
- Risk engine builds allocations.
- Portfolio engine plans position candidates.
- Lifecycle engine handles hold/exit/rebalance outputs.
- Stop, target, horizon, bucket, and lifecycle concepts exist.
- Portfolio rows now carry an explicit `position_state` and `state_transition_contract_json`. Approved/trimmed portfolio outputs are `PLANNED_ENTRY`, not assumed holdings; deferred rows are `DEFERRED`; and the contract requires lifecycle to prove entry with the first available close on or after portfolio `published_on`.
- Action consolidation now preserves portfolio `position_state`, `state_transition_contract_json`, target, and horizon fields from portfolio rows into `advisory_action_recommendations`, and the recommendation reason contract exposes a `portfolio_transition` evidence section so Action Queue/Symbol Detail can explain the portfolio handoff before any execution preview exists.
- Recommendation reason contracts now include a normalized `action_transition` evidence section for HOLD, BUY, BUY_MORE, SELL, PARTIAL_SELL, TIGHTEN_STOP, WATCH, and MANUAL_REVIEW. It states the expected state effect, whether the row is only a broker-order candidate, why broker execution is still disallowed at the action layer, and the next required stage such as execution preview, policy audit, monitoring, or operator review. Broker-capable BUY/BUY_MORE/SELL/PARTIAL_SELL transitions now explicitly report missing entry/add-on/exit preconditions until a valid entry/reference-price timestamp exists on or after `published_on`.
- Execution planning now blocks legacy/ambiguous portfolio rows that lack `position_state=PLANNED_ENTRY` or a valid portfolio transition contract. The blocked preview skips Dhan identity lookup, records `submit_blocked`, and carries the transition issue inside `safety_checks_json` instead of silently treating the row as executable.
- Execution planning now also blocks broker-capable action rows whose nested recommendation `action_transition` contract explicitly reports incomplete preconditions. The blocked preview skips Dhan identity lookup, records the missing transition precondition in `safety_checks_json`, and preserves the transition evidence in order-intent lineage.
- Operator API compact action payloads now preserve the nested `portfolio_transition_contract`, and the Action Queue renders a portfolio handoff boundary panel with planned-entry state, entry-evidence rule, broker-direct block status, and transition-contract issues.
- Lifecycle context now records price status, paper-entry assumption, stop/target status, and operator question for review-only states.
- Regression tests prevent backdated paper entries by requiring the lifecycle entry date to use the first available close on or after portfolio `published_on`.
- Lifecycle output now labels technical full exits as `TECHNICAL_FULL_EXIT` active exit conditions, so technical thesis failure is visible in bucket-status/audit fields instead of only in the suggested action reason.
- Tightened-stop baseline updates now write durable `advisory_lifecycle_policy_changes` audit rows in the same retryable transaction as the portfolio stop update, missing target baselines are initialized from lifecycle output with `target_initialized` audit rows, non-improving/invalid policy recommendations are tested to skip mutation/audit writes, and Symbol Detail/portfolio detail API now surface recent policy-change rows for operator review.
- Operator Health now checks recent `tighten_stop` rebalance actions for matching `advisory_lifecycle_policy_changes` rows and emits fix hints/fallback telemetry when stop-policy audit coverage cannot be verified.

Gaps:

- `Partial`: formal state machine from recommendation to assumed entry to holding to exit. Portfolio now emits an explicit planned-entry transition contract and does not assume holdings; action consolidation preserves that contract into action reason evidence; every consolidated action now carries an `action_transition` evidence section for hold/add/reduce/exit/policy-review semantics; broker-capable entry/add-on/full-exit/partial-exit rows now expose required preconditions, missing preconditions, precondition status, and the no-direct-broker boundary in both full and compact reason contracts; broker-capable rows specifically expose missing post-publication price evidence instead of looking execution-complete from a backdated/ambiguous row; broker-capable rows with missing or wrong buy/sell transaction side now fail-soft to `MANUAL_REVIEW` with the missing side recorded in the manual-review reason contract; Action Queue and Symbol Detail render those execution preconditions before approval/reconciliation gates; execution refuses ambiguous portfolio handoffs without the entry contract; Action Queue renders the planned-entry/execution-boundary state; lifecycle owns the transition to assumed entry after post-publication price evidence. Remaining work is full execution/reconciliation semantics for non-entry broker candidates and richer UI rendering for every transition state.
- `Done`: no backdated paper lifecycle entries from newly approved portfolio rows; tests cover ignoring pre-approval price history and routing missing post-approval prices to review-only lifecycle state.
- `Partial`: lifecycle needs stronger target/stop update rules and audit trail. Target trim, time-stop loser exit, technical full-exit bucket labels, tightened-stop baseline persistence, missing-target initialization with durable policy-change audit rows, Operator Health audit-coverage checks, Symbol Detail policy-change visibility, and no-backdated entry audit context have tests; broader trailing-stop rule calibration and target-change history for existing targets remain.
- `Partial`: partial exits, add-on buys, time stops, and emergency exits need deterministic tests. Lifecycle classification now covers technical partial exits, add-on pullbacks, loser time-stop exits, and emergency exits; remaining work is full build-output coverage for every state and portfolio mutation path.
- `Partial`: lifecycle review-only rows now include human-readable reason, current price when available, entry assumption, target/stop status, and operator question in context. Lifecycle/rebalance action-consolidation Manual Review rows now add explicit review-only/no-broker reason-contract boundary evidence for manual, stale, horizon, and target review states; broader non-lifecycle portfolio/action-consolidation Manual Review permutations remain.

## 14. Action Consolidation And Conflict Resolution

Status: `Partial`

Done:

- `advisory_action_recommendations` consolidates to one current action per symbol.
- Action conflicts and conflict rules are persisted.
- Conflict rules page exists.
- Resolved conflicts are audit-only and do not enter Manual Review.
- Repeated unresolved conflict rows for the same symbol/action pair are collapsed to the newest active Manual Review item, the API summary reports `duplicate_suppressed`, and the Manual Review UI shows a queue-hygiene card when duplicates are hidden.
- Reason contracts exist and can downgrade incomplete broker actions.
- Matched Manual Review wait-signal action candidates now satisfy the reason contract through explicit `wait_signal` evidence and `manual_review_wait_signal_matched` Manual Review boundary while remaining review-only and non-broker.
- Reason contracts now include same-symbol conflict/source-precedence evidence with winning source, losing candidates, conflict rule attribution, and deterministic precedence reason; compact Action Queue rows preserve and render that conflict evidence directly on each Action Queue card.
- Reason contracts now carry explicit market-gate evidence when positive broker actions are blocked by weak/risk-off market context, including BUY and BUY_MORE review-only Manual Review boundaries and blocked original action; compact Action Queue rows preserve the original action plus gate reason and breadth/risk metrics.
- Compact Action Queue/API display payloads now humanize internal reason/status codes such as `blocked_by_adversarial_review`, `positive_action_blocked_by_market_context`, and missing-field names before they reach the UI, while keeping action codes machine-readable.
- Broker-capable candidates downgraded by incomplete reason contracts now persist explicit Manual Review boundary evidence in the reason contract: original blocked action, missing fields, operator question, review-only effect, and `broker_execution_allowed=false`.
- Event-policy, playbook, market-gated portfolio, generic action-consolidation, and lifecycle/rebalance Manual Review rows now infer explicit Manual Review reason-contract evidence with source-specific or generic review boundaries, operator question/reason, review-only effect, source attribution, and `broker_execution_allowed=false`; lifecycle/rebalance review states distinguish manual, stale, horizon, and target review boundaries.
- Enabled deterministic conflict rules now have regression coverage proving they can directly affect candidate ranking when raw candidate priorities disagree; disabling rules falls back to ordinary priority/freshness ranking.
- Conflict Rules UI can now enable or disable existing deterministic rules through a narrow operator API update endpoint; this affects future ranking/reads and does not rewrite historical action rows.
- Conflict Rules UI/API can edit stored rule explanations for existing deterministic rules, and rule-table bootstrap preserves operator-edited explanation text instead of restoring the default text.
- Conflict Rules UI/API can promote latest unresolved manual action conflicts into disabled exact-match rule candidates with stored condition metadata; enabled promoted rules are consumed by the conflict resolver for future matching conflicts and by candidate ranking for same-symbol exact action/source pairs.
- Conflict Rules UI/API can edit supported `action_pair_exact` condition metadata for promoted/manual-resolution rules with backend validation, so unsupported condition shapes are rejected instead of silently saved.
- Built-in action-conflict classification now selects default rules by `rule_id` instead of list position, with regressions for same-action duplicate collapse and WATCH-loser classification. This prevents conflict audit rows from being mislabeled when default rule ordering changes.
- Promoted action-conflict rules now also support explicit `action_pair` conditions for action-code-only precedence across sources. The API can create/edit the condition type, the resolver and action ranker consume it, the UI explains the exact-vs-action-only boundary, and focused tests cover resolver, ranking, and API behavior.

Gaps:

- `Done`: enabled conflict rules influence `rank_action_candidates()` directly, with a focused regression for exit precedence overriding a misleading lower raw priority and disabled rules falling back to ordinary ranking.
- `Done`: enabled promoted exact-match conflict rules influence `rank_action_candidates()` directly for same-symbol action/source pairs, with regression coverage that nonmatching source conditions fall back to ordinary ranking.
- `Partial`: add UI controls to edit/disable conflict rules and promote manual resolutions into deterministic rules. Enable/disable controls, explanation editing, disabled-by-default manual promotion, resolver consumption, ranking consumption, exact-match condition editing, and action-code-only condition editing are wired; broader semantic rule types remain.
- `Partial`: every final action should show winning candidate, losing candidates, rule used, and why the loser lost. Backend reason contracts and compact Action Queue UI now show same-symbol source-precedence evidence, losing market-adjustment evidence, and winning rows touched by deterministic or promoted exact-match conflict precedence carry the rule id/reason/score into the reason contract; broader action-row attribution remains.
- `Partial`: add tests for same-symbol duplicate screeners, conflicting buy/sell/manual/watch rows, and market-gate downgrades. Market-gate blocking, compact market-gate visibility, explicit market-gated Manual Review boundary evidence, market-gated portfolio BUY and BUY_MORE Manual Review-over-WATCH source precedence, cautious-market portfolio BUY_MORE-over-WATCH sizing-reduction source precedence, event-policy Manual Review-over-market-adjusted BUY_MORE plus WATCH source precedence, adversarial-veto Manual Review-over-market-gated BUY_MORE plus WATCH source precedence, rebalance SELL-over-market-gated BUY_MORE plus WATCH source precedence, incomplete-contract Manual Review downgrade boundaries, event-policy/playbook/lifecycle/generic Manual Review boundary evidence, wait-signal, duplicate BUY screener collapse, default same-action duplicate rule attribution, default WATCH-loser rule attribution, portfolio BUY-over-WATCH precedence, lifecycle HOLD-over-WATCH precedence, portfolio BUY_MORE-over-portfolio BUY precedence, portfolio BUY_MORE-over-WATCH precedence, portfolio SELL-over-portfolio BUY precedence, SELL-over-WATCH precedence, generic MANUAL_REVIEW-over-portfolio BUY, PARTIAL_SELL-over-portfolio BUY, PARTIAL_SELL-over-WATCH, review-only TIGHTEN_STOP-over-portfolio BUY, review-only TIGHTEN_STOP-over-WATCH, MANUAL_REVIEW-over-WATCH precedence, BUY/SELL/MANUAL_REVIEW/WATCH collision attribution, event-policy/playbook/lifecycle source matrix, lifecycle exit over review overlays, event review over lifecycle hold, equal-priority freshness tie-breaks, source-precedence reason-contract paths, conflict-rule enablement/explanation updates, promoted exact-match resolver rules, promoted exact-match ranking rules, promoted action-pair resolver/ranking rules, and promoted exact-match reason-contract attribution have focused coverage; broader action-source permutations remain.
- `Done`: humanize compact Action Queue/API action reason fields before they reach the UI without rewriting stored recommendations or broker/execution contracts.

## 15. Manual Review Workbench

Status: `Partial`

Done:

- Manual Review page exists.
- Items are separated by lane: investment review, technical issue, research/config.
- Closing decisions remove items from active queue.
- `watch_for_event` now creates wait signals.
- Decision options now explain what happens, including a page-level decision guide and per-selected-choice active-queue/wait-signal/portfolio/action-row/broker effects before save.
- Manual Review rows and queue summaries show whether the item is investment-impacting, operational, research-only, or execution-blocking, plus the selected decision's queue effect before saving. Manual Review cards now render compact source evidence before the raw drawer so operators can understand the source row without `include_raw=true`.
- Operator manual documents every Manual Review dropdown decision with its runtime state, active-queue effect, side effects, and explicit non-authority over portfolio/action/config/broker mutation.
- Technical/operational failures are visible separately.
- `advisory/manual_review_state.py` centralizes allowed decisions, closing/non-closing behavior, next state, safety flags, decision-row construction, and wait-signal side effects.
- API decision responses now include `next_state`, `creates_wait_signal`, `mutates_portfolio`, `mutates_action_recommendation`, `submits_order`, plus the shared `manual_review_state` and `decision_effect` payloads used by the list view.
- Matched Manual Review wait signals now reopen as follow-up items, suppress the original waiting item from the active queue, and suppress the matched follow-up after an operator closes it.
- Manual Review decision filtering now falls back from exact transient item ids to canonical item identity, so a closed repeated action-conflict review stays closed even if the source row reappears with a different losing setup/source key.
- Manual Review API responses now include a `queue_contract` that separates displayed active items from closed/duplicate/matched-wait suppressed audit rows, states that hidden rows are audit-only, and confirms Manual Review decisions do not mutate portfolio rows, action recommendations, or broker orders. The Manual Review UI renders this contract above the queue.
- Incomplete reason-contract downgrades now carry explicit Manual Review boundary evidence and preserve the blocked original action without enabling broker execution.

Gaps:

- `Done`: API-level regression tests cover every decision option:
  - `needs_more_data`
  - `watch_for_event`
  - `add_operator_note`
  - `approve_for_manual_config`
  - `ignore`
  - `downgrade_to_no_action`
  - `mark_fixed`
- `Partial`: build an operator-decision state diagram and enforce it in code. Initial state/effect table, write-response state/effect contract, duplicate conflict-row suppression, canonical closed-conflict suppression, incomplete-contract Manual Review downgrade boundary, matched wait-signal reopen state, matched follow-up closure suppression, review-only action-candidate linkage, superseded metadata cleanup, and a mocked journey regression exist; broader downstream state coverage is not complete.
- `Done`: add bulk close/apply for superseded technical failures. Health exposes a guarded apply form that requires an operator reason and records `superseded_failure_cleanup_apply` in the command audit table while leaving portfolio/action/config/broker state unchanged.
- `Done`: link a manual-review item to its later wait-signal match and resulting review-only action candidate. Matched wait-signal follow-up is visible and the action candidate preserves the original item id plus match evidence; reason-contract enrichment now keeps the row `MANUAL_REVIEW`, complete, review-only, non-broker, and explicitly marked as `manual_review_wait_signal_matched`.
- `Done`: make “closing decision” versus “annotating decision” visually impossible to miss on Manual Review items.
- `Done`: show whether each Manual Review item and the overall Manual Review queue are investment-impacting, operational, research-only, or execution-blocking, and show compact source evidence for each item without requiring raw payload expansion.

## 16. Watchers And Fast Signal Refresh

Status: `Partial`

Done:

- `all_watchers.sh` runs continuous watch one-shot.
- OHLCV, announcement, news, router, wait-signal matching, signal refresh, operator snapshot, and trace summary are wired.
- Watcher cursor state exists.
- Watcher lock prevents overlapping runs.
- Signal-refresh rows now classify watcher output as `action_changed`, `wait_match_created`, or `evidence_only`, persist `previous_action` and `action_changed` as first-class fields, and write those fields into decision traces. The operator home page surfaces the effect beside recent watcher-triggered symbol decisions.
- Skipped not-due watcher cycles now publish per-source watcher events instead of only appearing inside the aggregate run summary.
- Shell-level watcher lock skips now publish per-source skipped watcher events plus a skipped summary without advancing cursors, marking success, or running downstream watcher/snapshot/trace steps.
- News and announcement watcher cycles now use bounded replay/catch-up windows, persist requested-from/requested-to metadata, and mark `catchup_truncated` when stale cursors exceed configured lookback limits.
- Watcher router context is now carried into fast signal refresh. Stop/invalidation price alerts can produce review-only `REDUCE_EXPOSURE_REVIEW`, entry-zone/breakout alerts can produce review-only `WATCH`, and news/announcement-only router context can produce `MANUAL_REVIEW` until event-policy/full advisory catches up; the authority contract still blocks portfolio/broker mutation.
- `/api/signal-refresh` now exposes a bounded compact `router_context` derived from the persisted signal payload, and the Operator home Live Signal Refresh cards render source type, trigger reason, priority, and rank so watcher-triggered review-only actions are explainable without opening raw JSON.
- Signal-refresh API rows now include a derived read-only operator next-step contract, and the Operator home Live Signal Refresh cards render it so operators know whether to review exposure, inspect Manual Review/event evidence, keep watching, or run full advisory before portfolio/execution changes.

Gaps:

- `Done`: watcher/signal-refresh output cannot be mistaken for authoritative portfolio reconciliation in API/UI shape. Signal-refresh rows persist and expose review-only/no-portfolio/no-broker/full-advisory-required authority fields; the full daily advisory remains the authoritative portfolio/action reconciliation path.
- `Done`: document which events trigger only signal refresh versus full advisory. The operator manual now lists watcher trigger boundaries for intraday OHLCV, news/announcements, context observations, material context triggers, wait-signal matches, skipped/failed/truncated watcher cycles, and the post-close advisory path.
- `Partial`: make missed watcher intervals provably catch up across all sources, not just some cursors. OHLCV, news, and announcement watcher cycles now have bounded catch-up metadata; broader downstream router/snapshot implications remain.
- `Done`: watcher output should show whether it changed an action, created a wait match, or only updated evidence.
- `Partial`: add tests for stale cursor, failed run, skipped lock, and catch-up behavior. OHLCV stale cursor/catch-up bounds, news/announcement bounded catch-up windows, failed-cycle sync-state recording, skipped not-due event publishing, and external lock-skipped event publishing now have focused coverage; broader downstream catch-up remains.

## 17. Execution And Broker Integration

Status: `Partial`

Done:

- Execution engine reads consolidated action recommendations.
- Execution planning is separated from live broker submission.
- Reason contracts block incomplete broker actions.
- Action-table order previews now carry an explicit safety contract requiring operator approval and broker reconciliation before live submission.
- Execution planning blocks closed, superseded, expired, or future-dated action rows before identity resolution or sizing.
- Operator snapshot/API compact action rows preserve execution safety contracts from dry-run order previews, and Action Queue renders a read-only approval/reconciliation gate.
- `/api/execution/approvals` and the Nuxt `/execution-approvals` page now provide a globally linked read-only execution approval workbench that lists latest dry-run execution rows, safety contracts, missing approval/reconciliation state, blockers, and explicit no-approval/no-submit boundaries.
- `/api/execution/approval-decision` and the `/execution-approvals` page now record audit-only operator decisions for dry-run execution rows. These decisions do not mutate `advisory_execution_orders`, update safety contracts, approve live submission, reconcile broker state, or submit orders.
- `/api/execution/approval-contract-update` and the `/execution-approvals` page now provide a second-stage reviewed update that can mark `operator_approval_status=approved` only after the latest audit decision is `approve_dry_run`. It keeps `broker_reconciliation_status` unchanged, forces `live_submission_allowed=false`, and does not submit broker orders.
- `/api/execution/reconcile` and the `/execution-approvals` page now provide broker reconciliation preview/apply controls. Preview is dry-run by default; apply requires `confirm=true`, persists only reconciliation/fill state from broker order-status reads, keeps operator approval unchanged, forces live submission blocked, and never submits broker orders.
- Live execution safety now has a default-on evidence checklist gate. Even with `STOCKEY_LIVE_TRADING_ENABLED=true`, a valid confirmation token, operator approval, and broker reconciliation, `submit_live_orders` blocks unless the safety contract has a passed live-evidence status and at least the configured minimum successful dry-run/reconciliation cycles.
- `/api/execution/evidence-review` and the `/execution-approvals` page now provide an evidence producer/review workflow. Preview is read-only; confirmed apply writes `advisory_execution_evidence_reviews`, requires enough historical successful dry-run/reconciliation cycles for the same symbol/setup/action, updates only `live_evidence_*` fields, forces `live_submission_allowed=false`, and does not submit broker orders.
- `/api/execution/live-allowance` and the `/execution-approvals` page now provide the final reviewed live-allowance contract. Preview is read-only; confirmed apply requires approval, reconciliation, passed evidence, exact confirmation phrase, and rationale, writes `advisory_execution_live_allowance_reviews`, sets only `live_submission_allowed=true`, and still does not submit broker orders.
- `/api/execution/live-submit-preflight` and the `/execution-approvals` page now provide the final read-only runbook layer. It checks current execution rows for approval, broker reconciliation, reviewed evidence, live allowance, broker identity, positive quantity, usable reference price, and stale dry-run row age; then returns blockers plus the exact manual CLI command and current order-set token. It does not write audit rows, mutate execution rows, or submit broker orders.
- Live allowance now requires a fresh post-allowance approval before preflight or CLI live submit can proceed. Applying live allowance sets `post_live_allowance_approval_status=missing`; a later `approve_dry_run` decision after `live_allowance_allowed_at` can mark it approved while preserving `live_submission_allowed=true`.
- The execution approval workbench now surfaces the same post-live-allowance approval blocker in row blockers, top summary counts, safety-boundary copy, and the row detail panel, so operators see the missing fresh approval before final live-submit preflight.
- `/api/execution/approvals` and the Nuxt execution workbench now expose source freshness/unavailable-source warnings for execution-preview rows, so stale or missing `advisory_execution_orders` context is visible before an operator relies on the dry-run approval workflow.
- `/api/execution/approvals` now returns per-row `readiness_checks` and a `readiness_summary`; the Nuxt workbench renders this checklist so an approved-looking row still shows blocked identity, price, quantity, reconciliation, evidence, live allowance, or post-allowance approval gates before any CLI live-submit preflight.
- Mocked broker account/reconciliation coverage verifies nested broker cash payload parsing, holdings/positions aggregation, broker order status mapping, fill extraction, and persisted reconciliation safety-contract updates without live broker execution.
- Dry-run execution previews now add `broker_identity_status`, `broker_identity_resolved`, and identity failure details to `safety_checks_json` and raw broker safety contracts when Dhan identity cannot be resolved.
- Dry-run execution previews now add `order_intent_lineage` to `safety_checks_json` and raw broker context, linking each order intent to the source action recommendation, reason contract summary/status, risk sizing, stop/target levels, and approval/reconciliation gate status.
- Dhan auth/token refresh exists.

Gaps:

- `Done`: keep live execution disabled until paper/dry-run reconciliation is proven over real runs. The engine now fail-closes live submission unless operator approval, reconciliation, live-evidence, explicit live-allowance, environment, and per-run confirmation-token gates are present; the UI can record approval decisions, update operator approval status, run reconciliation preview/apply, mark reviewed live evidence, set reviewed live allowance, and generate the manual live-submit preflight command/token without submitting orders.
- `Done`: add an execution approval UI gate with dry-run order preview. Backend dry-run plans, the read-only Action Queue, and `/execution-approvals` expose the approval/reconciliation contract and blockers; audit-only operator decision recording, reviewed operator-approval status updates, and broker reconciliation preview/apply controls exist.
- `Done`: add broker account/position reconciliation tests. Nested account payload parsing, holdings/positions aggregation, broker order status mapping, fill extraction, persisted reconciliation safety-contract updates, and operator API reconciliation preview/apply boundaries are covered with mocked broker clients.
- `Done`: prevent live order submission from any stale or manually closed action. Execution planning now blocks closed/superseded/expired/future-dated action rows.
- `Done`: every action-table order intent links back to action recommendation, reason contract, risk sizing, stop/target levels, and operator approval/reconciliation gate status inside the execution safety contract. Legacy fallback order paths remain disabled by default.

## 18. Research And ML

Status: `Partial`

Done:

- Event model prep/training runner exists.
- Research ledger exists.
- Event model promotion check exists.
- Event model artifact store supports S3/object storage.
- TS forecast workflow/evaluator and forecast-only paper-portfolio evaluator exist.
- Technical threshold calibration exists.
- Event-policy evaluator exists.

Gaps:

- `Partial`: formal promotion gate before any model influences live policy. Persisted event-model scores are now ignored by adversarial review by default (`research_only`) and only joined when `promoted` mode is selected and the event-model promotion-check scorecard is usable; broader model/TS promotion paths remain research-only and still need their own explicit gates before policy integration.
- `Done`: event-model promotion checks now publish a compact research-only scorecard with `usable_for_manual_review` or `not_usable`, key metrics, failed gate count, operator action, explicit broker/policy authority boundaries, and first-class fail-closed research-safety gates for leakage control, false-discovery control, and cost-adjusted baseline evidence; the Operations UI shows the scorecard plus a dedicated Research Safety Controls block so leakage, false-discovery, cost-adjusted baseline, and research-only authority cannot be confused with live-policy approval.
- `Done`: label coverage, diversity, score freshness, weekly run count, leakage-control evidence, false-discovery-control evidence, and cost-adjusted-baseline evidence are visible as first-class promotion gates. Event-model training now writes standardized research-control metadata for point-in-time leakage control, single fixed-config false-discovery control, and holdout predicted-positive versus passive-event after-cost baseline comparison. Cost-adjusted evidence passes only when the model's predicted-positive holdout trades beat the passive after-cost baseline.
- `Done`: event-model labeled training data joins intraday, macro, and exchange features on the event published-on date while keeping future OHLCV returns as labels only; focused regression tests prove next-day/anchor-day context is not used as training features. The shared realized-return helper used by technical threshold calibration, event-policy evaluation, and signal-quality evaluation emits a point-in-time return contract and has tests proving same-day prices are excluded. TS forecast evaluation now uses first close strictly after forecast `asof_date`, emits the same no-same-day/labels-only contract, and has focused regression coverage.
- `Partial`: TimesFM/TS forecasts are still research-only. The evaluator now uses a strict after-`asof_date` entry price and records a point-in-time return contract. The paper evaluator records forecast-only `PAPER_BUY` / `PAPER_SKIP` decisions, matured after-cost outcomes, naive-momentum baseline comparison, and advisory-action alignment; the Operator home page and legacy dashboard surface compact paper summaries. `advisory.ts_forecast_promotion_check` adds a read-only promotion gate for matured trades, win rate, after-cost return, momentum lift, exit-conflict rate, distinct dates, and symbol breadth. `advisory.ts_forecast_promotion` now creates manual-only review/decision audit rows and copyable TS forecast review-rule guidance when the gate returns `review_candidate`. `advisory.config_change_assistant` can now turn approved TS forecast promotion decisions into preview-only disabled `ts_forecast_review_rules` diffs, record audit-only reviewed-diff application decisions, and verify matching disabled TS rules when an operator marks a preview applied. `advisory.setup_registry.load_ts_forecast_review_rules`, `/api/research/ts-forecast-review-rules`, `/api/config-change/applications`, `/api/config-change/application-decision`, and the Operator home TS section now validate/show manually applied rules and surface no-broker/no-auto-policy boundaries. Remaining work is explicit low-weight consumption before any action, risk, or model integration.
- TS forecast promotion-check now fails closed when the paper-portfolio table is missing: the API returns a blocked `hold_research_only` payload with `evidence.available=false` and operator instructions instead of returning HTTP 500 on the operator home page.

## 19. Operator API

Status: `Partial`

Done:

- FastAPI serves dashboard, actions, traces, events, hypotheses, manual review, health, operations, calibration, artifacts, and conflict rules.
- Snapshot/caching is available for performance.
- API errors can be captured.
- Manual review decision endpoint persists operator decisions.
- Runtime metadata is exposed through `/api/runtime` with git revision, branch, dirty state, process start, latest source file mtime, stale-code detection, `api_schema` broker-disabled metadata, a permissive FastAPI/Pydantic response model, and mocked route-level smoke coverage.
- Critical Operator API payloads now include `api_schema` metadata with endpoint name, schema version, read-only status, and explicit broker-execution-disabled status for actions, health details, manual review, wait signals, and conflict-rule read/write responses.
- FastAPI/Pydantic response models are registered for critical Operator API routes: actions, health details, manual review queue/decision, wait signals/match, and conflict-rule read/write responses.
- Route-level FastAPI smoke coverage now exercises critical Operator API reads/writes with mocked payload builders, including actions, health details, manual review queue/decision, wait signals/match, and conflict-rule read/write responses. `/api/wait-signals/match` now also carries `api_schema` metadata.
- Symbol trace and symbol trace summary routes now publish permissive FastAPI/Pydantic response models, with route-level smoke coverage for success, query validation, and guarded 400/500 error paths.
- Event detail, event trace, and event trace summary routes now publish permissive FastAPI/Pydantic response models, with route-level smoke coverage for success and guarded 400/500 builder failures.
- Read-only Operations API routes for smoke checks, cron logs, command registry, and API errors now publish permissive FastAPI/Pydantic response models, include `api_schema` read-only/broker-disabled metadata, and have mocked route-level smoke coverage that avoids command execution.
- `python -m advisory.operator_smoke` now provides the canonical compact read-only preflight for status, trust level, fix hints, current blockers, and exact next commands. It is registered as an audited Operations UI command and skips Dhan validation by default to avoid login side effects.
- Read-only technical calibration GET routes now publish permissive FastAPI/Pydantic response models, include `api_schema` read-only/broker-disabled metadata, and have mocked route-level smoke coverage that avoids promotion-review writes.
- Read-only event-model research GET routes for promotion-check and artifact manifest now publish permissive FastAPI/Pydantic response models, include `api_schema` read-only/broker-disabled metadata, and have mocked route-level smoke coverage that avoids model promotion, S3 writes, command execution, and broker paths.
- Read-only hypotheses GET route now publishes a permissive FastAPI/Pydantic response model, includes `api_schema` read-only/broker-disabled metadata, and has mocked route-level smoke coverage that avoids hypothesis create/update/scan writes.
- Read-only event-policy/evaluation GET routes now publish permissive FastAPI/Pydantic response models, include `api_schema` read-only/broker-disabled metadata, and have mocked route-level smoke coverage that avoids evaluator runs, command execution, and broker paths.
- Read-only home, portfolio, and events GET routes now publish permissive FastAPI/Pydantic response models, include `api_schema` read-only/broker-disabled metadata, and have mocked route-level smoke coverage that avoids broker, command, and cleanup paths.
- Read-only summary, watchlist, and market-context GET routes now publish permissive FastAPI/Pydantic response models, include `api_schema` read-only/broker-disabled metadata, and have mocked route-level smoke coverage that avoids broker, command, and cleanup paths.
- Read-only signal-refresh GET route now publishes a permissive FastAPI/Pydantic response model, includes `api_schema` read-only/broker-disabled metadata, and has mocked route-level smoke coverage that avoids running refreshes, commands, cleanup, or broker paths.
- Read-only data-health GET route now publishes a permissive FastAPI/Pydantic response model, includes `api_schema` read-only/broker-disabled metadata, and has mocked route-level smoke coverage that avoids command, cleanup, and broker paths.
- Read-only action detail and portfolio detail GET routes now share a permissive FastAPI/Pydantic detail response model, include `api_schema` read-only/broker-disabled metadata, and have mocked route-level smoke coverage that avoids broker, command, and cleanup paths.
- Read-only runtime GET route now publishes a permissive FastAPI/Pydantic response model, includes `api_schema` read-only/broker-disabled metadata, and has mocked route-level smoke coverage that avoids broker, command, credential, cleanup, and DB paths.
- Read-only legacy health GET route now publishes a permissive FastAPI/Pydantic response model, includes `api_schema` broker-disabled metadata, and has mocked route-level smoke coverage that avoids broker, command, cleanup, credential, and DB paths.
- Technical-threshold, signal-quality, and event-policy promotion review/decision POST routes now publish shared permissive FastAPI/Pydantic response models. Event-policy promotion create/decision routes have mocked route-level smoke coverage that avoids DB writes and broker paths.
- Config-change preview POST routes for technical thresholds, signal-quality overlays, and event-policy review rules now publish a shared permissive FastAPI/Pydantic response model; the event-policy preview route has mocked route-level smoke coverage that avoids config writes and broker paths.
- Health details now accept `mode=fast|full`; fast is the default UI/API mode, while full intentionally runs heavyweight source/table/log/fallback diagnostics.
- API performance triage now separates fresh probe evidence from historical slowlog evidence. When `logs/performance/latest_api_latency_probe.json` is fresh, `scripts/api_performance_report.py` ranks currently probed routes first with `ranking_basis=fresh_probe`; unprobed historical rows stay visible as `historical_slowlog` and tell the operator to run a targeted probe before selecting that route.
- Read-only event detail, event trace, event trace summary, symbol trace, and symbol trace summary routes now enrich successful route responses with `api_schema` read-only/broker-disabled metadata even when mocked or alternate builders omit it, with focused route smoke assertions.
- Frontend TypeScript payload contracts now include `api_schema` metadata for event detail, event trace, symbol trace, and trace summary API responses, so the Nuxt client preserves the read-only/broker-disabled schema contract for trace/detail views.
- Frontend TypeScript payload contracts now also preserve read-only/broker-disabled `api_schema` metadata for Operations, event-model research, technical calibration, technical promotion-review listing/write results, and event-policy/evaluation payloads.
- Frontend TypeScript payload contracts now preserve read-only/broker-disabled `api_schema` metadata for action detail and portfolio detail payloads.
- Frontend TypeScript payload contracts now preserve `api_schema` metadata for the wait-signal match result payload without changing matcher, broker, cleanup, command, credential, or DB behavior.
- Frontend TypeScript payload contracts now preserve `api_schema` metadata for action-conflict rule read and narrow rule-write responses without changing conflict ranking, broker, cleanup, command, credential, or DB behavior.
- Snapshot-backed operator payloads now expose a normalized nullable `snapshot_warning` when the API serves a stale cached operator snapshot.

Gaps:

- `Done`: API exposes schema/version metadata for critical endpoints and runtime stale-code metadata for frontend detection.
- `Done`: Operator Health now checks `/api/runtime` separately from `/api/health`, flags stale Operator API processes as runtime blockers, and shows restart fix hints so fixed backend routes do not keep appearing broken through an old API process.
- `Done`: Operator Health CLI now accepts explicit `--format json|text`; JSON remains the default for cron/API tooling and text gives a concise terminal summary with trust level, blocker count, and top fix hints.
- `Done`: announcement `ParsedReport` dataclasses now expose a Pydantic-compatible `model_dump()` method, preventing structured-parse stage/report serialization from failing with `'ParsedReport' object has no attribute 'model_dump'`.
- `Done`: add typed response models for critical endpoints. Frontend types accept `api_schema` metadata for critical, trace/detail, action/portfolio detail, read-only operations/research, technical-calibration, event-policy, promotion-review writes, config-change previews, wait-signal match, and action-conflict rule route contracts, and FastAPI/OpenAPI now publishes Pydantic response models for critical route contracts.
- `Partial`: add endpoint-level tests for manual review, actions, symbol/event trace/detail, health, wait signals, and conflict rules. Focused schema-metadata, OpenAPI response-model coverage, and route-level FastAPI smoke coverage now exists for legacy health, health details, actions, manual review, wait signals, conflict rules, symbol trace, event detail/trace routes, event/symbol trace schema metadata, action/portfolio detail routes, read-only Operations routes, read-only technical calibration GET routes, read-only event-model and TS forecast research GET routes including evidence-unavailable blocked responses, the read-only hypotheses route, read-only event-policy/evaluation routes, read-only home/portfolio/events routes, read-only summary/watchlist/market-context routes, read-only signal-refresh route, read-only data-health route, and runtime route; broader low-priority API pages remain open.
- `To do`: compact APIs should be default for large pages.
- `Done`: API should surface stale snapshot warnings consistently on snapshot-backed payloads.

## 20. Frontend

Status: `Partial`

Done:

- Nuxt/Vue/Tailwind operator app exists.
- Main dashboard, events, symbol page, decision trace, health, hypotheses, manual review, operations, technical calibration, signal quality, and conflict rules pages exist.
- Operations now has a first-class “Run Operator Smoke Check” action backed by the audited command-run table.
- Manual review now explains selected dropdown effects.
- Manual Review now shows item-impact badges and decision-boundary panels so operators can distinguish closing, annotating, and wait-signal decisions before save.
- Shared symbol and link-button components now render as accessible navigational controls with title/aria-label, underline, border, and hover/focus treatment so they are distinguishable from status pills in Action Queue, Manual Review, Execution Approvals, and symbol/event contexts.
- Global header shows a stale API restart warning when `/api/runtime` reports running code older than source files on disk.
- Global header shows whether live broker submission is disabled or enabled according to `STOCKEY_LIVE_TRADING_ENABLED`, with the default-disabled state exposed by `/api/runtime`.
- A shared snapshot warning banner renders stale/fresh operator snapshot metadata on dashboard, events, health, and symbol-detail contexts backed by operator snapshots.
- Manual Review, Wait Signals, and Identity Issues show source-specific freshness warnings for stale visible rows, missing source timestamps, or skipped source queries.
- Action Queue shows read-only execution approval/reconciliation gates from dry-run order-preview safety contracts when available.
- Operator Journey is now a first-class read-only page that stitches manual-review decisions, wait signals, wait-signal matches, signal-refresh rows, action recommendations, portfolio rows, and execution previews into stage buckets plus a single newest-first timeline.
- Action Queue reason-contract panels now surface adversarial-review veto status and review reason from compact API rows.
- Action Queue reason-contract panels now show a dedicated market-gate block for positive actions downgraded to Manual Review by weak/risk-off market context.
- Event detail and decision trace client calls now use typed payloads that preserve operator `api_schema` metadata, including the read-only/broker-disabled contract added by the API.
- Action and portfolio detail client calls now use typed payloads that preserve operator `api_schema` metadata, including the read-only/broker-disabled contract added by the API.
- Wait Signal match client calls now use a typed payload that preserves operator `api_schema` metadata, including the broker-disabled contract added by the API.
- Action Conflict Rules client calls now use typed payloads that preserve operator `api_schema` metadata for read and narrow write responses, including the broker-disabled contract added by the API.
- Identity Issues is now a first-class read-only page for open Dhan security mapping failures and skipped-symbol repair context.
- Vitest component smoke coverage now mounts shared operator-safety components for snapshot warnings, source warnings, reason contracts, and trace manual-review wait links.
- Vitest page-level smoke coverage now mounts Manual Review, Action Queue, Health, and Symbol Detail pages with mocked read-only API payloads, asserting source freshness, identity/source blockers, decision boundaries, matched wait-signal follow-ups, execution approval/reconciliation gates, current blockers, superseded cleanup preview boundaries, stale snapshot warnings, and reason-contract safety evidence remain visible without broker execution or cleanup execution.

Gaps:

- `Done`: add an end-to-end “operator task journey” view: issue -> decision -> wait signal -> match -> refreshed action -> portfolio/execution implication. The read-only `/api/operator-journey` backend payload and Nuxt `/operator-journey` page now stitch manual decisions, wait signals, wait matches, signal refresh rows, action recommendations, portfolio rows, and execution previews into stage buckets plus a newest-first timeline.
- `Partial`: make stale data warnings visible on every page, not just health/snapshot contexts. Snapshot-backed dashboard, events, health, and symbol-detail contexts now share the same warning component; Manual Review, Wait Signals, and Identity Issues now show source-specific warnings, while lower-priority non-snapshot pages still need coverage.
- `Partial`: add UI tests or at least component-level smoke tests for key pages. Shared safety components plus page-level Manual Review, Action Queue, Health, and Symbol Detail safety flows now have Vitest mount coverage; lower-priority operator pages and write-flow interactions remain open.
- `Partial`: reduce tag/pill/link ambiguity everywhere. Shared `SymbolLink` and `LinkButton` now have link-specific visual and accessibility treatment, and Manual Review uses `LinkButton` for symbol/trace navigation; remaining work is a sweep of one-off `NuxtLink` buttons and custom pill-like links on lower-priority pages.
- `Partial`: add first-class pages for wait signals, skipped symbols, fallbacks, and unresolved identity issues. Wait Signals and read-only Identity Issues are first-class; broader fallback pages and identity close/reopen workflow remain.

## 21. Observability, Health, And Error Surfacing

Status: `Partial`

Done:

- Operator health exists with fix hints.
- Health page shows skipped/fallback/partial-data concepts.
- Health payload and page now show a prioritized “current blockers before advisory can be trusted” summary plus an Advisory Trust Gate derived from runtime/API state, core freshness, event evidence readiness, identity issues, signal-quality usability, and active degradation rows.
- Cron logs are written under `logs/cron`.
- Slow-operation log exists.
- Decision traces exist.
- Trace summaries exist.
- Symbol/event trace summaries now include direct manual-review decision -> wait-signal -> match links, and the Trace Timeline UI renders them when present.
- Recovered announcement OCR/parse rows with stale `last_error` are no longer treated as active Health degradations.
- Stale historical file-level parser failures are no longer treated as active Health degradations; they remain visible as suppressed/recovered context in the ingestion-state summary and degradation lifecycle feed.

Gaps:

- `To do`: all failures and fallbacks should have a durable row, not only log lines.
- `Partial`: health page should group failures by active/recovered/superseded. Runtime degradations, recovered cron/log signals, superseded cleanup candidates, recovered announcement errors, and stale historical file-level parser failures are grouped or marked with lifecycle state; remaining work is broader grouping consistency for every fallback telemetry source.
- `Done`: old resolved errors are closeable/supersedable for event-processing and recovered announcement-document errors. Read-time suppression, Health active/recovered/superseded-ready grouping, Health cleanup preview samples/commands, scheduled/audited dry-run visibility, durable superseded marking, and guarded operator apply exist.
- `Done`: add “current blockers before advisory can be trusted” summary and Advisory Trust Gate with `blocked`, `review_required`, and `usable` operating states.
- `Done`: trace should link manual decisions and wait signals directly.
- `Done`: add health checks for announcement/bhavcopy evidence readiness and LLM prompt/schema visibility. `advisory.event_data_quality` reports stale source tables, compact evidence freshness, announcement parse/OCR/text/S3 coverage, null exchange-event typing, missing corporate-action/earnings/deal feature influence, and raw-table scan risk, and `operator_health` surfaces these as fix hints/current blockers. The prompt registry is visible in API/UI, and core LLM/Codex output rows now persist prompt id, prompt version, and response schema version.

## 22. Testing

Status: `Partial`

Done:

- `tests/test_advisory_regression.py` is large and covers many regressions.
- Current targeted changes compile and typecheck.
- Manual-review wait-signal creation has a regression test update.

Gaps:

- `To do`: break giant regression test into domain-specific files.
- `To do`: add integration tests for the full advisory state machine using fixture tables.
- `To do`: add tests for every operator UI write action and its downstream effect.
- `Partial`: add tests that prevent stale/manual-closed/incomplete-contract rows from becoming actions or orders. Execution planning blocks stale/manual-closed rows, and incomplete reason-contract broker actions are downgraded with explicit review-only/no-broker boundary evidence; broader stale/manual-closed action/order permutations remain.
- `To do`: add tests for all action conflict precedence cases.
- `Partial`: add frontend unit/e2e tests for Manual Review, Action Queue, Symbol page, and Health. All four now have page-level safety smoke coverage for read-only safety evidence; write-flow interactions and full browser E2E remain open.
- `Partial`: add tests for announcement/bhavcopy evidence point-in-time behavior, exchange-event normalization, compact evidence table freshness, LLM signal schema validation, prompt-version recording, and technical-only versus technical-plus-evidence evaluation. Prompt contract metadata now has focused regression coverage across event evaluation, event-policy manual review, playbook action plans, company-memory reviews, announcement parsing, and technical-threshold promotion reviews; broader evidence/outcome tests remain.

## 23. Security And Safety

Status: `Partial`

Done:

- `.env` is used for sensitive credentials.
- Dhan live execution requires explicit flags.
- Execution planner separates planning and submission.
- Live Dhan execution now requires a per-run confirmation token in addition to `STOCKEY_LIVE_TRADING_ENABLED=true`; blocked safety checks expose the expected token for deliberate reruns.
- Manual/config approvals are audit-only in several places.
- `/api/runtime` and the global operator header expose the current live-trading env gate, defaulting to disabled, without adding any approval or broker-submission control.
- `utils.redaction` now masks common broker/API secrets, bearer/basic auth headers, tokenized URLs, mobile numbers, TOTP/PIN/password fields, and sensitive mapping keys before fallback telemetry, Operator API audit errors, Health cron summaries, and Operations cron-log tails are persisted or returned to the UI.

Gaps:

- `Partial`: ensure `.env`, caches, Chrome profile, token cache, and existing local log files never leak into committed files. UI-facing fallback telemetry, Operator API errors, and cron-log snippets are redacted, but future new log sinks must explicitly reuse `utils.redaction`.
- `Done`: add redaction for tokens, mobile numbers, auth URLs, and common broker/API auth strings in operator-facing telemetry/log payloads.
- `Done`: add a global “live trading disabled” operator setting visible in UI.
- `Done`: require explicit per-run confirmation for live execution. `submit_live_orders()` blocks planned rows unless the supplied CLI/env token matches the run-specific token derived from execution date, planned order count, and an order-set fingerprint covering symbols, quantities, security ids, and estimated values; focused tests cover missing-token, post-token approval blocking, confirmed safety-gated submission, and same-day/same-count token non-reuse.

## 24. Documentation

Status: `Partial`

Done:

- README describes the current high-level flow.
- `docs/scripts.md` has a useful script/table inventory.
- Operator PRD, manual, implementation docs, and runbooks exist.
- `docs/operators_manual.md` now has an operator boundary table explaining what full advisory, watchers, fast signal refresh, wait signals, and Manual Review each read, write, can change, and cannot do.
- `scripts/docs_state_audit.py --strict` now checks README, roadmap, analysis, and docs for stale removed scripts/static-dashboard commands plus required current-state coverage.
- Announcement text S3 offload storage columns are now applied through `utils.schema_migrations`, so failed metadata-column changes are visible in the schema registry and Operator Health instead of being untracked script DDL.
- `all_frontend.sh` now watches a lightweight source signature for the operator API and Nuxt frontend, emits `status=restart_requested`, and exits when relevant code changes so cron can release the lock and start a fresh API/frontend process instead of serving stale fixed endpoints indefinitely.

Gaps:

- `Partial`: docs and code drift frequently because behavior is changing fast. The docs-state audit now catches the highest-risk stale operator references, but it is not yet generated from live code metadata.
- `To do`: generate part of script inventory from code metadata instead of manually maintaining it.
- `Done`: add “operator decision effects” table to the manual.
- `Done`: document exact difference between full advisory, watcher signal refresh, wait signals, and manual review.
- `Done`: default Manual Review API/UI to `investment_review` lane while preserving `all_active_by_lane` counts and explicit `technical_issue`, `research_config`, and `all` filters. Technical/data repair work remains visible but no longer pollutes the default investment decision queue.

## 25. Recommended Next Development Order

1. `Partial`: harden operator-trust observability. Fallback telemetry and Health visibility are broad, with the 2026-06-12 scanner baseline at `{'records_fallback': 503, 'reraises': 53, 'self_protection': 2}` and zero normal `logs_only` / `silent_fallback` / `silent_handler` rows. `scripts/fallback_telemetry_coverage_report.py --fail-on-silent` now passes, so next work should focus on newly introduced source/API fallbacks and source-specific degraded-state semantics.
   - `Done`: Dhan automated login now retries through a subprocess if Playwright sync login hits an existing asyncio loop, and `all_advisory.sh` prints `scripts/advisory_stage_report.py` output automatically on failure so the failed stage is visible in the same cron log.
   - `Done`: `all_advisory.sh` now runs a marker-wrapped `dhan_auth_preflight` before advisory stages. It uses `python -m data.dhanlive.auth_cli ensure --auto-login` by default, so expired/missing Dhan tokens refresh or fail before the expensive intraday stage.
   - `Done`: advisory failure reporting now tracks the failing wrapper step. A `dhan_auth_preflight` failure prints the Chrome CDP startup command before the auth refresh command and does not show stale advisory stage timings from an earlier run.
   - `Done`: `all_advisory_preflight.sh` now provides a standalone pre-advisory readiness check that runs the same Dhan auth preflight plus compact operator smoke with Dhan included, without running `advisory.master_pipeline`, rebuilding portfolios, refreshing snapshots, or submitting broker orders. It is also available as an audited Operations command.
   - `Done`: `all_advisory_preflight.sh` now has a failure trap that prints the failing preflight step, CDP-first recovery command for Dhan auth failures, and Health rerun guidance for smoke failures.
   - `Done`: cron now runs `all_advisory_preflight.sh` at `18:55` weekdays, under its own lock and log, before the `19:10` post-close advisory run so Dhan/CDP/token readiness failures are visible before the expensive reconciliation window.
   - `Done`: Operator Health now classifies `all_advisory_preflight.log` CDP/token failures as `dhan_auth_preflight_failed` in the degradation feed and orders fix hints as log inspection -> Chrome CDP startup -> advisory preflight rerun -> Health rerun.
   - `Done`: compact operator smoke now exposes `dhan_readiness` when Dhan is included, covering token validation, cache health, `auth_refresh_ready`, auto-login configuration, CDP status, and whether CDP recovery is required before a long advisory run.
   - `Done`: `data.dhanlive.auth_cli ensure --auto-login` now catches token refresh/CDP failures and returns structured JSON with `operator_action`, while recording fallback telemetry, instead of emitting an unbounded traceback.
   - `Done`: Dhan cache Health now parses token expiry as UTC, reports `auth_refresh_ready`, and includes Chrome CDP reachability when automated login is configured, so operators can see whether advisory can self-refresh before running it.
   - `Done`: Dhan cache fix hints now put `scripts/start_chrome_cdp.sh` before token refresh when automated login is configured but Chrome CDP is unreachable, so operators do not retry Dhan refresh before starting the required browser endpoint.
   - `Verified`: `python -m advisory.intraday_features --symbols HUIL --lookback-days 1 --dry-run` now returns an intraday sync issue for `No Dhan security id mapped for NSE:HUIL` instead of crashing the advisory intraday stage.
   - `Done`: `all_advisory.sh` supports `ADVISORY_SKIP_POST_REFRESH=true` for targeted dry-run/smoke verification without rebuilding operator snapshots and trace summaries; scheduled/full runs still refresh both by default.
2. `Partial`: formalize the operator/manual-review state machine. Decision effects, incomplete-contract Manual Review downgrades, matched wait-signal reopen/suppression, stale-decision reopening after newer source rows, durable review-only action-candidate linkage, portfolio handoff evidence, and normalized action-transition contracts are enforced; broader UI write-flow coverage remains.
3. `Partial`: keep action conflict edit/promote UI and deterministic conflict rules under test. Built-in and promoted exact-match ranking influence, enable/disable UI, explanation editing, disabled-by-default manual-resolution promotion, and validated exact-match condition editing are covered; broader semantic rule types remain.
4. `Partial`: continue announcement/bhavcopy intelligence hardening. Compact evidence stores, event-data quality checks, prompt registry, company-memory review, and signal-quality evaluation exist; remaining work is wider downstream consumption and manually reviewed influence rules.
5. `Partial`: improve performance from evidence only. Use `scripts/api_performance_report.py`, endpoint payload-byte telemetry, and slow-operation state before adding indexes, snapshots, or detail endpoints. Fresh probe rows now outrank unprobed historical rows so current latency evidence drives the next optimization.
6. `Partial`: keep live broker execution disabled until dry-run, reconciliation, evidence, live-allowance, and manual CLI-token flows are proven over real runs. Backend previews, UI workflows, navigation, and focused tests exist; remaining work is operational proof, not direct UI broker submission.
7. `Done`: add tests for every manual-review decision and the core wait-signal downstream effect.
8. `Done`: connect matched wait signals back to the original manual-review item/action-candidate workflow.
9. `Done`: add superseded-error cleanup for old processing failures.
10. `Done`: add “why not approved buy/sell” explanation to Action Queue, stale API/code version indicator, current-blockers Health card, and Advisory Trust Gate.
11. `Done`: add focused adversarial-review and market-gate precedence/reason-contract visibility regressions.

## 26. Immediate Confidence Statement

The system is useful as an operator-assisted research/advisory platform. It is not yet “perfect” or fully safe as an autonomous trading system.

Trust level today:

- Data ingestion: medium, with source-specific fragility.
- Technical/rule scoring: medium, needs more validation and state tests.
- Event extraction/policy: medium. LLM failures and stale failures remain important; V1 event-policy actionability context now makes Manual Review rows easier to judge, and event-evidence quality gates plus prompt/schema version visibility are materially better controlled.
- Announcement/bhavcopy intelligence: medium-low. Useful data exists, but compact evidence stores, null typing repair, corporate-action influence, and LLM company-memory signal review are not yet complete.
- Action consolidation: medium, with conflict precedence now pinned for enabled-rule ranking and an enable/disable UI; edit/promote workflows and broader permutations still need tests.
- Manual review: improving, with explicit decision effects and matched wait reopen handling, but still needs durable downstream state tests.
- Wait signals: newly wired for manual review, but matching is still basic.
- Execution: low for live trading; keep in dry-run/manual approval mode.
- UI/debuggability: improving, but not yet complete enough to explain every decision without opening raw rows.

The next safest path is not giving more authority to more models. The next safest path is upgrading announcement and bhavcopy data into clean, compact, point-in-time evidence, making LLM signal proposals auditable, and keeping final action authority inside explicit, tested, visible deterministic policy.
