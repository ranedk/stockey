# Stockey

Stockey is an Indian-equity advisory research and operator system.

Core flow:

1. `./complete_data.sh` downloads, parses, and refreshes compact/context evidence from raw data.
2. `./all_watchers.sh` incrementally watches OHLCV, news, and announcements, including lower-priority market/context-overlay targets, review-only watch discovery from positive/watch overlays, review-only de-risk pressure from negative theme plus direct announcement/exchange/bhavcopy overlays, and bounded `CONTEXT_OVERLAY_WATCH` reconciliation. It matches hypothesis Wait Signals and writes fast per-symbol signal refresh rows. It self-locks, so overlapping cron/manual runs are skipped, and it resumes from `advisory_sync_state` cursors so skipped ticks do not lose the missed interval.
3. `./all_context_to_entry_repair.sh` runs the bounded context-to-entry repair chain: review-only context signal refresh, durable `CONTEXT_OVERLAY_WATCH` reconciliation, targeted context-watch technical refresh, rules-through-actions advisory, Action Queue refresh, and recommendation diagnostics. Use it when positive context exists but no BUY rows are produced because watch rows, technical confirmation, or candidates are stale.
4. `./all_advisory.sh` first runs bounded review-only context signal refresh when its sync state is not already current, reconciles durable `CONTEXT_OVERLAY_WATCH` rows, runs causal-memory signal refresh, then runs the authoritative batch advisory, lifecycle, risk, action consolidation, and execution-planning flow.
5. `./all_frontend.sh` runs the FastAPI operator API and Nuxt operator frontend.
6. `./all_research_evidence.sh` refreshes research-only context/event/action-transition evidence without event-model training or live policy changes.
7. `./all_ml.sh` is optional research for event-model training plus signal-quality/source-family/adversarial-review evidence refresh and stable multi-window gated promotion-review creation; it is not the default production decision path.

The operator frontend includes a `/screeners` workbench for validating Screener.in query syntax, fetching bounded preview rows with `persist=false`, and reviewing read-only screener contribution metrics before any query is registered as a production screener.

## Script Groups

### Recurring Cron Scripts

These are safe to run from `config/stockey.generated.crontab` at regular intervals because they are locked, incremental, or bounded:

| Script | When | Why |
| --- | --- | --- |
| `./all_frontend.sh` | every few minutes | Keeps the FastAPI operator API and Nuxt frontend alive, exits for cron restart when API/Nuxt source changes, and supports `--api-only`, `--web-only`, or `--both` for targeted manual restarts |
| `./all_watchers.sh` | every `10` minutes during market hours plus one post-close pass | Watches OHLCV, news, announcements, wait signals, and context-overlay watchlist pressure from persisted cursors; self-locks to avoid overlap |
| `./all_downloaders_queue.sh` | a few times during the day | Enqueues NSE/Dhan/Screener work instead of opening multiple single-client sessions |
| `./all_external_workers.sh` | after queued downloader enqueue | Drains Dhan, Screener, and NSE queues serially |
| `./complete_data.sh` | morning safety net and end-of-day pre-advisory catch-up | Runs full raw download + parser refresh + context-overlay refresh; use this to repair anything intraday jobs missed before advisory |
| `./all_advisory_preflight.sh` | before manual or post-close advisory | Refreshes/validates Dhan auth and runs compact operator smoke without running advisory |
| `./all_context_to_entry_repair.sh` | post-close before full advisory, or manually from recommendation diagnostics | Runs the bounded context-to-entry repair chain so positive news/macro/announcement/bhavcopy pressure is not blocked by stale context-watch rows, stale context-watch technical features, or stale same-date candidates |
| `./all_advisory.sh` | once daily after market close | Runs bounded review-only signal refresh only when not already current, persists context-overlay watchlist pressure, then produces the authoritative portfolio/action reconciliation |
| `./all_ml.sh --run-signal-quality-window-runner --include-signal-quality-split-reports` | weekly research window, if enabled | Long-running event-model research training plus research-only signal-quality/source-family evidence refresh, stable multi-window manual review-row creation, and split diagnostics for unstable families; not part of live decision authority |
| `./all_api_latency_probe.sh` | several times per market day | Probes operator API latency and records slow endpoints |
| `./all_operator_health.sh` | several times per market day | Runs read-only operator health with Dhan login skipped by default |
| `./all_hypothesis_scan.sh` | after watcher passes | Scans newly collected events against investor playbooks/hypotheses |
| `./all_ts_forecast_workflow.sh` | research-only intraday schedule | Refreshes experimental TS forecast watch rows |
| `./all_ts_forecast_evaluator.sh` | post-close research schedule | Evaluates matured TS forecasts after costs |
| `./all_ts_forecast_paper_portfolio.sh` | post-close research schedule | Builds a research-only forecast paper portfolio and compares it with naive momentum/advisory alignment |
| `./all_research_evidence.sh` | post-close research schedule | Refreshes context/event/action-transition research evidence without event-model prep/training/scoring, config mutation, portfolio mutation, or broker behavior |
| `./all_event_policy_evaluator.sh` | post-close research schedule | Evaluates event-policy actions/classes after costs |
| `./all_technical_threshold_calibration.sh` | weekly research schedule | Calibrates technical thresholds for manual review |

Cron uses named shell wrappers for recurring jobs so logs and Operations status have stable script names and `[stockey.script]` markers.

### Manual / Catch-Up / Long-Running Scripts

Use these when a day was missed, data looks stale, or you explicitly want a broad repair/backfill. They can take a long time and should not be run frequently during market hours:

