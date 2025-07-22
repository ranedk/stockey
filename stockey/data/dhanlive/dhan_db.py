from environs import Env

from utils.duck import get_sql, select_sql

env = Env()
env.read_env()


def get_nse_equity(ticker: str):
    return get_sql(
        f"""
        SELECT * from {env("SCHEMA")}.master_dhan_instruments
        WHERE instrument=='EQUITY' and instrument_type=='ES'
        and exch_id=='NSE' and underlying_symbol=='{ticker}' and valid_to IS NULL;""",
        env("DUCKDB"),
    )


def get_bse_equity(ticker: str):
    return get_sql(
        f"""
        SELECT * from {env("SCHEMA")}.master_dhan_instruments
        WHERE instrument=='EQUITY' and instrument_type=='ES'
        and exch_id=='BSE' and underlying_symbol=='{ticker}' and valid_to IS NULL;""",
        env("DUCKDB"),
    )
