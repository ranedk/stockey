from utils.duck import get_sql, select_sql
from environs import Env

env = Env()
env.read_env()


def get_nse_equity(ticker: str):
    return get_sql(
        f"""
        SELECT * from {env("SCHEMA")}.dhan_instrument_master
        WHERE instrument=='EQUITY' and instrument_type=='ES'
        and exch_id=='NSE' and underlying_symbol=='{ticker}' and valid_to IS NULL;""",
        env("DUCKDB"),
    )


def get_bse_equity(ticker: str):
    return get_sql(
        f"""
        SELECT * from {env("SCHEMA")}.dhan_instrument_master
        WHERE instrument=='EQUITY' and instrument_type=='ES'
        and exch_id=='BSE' and underlying_symbol=='{ticker}' and valid_to IS NULL;""",
        env("DUCKDB"),
    )
