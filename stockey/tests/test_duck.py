import pytest
import pandas as pd
import duckdb
from pathlib import Path
from utils.duck import generate_duckdb_schema, upsert_to_db


@pytest.fixture
def sample_dataframe():
    return pd.DataFrame(
        {
            "id": [1, 2, 3],
            "name": ["Alice", "Bob", "Charlie"],
            "age": [25, 30, 35],
            "score": [95.5, 88.0, 92.5],
            "active": [True, False, True],
            "created_at": pd.to_datetime(["2023-01-01", "2023-01-02", "2023-01-03"]),
        }
    )


@pytest.fixture
def temp_db_path(tmp_path):
    return str(tmp_path / "test.db")


def test_generate_duckdb_schema_basic(sample_dataframe):
    schema = generate_duckdb_schema(df=sample_dataframe, table_name="test_table")
    assert "CREATE TABLE IF NOT EXISTS test_table" in schema
    assert '"id" BIGINT' in schema
    assert '"name" TEXT' in schema
    assert '"age" BIGINT' in schema
    assert '"score" DOUBLE' in schema
    assert '"active" BOOLEAN' in schema
    assert '"created_at" TIMESTAMP' in schema


def test_generate_duckdb_schema_with_schema(sample_dataframe):
    schema = generate_duckdb_schema(
        df=sample_dataframe, table_name="test_table", schema="test_schema"
    )
    assert "CREATE TABLE IF NOT EXISTS test_schema.test_table" in schema


def test_generate_duckdb_schema_with_include_cols(sample_dataframe):
    schema = generate_duckdb_schema(
        df=sample_dataframe, table_name="test_table", include_cols=["id", "name"]
    )
    assert '"id" BIGINT' in schema
    assert '"name" TEXT' in schema
    assert '"age"' not in schema


def test_generate_duckdb_schema_with_unique_keys(sample_dataframe):
    schema = generate_duckdb_schema(
        df=sample_dataframe, table_name="test_table", unique_keys=["id"]
    )
    assert 'UNIQUE("id")' in schema


def test_generate_duckdb_schema_invalid_include_cols(sample_dataframe):
    with pytest.raises(ValueError, match="Columns not found in DataFrame"):
        generate_duckdb_schema(
            df=sample_dataframe,
            table_name="test_table",
            include_cols=["invalid_column"],
        )


def test_upsert_to_duckdb_auto_basic(sample_dataframe, temp_db_path):
    # Initial insert
    upsert_to_db(
        df=sample_dataframe,
        db_path=temp_db_path,
        schema="test_schema",
        table_name="test_table",
        unique_keys=["id"],
    )

    # Verify data
    con = duckdb.connect(temp_db_path)
    result = con.execute("SELECT * FROM test_schema.test_table ORDER BY id").fetchall()
    assert len(result) == 3
    assert result[0][0] == 1  # First row, id column
    con.close()


def test_upsert_to_duckdb_auto_update(sample_dataframe, temp_db_path):
    # Initial insert
    upsert_to_db(
        df=sample_dataframe,
        db_path=temp_db_path,
        schema="test_schema",
        table_name="test_table",
        unique_keys=["id"],
    )

    # Update with modified data
    updated_df = sample_dataframe.copy()
    updated_df.loc[0, "name"] = "Alice Updated"

    upsert_to_db(
        df=updated_df,
        db_path=temp_db_path,
        schema="test_schema",
        table_name="test_table",
        unique_keys=["id"],
    )

    # Verify update
    con = duckdb.connect(temp_db_path)
    result = con.execute(
        "SELECT name FROM test_schema.test_table WHERE id = 1"
    ).fetchone()
    assert result[0] == "Alice Updated"
    con.close()


def test_upsert_to_duckdb_auto_row_mismatch(sample_dataframe, temp_db_path):
    # Initial insert
    upsert_to_db(
        df=sample_dataframe,
        db_path=temp_db_path,
        schema="test_schema",
        table_name="test_table",
        unique_keys=["id"],
        mismatch_tol=0.1,
        min_rows_ignore_check=2,
    )

    # Try to update with significantly different row count
    small_df = sample_dataframe.iloc[0:1]

    with pytest.raises(ValueError, match="Row-count mismatch"):
        upsert_to_db(
            df=small_df,
            db_path=temp_db_path,
            schema="test_schema",
            table_name="test_table",
            unique_keys=["id"],
            mismatch_tol=0.1,
            min_rows_ignore_check=2,
        )


def test_upsert_to_duckdb_auto_missing_keys(sample_dataframe, temp_db_path):
    with pytest.raises(ValueError, match="Missing unique keys in DataFrame"):
        upsert_to_db(
            df=sample_dataframe,
            db_path=temp_db_path,
            schema="test_schema",
            table_name="test_table",
            unique_keys=["non_existent_key"],
        )
