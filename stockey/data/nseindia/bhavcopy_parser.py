# bhavcopy download from S3 and parse
import argparse
import io
import json
import tempfile
import zipfile
import glob
import os
from datetime import datetime, timedelta
from collections.abc import Iterable

import pandas as pd
from environs import Env
import redis

from utils.fallback_telemetry import record_local_fallback_event
from utils.company_master import attach_company_master_id
from utils.db import sql_to_df, upsert_to_db
from utils.ingestion_state import get_failed_entries, get_processed_keys, mark_failed, mark_processed
from utils import store
from utils.date import pd_to_datetime
from utils.schema_migrations import apply_schema_migration
from utils.sync import get_redis_client


env = Env()
env.read_env()

REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env("REDIS_PORT")
REDIS_SET = "bhav:parsed"
SOURCE_PREFIX = "bhavcopy"
EMPTY_VALID_STATUS = "empty_valid_source"
NSE_BHAVCOPY_PARSE_LOOKBACK_DAYS = max(env.int("NSE_BHAVCOPY_PARSE_LOOKBACK_DAYS", 365), 1)
SYNC_SOURCE_NAME = "data.nseindia.bhavcopy_parser"
STOCKEY_RUN_STATE: dict[str, object] = {}

rop = get_redis_client(REDIS_HOST, int(REDIS_PORT))


def emit(message: str) -> None:
    print(message, flush=True)


