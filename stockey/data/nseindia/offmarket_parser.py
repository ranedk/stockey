# offmarket parser
import os
import pandas as pd
from environs import Env
import redis

from utils.company_master import attach_company_master_id
from utils.db import db_session, upsert_to_db
from utils import store


env = Env()
env.read_env()

REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env("REDIS_PORT")
REDIS_SET = "bhav:parsed"

rop = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def ensure_unique_constraint(
    table_name: str,
    constraint_name: str,
    unique_keys: list[str],
    drop_constraints: list[str] | None = None,
    drop_indexes: list[str] | None = None,
) -> None:
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


def ensure_text_columns(table_name: str, column_names: list[str]) -> None:
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
    upsert_to_db(df, f"nseindia_{dtype}", unique_keys=unique_keys)
    rop.sadd(REDIS_SET, file_name)
    os.remove(csv_path)


if __name__ == "__main__":
    for f in store.list_files("nsedeals"):
        if rop.sismember(REDIS_SET, f):
            print(f"⏩ Already parsed: {f}")
            continue

        file_path = store.get_as_temp_file(f)
        process_csv(f, file_path)
        rop.sadd(REDIS_SET, f)

    rop.close()
