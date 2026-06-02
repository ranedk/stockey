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
