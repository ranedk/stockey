from __future__ import annotations

from typing import Iterable, Optional, Sequence

import pandas as pd

from .db import sql_to_df, upsert_to_db


COMPANY_MASTER_TABLE = "company_master"


def sync_company_master() -> pd.DataFrame:
    sharpely_columns = sql_to_df(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'master_sharpely_equity'
        """
    )
    has_sharpely_id = not sharpely_columns.empty and "sharpely_id" in set(sharpely_columns["column_name"])
    sharpely = sql_to_df(
        f"""
        SELECT
            symbol AS nse_ticker,
            bse_ticker,
            proper_name AS company_name,
            {"sharpely_id" if has_sharpely_id else "NULL::text AS sharpely_id"}
        FROM master_sharpely_equity
        """
    )
    if sharpely.empty:
        return sharpely

    sharpely["nse_ticker"] = _clean_text_series(sharpely["nse_ticker"])
    sharpely["bse_ticker"] = _clean_text_series(sharpely["bse_ticker"])
    sharpely["company_name"] = _clean_text_series(sharpely["company_name"])
    sharpely["sharpely_id"] = _clean_text_series(sharpely["sharpely_id"])

    sharpely = sharpely.dropna(subset=["nse_ticker", "bse_ticker"], how="all").copy()
    sharpely = sharpely.sort_values(["nse_ticker", "bse_ticker"], na_position="last")
    sharpely = sharpely.drop_duplicates(subset=["nse_ticker", "bse_ticker"], keep="last")

    dhan_bse = sql_to_df(
        """
        SELECT DISTINCT ON (security_id::text)
            security_id::text AS bse_ticker,
            security_id AS dhan_bse_id
        FROM master_dhan_instruments
        WHERE valid_to IS NULL
          AND exch_id = 'BSE'
          AND instrument = 'EQUITY'
          AND instrument_type = 'ES'
        ORDER BY security_id::text, load_ts DESC, valid_from DESC
        """
    )
    if not dhan_bse.empty:
        dhan_bse["bse_ticker"] = _clean_text_series(dhan_bse["bse_ticker"])

    dhan_nse = sql_to_df(
        """
        SELECT DISTINCT ON (underlying_symbol)
            underlying_symbol AS nse_ticker,
            security_id AS dhan_nse_id
        FROM master_dhan_instruments
        WHERE valid_to IS NULL
          AND exch_id = 'NSE'
          AND instrument = 'EQUITY'
          AND instrument_type = 'ES'
          AND underlying_symbol IS NOT NULL
        ORDER BY underlying_symbol, load_ts DESC, valid_from DESC
        """
    )
    if not dhan_nse.empty:
        dhan_nse["nse_ticker"] = _clean_text_series(dhan_nse["nse_ticker"])

    company_master = sharpely.merge(dhan_bse, on="bse_ticker", how="left")
    company_master = company_master.merge(dhan_nse, on="nse_ticker", how="left")
    company_master["company_master_id"] = company_master.apply(_build_company_master_id, axis=1)
    company_master = company_master[
        [
            "company_master_id",
            "company_name",
            "sharpely_id",
            "dhan_bse_id",
            "dhan_nse_id",
            "bse_ticker",
            "nse_ticker",
        ]
    ].copy()
    company_master = company_master.drop_duplicates(subset=["company_master_id"], keep="last")

    upsert_to_db(company_master, COMPANY_MASTER_TABLE, ["company_master_id"])
    return company_master


def attach_company_master_id(
    df: pd.DataFrame,
    *,
    ticker_column: str,
    exchange: Optional[str] = None,
    exchange_column: Optional[str] = None,
    target_column: str = "company_master_id",
) -> pd.DataFrame:
    if df.empty:
        return df
    if ticker_column not in df.columns:
        raise KeyError(f"Ticker column not found: {ticker_column}")
    if exchange is None and exchange_column is None:
        raise ValueError("Either exchange or exchange_column is required")

    mapped = df.copy()
    mapped[ticker_column] = _clean_text_series(mapped[ticker_column])

    if exchange_column is not None:
        if exchange_column not in mapped.columns:
            raise KeyError(f"Exchange column not found: {exchange_column}")
        mapped[exchange_column] = _clean_text_series(mapped[exchange_column]).str.upper()
        nse_mask = mapped[exchange_column].eq("NSE")
        bse_mask = mapped[exchange_column].eq("BSE")
        mapped.loc[nse_mask, target_column] = map_company_master_ids(
            mapped.loc[nse_mask, ticker_column],
            exchange="NSE",
        ).values
        mapped.loc[bse_mask, target_column] = map_company_master_ids(
            mapped.loc[bse_mask, ticker_column],
            exchange="BSE",
        ).values
        return mapped

    mapped[target_column] = map_company_master_ids(mapped[ticker_column], exchange=exchange)
    return mapped


def map_company_master_ids(tickers: Iterable[object], *, exchange: str) -> pd.Series:
    ticker_series = pd.Series(list(tickers), dtype="string")
    cleaned = _clean_text_series(ticker_series)
    if cleaned.dropna().empty:
        return pd.Series(pd.NA, index=ticker_series.index, dtype="string")

    exchange_upper = exchange.upper()
    if exchange_upper == "NSE":
        column = "nse_ticker"
    elif exchange_upper == "BSE":
        column = "bse_ticker"
    else:
        raise ValueError(f"Unsupported exchange: {exchange}")

    lookup = sql_to_df(
        f"""
        SELECT {column} AS ticker, company_master_id
        FROM {COMPANY_MASTER_TABLE}
        WHERE {column} = ANY(%s)
        """,
        params=(cleaned.dropna().unique().tolist(),),
    )
    if lookup.empty:
        return pd.Series(pd.NA, index=ticker_series.index, dtype="string")

    mapping = lookup.drop_duplicates(subset=["ticker"], keep="last").set_index("ticker")["company_master_id"]
    return cleaned.map(mapping).astype("string")


def load_company_master_records(ticker: str, exchanges: Optional[Sequence[str]] = None) -> pd.DataFrame:
    clean_ticker = _clean_scalar(ticker)
    if clean_ticker is None:
        return pd.DataFrame()

    exchanges_upper = [value.upper() for value in exchanges] if exchanges else []
    if not exchanges_upper:
        query = f"""
            SELECT *
            FROM {COMPANY_MASTER_TABLE}
            WHERE nse_ticker = %s OR bse_ticker = %s
            ORDER BY company_master_id
        """
        return sql_to_df(query, params=(clean_ticker, clean_ticker))

    frames: list[pd.DataFrame] = []
    if "NSE" in exchanges_upper:
        frames.append(
            sql_to_df(
                f"SELECT * FROM {COMPANY_MASTER_TABLE} WHERE nse_ticker = %s ORDER BY company_master_id",
                params=(clean_ticker,),
            )
        )
    if "BSE" in exchanges_upper:
        frames.append(
            sql_to_df(
                f"SELECT * FROM {COMPANY_MASTER_TABLE} WHERE bse_ticker = %s ORDER BY company_master_id",
                params=(clean_ticker,),
            )
        )
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates(subset=["company_master_id"], keep="last")


def _build_company_master_id(row: pd.Series) -> str:
    sharpely_id = _clean_scalar(row.get("sharpely_id"))
    nse_ticker = _clean_scalar(row.get("nse_ticker"))
    bse_ticker = _clean_scalar(row.get("bse_ticker"))
    if sharpely_id:
        return f"sharpely:{sharpely_id}"
    if nse_ticker:
        return f"nse:{nse_ticker}"
    if bse_ticker:
        return f"bse:{bse_ticker}"
    raise ValueError("Cannot build company_master_id without a ticker or sharpely_id")


def _clean_text_series(series: pd.Series) -> pd.Series:
    cleaned = series.astype("string").str.strip()
    return cleaned.mask(cleaned.eq("")).astype("string")


def _clean_scalar(value: object) -> Optional[str]:
    if pd.isna(value):
        return None
    text = str(value).strip()
    return text or None
