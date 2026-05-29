# Stockey Operator Web

Nuxt 3 read-only operator UI for the advisory stack.

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

V1 pages:

- Overview
- Event Inbox
- Investor Playbooks
- Data Health

The app must stay read-only until the decision trace, execution safety, and permission model are implemented.
