# Announcement Pipeline

This package downloads stock exchange announcements for a single ticker, stores raw and processed artifacts in S3, keeps processing state in PostgreSQL, and avoids repeating work that is already complete.

## What It Does

For a given `ticker` and date range, the pipeline:

1. Fetches announcements from NSE and/or BSE.
2. Filters them to the requested date range.
3. Stores raw announcement metadata.
4. Downloads source PDFs only when not already stored.
5. Runs OCR on the first 3 pages only when OCR is missing.
6. Categorizes the announcement from exchange text + OCR text.
7. Runs structured LLM parsing only for missing report outputs.
8. Stores all artifacts and state so reruns can skip completed work.

## Command

Run the pipeline with:

```bash
python -m announcement_pipeline --ticker 500112 --from-date 2026-03-01 --to-date 2026-03-21
```

Optional filters:

```bash
python -m announcement_pipeline \
  --ticker 500112 \
  --from-date 2026-03-01 \
  --to-date 2026-03-21 \
  --exchange BSE \
  --report Pnl
```

## Required Arguments

- `--ticker`
- `--from-date`
- `--to-date`

## Optional Arguments

- `--exchange`
  Restricts processing to one or more exchanges. Can be passed multiple times.
- `--report`
  Restricts parsing to one or more report types. Can be passed multiple times.

## Processing Flow

The main managed flow is in [managed_pipeline.py](/home/rane/code/alphabuy/alphabuy/announcement_pipeline/managed_pipeline.py).

### 1. Resolve Company

The pipeline looks up the requested ticker in the project database using [db.py](/home/rane/code/alphabuy/alphabuy/announcement_pipeline/db.py).

### 2. Fetch Announcements

The base fetch/OCR/categorize/parse logic is implemented in [pipeline.py](/home/rane/code/alphabuy/alphabuy/announcement_pipeline/pipeline.py).

### 3. Reuse Existing State

Before doing any work, the managed layer checks:

- `announcement_pipeline_documents`
- `announcement_pipeline_reports`

If a PDF, OCR output, or parsed report already exists, it is reused instead of recomputed.

### 4. Persist Artifacts

Artifacts are stored in S3 with deterministic keys under:

```text
announcement-pipeline/<exchange>/<ticker>/<yyyy/mm/dd>/<unique_id>/
```

Typical stored files:

- `raw_announcement.json`
- `source_document.pdf`
- `ocr_first_3_pages.txt`
- `parsed_reports/<ReportName>.json`

### 5. Persist State

State rows are upserted into PostgreSQL after each meaningful stage so interrupted runs can resume safely.

## State Tables

### `announcement_pipeline_documents`

Stores one row per announcement, including:

- identity fields like `unique_id`, `ticker`, `exchange`
- source metadata
- S3 keys for raw data, PDF, and OCR
- OCR text and page count
- categories
- statuses:
  - `pdf_status`
  - `ocr_status`
  - `parse_status`
- last error, if any

### `announcement_pipeline_reports`

Stores one row per parsed report, including:

- `unique_id`
- `report_name`
- `category`
- parsed JSON
- parsed report S3 key

## Idempotency Rules

If the same command is run again:

- already-known announcements are not re-created
- already-downloaded PDFs are reused from S3
- already-generated OCR is reused
- already-generated parsed reports are not requested again
- only missing steps are executed

This is the main contract of the managed pipeline.

## Key Modules

- [cli.py](/home/rane/code/alphabuy/alphabuy/announcement_pipeline/cli.py)
  Command-line entrypoint.
- [__main__.py](/home/rane/code/alphabuy/alphabuy/announcement_pipeline/__main__.py)
  Allows `python -m announcement_pipeline`.
- [managed_pipeline.py](/home/rane/code/alphabuy/alphabuy/announcement_pipeline/managed_pipeline.py)
  Stateful orchestration layer.
- [pipeline.py](/home/rane/code/alphabuy/alphabuy/announcement_pipeline/pipeline.py)
  Core fetch/download/OCR/categorize/parse logic.
- [state.py](/home/rane/code/alphabuy/alphabuy/announcement_pipeline/state.py)
  S3 key building, artifact persistence, DB upserts, state loading.
- [db.py](/home/rane/code/alphabuy/alphabuy/announcement_pipeline/db.py)
  Ticker lookup and DB connection helpers.
- [other_project_db.py](/home/rane/code/alphabuy/alphabuy/announcement_pipeline/other_project_db.py)
  Target project DB utilities used for upsert/query.
- [other_project_store.py](/home/rane/code/alphabuy/alphabuy/announcement_pipeline/other_project_store.py)
  Target project S3 utilities used for artifact storage.
- [prompts.py](/home/rane/code/alphabuy/alphabuy/announcement_pipeline/prompts.py)
  Embedded parsing prompts.
- [schemas.py](/home/rane/code/alphabuy/alphabuy/announcement_pipeline/schemas.py)
  Structured parsing schemas and category/report mappings.
