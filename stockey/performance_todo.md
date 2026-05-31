# Performance TODO

This plan is about making Stockey cheaper, faster, and easier to operate without adding too many moving pieces.

Current constraint: PostgreSQL is doing too much. It is serving as OLTP store, time-series store, text/blob store, frontend API store, research warehouse, and audit log. That works for correctness, but it makes DB size high and frontend queries slow.

Non-goals:

- Do not use DuckDB as the core store. Prior compatibility issues make it a poor fit here.
- Do not increase NSE browser concurrency. NSE blocks aggressively when Chrome/CDP connections or request volume from one IP goes up.
- Do not move everything to a complex lakehouse stack before exhausting simpler wins.

## Target Architecture

Keep Postgres as the control plane, not the warehouse.

Recommended near-term shape:

1. PostgreSQL: current state, latest serving tables, transactional metadata, action queue, audit identifiers.
2. S3-compatible object storage: raw PDFs, OCR text, full document summaries, raw JSON payloads, old logs, and cold historical exports.
3. Small frontend-serving cache: precomputed JSON/API tables for health, actions, portfolio, watchlist, and trace summaries.
4. Optional analytical store later: ClickHouse or BigQuery only after we measure which queries remain slow after slimming Postgres.

## Design Principles

- Keep hot paths tiny: frontend endpoints should query compact latest-state tables, not scan historical/raw tables.
- Store large text once: put large OCR/transcripts/raw payloads in S3 and keep only pointers plus small excerpts in Postgres.
- Separate latest state from history: `latest_*` or materialized summary tables should power UI; history remains queryable but not in every request.
- Avoid cloud bill surprises: BigQuery/Athena-like engines are good for occasional analytics, bad for unbounded frontend queries unless guarded.
- Use rate-limited queues for NSE: one controlled browser/session pipeline is safer than parallel Chrome.
- Optimize before rewriting: slow Python is usually not the bottleneck if queries transfer huge rows or scan unindexed tables.

## Immediate Work

### 1. Measure DB Size and Slow Tables

Goal: know exactly what is consuming storage and frontend latency.

Tasks:

- Add `scripts/db_size_report.py` to list largest tables, indexes, toast size, row counts, and estimated bloat.
- Add `scripts/api_latency_probe.py` to time all operator API endpoints and show slow SQL-backed sections.
- Add query logging around API payload builders with row count, elapsed time, and payload byte size.
- Add `performance_reports/` output directory for JSON snapshots.

Acceptance:

- One command shows top 30 DB tables by total size.
- One command shows top 10 slowest API sections.
- We can identify which tables contain large text columns or raw JSON.

### 2. Move Large Text and Raw Payloads Out of Postgres

Candidate data to offload:

- Announcement PDF OCR text.
- Full OCR text.
- Audio transcripts.
- Concise summaries if large.
- Raw announcement payloads.
- LLM input/output raw prompts where only trace pointer is needed.
- Historical raw JSON from screeners and ad hoc runs.
- Old cron logs and operator feed logs.

Postgres should keep:

- `s3_key` or object URI.
- content hash.
- small excerpt/snippet for UI.
- character count.
- created/updated timestamps.
- parse status and error status.

Tasks:

- Add a reusable `utils/blob_store.py` abstraction for S3-compatible storage.
- Add migration script: for selected tables, upload text/blob columns to object storage and replace large column with pointer/excerpt.
- Add loader helpers that fetch full text only when explicitly requested by trace/detail endpoints.
- Add size guardrails in upserts: warn when a text/json column exceeds a threshold.

Acceptance:

- Hot frontend queries do not fetch full OCR/transcript columns.
- DB size reduces materially after vacuum/reindex.
- Existing UI still shows summaries and can fetch full text on detail pages.

### 3. Add Frontend Serving Tables or Cache

Problem: the Nuxt frontend should not force expensive joins every refresh.

Tasks:

- Add `advisory_operator_snapshot_daily` or `advisory_operator_snapshot_latest`.
- Store compact JSON sections for:
  - summary
  - actions
  - portfolio
  - watchlist
  - event inbox
  - health
  - market context
  - TS watch
- Add `python -m advisory.operator_snapshot_builder` to refresh snapshots after advisory/watchers.
- Update API to read snapshot first, then fall back to live builders.
- Add payload size limit warnings.

