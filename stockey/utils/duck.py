import pandas as pd
import duckdb


def generate_duckdb_schema(
    df: pd.DataFrame,
    table_name: str,
    schema: str = None,
    include_cols: list[str] = None,
    unique_keys: list[str] = None,
) -> str:
    def duckdb_type_and_nullability(series: pd.Series):
        dtype = series.dtype
        nullable = series.isnull().any()

        if pd.api.types.is_integer_dtype(dtype):
            duck_type = "BIGINT"
        elif pd.api.types.is_float_dtype(dtype):
            duck_type = "DOUBLE"
        elif pd.api.types.is_bool_dtype(dtype):
            duck_type = "BOOLEAN"
        elif pd.api.types.is_datetime64_any_dtype(dtype):
            duck_type = "TIMESTAMP"
        else:
            duck_type = "TEXT"

        # if not nullable:
        #     duck_type += " NOT NULL"

        return duck_type

    # Filter DataFrame to only include selected columns
    if include_cols is not None:
        missing = set(include_cols) - set(df.columns)
        if missing:
            raise ValueError(f"Columns not found in DataFrame: {missing}")
        df = df[include_cols]

    full_table = f"{schema}.{table_name}" if schema else table_name

    column_defs = []
    for col in df.columns:
        duck_type = duckdb_type_and_nullability(df[col])
        column_defs.append(f'"{col}" {duck_type}')

    constraint = ""
    if unique_keys:
        unique_str = ", ".join(f'"{k}"' for k in unique_keys)
        constraint = f",\n    UNIQUE({unique_str})"

    create_stmt = f"""
    CREATE TABLE IF NOT EXISTS {full_table} (
        {','.join(column_defs)}{constraint}
    );
    """
    return create_stmt.strip()


def upsert_to_duckdb_auto(
    df,
    db_path: str,
    schema: str,
    table_name: str,
    unique_keys: list[str],
):
    con = duckdb.connect(db_path)
    full_table = f"{schema}.{table_name}"
    temp_table = f"{table_name}_temp"

    # Choose and validate columns
    cols = df.columns.tolist()
    missing_keys = [k for k in unique_keys if k not in cols]
    if missing_keys:
        raise ValueError(f"Missing unique keys in DataFrame: {missing_keys}")

    # Optional: reorder columns to maintain consistency
    df = df[cols]

    # Create main table schema
    create_main_sql = generate_duckdb_schema(
        df,
        schema=schema,
        table_name=table_name,
        include_cols=cols,
        unique_keys=unique_keys,
    )
    con.execute(create_main_sql)

    # Create temp table schema
    create_temp_sql = generate_duckdb_schema(
        df, table_name=temp_table, include_cols=cols, unique_keys=unique_keys
    )
    con.execute(f"DROP TABLE IF EXISTS {temp_table};")
    con.execute(create_temp_sql)

    # Insert data into temp table
    con.register("df", df)
    col_list = ", ".join(f'"{c}"' for c in cols)
    con.execute(
        f"""
        INSERT INTO {temp_table} ({col_list})
        SELECT {col_list} FROM df
    """
    )

    # Upsert from temp to main
    non_keys = [col for col in cols if col not in unique_keys]
    set_clause = ", ".join(f'"{col}" = EXCLUDED."{col}"' for col in non_keys)
    conflict_keys = ", ".join(f'"{col}"' for col in unique_keys)

    if unique_keys:
        keys_sql = ", ".join(f'"{k}"' for k in unique_keys)
        idx_name = f"idx_{table_name}_" + "_".join(unique_keys)
        con.execute(
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS {idx_name}
            ON {full_table} ({keys_sql});
            """
        )

    con.execute(
        f"""
        INSERT INTO {full_table} ({col_list})
        SELECT {col_list} FROM {temp_table}
        ON CONFLICT ({conflict_keys}) DO UPDATE SET {set_clause};
    """
    )

    con.close()


def get_sql(
    sql: str,
    db_path: str,
    params: tuple = (),
) -> pd.Series:
    """
    Run SQL on DuckDB, fetch result as a DataFrame.
    - If more than one row is returned, raise an error.
    - If no rows are returned, raise an error.
    - Otherwise, return the row as a pandas Series.

    Args:
        sql (str): SQL query to run.
        db_path (str): Path to DuckDB file. Default is in-memory.
        params (tuple): Query parameters for safe substitution.

    Returns:
        pd.Series: Single row of result.
    """
    con = duckdb.connect(db_path)
    try:
        df = con.execute(sql, params).fetchdf()
    finally:
        con.close()

    if df.empty:
        raise ValueError("Query returned no rows.")

    if len(df) > 1:
        raise ValueError(f"Query returned more than one row ({len(df)} rows).")

    return df.iloc[0]  # Return as pandas Series


def select_sql(
    sql: str,
    db_path: str,
    params: tuple = (),
) -> pd.DataFrame:
    """
    Run SQL on DuckDB and fetch all rows as a DataFrame.

    Args:
        sql (str): SQL query to run.
        db_path (str): Path to DuckDB file. Default is in-memory.
        params (tuple): Optional query parameters.

    Returns:
        pd.DataFrame: Query result as a DataFrame (can be empty if no rows match).
    """
    con = duckdb.connect(db_path)
    try:
        df = con.execute(sql, params).fetchdf()
    finally:
        con.close()

    return df
