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
5. Runs OCR on the first three pages for PDFs, or transcription for direct audio attachments, and stores that text in `three_page_ocr_text`.
6. Categorizes using exchange text plus the first-pass OCR/transcription.
7. If any category is present in the keys of `DOCUMENT_PYDANTIC_MAP`, runs full-document OCR and stores it in `full_ocr_text`.
8. If the category includes `EARNINGS_CALL`, looks for an audio link in the exchange payload or OCR text, downloads it, transcribes it, and stores it in `audio_transcript_text`.
9. Uses the summarization model to extract structured JSON matching `data/announcements/schemas.py`, and stores it in `announcement_pipeline_reports` plus `parsed_reports_json` on the document row.
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
- Direct audio attachments such as `mp3`, `wav`, `mp4`, `m4a`, `ogg`, and `webm` are transcribed through [`utils/transcribe`](../utils/transcribe) and stored in `three_page_ocr_text`.
- Earnings-call documents can also trigger a second audio pass from an extracted audio link; that transcript is stored in `audio_transcript_text`.
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
- `three_page_ocr_text`
- `full_ocr_text`
- `audio_transcript_text`
- `concise_summary_text`
- `parsed_reports_json`
- `last_error`

`announcement_pipeline_reports` stores one row per parsed report output and also carries `company_master_id`.
