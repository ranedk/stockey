import uuid
import io
from datetime import date, datetime
from typing import List, Tuple, Union

import pandas as pd
import pandas.api.types as pdt
import psycopg2
from environs import Env
from psycopg2 import sql
from psycopg2.extras import RealDictCursor

env = Env()
env.read_env()


DB_HOST = env("POSTGRES_HOST")
DB_PORT = env("POSTGRES_PORT")
DB_NAME = env("POSTGRES_DB")
DB_USER = env("POSTGRES_USER")
DB_PASSWORD = env("POSTGRES_PASSWORD")


def get_connection():
    """
    Returns a new connection to the configured PostgreSQL database.
    Usage:
        from utils.db import get_connection
        conn = get_connection()
    """
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD
    )

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
    unique_keys: List[str] | bool,
) -> str:
    """
    Return a `CREATE TABLE IF NOT EXISTS …` suited for PostgreSQL.
    Very simple dtype → SQL type mapping (expand as needed).
    """

    col_defs = []
    for col, dtype in df.dtypes.items():
        sql_type = pandas_to_postgres_type(dtype)
        col_defs.append(f'"{col}" {sql_type}')

    unique_clause = ""
    if unique_keys and isinstance(unique_keys, (list, tuple)):
        keys = ", ".join(f'"{k}"' for k in unique_keys)
        unique_clause = f", UNIQUE ({keys})"


    return f'CREATE TABLE IF NOT EXISTS {table_name} ({", ".join(col_defs)}{unique_clause});'


def upsert_to_db(
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
    def ident_from_table(tname: str) -> sql.Identifier:
        """Return sql.Identifier, handling optional schema-qualified names."""
        if "." in tname:
            schema, name = tname.split(".", 1)
            return sql.Identifier(schema, name)
        return sql.Identifier(tname)

    # Columns & conflicts
    full_table = ident_from_table(table_name)
    temp_table_name = f"{table_name}_temp_{uuid.uuid4().hex[:8]}"
    temp_table = ident_from_table(temp_table_name)

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
    create_temp_sql = generate_postgres_schema(df, temp_table_name, False)

    # --- execute ------------------------------------------------------------
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                # 1. Ensure main & temp tables
                cur.execute(create_main_sql)
                cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(temp_table))
                cur.execute(create_temp_sql)

                # 2. COPY data into temp
                copy_buf = io.StringIO()
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
                cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(temp_table))
            # commit on context exit


def get_sql(sql_query: str, params: Tuple = ()) -> pd.Series:
    """
    Run a parametrised SELECT and return exactly one row as a Series.

    Raises:
        ValueError - if zero or >1 rows are returned.

    Notes
    -----
    • Use **%s** placeholders in *sql* - that’s what psycopg2 expects.
      Example:  "SELECT * FROM mytable WHERE id = %s"
    """
    with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql_query, params)
        rows = cur.fetchall()  # list[dict]

    if not rows:
        raise ValueError("Query returned no rows.")
    if len(rows) > 1:
        raise ValueError(f"Query returned more than one row ({len(rows)} rows).")

    # RealDictCursor gives us a dict → easy DataFrame/Series conversion
    return pd.Series(rows[0])


def select_sql(sql_query: str, params: Tuple = ()) -> pd.DataFrame:
    """
    Run a parametrised SELECT and return every row as a DataFrame
    (empty DataFrame if no matches).
    """
    with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql_query, params)
        rows = cur.fetchall()

    return pd.DataFrame(rows)


def table_has_date(
    table: str,
    column: str,
    target: Union[date, datetime],
) -> bool:
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

    with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
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
        row = cur.fetchone()
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
                   WHERE  {column} = %s
                   LIMIT  1)                     AS has_target,
            MAX({column})                       AS latest_date
        FROM {schema}.{table}
        """
    ).format(
        schema=sql.Identifier("public"),
        table=sql.Identifier(table),
        column=sql.Identifier(column),
    )

    with conn.cursor() as cur:
        cur.execute(query, (date_only,))
        has_target, latest_date = cur.fetchone()

    if latest_date and isinstance(latest_date, datetime):
        latest_date = latest_date.date()
    return has_target, latest_date