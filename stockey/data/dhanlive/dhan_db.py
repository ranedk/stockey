import pandas as pd
from environs import Env

from utils.fallback_telemetry import record_fallback_event, record_local_fallback_event
from utils.identity_issues import record_dhan_identity_issue
from utils.db import get_sql, sql_to_df
from utils.company_master import load_company_master_records

env = Env()
env.read_env()


INDEX_ALIASES = {
    "NIFTY": ("NIFTY", "NIFTY 50", "NIFTY50", "NIFTY 50 INDEX", "NIFTY50 INDEX"),
    "NIFTY50": ("NIFTY", "NIFTY 50", "NIFTY50", "NIFTY 50 INDEX", "NIFTY50 INDEX"),
    "NIFTY 50": ("NIFTY", "NIFTY 50", "NIFTY50", "NIFTY 50 INDEX", "NIFTY50 INDEX"),
    "BANKNIFTY": ("BANKNIFTY", "NIFTY BANK", "NIFTYBANK", "BANK NIFTY", "NIFTY BANK INDEX"),
    "NIFTYBANK": ("BANKNIFTY", "NIFTY BANK", "NIFTYBANK", "BANK NIFTY", "NIFTY BANK INDEX"),
    "NIFTY BANK": ("BANKNIFTY", "NIFTY BANK", "NIFTYBANK", "BANK NIFTY", "NIFTY BANK INDEX"),
    "INDIAVIX": ("INDIAVIX", "INDIA VIX", "INDIA VIX INDEX"),
    "INDIA VIX": ("INDIAVIX", "INDIA VIX", "INDIA VIX INDEX"),
}


def index_search_terms(symbol: str) -> tuple[str, ...]:
    symbol_upper = str(symbol or "").strip().upper()
    compact = symbol_upper.replace(" ", "")
    raw_terms = INDEX_ALIASES.get(symbol_upper) or INDEX_ALIASES.get(compact) or (symbol_upper,)
    ordered: list[str] = []
    for term in [symbol_upper, compact, *raw_terms]:
        text = str(term or "").strip().upper()
        if text and text not in ordered:
            ordered.append(text)
    return tuple(ordered)


def _record_dhan_identity_local_fallback(
    *,
    fallback_type: str,
    reason: str,
    error: Exception,
    symbol: str,
    requested_exchange: str,
    asset_type: str,
    metadata: dict[str, object] | None = None,
) -> None:
    record_local_fallback_event(
        module="data.dhanlive.dhan_db",
        fallback_type=fallback_type,
        source="dhan_identity",
        severity="warn",
        reason=reason,
        error=error,
        metadata={
            "symbol": symbol,
            "requested_exchange": requested_exchange,
            "asset_type": asset_type,
            **(metadata or {}),
        },
    )


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
    terms = index_search_terms(symbol)
    query = """
        SELECT *
        FROM master_dhan_instruments
        WHERE valid_to IS NULL
          AND exch_id = %s
          AND segment = 'I'
          AND instrument = 'INDEX'
          AND instrument_type = 'INDEX'
          AND (
                UPPER(COALESCE(underlying_symbol, '')) = ANY(%s)
             OR UPPER(COALESCE(symbol_name, '')) = ANY(%s)
             OR UPPER(COALESCE(display_name, '')) = ANY(%s)
          )
        ORDER BY
            CASE
                WHEN UPPER(COALESCE(underlying_symbol, '')) = %s THEN 0
                WHEN UPPER(COALESCE(symbol_name, '')) = %s THEN 1
                WHEN UPPER(COALESCE(display_name, '')) = %s THEN 2
                ELSE 3
            END,
            load_ts DESC,
            valid_from DESC
        LIMIT 1
    """
    preferred = terms[0]
    return get_sql(query, (exchange_upper, list(terms), list(terms), list(terms), preferred, preferred, preferred))


