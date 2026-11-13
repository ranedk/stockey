# screener

Nuxt/Vue/Tailwind/TypeScript frontend for stockey's fundamentals screener. Read-only
view of the L1 universe, watchlist, and sectors, plus the one write action the whole
pipeline exposes: adding/removing a company from your portfolio (a human-committed
thesis, `fundamentals_l4_thesis` in stockey).

This is a pure client of stockey's data -- it has no database access and no business
logic of its own beyond display. All screening, alerting, and narrative generation
happens in `~/code/trading/stockey/fundamentals/`; see that repo's
`docs/FUNDAMENTAL_SCREENER_PRD.md` for the pipeline this UI is showing.

## Run

1. Start the API (from `stockey/`, with its venv active):
   ```sh
   uvicorn fundamentals.api.app:app --reload --port 8000
   ```
2. Start the dev server (from here):
   ```sh
   npm install
   npm run dev
   ```
   Defaults to `http://localhost:8000` for the API; override with
   `NUXT_PUBLIC_API_BASE` (see `.env.example`) if it's running elsewhere.

## Pages

- `/` -- L1 universe (the screened company list + the exact query/version that
  produced it)
- `/watchlist` -- every company with an active fundamental alert
- `/watchlist/[id]` -- the "why": narrative first, then the evidence trail (every
  alert that led here, expandable to the raw evidence bundle), descriptive
  context (L2 state / technicals / sector), and the portfolio add/remove action
- `/sectors` -- capital-cycle phase by sector, with which watchlist companies sit in
  each one and why
- `/portfolio` -- open and resolved theses

## Notes

- No auth, permissive localhost-only CORS on the API side -- personal single-user
  tool, not meant for public exposure.
- The API's write surface is deliberately narrow (portfolio create/resolve only);
  nothing in this UI can create an alert, watchlist entry, or narrative.
