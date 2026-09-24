# offmarket_parser.py -- parses block/bulk-deal and short-selling CSVs
# (data/nseindia/offmarket.py's downloads) into nseindia_block_deals/
# nseindia_bulk_deals/nseindia_short_selling. REVIVED 2026-08-29 (PRD §12
# todo #1) -- restored unchanged from before the 2026-08-15 retirement: this
# half of the pair was never broken (only the downloader's NSE navigation
# was); the parsing/dedup/upsert logic below needed no fix. Tables are
# auto-created by upsert_to_db (CREATE TABLE IF NOT EXISTS), no migration
# needed.
import json
import os
import pandas as pd
from environs import Env
import redis

from utils.fallback_telemetry import record_local_fallback_event
from utils.company_master import attach_company_master_id
from utils.db import db_session, execute_db_operation, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.ingestion_state import get_processed_keys, mark_failed, mark_processed
from utils import store
from utils.sync import get_redis_client


env = Env()
env.read_env()

REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env("REDIS_PORT")
REDIS_SET = "nsedeals:parsed"
SOURCE_PREFIX = "nsedeals"
SYNC_SOURCE_NAME = "data.nseindia.offmarket_parser"
EMPTY_VALID_STATUS = "empty_valid_source"
STOCKEY_RUN_STATE: dict[str, object] = {}

rop = get_redis_client(REDIS_HOST, int(REDIS_PORT))



def _ensure_date_column_is_timestamptz(table: str) -> None:
    """bulk/block deals' `date` was created TEXT (short_selling's is TIMESTAMPTZ), so every
    reader had to cast and a lexical comparison would silently misorder (2026-09-23 data
    audit). One format, no post-cast duplicates, so the cast is lossless."""
    apply_schema_migration(
        migration_id=f"20260923_{table}_date_to_timestamptz",
        description=f"{table}.date: TEXT -> TIMESTAMPTZ.",
        owner="data.nseindia.offmarket_parser",
        metadata={"tables": [table]},
        statements=[f"ALTER TABLE {table} ALTER COLUMN date TYPE TIMESTAMPTZ USING date::timestamptz"],
    )

def ensure_unique_constraint(
    table_name: str,
    constraint_name: str,
    unique_keys: list[str],
    drop_constraints: list[str] | None = None,
    drop_indexes: list[str] | None = None,
) -> None:
    def _ensure_constraint() -> None:
        with db_session() as (_, cur):
            cur.execute("SELECT to_regclass(%s) AS regclass_name", (table_name,))
            row = cur.fetchone()
            if not row or row[0] is None:
                return
            for drop_name in drop_constraints or []:
                cur.execute(f"ALTER TABLE {table_name} DROP CONSTRAINT IF EXISTS {drop_name}")
            for drop_name in drop_indexes or []:
                cur.execute(f"DROP INDEX IF EXISTS {drop_name}")
            cur.execute(
                """
                SELECT 1
                FROM pg_constraint
                WHERE conname = %s
                  AND conrelid = %s::regclass
                LIMIT 1
                """,
                (constraint_name, table_name),
            )
            if cur.fetchone():
                return
            cols = ", ".join(unique_keys)
            cur.execute(f"ALTER TABLE {table_name} ADD CONSTRAINT {constraint_name} UNIQUE ({cols})")

    execute_db_operation(
        _ensure_constraint,
        operation_name=f"offmarket_parser:ensure_unique_constraint:{table_name}",
    )


def ensure_text_columns(table_name: str, column_names: list[str]) -> None:
    def _ensure_text_columns() -> None:
        with db_session() as (_, cur):
            cur.execute("SELECT to_regclass(%s) AS regclass_name", (table_name,))
            row = cur.fetchone()
            if not row or row[0] is None:
                return
            for column_name in column_names:
                cur.execute(
                    """
                    SELECT data_type
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = %s
                      AND column_name = %s
                    """,
                    (table_name, column_name),
                )
                column = cur.fetchone()
                if not column:
                    continue
                if str(column[0]).lower() != "text":
                    cur.execute(
                        f"ALTER TABLE {table_name} ALTER COLUMN {column_name} TYPE TEXT USING {column_name}::text"
                    )

    execute_db_operation(
        _ensure_text_columns,
        operation_name=f"offmarket_parser:ensure_text_columns:{table_name}",
    )


def load_completed_keys(source_prefix: str) -> set[str]:
    return set(get_processed_keys(source_prefix)) | set(get_processed_keys(source_prefix, status=EMPTY_VALID_STATUS))