- [categorize.py](/home/rane/code/alphabuy/alphabuy/announcement_pipeline/categorize.py)
  Category rules.
- [ocr.py](/home/rane/code/alphabuy/alphabuy/announcement_pipeline/ocr.py)
  Paddle OCR wrapper.

## Environment Expectations

This package expects the target environment to already provide:

- PostgreSQL connection env vars
- S3 connection env vars
- OpenAI credentials
- OCR dependencies like PaddleOCR and `pdf2image`

It also assumes the target DB contains ticker-to-company mapping in:

- `home_company`
- `home_stockindex`

## Notes

- The current entrypoint is intentionally single-ticker oriented.
- The report filter is optional.
- The pipeline is optimized for repeated date-range ingestion runs.
- If a run fails midway, rerunning the same command should continue from stored state rather than restarting all work.

## Developer Notes

### Expected Document Table Shape

`announcement_pipeline_documents` is created implicitly through upserts, so the exact schema is driven by the payload written from [state.py](/home/rane/code/alphabuy/alphabuy/announcement_pipeline/state.py).

The important columns are:

- `unique_id`
- `exchange`
- `ticker`
- `company_name`
- `subject`
- `text`
- `filed_under_category`
- `exchange_category_id`
- `published_on`
- `exchange_published_on`
- `attachment_url`
- `attachment_name`
- `storage_prefix`
- `raw_s3_key`
- `pdf_s3_key`
- `ocr_s3_key`
- `number_of_pages`
- `three_page_ocr_text`
- `categories_json`
- `pdf_status`
- `ocr_status`
- `parse_status`
- `last_error`
- `created_at`
- `updated_at`

Recommended interpretation of status fields:

- `pdf_status`
  Usually `pending`, `completed`, or `unavailable`
- `ocr_status`
  Usually `pending`, `completed`, or `skipped`
- `parse_status`
  Usually `pending`, `completed`, `skipped`, or `failed`

### Expected Report Table Shape

`announcement_pipeline_reports` stores one row per parsed report output.

Important columns:

- `unique_id`
- `exchange`
- `ticker`
- `company_name`
- `published_on`
- `category`
- `report_name`
- `model_name`
- `report_json`
- `report_s3_key`
- `created_at`
- `updated_at`

The uniqueness rule is:

- one row per `unique_id + report_name`

### Rerun / Recovery Example

Assume you run:

```bash
python -m announcement_pipeline --ticker 500112 --from-date 2026-03-01 --to-date 2026-03-21
```

#### First Run

The pipeline:

1. fetches all announcements for the ticker in the requested range
2. stores raw announcement metadata
3. downloads PDFs
4. stores PDFs in S3
5. OCRs first 3 pages
6. stores OCR text
7. categorizes announcements
8. parses missing reports
9. stores parsed report JSON in both S3 and DB

#### Second Run With Same Command

The pipeline:

1. fetches the same announcement metadata again
2. looks up `unique_id` values in `announcement_pipeline_documents`
3. skips PDF download when `pdf_status=completed`
4. skips OCR when `ocr_status=completed`
5. skips report parsing when a given `unique_id + report_name` already exists
6. only processes anything that is still missing

#### Failure Recovery

If a run fails after PDF upload but before OCR completes:

- the document row already points to the stored PDF in S3
- `pdf_status` remains `completed`
- `ocr_status` remains `pending` or incomplete

On rerun:

- the pipeline reloads the PDF from S3
- it resumes from OCR
- it does not re-download the source file

If a run fails after OCR but before report parsing:

- OCR is reused from DB/S3
- only the missing reports are requested again

### Practical Extension Points

If you want to adapt this package in another project, the most common extension points are:

- [managed_pipeline.py](/home/rane/code/alphabuy/alphabuy/announcement_pipeline/managed_pipeline.py)
  Change orchestration rules, state transitions, or retry behavior.
- [state.py](/home/rane/code/alphabuy/alphabuy/announcement_pipeline/state.py)
  Change S3 key conventions, DB table names, or payload structure.
- [pipeline.py](/home/rane/code/alphabuy/alphabuy/announcement_pipeline/pipeline.py)
  Change exchange fetch behavior, OCR behavior, or parsing behavior.
- [prompts.py](/home/rane/code/alphabuy/alphabuy/announcement_pipeline/prompts.py)
  Tune extraction prompts.
- [schemas.py](/home/rane/code/alphabuy/alphabuy/announcement_pipeline/schemas.py)
  Add or remove report models and category mappings.

### Operational Notes

- The managed layer currently always stores raw announcement JSON, even if later stages are skipped.
- Categories are stored as JSON text in the document table to keep the storage layer simple.
- Parsed reports are stored twice by design:
  - structured JSON in DB for queryability
  - JSON files in S3 for easy retrieval and portability
- The package assumes `unique_id` is stable enough to act as the primary deduplication key across reruns.
- If exchange-side metadata changes for an already-known `unique_id`, reruns will update the document row because writes are done via upsert.
