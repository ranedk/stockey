from utils.duck import get_sql, select_sql
from environs import Env

env = Env()
env.read_env()


def get_nse_equity(ticker: str):
    return get_sql(
        f"""select * from meta.equity_master where symbol=='{ticker}';""",
        env("DUCKDB"),
    )


def get_bse_equity(ticker: str):
    return get_sql(
        f"""select * from meta.equity_master where bse_ticker=='{ticker}';""",
        env("DUCKDB"),
    )