def classify_offmarket_parse_failure(exc: Exception) -> str:
    message = f"{exc.__class__.__name__}: {exc}".lower()
    if isinstance(exc, pd.errors.EmptyDataError):
        return "empty_valid_source"
    if isinstance(exc, KeyError):
        return "schema_changed"
    if isinstance(exc, pd.errors.ParserError):
        return "schema_changed"
    schema_markers = [
        "file type not supported",
        "type not supported",
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


def _to_number(series: pd.Series) -> pd.Series:
    """Coerce an NSE numeric column that may carry Indian digit grouping.

    2026-09-01: the deals CSV now comes from NSE's own `&csv=true` endpoint (see
    data/nseindia/offmarket.py for why we stopped driving the download button), and it
    formats quantities with Indian grouping -- "1,70,00,000" rather than 17000000.
    Postgres rejects that for a bigint column, so four files failed to parse with
    InvalidTextRepresentation while short_selling (whose numbers were small enough to
    have no separator) went through fine.

    Written to normalize BOTH shapes rather than the new one only: a parser should not
    depend on which producer wrote the file, and the archive on disk contains years of
    files from the older download path. Non-numeric junk becomes NaN rather than
    raising, so one bad cell cannot fail an entire block.
    """
    if series.dtype.kind in "iuf":
        return series
    cleaned = (
        series.astype("string")
        .str.replace(",", "", regex=False)
        .str.replace("\u00a0", "", regex=False)
        .str.strip()
    )
    return pd.to_numeric(cleaned, errors="coerce")


def process_csv(file_name, csv_path):
    """Process a block deals, bulk deals and short selling CSV file."""
    print("Processing %s - %s" % (file_name, csv_path))
    df = pd.read_csv(csv_path)
    if "block_deals" in file_name:
        dtype = "block_deals"
        unique_keys = ["date", "symbol", "client_name", "buysell", "quantity", "price"]
        constraint_name = "nseindia_block_deals_date_symbol_client_name_buysell_quantity_price_key"
        drop_constraints = ["nseindia_block_deals_date_symbol_client_name_buysell_key"]
        drop_indexes = ["idx_nseindia_block_deals_date_symbol_client_name_buysell"]
        df.columns = ["date", "symbol", "security_name", "client_name", "buysell", "quantity", "price", "remarks"]
    elif "bulk_deals" in file_name:
        dtype = "bulk_deals"
        unique_keys = ["date", "symbol", "client_name", "buysell", "quantity", "price"]
        constraint_name = "nseindia_bulk_deals_date_symbol_client_name_buysell_quantity_price_key"
        drop_constraints = ["nseindia_bulk_deals_date_symbol_client_name_buysell_key"]
        drop_indexes = ["idx_nseindia_bulk_deals_date_symbol_client_name_buysell"]
        df.columns = ["date", "symbol", "security_name", "client_name", "buysell", "quantity", "price", "remarks"]
    elif "short_selling" in file_name:
        dtype = "short_selling"
        unique_keys = ["date", "symbol", "quantity"]
        constraint_name = "nseindia_short_selling_date_symbol_quantity_key"
        drop_constraints = ["nseindia_short_selling_date_symbol_key"]
        drop_indexes = ["idx_nseindia_short_selling_date_symbol"]
        df.columns = ["date", "symbol", "security_name", "quantity"]
    else:
        raise ValueError("file %s type not supported" % file_name)

    df["date"] = pd.to_datetime(df["date"], format="%d-%b-%Y", errors="coerce")
    for numeric_column in ("quantity", "price"):
        if numeric_column in df.columns:
            df[numeric_column] = _to_number(df[numeric_column])
    df = df.dropna(subset=["date", "symbol"]).copy()
    df = df.drop_duplicates(subset=unique_keys, keep="last").reset_index(drop=True)
    df = attach_company_master_id(df, ticker_column="symbol", exchange="NSE")
    ensure_text_columns(
        f"nseindia_{dtype}",
        ["symbol", "security_name", "client_name", "buysell", "remarks", "company_master_id"],
    )
    ensure_unique_constraint(
        f"nseindia_{dtype}",
        constraint_name,
        unique_keys,
        drop_constraints=drop_constraints,
        drop_indexes=drop_indexes,
    )
    if dtype in ("bulk_deals", "block_deals"):
        _ensure_date_column_is_timestamptz(f"nseindia_{dtype}")
    upsert_to_db(df, f"nseindia_{dtype}", unique_keys=unique_keys)
    state_status = EMPTY_VALID_STATUS if df.empty else "processed"
    mark_processed(SOURCE_PREFIX, file_name, status=state_status)
    if not df.empty:
        rop.sadd(REDIS_SET, file_name)
    os.remove(csv_path)
    return {
        "file_name": file_name,
        "dtype": dtype,
        "status": state_status,
        "rows": int(len(df)),
        "from_date": df["date"].min().date().isoformat() if not df.empty else None,
        "to_date": df["date"].max().date().isoformat() if not df.empty else None,
    }


def run_parser() -> dict[str, object]:
    parsed_files = load_completed_keys(SOURCE_PREFIX)
    state: dict[str, object] = {
        "source": SYNC_SOURCE_NAME,
        "rows": 0,
        "rows_read": 0,
        "rows_written": 0,
        "files_seen": 0,
        "files_considered": 0,
        "parsed_count": 0,
        "empty_valid_count": 0,
        "already_processed_count": 0,
        "failed_count": 0,
        "failed_classifications": {},
        "attempt_count": 0,
        "failed_attempt_count": 0,
        "parse_failed_count": 0,
        "fallback_used": False,
        "state_advanced": False,
    }
    from_dates = []
    to_dates = []
    for f in store.list_files("nsedeals"):
        state["files_seen"] = int(state["files_seen"]) + 1
        if f in parsed_files:
            print(f"⏩ Already parsed in DB state: {f}")
            state["already_processed_count"] = int(state["already_processed_count"]) + 1
            continue

        state["files_considered"] = int(state["files_considered"]) + 1
        state["attempt_count"] = int(state["attempt_count"]) + 1
        try:
            file_path = store.get_as_temp_file(f)
            result = process_csv(f, file_path)
        except Exception as exc:
            classification = classify_offmarket_parse_failure(exc)
            error_message = f"classification={classification}; {type(exc).__name__}: {exc}"
            if classification == "empty_valid_source":
                mark_processed(SOURCE_PREFIX, f, status=EMPTY_VALID_STATUS)
                parsed_files.add(f)
                state["empty_valid_count"] = int(state["empty_valid_count"]) + 1
                state["rows_written"] = int(state["rows_written"]) + 1
                state["state_advanced"] = True
                print(f"⏭️ Off-market empty valid source file={f} error={type(exc).__name__}: {exc}", flush=True)
                continue
            mark_failed(SOURCE_PREFIX, f, error_message)
            state["failed_count"] = int(state["failed_count"]) + 1
            state["failed_attempt_count"] = int(state["failed_attempt_count"]) + 1
            state["parse_failed_count"] = int(state["parse_failed_count"]) + 1
            classifications = dict(state["failed_classifications"])
            classifications[classification] = int(classifications.get(classification, 0)) + 1
            state["failed_classifications"] = classifications
            failed_files = state.setdefault("failed_files", [])
            if isinstance(failed_files, list):
                failed_files.append({"file_name": f, "classification": classification, "error": error_message})
            record_local_fallback_event(
                module=SYNC_SOURCE_NAME,
                source=f,
                fallback_type="nse_offmarket_parse_failed",
                severity="warn",
                reason=(
                    "NSE off-market/deals parser failed for this stored file; block/bulk/short-selling "
                    "evidence may be incomplete until the file is fixed or reparsed."
                ),
                error=exc,
                metadata={
                    "file_name": f,
                    "classification": classification,
                    "error_message": error_message,
                },
            )
            print(f"Off-market parse failed file={f} error={error_message}", flush=True)
            continue
        rows = int(result.get("rows") or 0)
        if str(result.get("status") or "") == EMPTY_VALID_STATUS:
            state["empty_valid_count"] = int(state["empty_valid_count"]) + 1
        else:
            state["parsed_count"] = int(state["parsed_count"]) + 1
        state["rows"] = int(state["rows"]) + rows
        state["rows_read"] = int(state["rows_read"]) + rows
        state["rows_written"] = int(state["rows_written"]) + (rows if rows > 0 else 1)
        state["state_advanced"] = True
        if result.get("from_date"):
            from_dates.append(str(result["from_date"]))
        if result.get("to_date"):
            to_dates.append(str(result["to_date"]))
        parsed_files.add(f)
    if from_dates:
        state["from_date"] = min(from_dates)
    if to_dates:
        state["to_date"] = max(to_dates)
    return state


def main() -> int:
    global STOCKEY_RUN_STATE
    STOCKEY_RUN_STATE = run_parser()

    rop.close()
    status = "partial" if int(STOCKEY_RUN_STATE.get("failed_count") or 0) else "ok"
    print(json.dumps({"status": status, **STOCKEY_RUN_STATE}, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
