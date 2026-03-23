from environs import Env

from utils.db import get_sql, sql_to_df
from utils.company_master import load_company_master_records

env = Env()
env.read_env()


def get_nse_equity(ticker: str):
    return get_sql(
        """
        SELECT * from master_dhan_instruments
        WHERE instrument='EQUITY' and instrument_type='ES'
        and exch_id='NSE' and underlying_symbol=%s and valid_to IS NULL;
        """,
        (ticker,),
    )


def get_bse_equity(ticker: str):
    return get_sql(
        """
        SELECT * from master_dhan_instruments
        WHERE instrument='EQUITY' and instrument_type='ES'
        and exch_id='BSE' and security_id::text=%s and valid_to IS NULL;
        """,
        (ticker,),
    )


def get_company_master_equity(ticker: str, exchange: str):
    exchange_upper = exchange.upper()
    company = load_company_master_records(ticker, exchanges=[exchange_upper])
    if company.empty:
        raise ValueError(f"No company_master row found for {exchange_upper}:{ticker}")
    return company.iloc[0]


def get_dhan_ohlcv_daily(
    ticker: str,
    *,
    exchange: str = "NSE",
    from_date: str | None = None,
    to_date: str | None = None,
):
    company = get_company_master_equity(ticker, exchange)
    conditions = ["company_master_id = %(company_master_id)s", "exchange = %(exchange)s"]
    params: dict[str, object] = {
        "company_master_id": company["company_master_id"],
        "exchange": exchange.upper(),
    }
    if from_date:
        conditions.append("date >= %(from_date)s")
        params["from_date"] = from_date
    if to_date:
        conditions.append("date <= %(to_date)s")
        params["to_date"] = to_date
    where_clause = " AND ".join(conditions)
    return sql_to_df(
        f"""
        SELECT *
        FROM dhan_ohlcv_daily
        WHERE {where_clause}
        ORDER BY date
        """,
        params=params,
    )


def get_dhan_ohlcv_intraday(
    ticker: str,
    *,
    exchange: str = "NSE",
    interval_minutes: int = 1,
    from_timestamp: str | None = None,
    to_timestamp: str | None = None,
):
    company = get_company_master_equity(ticker, exchange)
    conditions = [
        "company_master_id = %(company_master_id)s",
        "exchange = %(exchange)s",
        "interval_minutes = %(interval_minutes)s",
    ]
    params: dict[str, object] = {
        "company_master_id": company["company_master_id"],
        "exchange": exchange.upper(),
        "interval_minutes": interval_minutes,
    }
    if from_timestamp:
        conditions.append('"timestamp" >= %(from_timestamp)s')
        params["from_timestamp"] = from_timestamp
    if to_timestamp:
        conditions.append('"timestamp" <= %(to_timestamp)s')
        params["to_timestamp"] = to_timestamp
    where_clause = " AND ".join(conditions)
    return sql_to_df(
        f"""
        SELECT *
        FROM dhan_ohlcv_intraday
        WHERE {where_clause}
        ORDER BY "timestamp"
        """,
        params=params,
    )
