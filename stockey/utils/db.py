import io
from datetime import datetime, date
from typing import List, Tuple, Union
from environs import Env
import pandas as pd
import psycopg2
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
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD
    )

def generate_postgres_schema(
    df: pd.DataFrame,
    table_name: str,
    unique_keys: List[str] | bool,
) -> str:
    """
    Return a `CREATE TABLE IF NOT EXISTS …` suited for PostgreSQL.
    Very simple dtype → SQL type mapping (expand as needed).
    """
    pg_types = {
        "int64": "BIGINT",
        "int32": "INTEGER",
        "float64": "DOUBLE PRECISION",
        "float32": "REAL",
        "bool": "BOOLEAN",
        "datetime64[ns]": "TIMESTAMPTZ",
        "object": "TEXT",
        "string": "TEXT",
    }

    col_defs = []
    for col, dtype in df.dtypes.items():
        sql_type = pg_types.get(str(dtype), "TEXT")
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
) -> None:
    """
    Upserts a pandas DataFrame into a PostgreSQL table using psycopg2, ensuring unique constraints.

    Parameters:
        df (pd.DataFrame): The DataFrame to upsert into the database.
        table_name (str): The name of the table to upsert into.
        unique_keys (List[str]): List of column names that uniquely identify rows for upsert conflict resolution.

    Returns:
        None
    Raises:
        ValueError: If any unique key is missing from the DataFrame columns.
    """

    cols = df.columns.tolist()
    missing = [k for k in unique_keys if k not in cols]
    if missing:
        raise ValueError(f"DataFrame missing unique keys: {missing}")

    full_table = sql.Identifier(table_name)
    temp_table = sql.Identifier(f"{table_name}_temp")

    col_identifiers = [sql.Identifier(c) for c in cols]
    conflict_cols = sql.SQL(", ").join(sql.Identifier(c) for c in unique_keys)
    set_clause = sql.SQL(", ").join(
        sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(c), sql.Identifier(c))
        for c in cols if c not in unique_keys
    )

    create_main_sql = generate_postgres_schema(df, table_name, unique_keys)
    create_temp_sql = generate_postgres_schema(df, f"{table_name}_temp", False)

    with get_connection() as conn:
        with conn.cursor() as cur:
            # 1. Create main & temp tables
            cur.execute(create_main_sql)
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(temp_table))
            cur.execute(create_temp_sql)

            # 2. COPY DataFrame into temp
            copy_buf = io.StringIO()
            df.to_csv(copy_buf, header=False, index=False, sep="\t", na_rep="\\N")
            copy_buf.seek(0)
            cur.copy_expert(
                sql.SQL("COPY {} ({}) FROM STDIN WITH (FORMAT csv, DELIMITER E'\\t', NULL '\\N')").format(
                    temp_table, sql.SQL(", ").join(col_identifiers)
                ),
                copy_buf,
            )

            # 3. Ensure unique index
            if unique_keys:
                idx_name = f"idx_{table_name}_" + "_".join(unique_keys)
                cur.execute(
                    sql.SQL('CREATE UNIQUE INDEX IF NOT EXISTS {} ON {} ({})').format(
                        sql.Identifier(idx_name), full_table, conflict_cols
                    )
                )

            # 4. UPSERT
            cur.execute(
                sql.SQL("""
                    INSERT INTO {} ({})
                    SELECT {} FROM {}
                    ON CONFLICT ({}) DO UPDATE SET {};
                """).format(
                    full_table,
                    sql.SQL(", ").join(col_identifiers),
                    sql.SQL(", ").join(col_identifiers),
                    temp_table,
                    conflict_cols,
                    set_clause,
                )
            )

            # 5. Drop temp table
            cur.execute(sql.SQL("DROP TABLE {}").format(temp_table))
        # leaving the with-block commits automatically


def get_sql(sql: str, params: Tuple = ()) -> pd.Series:
    """
    Run a parametrised SELECT and return exactly one row as a Series.

    Raises:
        ValueError – if zero or >1 rows are returned.

    Notes
    -----
    • Use **%s** placeholders in *sql* – that’s what psycopg2 expects.
      Example:  "SELECT * FROM mytable WHERE id = %s"
    """
    with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()        # list[dict]

    if not rows:
        raise ValueError("Query returned no rows.")
    if len(rows) > 1:
        raise ValueError(f"Query returned more than one row ({len(rows)} rows).")

    # RealDictCursor gives us a dict → easy DataFrame/Series conversion
    return pd.Series(rows[0])


def select_sql(sql: str, params: Tuple = ()) -> pd.DataFrame:
    """
    Run a parametrised SELECT and return every row as a DataFrame
    (empty DataFrame if no matches).
    """
    with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
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
        if row['data_type'] not in ["date", "timestamp", "timestamp with time zone"]:
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
        schema=sql.Identifier('public'),
        table=sql.Identifier(table),
        column=sql.Identifier(column),
    )

    with conn.cursor() as cur:
        cur.execute(query, (date_only,))
        has_target, latest_date = cur.fetchone()

    if latest_date and isinstance(latest_date, datetime):
        latest_date = latest_date.date()
    return has_target, latest_date