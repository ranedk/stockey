# indices download from S3 and parse
import json
import tempfile
import zipfile
import glob
import os
from datetime import datetime, timedelta

import pandas as pd
from environs import Env
import redis

from utils.fallback_telemetry import record_local_fallback_event
from utils.db import upsert_to_db
from utils.ingestion_state import get_failed_entries, get_processed_keys, mark_failed, mark_processed
from utils import store
from utils.sync import get_redis_client


env = Env()
env.read_env()

REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env("REDIS_PORT")
REDIS_SET = "indices:parsed"
SOURCE_PREFIX = "indices"
EMPTY_VALID_STATUS = "empty_valid_source"
NSE_INDICES_PARSE_LOOKBACK_DAYS = max(env.int("NSE_INDICES_PARSE_LOOKBACK_DAYS", 365), 1)
SYNC_SOURCE_NAME = "data.nseindia.indices_parser"
STOCKEY_RUN_STATE: dict[str, object] = {}

rop = get_redis_client(REDIS_HOST, int(REDIS_PORT))


def emit(message: str) -> None:
    print(message, flush=True)


def extract_indices_date_from_key(key: str) -> str | None:
    filename = os.path.basename(key)
    try:
        return datetime.strptime(filename, "indices_%Y-%m-%d.zip").strftime("%Y-%m-%d")
    except ValueError as exc:
        record_local_fallback_event(
            module=SYNC_SOURCE_NAME,
            source=str(key),
            fallback_type="nse_indices_key_date_parse_failed",
            severity="warn",
            reason="Indices object key did not match the expected indices_YYYY-MM-DD.zip format and was skipped by the parser.",
            error=exc,
            metadata={"key": str(key), "source_prefix": SOURCE_PREFIX},
        )
        return None


def should_consider_key(key: str, *, today: datetime | None = None, backfill: bool = False) -> bool:
    file_date = extract_indices_date_from_key(key)
    if file_date is None:
        return False
    if backfill:                    # backfill ignores the rolling lookback -> every stored index day considered
        return True
    today = today or datetime.today()
    cutoff = (today - timedelta(days=NSE_INDICES_PARSE_LOOKBACK_DAYS)).date()
    return datetime.strptime(file_date, "%Y-%m-%d").date() >= cutoff


def is_empty_file(path: str) -> bool:
    try:
        return os.path.getsize(path) == 0
    except OSError as exc:
        record_local_fallback_event(
            module=SYNC_SOURCE_NAME,
            source=str(path),
            fallback_type="nse_indices_zip_stat_failed",
            severity="warn",
            reason="Could not stat indices zip size before parsing; parser will treat it as non-empty and continue.",
            error=exc,
            metadata={"path": str(path)},
        )
        return False


def parse_indices_close(path):
    df = pd.read_csv(path)
    df.columns = [
        "index_name",
        "date",
        "open",
        "high",
        "low",
        "close",
        "points_change",
        "percent_change",
        "volume",
        "turnover_cr",
        "pe",
        "pb",
        "div_yield",
    ]

    df = df.reset_index(drop=True)
    df["index_name"] = df["index_name"].astype("string").str.strip()
    try:
        df["date"] = pd.to_datetime(df["date"], format="%d-%m-%Y")
    except ValueError as exc:
        record_local_fallback_event(
            module=SYNC_SOURCE_NAME,
            source=str(path),
            fallback_type="nse_indices_date_fallback",
            severity="info",
            reason="Indices close date did not match dash-separated format; trying slash-separated fallback parser.",
            deterministic_fallback=True,
            error=exc,
            metadata={"path": str(path), "fallback_format": "%d/%m/%Y"},
        )
        df["date"] = pd.to_datetime(df["date"], format="%d/%m/%Y")
    for col in [
        "open",
        "high",
        "low",
        "close",
        "points_change",
        "percent_change",
        "volume",
        "turnover_cr",
        "pe",
        "pb",
        "div_yield",
    ]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["date", "index_name"])
    df = df.drop_duplicates(subset=["date", "index_name"], keep="last")

    upsert_to_db(
        df,
        "nseindia_indices",
        unique_keys=["date", "index_name"],
        timescaledb_column="date"
    )
    return df


def load_completed_keys(source_prefix: str) -> set[str]:
    return set(get_processed_keys(source_prefix)) | set(get_processed_keys(source_prefix, status=EMPTY_VALID_STATUS))