| Script | Use |
| --- | --- |
| `./all_downloaders.sh` | Download-only broad catch-up for missing raw data |
| `./all_parsers.sh` | Parse-only catch-up after raw files are present |
| `./complete_data.sh` | Full download + parse + context-overlay catch-up/backfill; useful end-of-day, after a missed day, or before a major advisory rerun |
| `./all_advisory_preflight.sh` | Dhan/CDP/token and compact smoke preflight before spending hours on `all_advisory.sh` |
| `./all_ml.sh --run-signal-quality-window-runner --include-signal-quality-split-reports` | Long-running research/model-training flow; run manually when validating model quality, signal-quality overlays, source-family evidence, and split diagnostics, or rerun weekly in a dedicated research cron window |
| `./all_advisory_codex.sh` | Debug/repair wrapper for advisory failures; use manually, not as normal cron |
| `./all_analysis_codex.sh` | Manual bounded Codex development loop that picks the next `analysis.md` slice, implements it, validates it, and updates the board |

Important docs:

- `docs/operators_manual.md`: daily runbook and cron behavior.
- `docs/scripts.md`: script and table inventory.
- `docs/operator_app_prd.md`: operator frontend/API contract.
- `analysis.md`: detailed engineering audit and implementation-gap tracker.
- `todo.md`: current roadmap, including the long-term performance architecture backlog.

Current architecture keeps LLM use bounded to extraction, review notes, hypothesis/playbook assistance, and manual-review context. Production action decisions are consolidated through deterministic policy, technical, risk, lifecycle, and reason-contract layers before any execution planning.

Cron startup:

```sh
python builder.py
./go-crond config/stockey.generated.crontab --allow-unprivileged
```

The generated cron file is `go-crond`/system-crontab style and includes a username column. Use the Operations page or `python scripts/cron_preflight.py` before starting it after config changes.

Dhan and Screener browser-backed automation need a Chrome remote-debugging session when auto-login is required:

```sh
scripts/start_chrome_cdp.sh
```

Keep `CDP_ENDPOINT=http://localhost:9222` configured. Dhan auto-login fails hard when Chrome/CDP is unavailable; start this session before retrying `python -m data.dhanlive.auth_cli ensure --auto-login` or `./all_advisory.sh`. If Dhan pages are slow between mobile, TOTP, PIN, and redirect steps, tune `DHAN_AUTO_LOGIN_STEP_TIMEOUT_MS` rather than falling back to manual browser consent.

General flow of data:

  1. `complete_data.sh` downloads and parses raw market, macro, company, NSE, news, and announcement data, then refreshes compact evidence and review-only context overlays.
  2. Screener.in production/ad hoc screeners create the candidate universe.
  3. Snapshot builders create macro, macro-context-overlay, compact bhavcopy/context-overlay, compact announcement/context-overlay, regime, fundamental, technical, intraday, exchange-event, exchange-context-overlay, and market-context rows.
  4. Feature freshness checks explain whether required inputs are fresh, stale, missing, or intentionally skipped for visible action rows; rules, risk, portfolio, lifecycle, and actions now record dependency-gate summaries. Blocked rules gates move `PASS_NOW` to watch, blocked risk gates move positive allocations to manual review, blocked portfolio gates defer positive planned capital, lifecycle gates annotate outputs without hiding exits, and blocked action gates downgrade positive broker actions to Manual Review.
  5. Rule and technical engines score setups into pass, watch, abstain, or reject states.
  6. Watchers refresh active watchlist and open-position symbols with OHLCV, news, and announcements.
  7. Watcher cursors advance only after persisted source work succeeds; if a watcher run is skipped or fails, the next run catches up from the last successful cursor.
  8. Router reevaluates only symbols with changed evidence, then refreshes bounded review-only Action Queue rows for successfully refreshed symbols without granting portfolio or broker authority.
  9. Codex/LLM is used for extraction, summaries, playbook notes, and manual-review context, not direct trade authority.
  10. Event policy, adversarial review, regime, risk, and lifecycle layers produce entry/hold/add/partial-exit/full-exit intent.
  11. Action consolidation creates one final action per symbol.
  12. Execution planning converts only validated action contracts into broker-order plans.
  13. Nuxt/FastAPI expose recommendations, traces, health, stage feature-gate blockers, per-symbol gate effects, feature freshness, errors, fallbacks, and fix hints.
  14. Research jobs evaluate TS forecasts, forecast-only paper portfolios, event policies, technical thresholds, and optional ML separately from live policy; TS forecast promotion can create manual review/decision audit rows only after paper gates pass.

Current next-development focus:

1. Keep source degradation semantics strict where operator trust depends on them: no-data, source-unavailable, auth-unavailable, parser-bug, and fallback-used should be visible in Health/Data Health instead of buried in logs.
2. Continue fallback telemetry triage using `python scripts/fallback_telemetry_coverage_report.py --format json`; prioritize source/API paths over intentionally best-effort JSON parsing helpers.
3. Continue UI-first operations for unresolved manual, research, S3 artifact, and reviewed-config workflows.
4. Use `python scripts/api_performance_report.py --limit 20`, backed by cron-generated `logs/performance/latest_api_latency_probe.json` and slow-operation state, to choose the next frontend/API latency fix from evidence.
5. Keep `.env.example` and docs current with `python scripts/env_example_audit.py --strict` and `python scripts/docs_state_audit.py --strict`.
6. Run `python scripts/cron_preflight.py` before starting `go-crond`; the same safe read-only check is available from Operations as `Cron Preflight`.
7. Run `python scripts/context_gate_policy_audit.py --fail-on-single-regime` after env/config changes; it should exit 0 unless a broad single-regime hard BUY gate was intentionally re-enabled.
