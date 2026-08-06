# Utility Commands

## Dump one table locally

```sh
export PGPASSWORD=stockey
pg_dump --host=localhost --port=5432 --username=stockey --table=public.<table_name> --data-only stockey > <table_name>.sql
```

## Restore one table to a server

```sh
export PGPASSWORD=stockey
psql --host=<server_ip> --port=5432 --username=stockey --dbname=stockey --file=<table_name>.sql
```

## Refresh schema summary

```sh
python -m utils.db_schema_dump --schemas public
```

## OCR PDFs with LLMs

The OCR utility lives under [`utils/ocr`](../utils/ocr).

It supports:

- Codex CLI via `codex exec` with image input
- OpenAI via `OPENAI_API_KEY`
- Gemini via `GEMINI_KEY`
- page selection using `all`, single pages, comma lists, or ranges

Examples:

```sh
python -m utils.ocr /path/to/file.pdf
python -m utils.ocr /path/to/file.pdf --provider codex --pages 1
python -m utils.ocr /path/to/file.pdf --provider gemini --pages 1
python -m utils.ocr /path/to/file.pdf --provider openai --pages 1,3-5
python -m utils.ocr /path/to/file.pdf --provider both --pages all
```

Standalone utility, not invoked by any cron/KEEP-scope collector (the
announcement pipeline that used to call it was removed in the pure-TA cut —
`docs/DATA_INVENTORY.md`). Set `OCR_USING=codex` or `OCR_USING=codex:<model>`
to route through Codex CLI instead of hosted APIs; `CODEX_CLI_OCR_MODEL`
chooses the default model for that path.

Tested locally with:

```sh
python -m utils.ocr /tmp/stockey_ocr_test/sample_ocr.pdf --provider codex --pages 1
python -m utils.ocr /tmp/stockey_ocr_test/sample_ocr.pdf --provider gemini --pages 1
python -m utils.ocr /tmp/stockey_ocr_test/sample_ocr.pdf --provider openai --pages 1
```

## Transcribe audio from URLs

The transcription utility lives under [`utils/transcribe`](../utils/transcribe).

It:

- downloads an audio file from a URL
- sends it to OpenAI or Gemini
- returns plain transcript text in JSON

Examples:

```sh
python -m utils.transcribe https://example.com/audio.mp3
python -m utils.transcribe https://example.com/audio.wav --provider gemini
python -m utils.transcribe https://example.com/audio.mp4 --provider openai
python -m utils.transcribe https://example.com/audio.mp3 --provider both
```

Model notes:

- Gemini uses `gemini-3-flash-preview`
- OpenAI uses `gpt-4o-mini-transcribe`
- GPT-5 nano is not used for audio because OpenAI does not currently support audio input on that model

Tested locally with a generated mp3 served over localhost:

```sh
python -m utils.transcribe http://127.0.0.1:8765/sample.mp3 --provider openai
python -m utils.transcribe http://127.0.0.1:8765/sample.mp3 --provider gemini
```

## Review security identity issues

```sh
python -m data.nseindia.security_history
python scripts/sql_query_runner.py --read-only "select * from dim_security_review_events where needs_review = true order by confidence desc, last_seen desc limit 50"
```

Manual overrides go into `dim_security_overrides`. Use them for mergers, demergers, scheme changes, and any rename the heuristics flag incorrectly.

## Recheck Dhan/security identity issues

Open Dhan security mapping failures are stored in `advisory_identity_issues`
(table name predates the pure-TA cut; the module itself now lives in `utils/`,
not `advisory/`). There is no operator UI for this anymore — CLI only.

The recheck path understands common index aliases such as `NIFTY50`,
`NIFTY 50`, `BANKNIFTY`, `NIFTY BANK`, `INDIAVIX`, and `INDIA VIX`, so
alias-only benchmark/index failures should close through the same
preview/apply flow after the Dhan master is fresh.

Dry run first:

```sh
python -m data.dhanlive.scrip_master
python -m utils.identity_issues --limit 100
```

If the dry run shows rows as `would_resolve`, close them:

```sh
python -m utils.identity_issues --apply --limit 100
```

Do not manually delete identity rows. The resolver keeps attempt counts and
resolution metadata so later runs can distinguish active issues from fixed
mappings.
