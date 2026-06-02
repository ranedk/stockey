# Stockey

Stockey is an Indian-equity advisory research and operator system.

Core flow:

1. `./complete_data.sh` downloads and parses raw data.
2. `./all_watchers.sh` incrementally watches OHLCV, news, and announcements.
3. `./all_advisory.sh` runs the batch advisory, lifecycle, risk, action consolidation, and execution-planning flow.
4. `./all_frontend.sh` runs the FastAPI operator API and Nuxt operator frontend.
5. `./all_ml.sh` is optional research for event-model training; it is not the default production decision path.

Important docs:

- `docs/operators_manual.md`: daily runbook and cron behavior.
- `docs/scripts.md`: script and table inventory.
- `docs/operator_app_prd.md`: operator frontend/API contract.
- `performance_todo.md`: performance roadmap and completed cleanup work.

Current architecture keeps LLM use bounded to extraction, review notes, hypothesis/playbook assistance, and manual-review context. Production action decisions are consolidated through deterministic policy, technical, risk, lifecycle, and reason-contract layers before any execution planning.

General flow of data:

  1. `complete_data.sh` downloads and parses raw market, macro, company, NSE, news, and announcement data.
  2. Screener.in production/ad hoc screeners create the candidate universe.
  3. Snapshot builders create macro, regime, fundamental, technical, intraday, exchange-event, and market-context rows.
  4. Rule and technical engines score setups into pass, watch, abstain, or reject states.
  5. Watchers refresh active watchlist and open-position symbols with OHLCV, news, and announcements.
  6. Router reevaluates only symbols with changed evidence.
  7. Codex/LLM is used for extraction, summaries, playbook notes, and manual-review context, not direct trade authority.
  8. Event policy, adversarial review, regime, risk, and lifecycle layers produce entry/hold/add/partial-exit/full-exit intent.
  9. Action consolidation creates one final action per symbol.
  10. Execution planning converts only validated action contracts into broker-order plans.
  11. Nuxt/FastAPI expose recommendations, traces, health, errors, fallbacks, and fix hints.
  12. Research jobs evaluate TS forecasts, event policies, technical thresholds, and optional ML separately from live policy.