Acceptance:

- Main dashboard endpoint responds from a compact snapshot in under 500 ms on local network.
- API can still force live rebuild for debugging with a flag.

### 4. Index and Partition Audit

Tasks:

- Add an index audit for all API queries and slow pipeline queries.
- Add missing indexes on common filters:
  - `symbol`
  - `asof_date`
  - `published_on`
  - `unique_id`
  - `company_master_id`
  - `setup_id`
  - `load_ts`
- Ensure hypertables/partitioned tables use unique keys that include partitioning columns.
- Add retention/compression policy for cold intraday and event history where Timescale is available.
- Add materialized/latest tables for trace summaries instead of joining full trace history in UI.

Acceptance:

- No frontend endpoint scans large historical tables without date/symbol filters.
- `EXPLAIN` exists in docs for the top slow queries before/after.

## Storage and Compute Options

### Option A: Keep Self-Hosted Postgres + S3 Offload

Best first move.

Pros:

- Least moving pieces.
- Keeps current SQL compatibility.
- Lowest migration risk.
- Strong transactional behavior for actions, decisions, and trace metadata.

Cons:

- Still limited for large analytical scans.
- Needs schema discipline and snapshot tables.

Use for:

- Operator state.
- Latest recommendations.
- Action queue.
- Trace metadata.
- Research run metadata.
- Small event tensors.

Do not use for:

- Full OCR text.
- Long transcripts.
- Raw document payloads.
- Huge historical feature matrices after they become cold.

### Option B: Postgres + Timescale Compression/Continuous Aggregates

Good if current Postgres already has Timescale and most pain is OHLCV/intraday/time-series size.

Pros:

- Minimal application changes.
- Compression and continuous aggregates can reduce time-series cost.
- Keeps Postgres SQL.

Cons:

- Does not solve large text/blob storage.
- Self-managed Timescale tuning still needs care.

Use for:

- OHLCV.
- Intraday candles.
- daily symbol features.
- regime/macro time series.

### Option C: Postgres + ClickHouse

Best self-hosted analytical companion if BigQuery query cost or cloud dependency is unattractive.

Pros:

- Very fast analytical scans.
- Good compression.
- Cheap on a single Hetzner/DigitalOcean VM if managed carefully.
- Excellent for append-only facts: OHLCV, events, features, evaluations.

Cons:

- Another database to operate.
- Not a replacement for transactional Postgres.
- Requires replication/export jobs.

Use for:

- Historical OHLCV/features.
- event policy evaluation.
- model training datasets.
- dashboard analytics over history.

Avoid for:

- action queue.
- broker execution state.
- mutable operator decisions.

### Option D: BigQuery

Good for occasional heavy research queries if we export Parquet/CSV to Cloud Storage or stream selected tables.

Pros:

- No server to manage.
- Excellent for big analytical scans.
- Good for ad hoc research over cold historical data.

Cons:

- Query cost can surprise if frontend or cron accidentally scans large tables.
- Data transfer and duplication from S3/AWS/DO can become annoying.
- Not ideal as a low-latency frontend serving store.

Use for:

- Research backtests.
- evaluating event policies over years.
- large historical feature scans.
- periodic ML dataset builds.

Guardrails required:

- never power live frontend directly from BigQuery
- partition every table by date
- cluster by symbol/setup/event class
- require max bytes billed
- only query curated narrow tables, not raw text blobs

### Option E: S3 + Athena

Viable if we stay AWS/S3-native and want SQL over Parquet without managing ClickHouse.

Pros:

- Cheap storage.
- Serverless.
- Good for occasional SQL over cold Parquet.

Cons:

- Slower and less interactive than ClickHouse for iterative work.
- Query cost still depends on bytes scanned.
- Needs partition discipline.

Use for:

- cold historical archives.
- monthly research jobs.

### Option F: Object Storage Only for Text + Current Postgres

This is the highest ROI subset and should happen regardless of analytical store choice.

Pros:

- Very low cost.
- Simplifies DB.
- Reduces frontend payloads.

Cons:

- Full-text search requires either Postgres tsvector over excerpts, OpenSearch, or a dedicated search index later.

## Recommended Path

Phase 1: Postgres cleanup and S3 offload.