def chunked(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


def extract_bhavcopy_date_from_key(key: str) -> pd.Timestamp | None:
    try:
        return pd.to_datetime(os.path.basename(key), format="bhavcopy_%Y-%m-%d.zip")
    except ValueError as exc:
        record_local_fallback_event(
            module=SYNC_SOURCE_NAME,
            source=str(key),
            fallback_type="nse_bhavcopy_key_date_parse_failed",
            severity="warn",
            reason="Bhavcopy object key did not match the expected bhavcopy_YYYY-MM-DD.zip format and was skipped by the parser.",
            error=exc,
            metadata={"key": str(key), "source_prefix": SOURCE_PREFIX},
        )
        return None


def should_consider_key(key: str, *, today: datetime | None = None, backfill: bool = False) -> bool:
    file_date = extract_bhavcopy_date_from_key(key)
    if file_date is None:
        return False
    if backfill:                    # backfill ignores the rolling lookback -> every stored day is considered
        return True
    today = today or datetime.today()
    cutoff = pd.Timestamp((today - timedelta(days=NSE_BHAVCOPY_PARSE_LOOKBACK_DAYS)).date())
    return file_date.normalize() >= cutoff


def load_existing_ohlcv_dates(target_dates: Iterable[pd.Timestamp]) -> set[str]:
    normalized = sorted({pd.Timestamp(value).strftime("%Y-%m-%d") for value in target_dates if pd.notna(value)})
    if not normalized:
        return set()

    existing: set[str] = set()
    for batch in chunked(normalized, 500):
        placeholders = ", ".join(["%s"] * len(batch))
        query = f"""
            SELECT DISTINCT date::date AS parsed_date
            FROM nseindia_ohlcv
            WHERE date::date IN ({placeholders})
        """
        try:
            frame = sql_to_df(query, params=tuple(batch))
        except Exception as exc:
            message = str(exc).lower()
            if exc.__class__.__name__ == "UndefinedTable" or "does not exist" in message:
                return set()
            raise
        if frame.empty:
            continue
        existing.update(
            pd.to_datetime(frame["parsed_date"], errors="coerce")
            .dropna()
            .dt.strftime("%Y-%m-%d")
            .tolist()
        )
    return existing


def load_completed_keys(source_prefix: str) -> set[str]:
    return set(get_processed_keys(source_prefix)) | set(get_processed_keys(source_prefix, status=EMPTY_VALID_STATUS))


def classify_bhavcopy_parse_failure(exc: Exception) -> str:
    message = f"{exc.__class__.__name__}: {exc}".lower()
    if isinstance(exc, zipfile.BadZipFile) or "bad_bhavcopy_zip" in message or "badzipfile" in message:
        return "bad_file_retryable"
    if "nested_cm_zip" in message or "nested_bhavcopy_zip" in message or "nested_pr_zip" in message:
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


def _date_window_from_keys(keys: Iterable[str]) -> tuple[str | None, str | None]:
    dates = [
        value.strftime("%Y-%m-%d")
        for value in (extract_bhavcopy_date_from_key(key) for key in keys)
        if value is not None
    ]
    if not dates:
        return None, None
    return min(dates), max(dates)


def run_parser(*, backfill: bool = False, from_date: str | None = None,
               to_date: str | None = None, force: bool = False) -> dict[str, object]:
    lo = pd.Timestamp(from_date) if from_date else None
    hi = pd.Timestamp(to_date) if to_date else None

    def _in_range(key: str) -> bool:
        if lo is None and hi is None:
            return True
        d = extract_bhavcopy_date_from_key(key)
        if d is None:
            return False
        return (lo is None or d >= lo) and (hi is None or d <= hi)

    all_files = list(store.list_files("bhavcopy"))
    files = [key for key in all_files if should_consider_key(key, backfill=backfill) and _in_range(key)]
    keyed_dates = {key: extract_bhavcopy_date_from_key(key) for key in files}
    processed_keys = load_completed_keys(SOURCE_PREFIX)
    parsed_dates = load_existing_ohlcv_dates(
        value for value in keyed_dates.values() if value is not None
    )
    failed_entries = {row["object_key"]: row for row in get_failed_entries(SOURCE_PREFIX)}
    summary: dict[str, object] = {
        "source": SOURCE_PREFIX,
        "rows": 0,
        "rows_read": len(files),
        "rows_written": 0,
        "files_seen": len(all_files),
        "files_considered": len(files),
        "skipped_lookback_count": max(len(all_files) - len(files), 0),
        "already_processed_count": 0,
        "already_parsed_db_count": 0,
        "parsed_count": 0,
        "empty_processed_count": 0,
        "failed_count": 0,
        "failed_classifications": {},
        "prior_failed_count": len(failed_entries),
        "failed_keys": [],
        "empty_keys": [],
        "from_date": None,
        "to_date": None,
        "lookback_days": NSE_BHAVCOPY_PARSE_LOOKBACK_DAYS,
        "fallback_used": False,
        "state_advanced": False,
    }
    from_date, to_date = _date_window_from_keys(files)
    summary["from_date"] = from_date
    summary["to_date"] = to_date

    if failed_entries:
        emit(f"⚠️ Found {len(failed_entries)} previously failed bhavcopy key(s) in DB state")
        for key in sorted(failed_entries)[:10]:
            row = failed_entries[key]
            emit(f"⚠️ Prior failure key={key} at={row.get('processed_at')} error={row.get('error_message')}")

    for key in files:
        parsed_date = keyed_dates.get(key)
        in_db = parsed_date is not None and parsed_date.strftime("%Y-%m-%d") in parsed_dates
        # --force re-parses EVERY stored day in range regardless of skips -> re-runs the whole per-day zip so
        # ALL numerical tables (mcap, delivery, volatility, circuit, ...) are backfilled, not just OHLCV.
        if not force:
            # Default behavior is unchanged: skip anything marked processed. --backfill re-parses a stored day
            # whose OHLCV is NOT yet in the DB (deep history the lookback / a stale marker left unparsed).
            if key in processed_keys and not (backfill and not in_db):
                emit(f"⏩ Already processed in DB state: {key}")
                summary["already_processed_count"] = int(summary["already_processed_count"]) + 1
                continue
            if in_db:
                emit(f"⏩ Already parsed in DB: {key}")
                mark_processed(SOURCE_PREFIX, key)
                processed_keys.add(key)
                summary["already_parsed_db_count"] = int(summary["already_parsed_db_count"]) + 1
                continue
        try:
            file_path = store.get_as_temp_file(key)
        except Exception as exc:
            # store.get_as_temp_file already retries transient S3 read timeouts; if it still fails, skip
            # this one key (record it for retry) rather than aborting the whole multi-hour backfill.
            error_message = f"classification=download_failed; {exc.__class__.__name__}: {exc}"
            emit(f"❌ Failed to download bhavcopy key={key}: {error_message}")
            record_local_fallback_event(
                module=SYNC_SOURCE_NAME,
                source=str(key),
                fallback_type="nse_bhavcopy_download_failed",
                severity="warn",
                reason="Bhavcopy archive download failed after retries; marked failed for retry so the backfill continues.",
                error=exc,
                metadata={"key": str(key), "source_prefix": SOURCE_PREFIX},
            )
            mark_failed(SOURCE_PREFIX, key, error_message)
            summary["failed_count"] = int(summary["failed_count"]) + 1
            classifications = dict(summary["failed_classifications"])
            classifications["download_failed"] = int(classifications.get("download_failed", 0)) + 1
            summary["failed_classifications"] = classifications
            continue
        emit(f"For: {key}")
        # A zero-byte archive is a failed/placeholder download, never a valid empty day.
        # Marking it processed would silently freeze the OHLCV feed at the prior date
        # (observed 2026-07-10: an empty 07-09 zip halted bhavcopy, RS, and technicals).
        try:
            if os.path.getsize(file_path) == 0:
                error_message = "classification=empty_download; zero-byte bhavcopy archive"
                emit(f"❌ Zero-byte bhavcopy archive key={key}; marking failed for re-download")
                record_local_fallback_event(
                    module=SYNC_SOURCE_NAME,
                    source=str(key),
                    fallback_type="nse_bhavcopy_zero_byte_archive",
                    severity="error",
                    reason="Downloaded bhavcopy archive is zero bytes; marked failed so the next download retries instead of freezing the feed.",
                    error=ValueError(error_message),
                    metadata={"key": str(key), "source_prefix": SOURCE_PREFIX},
                )
                mark_failed(SOURCE_PREFIX, key, error_message)
                # evict the empty object so the downloader treats the date as missing and
                # re-fetches (mark_failed alone leaves "file exists" blocking the retry)
                try:
                    store.delete_file(key)
                    emit(f"🗑️ Evicted zero-byte archive from store: {key}")
                except Exception as evict_exc:
                    emit(f"⚠️ Could not evict zero-byte archive {key}: {evict_exc}")
                summary["failed_count"] = int(summary["failed_count"]) + 1
                classifications = dict(summary["failed_classifications"])
                classifications["empty_download"] = int(classifications.get("empty_download", 0)) + 1
                summary["failed_classifications"] = classifications
                continue
        except OSError:
            pass
        try:
            parsed = unzip_and_process(file_path)
        except Exception as exc:
            classification = classify_bhavcopy_parse_failure(exc)
            error_message = f"classification={classification}; {exc.__class__.__name__}: {exc}"
            emit(f"❌ Failed to parse bhavcopy key={key}: {error_message}")
            record_local_fallback_event(
                module=SYNC_SOURCE_NAME,
                source=str(key),
                fallback_type="nse_bhavcopy_parse_failed",
                severity="error" if classification == "parser_bug" else "warn",
                reason="Bhavcopy archive parse failed; this market-wide evidence file remains marked failed for retry/review.",
                error=exc,
                metadata={
                    "classification": classification,
                    "key": str(key),
                    "file_path": str(file_path),
                    "source_prefix": SOURCE_PREFIX,
                },
            )
            mark_failed(SOURCE_PREFIX, key, error_message)
            summary["failed_count"] = int(summary["failed_count"]) + 1
            classifications = dict(summary["failed_classifications"])
            classifications[classification] = int(classifications.get(classification, 0)) + 1
            summary["failed_classifications"] = classifications
            failed_keys = list(summary["failed_keys"])
            if len(failed_keys) < 20:
                failed_keys.append(key)
            summary["failed_keys"] = failed_keys
            continue
        if parsed:
            mark_processed(SOURCE_PREFIX, key)
            processed_keys.add(key)
            rop.sadd(REDIS_SET, key)
            summary["parsed_count"] = int(summary["parsed_count"]) + 1
        else:
            emit(f"⏭️ Marking bhavcopy key as processed without OHLCV rows: {key}")
            mark_processed(SOURCE_PREFIX, key, status=EMPTY_VALID_STATUS)
            processed_keys.add(key)
            summary["empty_processed_count"] = int(summary["empty_processed_count"]) + 1
            empty_keys = list(summary["empty_keys"])
            if len(empty_keys) < 20:
                empty_keys.append(key)
            summary["empty_keys"] = empty_keys

    rop.close()
    advanced_count = int(summary["parsed_count"]) + int(summary["empty_processed_count"]) + int(summary["already_parsed_db_count"])
    summary["rows"] = advanced_count
    summary["rows_written"] = advanced_count
    summary["state_advanced"] = advanced_count > 0
    return summary


def with_company_master(df: pd.DataFrame) -> pd.DataFrame:
    return attach_company_master_id(df, ticker_column="symbol", exchange="NSE")


def ensure_mcap_last_trade_date_is_typed_date() -> None:
    """One-time migration (2026-08-15): last_trade_date was stored as free TEXT, mixing real
    'DD Mon YYYY' date strings with a literal 'Not Traded' sentinel for symbols with no trades --
    confirmed live, 3230 of 1.6M rows. That conflates NULL with a magic string and makes any
    ORDER BY/range query on the column lexicographic garbage, not chronological (audit finding,
    2026-08-15). Converts the column to a real DATE in place; 'Not Traded' becomes a genuine NULL."""
    apply_schema_migration(
        migration_id="20260815_nseindia_mcap_last_trade_date_to_date",
        description="nseindia_mcap.last_trade_date: TEXT (mixing dates with a 'Not Traded' sentinel) -> DATE (NULL for not-traded).",
        owner=SYNC_SOURCE_NAME,
        metadata={"tables": ["nseindia_mcap"]},
        statements=[
            """
            ALTER TABLE nseindia_mcap ALTER COLUMN last_trade_date TYPE DATE
            USING (CASE WHEN last_trade_date ~ '^[0-9]{1,2} [A-Za-z]{3} [0-9]{4}$'
                        THEN to_date(last_trade_date, 'DD Mon YYYY')
                        ELSE NULL END)
            """
        ],
    )


def parse_mcap(path):
    emit("Processing MCAP")
    ensure_mcap_last_trade_date_is_typed_date()
    data = open(path).read()
    lines = [line.strip().rstrip(".") for line in data.strip().split("\n")]
    cleaned_data = "\n".join(lines[:-3])
    df = pd.read_csv(io.StringIO(cleaned_data), skiprows=1)
    df = df.reset_index(drop=True)
    df = df.apply(lambda x: x.str.strip() if x.dtype == "object" else x)
    df.columns = [
        "trade_date",
        "symbol",
        "series",
        "security_name",
        "category",
        "last_trade_date",
        "face_value_rs",
        "issue_size",
        "close_price_paid_up_value_rs",
        "market_cap_rs",
    ]
    df["date"] = pd.to_datetime(df["trade_date"], format="%d %b %Y")
    df = df.drop(columns=["trade_date", "security_name"])
    # 'Not Traded' (a symbol with zero trades on this trade_date) is not a date -- a real NULL,
    # not a magic string (see ensure_mcap_last_trade_date_is_typed_date's docstring).
    df["last_trade_date"] = pd.to_datetime(df["last_trade_date"], format="%d %b %Y", errors="coerce")
    df["issue_size"] = pd.to_numeric(df["issue_size"], errors="coerce").astype("Int64")
    for c in ["face_value_rs", "close_price_paid_up_value_rs", "market_cap_rs"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    unique_keys = ["date", "symbol", "series"]
    df = df.drop_duplicates(subset=unique_keys, keep="last")
    df = with_company_master(df)
    upsert_to_db(df, "nseindia_mcap", unique_keys=unique_keys)
    return df


def parse_mto(path):
    # Delivery position (MTO_<ddmmyyyy>.DAT): retired 2026-08-15 as write-only, revived 2026-09-11 --
    # systrader reads delivery % as a slicing trait (speculative vs investor volume).
    emit("Processing MTO")
    parts = path.split("_")[-1].split(".")[0]
    for_date = pd.to_datetime(parts, format="%d%m%Y")
    # header=None: the 4 skipped lines are preamble (title / "10,MTO" / trade-date / column-name row); the
    # first "20,..." security row must be read as DATA, not consumed as the header (that silently dropped
    # the first stock's delivery every day). Columns are assigned explicitly below.
    df = pd.read_csv(path, skiprows=4, header=None)
    df = df.reset_index(drop=True)
    df.columns = [
        "record_type",
        "sr_no",
        "symbol",
        "series",
        "volume",
        "deliverable_volume",
        "deliverable_percent",
    ][: df.shape[1]]
    df["date"] = for_date
    # keep only security rows (record_type == 20); drop the "90,..." grand-total trailer if present.
    df = df[df["record_type"].astype(str).str.strip() == "20"]
    for c in ["volume", "deliverable_volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")
    df["deliverable_percent"] = pd.to_numeric(
        df["deliverable_percent"], errors="coerce"
    )
    df["symbol"] = df["symbol"].astype(str).str.strip()
    df["series"] = df["series"].astype(str).str.strip()
    # sr_no is text: the "90,MTO,..." trailer puts a word in this column, so pandas reads it as text on a
    # day with a trailer and as integers on a day without one -- pin it so the column type never flips.
    df["sr_no"] = df["sr_no"].astype(str).str.strip()
    unique_keys = ["date", "symbol"]
    df = df.drop_duplicates(subset=unique_keys, keep="last")
    df = with_company_master(df)
    upsert_to_db(df, "nseindia_mto", unique_keys=unique_keys)
    return df


def parse_circuit_hit(path):
    # Price-band hits (bh<ddmmyy>.csv in the PR zip): which stocks hit their upper (H) or lower (L) band.
    # Retired 2026-08-15 as write-only, revived 2026-09-11 -- systrader reads it as a slicing trait.
    emit("Processing Circuit Hit")
    parts = os.path.basename(path)
    try:
        for_date = pd.to_datetime(parts, format="bh%d%m%y.csv")
    except ValueError as exc: # Format issue
        record_local_fallback_event(
            module=SYNC_SOURCE_NAME,
            source=str(path),
            fallback_type="nse_bhavcopy_circuit_hit_date_fallback",
            severity="info",
            reason="Circuit-hit file date did not match short-year format; trying long-year fallback parser.",
            deterministic_fallback=True,
            error=exc,
            metadata={"path": str(path), "filename": parts, "fallback_format": "bh%d%m%Y.csv"},
        )
        for_date = pd.to_datetime(parts, format="bh%d%m%Y.csv")

    df = pd.read_csv(path, usecols=[0, 1, 3], encoding='utf-8', encoding_errors='ignore')
    df.columns = ["symbol", "series", "circuit_hit"]
    df["date"] = for_date
    unique_keys = ["date", "symbol", "series", "circuit_hit"]
    df = df.drop_duplicates(subset=unique_keys, keep="last")
    df = with_company_master(df)
    upsert_to_db(
        df,
        "nseindia_circuit_hit",
        unique_keys=unique_keys,
    )
    return df


def parse_corporate_actions_bc(path):
    emit("Processing Corporate Actions BC")
    cols = [
        "series",
        "symbol",
        "security_name",
        "record_date",
        "bc_start_date",
        "bc_end_date",
        "date",
        "nd_start_date",
        "nd_end_date",
        "subject",
    ]
    # The trailing `subject` (PURPOSE) column is free text with unquoted commas, so some rows carry
    # >10 comma-separated fields (e.g. "DIVIDEND RS 5, SPECIAL"). A plain read_csv raised
    # "Expected 10 fields, saw 11" and lost the WHOLE day's corporate actions. Split with maxsplit so
    # the overflow folds back into `subject` and every record survives.
    records = []
    with open(path, encoding="utf-8", errors="ignore") as fh:
        next(fh, None)  # discard header row
        for raw in fh:
            line = raw.rstrip("\r\n")
            if not line.strip():
                continue
            parts = line.split(",", len(cols) - 1)
            if len(parts) < len(cols):
                parts = parts + [""] * (len(cols) - len(parts))
            records.append(parts)
    df = pd.DataFrame(records, columns=cols)
    df = df.apply(lambda x: x.str.strip() if x.dtype == "object" else x)

    for col in ["record_date", "bc_start_date", "bc_end_date", "date", "nd_start_date", "nd_end_date"]:
        s = (
            df[col]
            .astype("string")
            .str.strip()
            .replace({"": pd.NA, "-": pd.NA, "None": pd.NA, "null": pd.NA})
        )
        # NSE switched the corporate-actions date format from DD/MM/YYYY to ISO YYYY-MM-DD in ~2025-10
        # (alongside the filename lowercasing). Coalesce both so neither era's dates coerce to NaT --
        # which would drop every row via the dropna below and silently zero out corporate actions.
        parsed = pd.to_datetime(s, format="%d/%m/%Y", errors="coerce")
        parsed = parsed.fillna(pd.to_datetime(s, format="%Y-%m-%d", errors="coerce"))
        df[col] = parsed

    df = df.dropna(subset=["date", "symbol", "series", "subject"])
    unique_keys = ["date", "symbol", "series", "subject"]
    df = df.drop_duplicates(subset=unique_keys, keep="last")
    df = with_company_master(df)
    upsert_to_db(df, "nseindia_corporate_actions_bc_raw", unique_keys=unique_keys, timescaledb_column="date")
    return df

def parse_bhavcopy(csv_path):
    emit("Processing BhavCopy")
    df = pd.read_csv(csv_path)
    cmap = {
        'TradDt': 'date',
        'ISIN': 'isin',
        'TckrSymb': 'symbol',
        'SctySrs': 'series',
        'OpnPric': 'open',
        'HghPric': 'high',
        'LwPric': 'low',
        'ClsPric': 'close',
        'LastPric': 'last',
        'PrvsClsgPric': 'previous_close',
        'TtlTradgVol': 'volume',
        'TtlTrfVal': 'total_value',
        'TtlNbOfTxsExctd': 'number_of_trades',
    }
    df = df[list(cmap.keys())]
    df.columns = list(cmap.values())
    df = df.apply(lambda x: x.str.strip() if x.dtype == "object" else x)
    for c in [
        "open",
        "high",
        "low",
        "close",
        "last",
        "previous_close",
        "volume",
        "total_value",
        "number_of_trades",
    ]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = pd_to_datetime(df, "date", formats=["%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y"])
    unique_keys = ["date", "symbol", "series"]
    df = df.drop_duplicates(subset=unique_keys, keep="last")
    df = with_company_master(df)
    upsert_to_db(df, "nseindia_ohlcv", unique_keys=unique_keys)
    return df


def parse_ohlcv(csv_path):
    emit("Processing OHLCV")
    df = pd.read_csv(csv_path, skiprows=1)
    df = df.iloc[:, :13]
    df = df.reset_index(drop=True)
    df.columns = [
        "symbol",
        "series",
        "open",
        "high",
        "low",
        "close",
        "last",
        "previous_close",
        "volume",
        "total_value",
        "date",
        "number_of_trades",
        "isin",
    ]
    df = df.apply(lambda x: x.str.strip() if x.dtype == "object" else x)
    for c in [
        "open",
        "high",
        "low",
        "close",
        "last",
        "previous_close",
        "volume",
        "total_value",
        "number_of_trades",
    ]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = pd_to_datetime(df, "date", formats=["%d-%b-%Y", "%d-%b-%y"])
    unique_keys = ["date", "symbol", "series"]
    df = df.drop_duplicates(subset=unique_keys, keep="last")
    df = with_company_master(df)
    upsert_to_db(df, "nseindia_ohlcv", unique_keys=unique_keys)
    return df


def _ci_glob(root: str, prefix: str, suffix: str = ".csv") -> list[str]:
    """Case-insensitive recursive match of basenames starting with `prefix` and ending with `suffix`.

    NSE lowercased the PR-zip report filenames in ~2025-10 (Bc->bc, MCAP->mcap), so the old
    case-sensitive `glob("Bc*.csv")` / `glob("MCAP*.csv")` silently stopped matching -- corporate
    actions and market cap went missing for months while the run still reported success. Matching
    case-insensitively survives NSE's casing changes.
    """
    p, s = prefix.lower(), suffix.lower()
    matches: list[str] = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            low = name.lower()
            if low.startswith(p) and low.endswith(s):
                matches.append(os.path.join(dirpath, name))
    return matches


def is_empty_zip(path: str) -> bool:
    try:
        return os.path.getsize(path) == 0
    except OSError as exc:
        record_local_fallback_event(
            module=SYNC_SOURCE_NAME,
            source=str(path),
            fallback_type="nse_bhavcopy_zip_stat_failed",
            severity="warn",
            reason="Could not stat bhavcopy zip size before parsing; parser will treat it as non-empty and continue.",
            error=exc,
            metadata={"path": str(path)},
        )
        return False


def unzip_and_process(zip_path):
    emit("Processing %s" % zip_path)
    if is_empty_zip(zip_path):
        emit(f"⏭️ Skipping empty zip: {zip_path}")
        os.remove(zip_path)
        return False

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                zip_ref.extractall(tmpdir)
        except zipfile.BadZipFile as exc:
            emit(f"❌ Bad bhavcopy zip: {zip_path} - {exc}")
            os.remove(zip_path)
            raise RuntimeError(f"bad_bhavcopy_zip:{os.path.basename(zip_path)}") from exc

        failures: list[str] = []

        def _run(label: str, file_path: str, parser) -> None:
            try:
                parser(file_path)
            except Exception as exc:
                # A file inside the bhavcopy archive failed to parse -- the parent archive is marked
                # failed for retry/review, but this file's own failure doesn't stop the others in the
                # same zip from being attempted. (Used to also have a "soft skip" path for a KNOWN
                # corrupt-at-source signature -- cat_turnover, the only caller of it -- retired
                # 2026-08-15 along with that parser; removed with it since nothing else used it.)
                emit(f"❌ Failed {label} file={file_path}: {exc.__class__.__name__}: {exc}")
                record_local_fallback_event(
                    module=SYNC_SOURCE_NAME,
                    source=str(file_path),
                    fallback_type="nse_bhavcopy_file_parse_failed",
                    severity="warn",
                    reason="A file inside the bhavcopy archive failed to parse; the parent archive will be marked failed for retry/review.",
                    error=exc,
                    metadata={
                        "label": str(label),
                        "file_path": str(file_path),
                        "filename": os.path.basename(file_path),
                    },
                )
                failures.append(f"{label}:{os.path.basename(file_path)}:{exc.__class__.__name__}:{exc}")

        nested_zips = glob.glob(os.path.join(tmpdir, "**", "cm*.zip"), recursive=True)
        for nested_zip in nested_zips:
            if is_empty_zip(nested_zip):
                emit(f"⏭️ Skipping empty nested zip: {nested_zip}")
                continue
            with tempfile.TemporaryDirectory() as nested_tmpdir:
                try:
                    with zipfile.ZipFile(nested_zip, "r") as nested_ref:
                        nested_ref.extractall(nested_tmpdir)
                except zipfile.BadZipFile as exc:
                    failures.append(f"nested_cm_zip:{os.path.basename(nested_zip)}:{exc.__class__.__name__}:{exc}")
                    emit(f"❌ Bad nested CM zip: {nested_zip} - {exc}")
                    record_local_fallback_event(
                        module=SYNC_SOURCE_NAME,
                        source=str(nested_zip),
                        fallback_type="nse_bhavcopy_nested_zip_failed",
                        severity="warn",
                        reason="Nested CM bhavcopy archive is corrupt; parent archive will be marked failed for retry/review.",
                        error=exc,
                        metadata={"nested_zip": str(nested_zip), "nested_type": "cm"},
                    )
                    continue

                cm_files = glob.glob(os.path.join(nested_tmpdir, "**", "cm*.csv"), recursive=True)
                for file_path in cm_files:
                    if not os.path.isdir(file_path):
                        _run("ohlcv", file_path, parse_ohlcv)

        nested_zips = glob.glob(os.path.join(tmpdir, "**", "BhavCopy*.zip"), recursive=True)
        for nested_zip in nested_zips:
            if is_empty_zip(nested_zip):
                emit(f"⏭️ Skipping empty nested zip: {nested_zip}")
                continue
            with tempfile.TemporaryDirectory() as nested_tmpdir:
                try:
                    with zipfile.ZipFile(nested_zip, "r") as nested_ref:
                        nested_ref.extractall(nested_tmpdir)
                except zipfile.BadZipFile as exc:
                    failures.append(f"nested_bhavcopy_zip:{os.path.basename(nested_zip)}:{exc.__class__.__name__}:{exc}")
                    emit(f"❌ Bad nested BhavCopy zip: {nested_zip} - {exc}")
                    record_local_fallback_event(
                        module=SYNC_SOURCE_NAME,
                        source=str(nested_zip),
                        fallback_type="nse_bhavcopy_nested_zip_failed",
                        severity="warn",
                        reason="Nested BhavCopy archive is corrupt; parent archive will be marked failed for retry/review.",
                        error=exc,
                        metadata={"nested_zip": str(nested_zip), "nested_type": "bhavcopy"},
                    )
                    continue

                bhav_files = glob.glob(os.path.join(nested_tmpdir, "**", "BhavCopy*.csv"), recursive=True)
                for file_path in bhav_files:
                    if not os.path.isdir(file_path):
                        _run("bhavcopy", file_path, parse_bhavcopy)

        nested_zips = glob.glob(os.path.join(tmpdir, "**", "PR*.zip"), recursive=True)
        for nested_zip in nested_zips:
            if is_empty_zip(nested_zip):
                emit(f"⏭️ Skipping empty nested zip: {nested_zip}")
                continue
            with tempfile.TemporaryDirectory() as nested_tmpdir:
                try:
                    with zipfile.ZipFile(nested_zip, "r") as nested_ref:
                        nested_ref.extractall(nested_tmpdir)
                except zipfile.BadZipFile as exc:
                    failures.append(f"nested_pr_zip:{os.path.basename(nested_zip)}:{exc.__class__.__name__}:{exc}")
                    emit(f"❌ Bad nested PR zip: {nested_zip} - {exc}")
                    record_local_fallback_event(
                        module=SYNC_SOURCE_NAME,
                        source=str(nested_zip),
                        fallback_type="nse_bhavcopy_nested_zip_failed",
                        severity="warn",
                        reason="Nested PR bhavcopy archive is corrupt; parent archive will be marked failed for retry/review.",
                        error=exc,
                        metadata={"nested_zip": str(nested_zip), "nested_type": "pr"},
                    )
                    continue

                # case-insensitive: NSE lowercased these PR-zip filenames in ~2025-10 (Bc->bc, MCAP->mcap),
                # which silently dropped corporate actions + market cap under the old case-sensitive globs.
                bc_files = _ci_glob(nested_tmpdir, "bc")
                for file_path in bc_files:
                    _run("corporate_actions_bc", file_path, parse_corporate_actions_bc)

                # Price-band hits (bh<ddmmyy>.csv) -- revived 2026-09-11 with parse_circuit_hit.
                bh_files = _ci_glob(nested_tmpdir, "bh")
                for file_path in bh_files:
                    _run("circuit_hit", file_path, parse_circuit_hit)

                mcap_files = _ci_glob(nested_tmpdir, "mcap")
                for file_path in mcap_files:
                    _run("mcap", file_path, parse_mcap)

        # NSE ships the security-wise delivery report as MTO_<ddmmyyyy>.DAT (not .CSV) at the top level of
        # the day's archive; the .CSV glob is kept for any older layout.
        mto_files = glob.glob(os.path.join(tmpdir, "**", "MTO_*.DAT"), recursive=True)
        mto_files += glob.glob(os.path.join(tmpdir, "**", "MTO_*.CSV"), recursive=True)
        for file_path in mto_files:
            _run("mto", file_path, parse_mto)

        if failures:
            emit(f"⚠️ Completed bhavcopy zip with {len(failures)} file-level failure(s): {zip_path}")
            raise RuntimeError("; ".join(failures[:20]))

    os.remove(zip_path)
    return True


def main() -> int:
    global STOCKEY_RUN_STATE
    ap = argparse.ArgumentParser(description="Parse stored NSE bhavcopy archives into OHLCV + related tables.")
    ap.add_argument("--backfill", action="store_true",
                    help="Ignore the parse lookback and re-parse every stored day whose OHLCV is missing "
                         "from nseindia_ohlcv (recovers the deep history already downloaded to the store).")
    ap.add_argument("--from", dest="from_date", default=None, help="Only consider stored days >= this date (YYYY-MM-DD)")
    ap.add_argument("--to", dest="to_date", default=None, help="Only consider stored days <= this date (YYYY-MM-DD)")
    ap.add_argument("--force", action="store_true",
                    help="Re-parse every stored day in range regardless of skips -> backfills ALL numerical "
                         "tables (mcap/volatility/circuit/...) for the deep history, not just OHLCV.")
    args, _ = ap.parse_known_args()          # tolerate a test/pytest argv
    STOCKEY_RUN_STATE = run_parser(backfill=args.backfill, from_date=args.from_date,
                                   to_date=args.to_date, force=args.force)
    # A hardcoded "ok"/0 here regardless of outcome would hide real parse failures behind a
    # false success -- this module's purpose is CRITICAL in download_runner's classification.
    # Only a TOTAL failure (nothing parsed despite failures) flips this -- a partial run (one
    # bad file among many good ones) is still real forward progress and stays "ok", matching
    # download_runner's own partial_failed-vs-parse_failed distinction.
    failed_count = int(STOCKEY_RUN_STATE.get("failed_count") or 0)
    rows_written = int(STOCKEY_RUN_STATE.get("rows_written") or STOCKEY_RUN_STATE.get("rows") or 0)
    total_failure = failed_count > 0 and rows_written == 0
    run_status = "parse_failed" if total_failure else "ok"
    print(json.dumps({"status": run_status, **STOCKEY_RUN_STATE}, ensure_ascii=False, default=str), flush=True)
    return 1 if total_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
