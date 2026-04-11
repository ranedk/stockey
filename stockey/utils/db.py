"""Database utilities."""
import uuid
from contextlib import contextmanager
from datetime import date, datetime
import sys
import tempfile
import time
from typing import Callable, List, Sequence, Tuple, TypeVar, Union

import pandas as pd
import pandas.api.types as pdt
from environs import Env
from psycopg2 import sql
from psycopg2.extras import RealDictCursor
import sqlalchemy as sa

env = Env()
env.read_env()


DB_HOST = env("POSTGRES_HOST")
DB_PORT = env("POSTGRES_PORT")
DB_NAME = env("POSTGRES_DB")
DB_USER = env("POSTGRES_USER")
DB_PASSWORD = env("POSTGRES_PASSWORD")
SQL_TO_DF_RETRIES = env.int("SQL_TO_DF_RETRIES", 2)
SQL_TO_DF_RETRY_SLEEP_SECONDS = env.float("SQL_TO_DF_RETRY_SLEEP_SECONDS", 1.0)
SQL_TO_DF_STATEMENT_TIMEOUT_MS = env.int("SQL_TO_DF_STATEMENT_TIMEOUT_MS", 0)
SQL_TO_DF_CHUNK_SIZE = env.int("SQL_TO_DF_CHUNK_SIZE", 0)
DB_OPERATION_ATTEMPTS = max(env.int("DB_OPERATION_ATTEMPTS", 3), 3)
DB_POOL_RECYCLE_SECONDS = env.int("DB_POOL_RECYCLE_SECONDS", 300)

# Singleton connection engine for sqlalchemy
_engine = sa.create_engine(
    f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}",
    pool_pre_ping=True,
    pool_recycle=DB_POOL_RECYCLE_SECONDS,
)

T = TypeVar("T")


def qualified_identifier(name: str) -> sql.Identifier:
    """Return a quoted identifier for `table` or `schema.table` names."""
    parts = [part for part in name.split(".") if part]
    if not parts:
        raise ValueError("Identifier cannot be empty")
    return sql.Identifier(*parts)


def _is_transient_db_error(exc: Exception) -> bool:
    name = exc.__class__.__name__
    if name in {"QueryCanceled", "OperationalError", "InterfaceError"}:
        return True
    message = str(exc).lower()
    transient_markers = [
        "statement timeout",
        "canceling statement due to statement timeout",
        "server closed the connection",
        "connection not open",
        "terminating connection",
        "could not connect",
        "timeout expired",
    ]
    return any(marker in message for marker in transient_markers)


def dispose_db_pool() -> None:
    try:
        _engine.dispose()
    except Exception:
        pass


