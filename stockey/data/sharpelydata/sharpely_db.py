from utils.db import get_sql
from environs import Env

env = Env()
env.read_env()


def get_nse_equity(ticker: str):
    return get_sql(
        f"""select * from master_sharpely_equity where symbol='{ticker}';""",
    )


def get_bse_equity(ticker: str):
    return get_sql(
        f"""select * from master_sharpely_equity where bse_ticker='{ticker}';""",
    )
