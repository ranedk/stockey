import pandas as pd
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
    if company.empty and exchange_upper == "NSE":
        company = load_company_master_records(ticker, exchanges=["BSE"])
    elif company.empty and exchange_upper == "BSE":
        company = load_company_master_records(ticker, exchanges=["NSE"])
    if company.empty:
        company = load_company_master_records(ticker)
    if company.empty:
        raise ValueError(f"No company_master row found for {exchange_upper}:{ticker}")
    return company.iloc[0]


def get_index_instrument(symbol: str, exchange: str):
    exchange_upper = exchange.upper()
    symbol_upper = symbol.strip().upper()
    query = """
        SELECT *
        FROM master_dhan_instruments
        WHERE valid_to IS NULL
          AND exch_id = %s
          AND segment = 'I'
          AND instrument = 'INDEX'
          AND instrument_type = 'INDEX'
          AND (
                UPPER(COALESCE(underlying_symbol, '')) = %s
             OR UPPER(COALESCE(symbol_name, '')) = %s
             OR UPPER(COALESCE(display_name, '')) = %s
          )
        ORDER BY load_ts DESC, valid_from DESC
        LIMIT 1
    """
    return get_sql(query, (exchange_upper, symbol_upper, symbol_upper, symbol_upper))


def resolve_dhan_identity(identifier: str, exchange: str, asset_type: str = "stock") -> dict[str, object]:
    asset_type_lower = asset_type.lower()
    exchange_upper = exchange.upper()

    if asset_type_lower == "stock":
        company = get_company_master_equity(identifier, exchange_upper)
        if exchange_upper == "NSE":
            security_id = company.get("dhan_nse_id")
            resolved_ticker = company.get("nse_ticker")
            resolved_exchange = "NSE"
            exchange_segment = "NSE_EQ"
            if pd.isna(security_id):
                security_id = _resolve_nse_fallback_security_id(company)
            if pd.isna(security_id):
                security_id = company.get("dhan_bse_id")
                resolved_ticker = company.get("bse_ticker")
                resolved_exchange = "BSE"
                exchange_segment = "BSE_EQ"
        elif exchange_upper == "BSE":
            security_id = company.get("dhan_bse_id")
            resolved_ticker = company.get("bse_ticker")
            resolved_exchange = "BSE"
            exchange_segment = "BSE_EQ"
            if pd.isna(security_id):
                security_id = company.get("dhan_nse_id")
                resolved_ticker = company.get("nse_ticker")
                resolved_exchange = "NSE"
                exchange_segment = "NSE_EQ"
        else:
            raise ValueError(f"Unsupported exchange for stock: {exchange}")
        if pd.isna(security_id):
            raise ValueError(f"No Dhan security id mapped for {exchange_upper}:{identifier}")
        return {
            "company_master_id": company["company_master_id"],
            "asset_type": "stock",
            "exchange": resolved_exchange,
            "ticker": str(resolved_ticker).strip(),
            "security_id": int(security_id),
            "exchange_segment": exchange_segment,
            "instrument": "EQUITY",
        }

    if asset_type_lower in {"index", "benchmark"}:
        instrument = get_index_instrument(identifier, exchange_upper)
        return {
            "company_master_id": None,
            "asset_type": asset_type_lower,
            "exchange": exchange_upper,
            "ticker": str(instrument.underlying_symbol).strip(),
            "security_id": int(instrument.security_id),
            "exchange_segment": "IDX_I",
            "instrument": "INDEX",
        }

    raise ValueError(f"Unsupported asset_type: {asset_type}")


def _resolve_nse_fallback_security_id(company: pd.Series) -> int | None:
    dhan_bse_id = company.get("dhan_bse_id")
    if pd.isna(dhan_bse_id):
        return None
    fallback = sql_to_df(
        """
        SELECT n.security_id
        FROM master_dhan_instruments b
        JOIN master_dhan_instruments n
          ON n.isin = b.isin
        WHERE b.security_id = %s
          AND b.exch_id = 'BSE'
          AND b.valid_to IS NULL
          AND n.exch_id = 'NSE'
          AND n.instrument = 'EQUITY'
          AND n.instrument_type = 'ES'
          AND n.valid_to IS NULL
        ORDER BY n.load_ts DESC, n.valid_from DESC
        LIMIT 1
        """,
        params=(int(dhan_bse_id),),
    )
    if fallback.empty:
        return None
    value = fallback.iloc[0]["security_id"]
    return None if pd.isna(value) else int(value)


def get_dhan_ohlcv_daily(
    ticker: str,
    *,
    exchange: str = "NSE",
    asset_type: str = "stock",
    from_date: str | None = None,
    to_date: str | None = None,
):
    identity = resolve_dhan_identity(ticker, exchange, asset_type=asset_type)
    conditions = [
        "exchange = %(exchange)s",
        "security_id = %(security_id)s",
        "asset_type = %(asset_type)s",
    ]
    params: dict[str, object] = {
        "security_id": identity["security_id"],
        "exchange": str(identity["exchange"]).upper(),
        "asset_type": asset_type.lower(),
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
    asset_type: str = "stock",
    interval_minutes: int = 1,
    from_timestamp: str | None = None,
    to_timestamp: str | None = None,
):
    identity = resolve_dhan_identity(ticker, exchange, asset_type=asset_type)
    conditions = [
        "exchange = %(exchange)s",
        "security_id = %(security_id)s",
        "asset_type = %(asset_type)s",
        "interval_minutes = %(interval_minutes)s",
    ]
    params: dict[str, object] = {
        "security_id": identity["security_id"],
        "exchange": str(identity["exchange"]).upper(),
        "asset_type": asset_type.lower(),
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
