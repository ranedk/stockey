# Announcement Pipeline

The announcement pipeline lives under [`data/announcements`](../data/announcements) and ingests exchange announcements for a single requested ticker and exchange pair, resolving that identity through `company_master`.

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

1. Resolves the request through `utils/company_master.py`.
2. Fetches NSE or BSE announcements.
3. Persists raw metadata into `announcement_pipeline_documents`.
4. Downloads the attachment and stores it in object storage.
5. Runs OCR on the first three pages for PDFs, or transcription for direct audio attachments, and stores the full text in object storage.
6. Categorizes using exchange text plus the first-pass OCR/transcription.
7. If any category is present in the keys of `DOCUMENT_PYDANTIC_MAP`, runs full-document OCR and stores the full text in object storage.
8. If the category includes `EARNINGS_CALL`, looks for an audio link in the exchange payload or OCR text, downloads it, transcribes it, and stores the transcript in object storage.
9. Uses the summarization model to extract structured JSON matching `data/announcements/schemas.py`, and stores the parsed report payload in object storage with metadata on `announcement_pipeline_reports`.
10. Generates a separate short concise summary and stores it in `concise_summary_text`.

## Identity Model

The pipeline no longer uses the borrowed `CompanyTarget` / `load_companies_by_ticker` flow.

It now resolves targets using:

- `data/announcements/db.py`
- `utils/company_master.py`

Each stored announcement row includes:

- `company_master_id`
- `exchange`
- `ticker`
- `company_name`

This lets NSE and BSE announcements for the same company join on a single stable identity.

## Runtime Notes

- NSE bootstrap is retried and can fall back to the announcements landing page when the homepage returns intermittent `403`.
- OCR, transcription, and summarization models are env-configurable:
  - `OCR_USING=gemini-3-flash-preview` or `OCR_USING=gpt-5-nano`
  - `TRANSCRIBE_WITH=gemini-3-flash-preview` or an OpenAI transcription model
  - `SUMMARIZE_WITH=gpt-5-mini-2025-08-07`
- OCR/transcription is best-effort. Failures are logged, `ocr_status = failed` is recorded, and the ingest continues.
- `announcement_pipeline_documents` and `announcement_pipeline_reports` are created lazily by the upsert layer. The first run no longer assumes the tables already exist.
- Heavy OCR/transcript/report text defaults to pointer mode: S3 keys, hashes, byte counts, character counts, and excerpts are kept in Postgres, while the full text stays in object storage. Set `ANNOUNCEMENT_POSTGRES_TEXT_MODE=inline` only for debugging or legacy behavior.

## Key Modules

- `data/announcements/cli.py`
- `data/announcements/managed_pipeline.py`
- `data/announcements/pipeline.py`
- `data/announcements/state.py`
- `data/announcements/db.py`
## Required Environment

This path expects working values for:

- PostgreSQL env vars used by `utils/db.py`
- S3 env vars used by `utils/store.py`
- `OPENAI_API_KEY` if structured parsing is enabled
- `GEMINI_KEY` if Gemini is used for OCR or transcription

Required Python packages for the announcement path include:

- `jinja2`
- `openai`
- `pdf2image`
- `requests`

PDF OCR also requires the Poppler command-line tools used by `pdf2image`.

Install Poppler:

```sh
brew install poppler
```

On Ubuntu:

```sh
sudo apt-get install poppler-utils
```

If Poppler is installed outside cron's `PATH`, set `POPPLER_PATH` to the directory containing `pdfinfo` and `pdftoppm`, for example:

```sh
POPPLER_PATH=/opt/homebrew/bin
```

## Attachment handling

- PDF attachments are downloaded and OCRed through [`utils/ocr`](../utils/ocr).
- Direct audio attachments such as `mp3`, `wav`, `mp4`, `m4a`, `ogg`, and `webm` are transcribed through [`utils/transcribe`](../utils/transcribe) and stored through object storage pointers.
- Earnings-call documents can also trigger a second audio pass from an extracted audio link; that transcript is stored through object storage pointers.
- Full-document OCR is only done for categories mapped in `DOCUMENT_PYDANTIC_MAP`.

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
- `ocr_s3_key`, `ocr_sha256`, `ocr_chars`, `ocr_bytes`, `ocr_excerpt`
- `full_ocr_s3_key`, `full_ocr_sha256`, `full_ocr_chars`, `full_ocr_bytes`, `full_ocr_excerpt`
- `audio_transcript_s3_key`, `audio_transcript_sha256`, `audio_transcript_chars`, `audio_transcript_bytes`, `audio_transcript_excerpt`
- `concise_summary_text`
- `concise_summary_s3_key`, `concise_summary_sha256`, `concise_summary_chars`, `concise_summary_bytes`, `concise_summary_excerpt`
- `parsed_reports_json`
- `last_error`

`announcement_pipeline_reports` stores one row per parsed report output and also carries `company_master_id`, `report_s3_key`, `report_sha256`, `report_chars`, `report_bytes`, and `report_excerpt`.

## Text Offload Migration

Existing rows that still have large inline OCR/transcript/report JSON can be migrated incrementally:

```sh
python scripts/offload_announcement_text_to_s3.py --dry-run --limit 100 --manifest-path logs/performance/announcement_text_offload_dry_run.json
python scripts/offload_announcement_text_to_s3.py --limit 500 --manifest-path logs/performance/announcement_text_offload_apply.json
```

Use `--keep-inline` if you want to upload and add metadata first without nulling the heavy inline columns. The manifest records planned/uploaded S3 keys, hashes, byte counts, excerpts, and whether each text column will be nulled, so review the dry-run manifest before applying.

Validate offloaded pointers after migration:

```sh
python scripts/validate_announcement_s3_pointers.py --dry-run --limit 100
python scripts/validate_announcement_s3_pointers.py --limit 100
python scripts/validate_announcement_s3_pointers.py --limit 25 --verify-hash
```

The default validation uses S3 `HEAD` and compares stored byte counts when available. `--verify-hash` downloads each object and compares SHA-256, so run it on a small sample first.