- Measure DB table/index/toast size.
- Move large text/raw payloads to S3-compatible storage.
- Add compact frontend snapshots.
- Add missing indexes.
- Add retention/compression for time-series.

Phase 2: Query-serving optimization.

- Make API read snapshots first.
- Add symbol/date constrained trace APIs.
- Add payload size budgets.
- Add API latency health thresholds.

Phase 3: Analytical store decision.

Choose after measurements:

- If most pain is historical OHLCV/features: try self-hosted ClickHouse.
- If most pain is occasional research scans and you want no ops: try BigQuery with hard byte limits.
- If most pain is current frontend speed: do not add BigQuery; fix snapshots/indexes first.

Phase 4: Parallelism.

- Keep NSE ingestion single-lane with backoff and state.
- Parallelize non-NSE independent CPU/DB-light tasks:
  - feature computation by symbol chunks
  - event policy evaluation
  - technical scoring
  - model dataset transforms
  - frontend snapshot section builds
- Use worker queue with DB connection caps, not unbounded threads.

Phase 5: Optional Go.

Go may help for:

- lightweight API serving from precomputed snapshots
- file/blob movement
- concurrent non-NSE HTTP ingestion
- log/health probes
- Redis pub-sub daemon

Go likely will not help first for:

- pandas-heavy ML prep
- sklearn/xgboost model training
- LLM orchestration
- slow SQL caused by large scans

Decision rule: rewrite in Go only after SQL/payload measurements show Python runtime overhead is the bottleneck. Until then, optimize schema, indexes, snapshots, and payload size.

## NSE Strategy

Do:

- use one browser/CDP lane for NSE-heavy flows
- persist every successful step so retries resume
- deduplicate downloaded/parsed state from DB where safe
- add backoff and cookie/session reset
- queue NSE work explicitly

Do not:

- run multiple Chrome sessions from the same IP
- parallelize NSE announcement/calendar scraping
- put NSE into high-frequency cron loops without cursor/state checks

Potential improvement:

- Add `nse_ingestion_queue` table with:
  - source
  - target symbol/date
  - priority
  - status
  - attempt_count
  - next_attempt_at
  - last_error
- One worker drains the queue with conservative rate limits.

## Concrete TODO List

### P0

- [ ] Add DB size report script.
- [ ] Add API latency probe script.
- [ ] Identify top large text/json columns.
- [ ] Add frontend snapshot table and snapshot builder.
- [ ] Update API to serve from snapshots first.
- [ ] Add S3/blob pointer migration for announcement OCR/full text/transcripts.
- [ ] Add missing indexes for current API queries.

### P1

- [ ] Add hot/cold retention policy for old intraday and trace rows.
- [ ] Add payload size logging to API responses.
- [ ] Add trace summary materialization.
- [ ] Add `nse_ingestion_queue` and single-lane worker.
- [ ] Parallelize non-NSE feature jobs with bounded worker count and DB connection cap.

### P2

- [ ] Run a ClickHouse proof of concept using exported OHLCV/features/evaluation tables.
- [ ] Run a BigQuery proof of concept for event-policy/technical-threshold historical evaluation with byte limits.
- [ ] Compare monthly estimated cost and operational complexity.
- [ ] Decide whether analytical store is worth adding after Postgres/S3/snapshot work.

### P3

- [ ] Evaluate Go for a small standalone API/cache/log probe only if Python serving remains a bottleneck after snapshots.
- [ ] Evaluate OpenSearch/Meilisearch only if full-text document search becomes important.

## Initial Recommendation

Do not move the live app to BigQuery first.

Start with:

1. Postgres as metadata/control plane.
2. S3-compatible object storage for heavy text/raw payloads.
3. compact Postgres snapshot tables for frontend serving.
4. Timescale compression/retention for time-series if available.
5. ClickHouse or BigQuery only for research/historical analytics after measurement.

This keeps costs controlled, preserves current app compatibility, and addresses the biggest likely bottleneck: Postgres storing and serving too much heavy text/history to latency-sensitive endpoints.

## References to Recheck Before Buying

- BigQuery pricing: https://cloud.google.com/bigquery/pricing
- S3 pricing: https://aws.amazon.com/s3/pricing/
- ClickHouse Cloud pricing/docs: https://clickhouse.com/pricing
- Timescale compression/product docs: https://www.timescale.com/compression
