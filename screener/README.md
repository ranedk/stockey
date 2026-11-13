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

- The live site (screener.service) runs `.output/` and that is still the OLD app. This app
  builds to `.output-next/` so a build never touches production.
- `deploy/deploy.sh` builds `legacy/` and publishes it. Switching the live site to this app is
  a deliberate step (service ExecStart -> `.output-next`, nginx `/_nuxt/` root, API service),
  not yet done.

## Pages

- `/` Markets: indices, breadth, trend-stage mix, sector indices, sector map, movers.
- `/screener`: ready screens, size/sector/tag filters, any numeric condition from the
  catalogue, column sets, sort, CSV export. State lives in the URL.
- `/company/[symbol]`: header with tags and stage, key stats, price chart (line/candles,
  50/200-day averages, volume), scorecard against the industry median, financial charts,
  statements (P&L, balance sheet, cash flow), peers, about.
- `/sectors`: median returns and uptrend share per sector.

## Design rules

- Colour tokens in `app/assets/css/main.css` (light and dark each chosen, not inverted);
  charts read them through `useChartTheme()`.
- Up/down is green/red AND an arrow or sign; tags and verdicts carry their rule in a tooltip.
- One measure per chart; no second y-axis. Legends whenever there are two or more series.
- Indian number formats: crores, lakhs, `en-IN` grouping (`app/utils/format.ts`).
