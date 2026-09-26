import pandas as pd

from utils.company_master import map_company_master_ids
from utils.db import sql_to_df, upsert_to_db


def build_dim_security() -> pd.DataFrame:
    history = sql_to_df(
        """
        SELECT
            security_id,
            raw_security_key,
            symbol,
            series,
            isin,
            effective_from,
            effective_to,
            price_row_count,
            mapping_source,
            confidence,
            relation_type
        FROM dim_security_history
        """
    )
    if history.empty:
        return history
    history["effective_from"] = pd.to_datetime(history["effective_from"], utc=True)
    history["effective_to"] = pd.to_datetime(history["effective_to"], utc=True)

    dhan = sql_to_df(
        """
        SELECT DISTINCT
            underlying_symbol AS symbol,
            series,
            exch_id,
            display_name,
            instrument,
            instrument_type,
            tick_size,
            lot_size
        FROM master_dhan_instruments
        WHERE valid_to IS NULL
          AND exch_id = 'NSE'
        """
    )
    company_master = sql_to_df(
        """
        SELECT
            company_master_id,
            nse_ticker AS symbol,
            cm.bse_ticker,
            company_name,
            sector_code
        FROM company_master cm
        LEFT JOIN master_sharpely_equity mse
               ON mse.symbol = cm.nse_ticker
              AND (
                    (mse.bse_ticker = cm.bse_ticker)
                    OR (mse.bse_ticker IS NULL AND cm.bse_ticker IS NULL)
                  )
        """
    )

    latest = history.sort_values(["security_id", "effective_to"]).drop_duplicates(
        subset=["security_id"],
        keep="last",
    )
    # `latest`'s own price_row_count (just one history row's value) is dropped before merging --
    # `summary`'s is the real SUM across the security's full identity history, which is what
    # "price_row_count" is meant to mean on the consolidated dim_security row. Both columns
    # otherwise share the same name and the merge below would silently rename them to
    # price_row_count_x/_y instead of erroring -- found live 2026-08-15: dim_security has exactly
    # that _x/_y split today, and no query anywhere selects either suffixed name.
    latest = latest.drop(columns=["price_row_count"])
    summary = history.groupby("security_id", dropna=False).agg(
        first_trade_date=("effective_from", "min"),
        last_trade_date=("effective_to", "max"),
        price_row_count=("price_row_count", "sum"),
    ).reset_index()

    dhan = dhan.sort_values(["symbol", "series"]).drop_duplicates(subset=["symbol", "series"], keep="last")
    company_master = company_master.sort_values(["symbol"]).drop_duplicates(subset=["symbol"], keep="last")
    df = latest.merge(summary, on="security_id", how="left")
    df = df.merge(dhan, on=["symbol", "series"], how="left")
    df = df.merge(company_master, on="symbol", how="left")
    # NSE renames (TATAMOTORS -> TMPV) leave the new symbol matching no company_master
    # row by ticker; map_company_master_ids falls back to the ISIN-derived alias table, so
    # the dimension carries the existing company id -- and its sector -- for the renamed
    # symbol instead of NULL (2026-09-24: 52 active EQ symbols).
    missing = df["company_master_id"].isna() & df["series"].isin(["EQ", "BE", "SM", "ST"])
    if missing.any():
        df.loc[missing, "company_master_id"] = map_company_master_ids(df.loc[missing, "symbol"], exchange="NSE")
        by_id = company_master.drop_duplicates(subset=["company_master_id"]).set_index("company_master_id")
        for column in ("company_name", "sector_code", "bse_ticker"):
            if column in df.columns and column in by_id.columns:
                fill = df.loc[missing, "company_master_id"].map(by_id[column])
                df.loc[missing, column] = df.loc[missing, column].fillna(fill)
    # Sector by ISIN where the symbol join found none: Sharpely's symbol can differ from
    # NSE's (renames, conventions) while the ISIN agrees -- 123 of 482 active EQ names
    # had a Sharpely sector reachable only this way (2026-09-24). ISIN was not stored
    # from the Sharpely feed before that date.
    if "sector_code" in df.columns and "isin" in df.columns and df["sector_code"].isna().any():
        try:
            by_isin = sql_to_df(
                "SELECT DISTINCT ON (isin) isin, sector_code FROM master_sharpely_equity "
                " WHERE isin IS NOT NULL AND sector_code IS NOT NULL ORDER BY isin"
            )
        except Exception:  # noqa: BLE001 -- pre-2026-09-24 table has no isin column
            by_isin = pd.DataFrame(columns=["isin", "sector_code"])
        if not by_isin.empty:
            no_sector = df["sector_code"].isna()
            df.loc[no_sector, "sector_code"] = df.loc[no_sector, "isin"].map(by_isin.set_index("isin")["sector_code"])
    df["is_active_recent"] = df["last_trade_date"] >= (pd.Timestamp.utcnow() - pd.Timedelta(days=31))
    df = df.sort_values(["security_id", "last_trade_date"]).drop_duplicates(subset=["security_id"], keep="last")
    return df


def run():
    df = build_dim_security()
    if df.empty:
        return
    upsert_to_db(df, "dim_security", unique_keys=["security_id"])


STOCKEY_RUN_STATE: dict[str, object] = {}


def main() -> int:
    global STOCKEY_RUN_STATE
    run()
    counts = sql_to_df("SELECT count(*) AS n, max(effective_to) AS latest FROM dim_security")
    STOCKEY_RUN_STATE = {
        "source": "data.nseindia.security_dimension",
        "rows": int(counts.iloc[0]["n"]),
        "latest_observed": str(counts.iloc[0]["latest"]),
        "state_advanced": True,
        "status": "ok",
    }
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
