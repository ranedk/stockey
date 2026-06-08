# Stockey

Stockey is an Indian-equity advisory research and operator system.

Core flow:

1. `./complete_data.sh` downloads and parses raw data.
2. `./all_watchers.sh` incrementally watches OHLCV, news, and announcements, matches hypothesis Wait Signals, and writes fast per-symbol signal refresh rows. It self-locks, so overlapping cron/manual runs are skipped, and it resumes from `advisory_sync_state` cursors so skipped ticks do not lose the missed interval.
3. `./all_advisory.sh` runs the authoritative batch advisory, lifecycle, risk, action consolidation, and execution-planning flow.
4. `./all_frontend.sh` runs the FastAPI operator API and Nuxt operator frontend.
5. `./all_ml.sh` is optional research for event-model training; it is not the default production decision path.

## Script Groups

### Recurring Cron Scripts

These are safe to run from `config/stockey.generated.crontab` at regular intervals because they are locked, incremental, or bounded:

| Script | When | Why |
| --- | --- | --- |
| `./all_frontend.sh` | every few minutes | Keeps the FastAPI operator API and Nuxt frontend alive |
| `./all_watchers.sh` | every `10` minutes during market hours plus one post-close pass | Watches OHLCV, news, announcements, and wait signals from persisted cursors; self-locks to avoid overlap |
| `./all_downloaders_queue.sh` | a few times during the day | Enqueues NSE/Dhan/Screener work instead of opening multiple single-client sessions |
| `./all_external_workers.sh` | after queued downloader enqueue | Drains Dhan, Screener, and NSE queues serially |
| `./complete_data.sh` | morning safety net and end-of-day pre-advisory catch-up | Runs full raw download + parser refresh; use this to repair anything intraday jobs missed before advisory |
| `./all_advisory.sh` | once after market close | Produces the authoritative portfolio/action reconciliation |
| `./all_ml.sh` | weekly research window, if enabled | Long-running event-model research training; not part of live decision authority |

Cron also runs selected Python modules directly for health checks, hypothesis scans, TS research refresh/evaluation, event-policy evaluation, and technical-threshold calibration.

### Manual / Catch-Up / Long-Running Scripts

Use these when a day was missed, data looks stale, or you explicitly want a broad repair/backfill. They can take a long time and should not be run frequently during market hours:

| Script | Use |
| --- | --- |
| `./all_downloaders.sh` | Download-only broad catch-up for missing raw data |
| `./all_parsers.sh` | Parse-only catch-up after raw files are present |
| `./complete_data.sh` | Full download + parse catch-up/backfill; useful end-of-day, after a missed day, or before a major advisory rerun |
| `./all_ml.sh` | Long-running research/model-training flow; run manually when validating model quality or rerun weekly in a dedicated research cron window |
| `./all_advisory_codex.sh` | Debug/repair wrapper for advisory failures; use manually, not as normal cron |
| `./all_analysis_codex.sh` | Manual bounded Codex development loop that picks the next `analysis.md` slice, implements it, validates it, and updates the board |

Important docs:

- `docs/operators_manual.md`: daily runbook and cron behavior.
- `docs/scripts.md`: script and table inventory.
- `docs/operator_app_prd.md`: operator frontend/API contract.
- `todo.md`: current roadmap, including the long-term performance architecture backlog.

Current architecture keeps LLM use bounded to extraction, review notes, hypothesis/playbook assistance, and manual-review context. Production action decisions are consolidated through deterministic policy, technical, risk, lifecycle, and reason-contract layers before any execution planning.

General flow of data:

  1. `complete_data.sh` downloads and parses raw market, macro, company, NSE, news, and announcement data.
  2. Screener.in production/ad hoc screeners create the candidate universe.
  3. Snapshot builders create macro, regime, fundamental, technical, intraday, exchange-event, and market-context rows.
  4. Rule and technical engines score setups into pass, watch, abstain, or reject states.
  5. Watchers refresh active watchlist and open-position symbols with OHLCV, news, and announcements.
  6. Watcher cursors advance only after persisted source work succeeds; if a watcher run is skipped or fails, the next run catches up from the last successful cursor.
  7. Router reevaluates only symbols with changed evidence.
  8. Codex/LLM is used for extraction, summaries, playbook notes, and manual-review context, not direct trade authority.
  9. Event policy, adversarial review, regime, risk, and lifecycle layers produce entry/hold/add/partial-exit/full-exit intent.
  10. Action consolidation creates one final action per symbol.
  11. Execution planning converts only validated action contracts into broker-order plans.
  12. Nuxt/FastAPI expose recommendations, traces, health, errors, fallbacks, and fix hints.
  13. Research jobs evaluate TS forecasts, event policies, technical thresholds, and optional ML separately from live policy.
