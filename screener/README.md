# screener: the research platform web app

The product front end (PLATFORM_PRD.md). Nuxt 4 + Nuxt UI 4 (dashboard shell, tables, command
palette) + ECharts. It replaces the old hand-built pages, which live in `legacy/` and stay
deployed until the switch-over.

## Run (development)

```sh
# API (workspace root /api), reads the serving tables
cd ../api && .venv/bin/uvicorn app.main:app --port 8110
# app
npm install && npm run dev        # http://127.0.0.1:3100, proxies /_api/** to :8110
```

From a laptop: `ssh -L 3100:127.0.0.1:3100 <host>` and open http://localhost:3100.

## Build and deploy: read before running anything

- LIVE since 2026-11-14: `./deploy/deploy.sh` builds this app to `.output-next/`, copies it to
  `.output/` (what screener.service runs), publishes `/_nuxt` assets to /var/www/screener for
  nginx, (re)installs and restarts `platform-api.service` (the API on :8110, unit in
  `../api/deploy/`), restarts the app and checks the whole chain.
- A plain `npx nuxt build` only writes `.output-next/`, never the live `.output/`.
- Rollback: `./deploy/deploy.sh --legacy` ships the old app from `legacy/`.
- Stop the dev server (:3100) before deploying: both use `.nuxt/`.
- nginx gives the app 120 s per request; stockey's `/api/platform-issues` takes longer, so the
  open-issues section of Data health fails publicly until that query is made faster.

## Pages

- `/` Markets: indices, breadth, trend-stage mix, sector indices, sector map, movers.
- `/screener`: ready screens, size/sector/tag filters, any numeric condition from the
  catalogue, column sets, sort, CSV export. State lives in the URL.
- `/company/[symbol]`: header with tags and stage, key stats, price chart (line/candles,
  50/200-day averages, volume), scorecard against the industry median, financial charts,
  statements (P&L, balance sheet, cash flow), peers, about.
- `/sectors`: median returns and uptrend share per sector; second tab, capacity vs demand per
  sector (stockey's sector cycle) with the watchlist companies in each.
- Ported from the old site (2026-11-13), reading the older backends through `/_stockey/**`
  (:8000, fundamentals API) and `/_systrader/**` (:8090) via `composables/useBackends.ts`:
  `/themes`, `/stages` (industry trend stages), `/watchlist`, `/portfolio`, `/investors`,
  `/paper` (frozen-rule paper tracks), `/strategies` (event triggers), `/paradigms`,
  `/data-health`, `/todos`. The old Universe page is replaced by `/screener`.

## Design rules

- Colour tokens in `app/assets/css/main.css` (light and dark each chosen, not inverted);
  charts read them through `useChartTheme()`.
- Up/down is green/red AND an arrow or sign; tags and verdicts carry their rule in a tooltip.
- One measure per chart; no second y-axis. Legends whenever there are two or more series.
- Indian number formats: crores, lakhs, `en-IN` grouping (`app/utils/format.ts`).
