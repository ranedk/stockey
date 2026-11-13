# api: the research platform's public API

FastAPI over the serving tables (`serving_company`, `serving_financials`, built by
`stockey/serving/`). Read-only for now; accounts (PLATFORM_PRD task A2) come next.

```sh
python3 -m venv .venv && .venv/bin/pip install fastapi 'uvicorn[standard]' 'psycopg[binary,pool]' argon2-cffi pytest httpx
.venv/bin/uvicorn app.main:app --port 8110     # 8100 is the infra orchestrator
.venv/bin/python -m pytest -q tests
```

Database: `API_DATABASE_URL`, else stockey's `.env`. Every filter/sort field is whitelisted by
the metric catalogue (`app/catalog.py`); values are bound parameters.

Endpoints (`/api/v1`): `health`, `catalog`, `search`, `companies` (list/screen:
`f=field:op:value`, ops gt/gte/lt/lte/eq/in/has), `companies/{symbol}`, `.../prices`,
`.../financials`, `market/overview`, `market/index`, `meta/freshness`, `meta/classes`.
