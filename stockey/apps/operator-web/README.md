# Stockey Operator Web

Nuxt 3 operator UI for the advisory stack. Most pages are read-only; the few write flows are explicit operator actions for bounded review state, identity repair confirmation, playbook management, and audited dry-run operations. The UI must not submit live broker orders.

From the repo root, run the API and frontend together:

```sh
./all_frontend.sh
```

The wrapper uses `nvm use default` by default, so cron gets the same Node runtime as an interactive shell. Override with `OPERATOR_WEB_USE_NVM=false` or `OPERATOR_WEB_NVM_VERSION=<version>` if needed.

Or run the Python API first:

```sh
python -m advisory.api.app --host 127.0.0.1 --port 8765
```

Then run the frontend:

```sh
cd apps/operator-web
npm install
NUXT_PUBLIC_API_BASE=http://127.0.0.1:8765 npm run dev
```

Current pages and work areas:

- Overview
- Event Inbox
- Investor Playbooks
- Data Health
- Health and Operations
- Manual Review
- Wait Signals
- Identity Issues
- Decision Trace
- Symbol Detail
- Prompt Registry
- Signal Quality
- Technical Calibration
- Research Evidence

Important current UI contracts:

- Action Queue rows show reason contracts, execution safety, latest prices, company-memory review evidence, technical state/trigger/pivot/sub-score/exit context, and required feature freshness summaries.
- Symbol Detail and reason-contract panels reuse the same Technical Decision panel so technical evidence is visible even when it is nested inside `recommendation_reason.evidence`.
- The home page Market Context block separates material top-context events (`Triggered`) from broad context-only evidence (`Observed`) so watcher noise is visible before it reaches advisory review.
- Symbol Detail pages show the full Data Inputs Used / Freshness Contract panel.
- Manual Review decisions are bounded operator state changes; they do not mutate portfolio rows, action recommendations, config, or broker orders.
- Live trading remains disabled unless a separate broker execution approval/reconciliation model is deliberately implemented and reviewed.