def with_db_retries(
    operation: Callable[[], T],
    *,
    attempts: int | None = None,
    retry_sleep_seconds: float | None = None,
    operation_name: str = "db_operation",
) -> T:
    max_attempts = max(int(attempts or DB_OPERATION_ATTEMPTS), 3)
    sleep_seconds = SQL_TO_DF_RETRY_SLEEP_SECONDS if retry_sleep_seconds is None else float(retry_sleep_seconds)
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return operation()
        except Exception as exc:
            last_exc = exc
            if not _is_transient_db_error(exc) or attempt >= max_attempts:
                raise
            dispose_db_pool()
            print(
                f"[utils.db] transient postgres error in {operation_name}; reconnecting attempt={attempt + 1}/{max_attempts} error={exc.__class__.__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(sleep_seconds * attempt)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"{operation_name} failed without exception")

@contextmanager
def db_session(dict_factory: bool = False):
    """
    Provides a context-managed database session.

    This handles opening a connection, getting a cursor, and managing
    commits and rollbacks. The connection is automatically closed
    upon exiting the `with` block.
    """
    conn = None
    cur = None
    try:
        def _open_session():
            opened_conn = None
            try:
                opened_conn = _engine.raw_connection()
                if dict_factory:
                    opened_cur = opened_conn.cursor(cursor_factory=RealDictCursor)
                else:
                    opened_cur = opened_conn.cursor()
                return opened_conn, opened_cur
            except Exception:
                if opened_conn:
                    try:
                        opened_conn.close()
                    except Exception:
                        pass
                raise

        conn, cur = with_db_retries(_open_session, operation_name="db_session:connect")
        yield conn, cur
        conn.commit()
    except Exception as e:
        if _is_transient_db_error(e):
            dispose_db_pool()
        if conn:
            try:
                conn.rollback()
            except Exception as rollback_exc:
                if _is_transient_db_error(rollback_exc):
                    dispose_db_pool()
        raise e
    finally:
        if cur:
            try:
                cur.close()
            except Exception:
                pass
        if conn:
            try:
                conn.close()
            except Exception:
                pass

def pandas_to_postgres_type(dtype: str):
    PANDAS_TO_POSTGRES = {
        # ----- NumPy integers -----
        "int8":  "SMALLINT",
        "int16": "SMALLINT",
        "int32": "INTEGER",
        "int64": "BIGINT",

        # Unsigned (pick safe wider PG types; uint64 can exceed BIGINT)
        "uint8":  "SMALLINT",
        "uint16": "INTEGER",
        "uint32": "BIGINT",
        "uint64": "NUMERIC(20)",  # use NUMERIC to avoid overflow

        # ----- Nullable pandas integers -----
        "Int8":  "SMALLINT",
        "Int16": "SMALLINT",
        "Int32": "INTEGER",
        "Int64": "BIGINT",

        # Unsigned nullable
        "UInt8":  "SMALLINT",
        "UInt16": "INTEGER",
        "UInt32": "BIGINT",
        "UInt64": "NUMERIC(20)",

        # ----- Floats -----
        "float16": "REAL",
        "float32": "REAL",
        "float64": "DOUBLE PRECISION",

        # Nullable floats
        "Float32": "REAL",
        "Float64": "DOUBLE PRECISION",

        # ----- Booleans -----
        "bool":     "BOOLEAN",
        "boolean":  "BOOLEAN",  # pandas nullable BooleanDtype

        # ----- Strings / text -----
        "string":          "TEXT",
        "string[python]":  "TEXT",
        "string[pyarrow]": "TEXT",
        "object":          "TEXT",  # generic fallback for Python objects

        # ----- Datetime-like -----
        # Always map to TIMESTAMPTZ for TimescaleDB time columns
        "datetime64[ns]":        "TIMESTAMPTZ",
        "datetime64[ns, UTC]":   "TIMESTAMPTZ",
        # (Other tz variants will be handled by a rule-based fallback; see helper below.)

        # ----- Timedelta-like -----
        "timedelta64[ns]": "INTERVAL",

        # ----- Categorical -----
        "category": "TEXT",  # store labels; if you store codes, use SMALLINT/INT

        # ----- Complex (no native PG complex) -----
        "complex64":  "TEXT",
        "complex128": "TEXT",

        # ----- Periods (choose representation; here we use DATE where sensible) -----
        "period[D]":      "DATE",
        "period[M]":      "DATE",
        "period[Q-DEC]":  "DATE",
        "period[A-DEC]":  "DATE",

        # ----- Intervals (pandas Interval) -----
        # Representation varies by subtype; simplest is TEXT
        "interval[int64]":         "TEXT",
        "interval[float64]":       "TEXT",
        "interval[datetime64[ns]]":"TEXT",

        # ----- Sparse (store dense values as their logical type or TEXT) -----
        "Sparse[int64]":    "BIGINT",
        "Sparse[float64]":  "DOUBLE PRECISION",
        "Sparse[boolean]":  "BOOLEAN",
        "Sparse[string]":   "TEXT",

        # ===== PyArrow-backed (when using Arrow dtype backend) =====
        # Numerics
        "int8[pyarrow]":    "SMALLINT",
        "int16[pyarrow]":   "SMALLINT",
        "int32[pyarrow]":   "INTEGER",
        "int64[pyarrow]":   "BIGINT",
        "uint8[pyarrow]":   "SMALLINT",
        "uint16[pyarrow]":  "INTEGER",
        "uint32[pyarrow]":  "BIGINT",
        "uint64[pyarrow]":  "NUMERIC(20)",
        "float32[pyarrow]": "REAL",
        "float64[pyarrow]": "DOUBLE PRECISION",
        "boolean[pyarrow]": "BOOLEAN",
        "decimal[pyarrow]": "NUMERIC",     # precision/scale not embedded in key

        # Binary
        "binary[pyarrow]":       "BYTEA",
        "large_binary[pyarrow]": "BYTEA",

        # Dates / times
        "date32[pyarrow]":  "DATE",
        "date64[pyarrow]":  "DATE",
        "time32[pyarrow]":  "TIME",
        "time64[pyarrow]":  "TIME",

        # Timestamps / durations (units/tz may vary; see helper below)
        "timestamp[pyarrow]": "TIMESTAMPTZ",
        "duration[pyarrow]":  "INTERVAL",
    }

    key = str(dtype)
    if key in PANDAS_TO_POSTGRES:
        return PANDAS_TO_POSTGRES[key]

    if pdt.is_datetime64_any_dtype(dtype):
        return "TIMESTAMPTZ"        # good default for TimescaleDB
    if pdt.is_timedelta64_dtype(dtype):
        return "INTERVAL"
    if pdt.is_integer_dtype(dtype):
        # Best effort when width is unknown
        return "BIGINT" if "64" in key or "Int64" in key else "INTEGER"
    if pdt.is_float_dtype(dtype):
        return "DOUBLE PRECISION"
    if pdt.is_bool_dtype(dtype):
        return "BOOLEAN"
    if pdt.is_categorical_dtype(dtype):
        return "TEXT"
    if pdt.is_string_dtype(dtype):
        return "TEXT"

    # Arrow timestamp variants like 'timestamp[us, tz=UTC][pyarrow]' land here
    if "timestamp" in key and "pyarrow" in key:
        return "TIMESTAMPTZ"

    # Last-resort default
    return "TEXT"


def generate_postgres_schema(
    df: pd.DataFrame,
    table_name: str,
    unique_keys: Sequence[str] | bool,
    *,
    temporary: bool = False,
) -> sql.Composed:
    """
    Return a `CREATE TABLE IF NOT EXISTS …` suited for PostgreSQL.
    Very simple dtype → SQL type mapping (expand as needed).
    """

    col_defs: list[sql.Composed] = []
    for col, dtype in df.dtypes.items():
        sql_type = pandas_to_postgres_type(dtype)
        col_defs.append(
            sql.SQL("{} {}").format(sql.Identifier(col), sql.SQL(sql_type))
        )

    create_prefix = sql.SQL("CREATE TEMP TABLE IF NOT EXISTS {}").format(
        qualified_identifier(table_name)
    ) if temporary else sql.SQL("CREATE TABLE IF NOT EXISTS {}").format(
        qualified_identifier(table_name)
    )
    table_body = sql.SQL(", ").join(col_defs)
    if unique_keys and isinstance(unique_keys, (list, tuple)):
        table_body = sql.SQL("{}, UNIQUE ({})").format(
            table_body,
            sql.SQL(", ").join(sql.Identifier(k) for k in unique_keys),
        )

    return sql.SQL("{} ({});").format(
        create_prefix,
        table_body,
    )


def _upsert_to_db_once(
    df: pd.DataFrame,
    table_name: str,
    unique_keys: List[str],
    timescaledb_column: str | None = None,
) -> None:
    """
    Upserts a pandas DataFrame into a PostgreSQL table using psycopg2.
    """

    if df.empty:
        return  # nothing to do

    cols = df.columns.tolist()
    missing = [k for k in unique_keys if k not in cols]
    if missing:
        raise ValueError(f"DataFrame missing unique keys: {missing}")

    # --- helpers ------------------------------------------------------------
    # Columns & conflicts
    full_table = qualified_identifier(table_name)
    temp_table_name = f"_tmp_{table_name.replace('.', '_')}_{uuid.uuid4().hex[:8]}"
    temp_table = qualified_identifier(temp_table_name)

    col_identifiers = [sql.Identifier(c) for c in cols]
    conflict_identifiers = [sql.Identifier(c) for c in unique_keys]

    update_cols = [c for c in cols if c not in unique_keys]
    if update_cols:
        set_clause = sql.SQL(", ").join(
            sql.Composed(
                [sql.Identifier(c), sql.SQL(" = EXCLUDED."), sql.Identifier(c)]
            )
            for c in update_cols
        )
        on_conflict = sql.SQL("DO UPDATE SET {set}").format(set=set_clause)
    else:
        on_conflict = sql.SQL("DO NOTHING")

    # --- create table DDLs (yours) -----------------------------------------
    create_main_sql = generate_postgres_schema(df, table_name, unique_keys)
    create_temp_sql = generate_postgres_schema(
        df,
        temp_table_name,
        False,
        temporary=True,
    )

    # --- execute ------------------------------------------------------------
    with db_session() as (conn, cur):
        try:
            # 1. Ensure main & temp tables
            cur.execute(create_main_sql)
            schema_name, base_table_name = (
                table_name.split(".", 1) if "." in table_name else ("public", table_name)
            )
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                """,
                (schema_name, base_table_name),
            )
            existing_columns = {row[0] for row in cur.fetchall()}
            for col, dtype in df.dtypes.items():
                if col in existing_columns:
                    continue
                cur.execute(
                    sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} {}").format(
                        full_table,
                        sql.Identifier(col),
                        sql.SQL(pandas_to_postgres_type(dtype)),
                    )
                )
            cur.execute(create_temp_sql)

            # 2. COPY data into temp. Use a local temp file instead of keeping
            # large CSV payloads in memory.
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8", newline="") as copy_buf:
                df.to_csv(copy_buf, header=False, index=False, sep="\t", na_rep="\\N")
                copy_buf.seek(0)
                cur.copy_expert(
                    sql.SQL(
                        "COPY {} ({}) FROM STDIN WITH (FORMAT csv, DELIMITER E'\\t', NULL '\\N')"
                    ).format(temp_table, sql.SQL(", ").join(col_identifiers)),
                    copy_buf,
                )

            # 3.a. Ensure unique index (optional if you already have a PK/unique)
            if unique_keys:
                idx_name = f"idx_{table_name.replace('.', '_')}_{'_'.join(unique_keys)}"
                cur.execute(
                    sql.SQL(
                        "CREATE UNIQUE INDEX IF NOT EXISTS {} ON {} ({})"
                    ).format(
                        sql.Identifier(idx_name),
                        full_table,
                        sql.SQL(", ").join(conflict_identifiers),
                    )
                )

            # 3.b. Promote to TimescaleDB hypertable (optional)
            if timescaledb_column:
                cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb;")
                cur.execute(
                    "SELECT create_hypertable(%s, %s, if_not_exists => TRUE);",
                    (table_name, timescaledb_column),
                )

            # 4. UPSERT
            insert_sql = sql.SQL(
                """
                INSERT INTO {dest} ({cols})
                SELECT {cols} FROM {src}
                ON CONFLICT ({conflict_cols}) {on_conflict}
                """
            ).format(
                dest=full_table,
                src=temp_table,
                cols=sql.SQL(", ").join(col_identifiers),
                conflict_cols=sql.SQL(", ").join(conflict_identifiers),
                on_conflict=on_conflict,
            )

            cur.execute(insert_sql)

        finally:
            # 5. Drop temp regardless of success
            try:
                cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(temp_table))
            except Exception:
                pass


def upsert_to_db(
    df: pd.DataFrame,
    table_name: str,
    unique_keys: List[str],
    timescaledb_column: str | None = None,
) -> None:
    with_db_retries(
        lambda: _upsert_to_db_once(
            df,
            table_name,
            unique_keys,
            timescaledb_column=timescaledb_column,
        ),
        operation_name=f"upsert_to_db:{table_name}",
    )


def _fetch_sql_to_df_once(
    sql_query: str,
    params: Tuple | None = None,
    *,
    statement_timeout_ms: int | None = None,
    chunksize: int | None = None,
) -> pd.DataFrame:
    """
    Run a parametrised SELECT and return every row as a DataFrame
    (empty DataFrame if no matches).
    """
    with db_session() as (_, cur):
        timeout_ms = SQL_TO_DF_STATEMENT_TIMEOUT_MS if statement_timeout_ms is None else int(statement_timeout_ms)
        if timeout_ms and timeout_ms > 0:
            cur.execute("SET LOCAL statement_timeout = %s", (int(timeout_ms),))
        cur.execute(sql_query, params or ())
        columns = [desc[0] for desc in cur.description] if cur.description else []
        if chunksize and chunksize > 0:
            frames: list[pd.DataFrame] = []
            while True:
                rows = cur.fetchmany(int(chunksize))
                if not rows:
                    break
                frames.append(pd.DataFrame(rows, columns=columns))
            return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=columns)
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=columns)


def sql_to_df(
    sql_query: str,
    params: Tuple | None = None,
    *,
    retries: int | None = None,
    statement_timeout_ms: int | None = None,
    chunksize: int | None = None,
) -> pd.DataFrame:
    attempts = max(int(SQL_TO_DF_RETRIES if retries is None else retries), 0) + 1
    attempts = max(attempts, 3)
    chunk_size = SQL_TO_DF_CHUNK_SIZE if chunksize is None else int(chunksize or 0)
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _fetch_sql_to_df_once(
                sql_query,
                params,
                statement_timeout_ms=statement_timeout_ms,
                chunksize=chunk_size,
            )
        except Exception as exc:
            last_exc = exc
            if attempt >= attempts or not _is_transient_db_error(exc):
                raise
            dispose_db_pool()
            print(
                f"[utils.db] transient postgres error in sql_to_df; reconnecting attempt={attempt + 1}/{attempts} error={exc.__class__.__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(float(SQL_TO_DF_RETRY_SLEEP_SECONDS) * attempt)
    if last_exc is not None:
        raise last_exc
    return pd.DataFrame()

def get_sql( sql_query: str, params: Tuple = ()) -> pd.Series:
    """
    Run a parametrised SELECT and return exactly one row as a Series.

    Raises:
        ValueError - if zero or >1 rows are returned.

    Notes
    -----
    • Use **%s** placeholders in *sql* - that’s what psycopg2 expects.
      Example:  "SELECT * FROM mytable WHERE id = %s"
    """
    def _execute() -> list[dict]:
        with db_session(dict_factory=True) as (conn, cur):
            cur.execute(sql_query, params)
            return cur.fetchall()  # list[dict]

    rows = with_db_retries(_execute, operation_name="get_sql")

    if not rows:
        raise ValueError("Query returned no rows.")
    if len(rows) > 1:
        raise ValueError(f"Query returned more than one row ({len(rows)} rows).")

    # RealDictCursor gives us a dict → easy DataFrame/Series conversion
    return pd.Series(rows[0])


def table_has_date(
    table: str,
    column: str,
    target: Union[date, datetime],
) -> tuple[bool, date | None]:
    """
    Return True if *table.column* contains *target* ignoring any time part.

    Parameters
    ----------
    table   : table name (unquoted; will be wrapped safely)
    column  : column name (unquoted)
    target  : a datetime.date or datetime.datetime

    Raises
    ------
    ValueError if the column is not of type DATE.
    """

    def _fetch_column() -> dict | None:
        with db_session(dict_factory=True) as (conn, cur):
            cur.execute(
                """
                SELECT data_type
                FROM   information_schema.columns
                WHERE  table_schema = 'public'
                  AND  table_name   = %s
                  AND  column_name  = %s
                """,
                (table, column),
            )
            return cur.fetchone()

    row = with_db_retries(_fetch_column, operation_name="table_has_date:column")
    if row is None:
        raise ValueError(f"{table}.{column} does not exist")
    if row["data_type"] not in ["date", "timestamp", "timestamp with time zone"]:
        raise ValueError(
            f"{table}.{column} is {row['data_type'].upper()}, not DATE"
        )

    date_only = target.date() if isinstance(target, datetime) else target

    query = sql.SQL(
        """
        SELECT
            EXISTS(SELECT 1
                   FROM   {schema}.{table}
                   WHERE  {column}::date = %s
                   LIMIT  1)                     AS has_target,
            MAX({column}::date)                 AS latest_date
        FROM {schema}.{table}
        """
    ).format(
        schema=sql.Identifier("public"),
        table=sql.Identifier(table),
        column=sql.Identifier(column),
    )

    def _fetch_date_status() -> dict:
        with db_session(dict_factory=True) as (_, cur):
            cur.execute(query, (date_only,))
            return cur.fetchone()

    row = with_db_retries(_fetch_date_status, operation_name="table_has_date:status")
    has_target = row["has_target"]
    latest_date = row["latest_date"]

    if latest_date and isinstance(latest_date, datetime):
        latest_date = latest_date.date()
    return has_target, latest_date


def get_max_date(feature_table):
    """
    Return the maximum date from the feature table.
    """
    try:
        df = sql_to_df(f"SELECT max(date) AS max_date FROM {feature_table};")
        if df["max_date"].notna().any():
            dt = pd.to_datetime(df.loc[0, "max_date"])
            if getattr(dt, "tzinfo", None) is None:
                dt = dt.tz_localize("UTC")
            else:
                dt = dt.tz_convert("UTC")
            return dt.normalize()
    except Exception:
        pass  # table may not exist on first run
    return None