def resolve_dhan_identity(identifier: str, exchange: str, asset_type: str = "stock") -> dict[str, object]:
    asset_type_lower = asset_type.lower()
    exchange_upper = exchange.upper()

    if asset_type_lower == "stock":
        company = get_company_master_equity(identifier, exchange_upper)
        fallback_tried: list[dict[str, object]] = []
        if exchange_upper == "NSE":
            security_id = company.get("dhan_nse_id")
            resolved_ticker = company.get("nse_ticker")
            resolved_exchange = "NSE"
            exchange_segment = "NSE_EQ"
            if pd.isna(security_id):
                security_id = _resolve_nse_fallback_security_id(company)
                fallback_tried.append(
                    {
                        "exchange": "NSE",
                        "method": "bse_isin_join",
                        "result": "found" if not pd.isna(security_id) else "missing",
                    }
                )
            if pd.isna(security_id):
                security_id = company.get("dhan_bse_id")
                resolved_ticker = company.get("bse_ticker")
                resolved_exchange = "BSE"
                exchange_segment = "BSE_EQ"
                fallback_tried.append(
                    {
                        "exchange": "BSE",
                        "method": "company_master_dhan_bse_id",
                        "result": "found" if not pd.isna(security_id) else "missing",
                    }
                )
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
                fallback_tried.append(
                    {
                        "exchange": "NSE",
                        "method": "company_master_dhan_nse_id",
                        "result": "found" if not pd.isna(security_id) else "missing",
                    }
                )
        else:
            raise ValueError(f"Unsupported exchange for stock: {exchange}")
        if pd.isna(security_id):
            error_text = f"No Dhan security id mapped for {exchange_upper}:{identifier}"
            try:
                record_fallback_event(
                    module="data.dhanlive.dhan_db",
                    source="dhan_identity",
                    fallback_type="dhan_identity_unresolved",
                    severity="error",
                    symbol=identifier,
                    reason=error_text,
                    fallback_used=bool(fallback_tried),
                    metadata={"requested_exchange": exchange_upper, "asset_type": asset_type_lower, "fallback_tried": fallback_tried},
                )
            except Exception as telemetry_exc:
                _record_dhan_identity_local_fallback(
                    fallback_type="dhan_identity_telemetry_failed",
                    reason="DB-backed Dhan identity unresolved telemetry failed; local fallback telemetry was recorded instead.",
                    error=telemetry_exc,
                    symbol=identifier,
                    requested_exchange=exchange_upper,
                    asset_type=asset_type_lower,
                    metadata={"original_error": error_text, "fallback_tried": fallback_tried},
                )
            try:
                record_dhan_identity_issue(
                    symbol=identifier,
                    requested_exchange=exchange_upper,
                    asset_type=asset_type_lower,
                    company=company,
                    fallback_tried=fallback_tried,
                    error_text=error_text,
                )
            except Exception as exc:
                _record_dhan_identity_local_fallback(
                    fallback_type="dhan_identity_issue_record_failed",
                    reason="Dhan identity issue persistence failed; unresolved identity will still raise to the caller.",
                    error=exc,
                    symbol=identifier,
                    requested_exchange=exchange_upper,
                    asset_type=asset_type_lower,
                    metadata={"original_error": error_text, "fallback_tried": fallback_tried},
                )
                print(f"[dhan.identity] failed to record identity issue for {exchange_upper}:{identifier}: {exc}", flush=True)
            raise ValueError(error_text)
        if fallback_tried:
            record_fallback_event(
                module="data.dhanlive.dhan_db",
                source="dhan_identity",
                fallback_type="dhan_identity_fallback",
                severity="warn",
                symbol=identifier,
                reason=f"Resolved {exchange_upper}:{identifier} through fallback exchange/method.",
                metadata={
                    "requested_exchange": exchange_upper,
                    "resolved_exchange": resolved_exchange,
                    "resolved_ticker": resolved_ticker,
                    "fallback_tried": fallback_tried,
                },
            )
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
        try:
            instrument = get_index_instrument(identifier, exchange_upper)
        except Exception as exc:
            error_text = f"No Dhan index security id mapped for {exchange_upper}:{identifier}"
            fallback_tried = [
                {
                    "exchange": exchange_upper,
                    "method": "index_alias_lookup",
                    "aliases": list(index_search_terms(identifier)),
                    "result": "missing",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            ]
            try:
                record_fallback_event(
                    module="data.dhanlive.dhan_db",
                    source="dhan_identity",
                    fallback_type="dhan_index_identity_unresolved",
                    severity="error",
                    symbol=identifier,
                    reason=error_text,
                    fallback_used=True,
                    metadata={"requested_exchange": exchange_upper, "asset_type": asset_type_lower, "fallback_tried": fallback_tried},
                )
            except Exception as telemetry_exc:
                _record_dhan_identity_local_fallback(
                    fallback_type="dhan_index_identity_telemetry_failed",
                    reason="DB-backed Dhan index identity unresolved telemetry failed; local fallback telemetry was recorded instead.",
                    error=telemetry_exc,
                    symbol=identifier,
                    requested_exchange=exchange_upper,
                    asset_type=asset_type_lower,
                    metadata={"original_error": error_text, "fallback_tried": fallback_tried},
                )
            try:
                record_dhan_identity_issue(
                    symbol=identifier,
                    requested_exchange=exchange_upper,
                    asset_type=asset_type_lower,
                    fallback_tried=fallback_tried,
                    error_text=error_text,
                )
            except Exception as record_exc:
                _record_dhan_identity_local_fallback(
                    fallback_type="dhan_index_identity_issue_record_failed",
                    reason="Dhan index identity issue persistence failed; unresolved identity will still raise to the caller.",
                    error=record_exc,
                    symbol=identifier,
                    requested_exchange=exchange_upper,
                    asset_type=asset_type_lower,
                    metadata={"original_error": error_text, "fallback_tried": fallback_tried},
                )
                print(f"[dhan.identity] failed to record index identity issue for {exchange_upper}:{identifier}: {record_exc}", flush=True)
            raise ValueError(error_text) from exc
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
