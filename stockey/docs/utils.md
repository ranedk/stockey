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

- OpenAI via `OPENAI_API_KEY`
- Gemini via `GEMINI_KEY`
- page selection using `all`, single pages, comma lists, or ranges

Examples:

```sh
python -m utils.ocr /path/to/file.pdf
python -m utils.ocr /path/to/file.pdf --provider gemini --pages 1
python -m utils.ocr /path/to/file.pdf --provider openai --pages 1,3-5
python -m utils.ocr /path/to/file.pdf --provider both --pages all
```

Tested locally with:

```sh
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