def classify_indices_parse_failure(exc: Exception) -> str:
    message = f"{exc.__class__.__name__}: {exc}".lower()
    if isinstance(exc, zipfile.BadZipFile) or "bad_indices_zip" in message or "badzipfile" in message:
        return "bad_file_retryable"
    if "not a zip" in message or "file is not a zip" in message or "corrupt" in message:
        return "bad_file_retryable"
    if isinstance(exc, KeyError):
        return "schema_changed"
    if isinstance(exc, pd.errors.ParserError):
        return "schema_changed"
    schema_markers = [
        "columns are missing",
        "columns overlap",
        "columns passed",
        "not in index",
        "usecols do not match",
        "length mismatch",
        "expected axis has",
        "expected fields",
        "found in axis",
    ]
    if any(marker in message for marker in schema_markers):
        return "schema_changed"
    return "parser_bug"


def _parse_result(status: str, *, rows: int = 0) -> dict[str, object]:
    return {"status": status, "rows": int(rows)}


def unzip_and_process(zip_path):
    emit("Processing %s" % zip_path)
    if is_empty_file(zip_path):
        emit(f"⏭️ Skipping empty indices zip: {zip_path}")
        os.remove(zip_path)
        return _parse_result(EMPTY_VALID_STATUS)

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                zip_ref.extractall(tmpdir)
        except zipfile.BadZipFile as exc:
            emit(f"❌ Bad indices zip: {zip_path} - {exc}")
            os.remove(zip_path)
            raise RuntimeError(f"bad_indices_zip:{os.path.basename(zip_path)}") from exc

        indices_close_files = glob.glob(os.path.join(tmpdir, "ind_close_*.csv"))
        rows_written = 0
        for file_path in indices_close_files:
            try:
                df = parse_indices_close(file_path)
            except Exception as exc:
                record_local_fallback_event(
                    module=SYNC_SOURCE_NAME,
                    source=str(file_path),
                    fallback_type="nse_indices_file_parse_failed",
                    severity="warn",
                    reason="A file inside the indices archive failed to parse; the parent archive will be marked failed for retry/review.",
                    error=exc,
                    metadata={"file_path": str(file_path), "filename": os.path.basename(file_path)},
                )
                raise
            rows_written += 0 if df is None else len(df)

    os.remove(zip_path)
    if rows_written <= 0:
        emit(f"⏭️ Indices zip had no index close rows: {zip_path}")
        return _parse_result(EMPTY_VALID_STATUS)
    return _parse_result("parsed", rows=rows_written)


def _date_window_from_keys(keys: list[str]) -> tuple[str | None, str | None]:
    dates = [value for value in (extract_indices_date_from_key(key) for key in keys) if value]
    if not dates:
        return None, None
    return min(dates), max(dates)


