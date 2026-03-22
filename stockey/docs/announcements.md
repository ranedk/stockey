# Announcement Pipeline

The announcement pipeline lives under [`data/announcements`](/home/rane/code/stockey/data/announcements) and ingests exchange announcements for a single requested ticker and exchange pair, resolving that identity through `company_master`.

## Command

```bash
python -m data.announcements.cli \
  --ticker SHAKTIPUMP \
  --exchange NSE \
  --from-date 2026-03-01 \
  --to-date 2026-03-22
```

Optional:

```bash
python -m data.announcements.cli \
  --ticker 531431 \
  --exchange BSE \
  --from-date 2026-03-01 \
  --to-date 2026-03-22 \
  --report OrderBookReport
```

## Flow

For a given `ticker`, `exchange`, and date range, the managed pipeline:

1. Resolves the request through [`company_master`](/home/rane/code/stockey/utils/company_master.py).
2. Fetches NSE or BSE announcements.
3. Persists raw metadata into `announcement_pipeline_documents`.
4. Downloads PDFs when available.
5. Attempts OCR on the first three pages.
6. Categorizes using exchange text plus OCR text when available.
7. Parses structured report outputs only when categories and report prompts apply.
8. Stores parsed outputs in `announcement_pipeline_reports`.

## Identity Model

The pipeline no longer uses the borrowed `CompanyTarget` / `load_companies_by_ticker` flow.

It now resolves targets using:

- [`data/announcements/db.py`](/home/rane/code/stockey/data/announcements/db.py)
- [`utils/company_master.py`](/home/rane/code/stockey/utils/company_master.py)

Each stored announcement row includes:

- `company_master_id`
- `exchange`
- `ticker`
- `company_name`

This lets NSE and BSE announcements for the same company join on a single stable identity.

## Runtime Notes

- NSE bootstrap is retried and can fall back to the announcements landing page when the homepage returns intermittent `403`.
- OCR is best-effort. On this machine, PaddleOCR can fail during inference even after model download; the pipeline logs the OCR failure, marks `ocr_status = failed`, and continues the ingest instead of failing the whole announcement.
- `announcement_pipeline_documents` and `announcement_pipeline_reports` are created lazily by the upsert layer. The first run no longer assumes the tables already exist.

## Key Modules

- [`data/announcements/cli.py`](/home/rane/code/stockey/data/announcements/cli.py)
- [`data/announcements/managed_pipeline.py`](/home/rane/code/stockey/data/announcements/managed_pipeline.py)
- [`data/announcements/pipeline.py`](/home/rane/code/stockey/data/announcements/pipeline.py)
- [`data/announcements/state.py`](/home/rane/code/stockey/data/announcements/state.py)
- [`data/announcements/db.py`](/home/rane/code/stockey/data/announcements/db.py)
- [`data/announcements/ocr.py`](/home/rane/code/stockey/data/announcements/ocr.py)

## Required Environment

This path expects working values for:

- PostgreSQL env vars used by [`utils/db.py`](/home/rane/code/stockey/utils/db.py)
- S3 env vars used by [`utils/store.py`](/home/rane/code/stockey/utils/store.py)
- `OPENAI_API_KEY` if structured parsing is enabled

Required Python packages for the announcement path include:

- `jinja2`
- `openai`
- `pdf2image`
- `paddleocr`
- `paddlepaddle`

## Stored State

`announcement_pipeline_documents` stores one row per announcement, including:

- `unique_id`
- `company_master_id`
- `exchange`
- `ticker`
- `company_name`
- `pdf_status`
- `ocr_status`
- `parse_status`
- `last_error`

`announcement_pipeline_reports` stores one row per parsed report output and also carries `company_master_id`.
