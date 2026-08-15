import pandas as pd

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
    df["is_active_recent"] = df["last_trade_date"] >= (pd.Timestamp.utcnow() - pd.Timedelta(days=31))
    df = df.sort_values(["security_id", "last_trade_date"]).drop_duplicates(subset=["security_id"], keep="last")
    return df


def run():
    df = build_dim_security()
    if df.empty:
        return
    upsert_to_db(df, "dim_security", unique_keys=["security_id"])


if __name__ == "__main__":
    run()