def run_parser(*, backfill: bool = False, from_date: str | None = None,
               to_date: str | None = None, force: bool = False) -> dict[str, object]:
    def _in_range(key: str) -> bool:
        if from_date is None and to_date is None:
            return True
        fd = extract_indices_date_from_key(key)
        if fd is None:
            return False
        d = datetime.strptime(fd, "%Y-%m-%d").date()
        return ((from_date is None or d >= datetime.strptime(from_date, "%Y-%m-%d").date())
                and (to_date is None or d <= datetime.strptime(to_date, "%Y-%m-%d").date()))

    all_files = list(store.list_files("indices"))
    files = [key for key in all_files if should_consider_key(key, backfill=backfill) and _in_range(key)]
    parsed_files = load_completed_keys(SOURCE_PREFIX)
    failed_entries = {row["object_key"]: row for row in get_failed_entries(SOURCE_PREFIX)}
    from_date, to_date = _date_window_from_keys(files)
    summary: dict[str, object] = {
        "source": SOURCE_PREFIX,
        "rows": 0,
        "rows_read": len(files),
        "rows_written": 0,
        "files_seen": len(all_files),
        "files_considered": len(files),
        "skipped_lookback_count": max(len(all_files) - len(files), 0),
        "already_processed_count": 0,
        "parsed_count": 0,
        "empty_or_incomplete_count": 0,
        "empty_valid_count": 0,
        "failed_count": 0,
        "failed_classifications": {},
        "prior_failed_count": len(failed_entries),
        "failed_keys": [],
        "empty_or_incomplete_keys": [],
        "from_date": from_date,
        "to_date": to_date,
        "lookback_days": NSE_INDICES_PARSE_LOOKBACK_DAYS,
        "fallback_used": False,
        "state_advanced": False,
    }
    if failed_entries:
        emit(f"⚠️ Found {len(failed_entries)} previously failed indices key(s) in DB state")
        for key in sorted(failed_entries)[:10]:
            row = failed_entries[key]
            emit(f"⚠️ Prior failure key={key} at={row.get('processed_at')} error={row.get('error_message')}")
    for f in files:
        if f in parsed_files and not force:          # --force re-parses stored index days already marked done
            emit(f"⏩ Already parsed in DB state: {f}")
            summary["already_processed_count"] = int(summary["already_processed_count"]) + 1
            continue
        file_date = extract_indices_date_from_key(f)
        file_path = store.get_as_temp_file(f)
        emit(
            f"For indices key={f} date={file_date or 'unknown'} temp_file={file_path}"
        )
        try:
            parsed = unzip_and_process(file_path)
        except Exception as exc:
            classification = classify_indices_parse_failure(exc)
            error_message = f"classification={classification}; {exc.__class__.__name__}: {exc}"
            emit(f"❌ Failed to parse indices key={f}: {error_message}")
            record_local_fallback_event(
                module=SYNC_SOURCE_NAME,
                source=str(f),
                fallback_type="nse_indices_parse_failed",
                severity="error" if classification == "parser_bug" else "warn",
                reason="Indices archive parse failed; benchmark/regime evidence remains marked failed for retry/review.",
                error=exc,
                metadata={
                    "classification": classification,
                    "key": str(f),
                    "file_path": str(file_path),
                    "source_prefix": SOURCE_PREFIX,
                },
            )
            mark_failed(SOURCE_PREFIX, f, error_message)
            summary["failed_count"] = int(summary["failed_count"]) + 1
            classifications = dict(summary["failed_classifications"])
            classifications[classification] = int(classifications.get(classification, 0)) + 1
            summary["failed_classifications"] = classifications
            failed_keys = list(summary["failed_keys"])
            if len(failed_keys) < 20:
                failed_keys.append(f)
            summary["failed_keys"] = failed_keys
            continue
        if isinstance(parsed, dict):
            parse_status = str(parsed.get("status") or "")
        else:
            parse_status = "parsed" if parsed else EMPTY_VALID_STATUS
        if parse_status == "parsed":
            mark_processed(SOURCE_PREFIX, f)
            rop.sadd(REDIS_SET, f)
            parsed_files.add(f)
            summary["parsed_count"] = int(summary["parsed_count"]) + 1
        else:
            emit(f"⏭️ Marking indices key as processed without index rows: {f}")
            mark_processed(SOURCE_PREFIX, f, status=EMPTY_VALID_STATUS)
            parsed_files.add(f)
            summary["empty_or_incomplete_count"] = int(summary["empty_or_incomplete_count"]) + 1
            summary["empty_valid_count"] = int(summary["empty_valid_count"]) + 1
            empty_keys = list(summary["empty_or_incomplete_keys"])
            if len(empty_keys) < 20:
                empty_keys.append(f)
            summary["empty_or_incomplete_keys"] = empty_keys
    rop.close()
    advanced_count = int(summary["parsed_count"]) + int(summary["empty_valid_count"])
    summary["rows"] = advanced_count
    summary["rows_written"] = advanced_count
    summary["state_advanced"] = advanced_count > 0
    return summary


def main() -> int:
    global STOCKEY_RUN_STATE
    import argparse
    ap = argparse.ArgumentParser(description="Parse stored NSE indices archives into nseindia_indices.")
    ap.add_argument("--backfill", action="store_true", help="Ignore the parse lookback -> consider every stored index day")
    ap.add_argument("--force", action="store_true", help="Re-parse stored index days already marked done (full history backfill)")
    ap.add_argument("--from", dest="from_date", default=None, help="Only consider stored days >= this date (YYYY-MM-DD)")
    ap.add_argument("--to", dest="to_date", default=None, help="Only consider stored days <= this date (YYYY-MM-DD)")
    args, _ = ap.parse_known_args()
    STOCKEY_RUN_STATE = run_parser(backfill=args.backfill, force=args.force,
                                   from_date=args.from_date, to_date=args.to_date)
    print(json.dumps({"status": "ok", **STOCKEY_RUN_STATE}, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
