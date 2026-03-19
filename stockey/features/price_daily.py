import numpy as np
import pandas as pd

from utils.db import sql_to_df, upsert_to_db
from .tutils import get_max_date


ROLLING_LOOKBACK_DAYS = 80


def load_price_base(start_date: pd.Timestamp | None = None) -> pd.DataFrame:
    params = ()
    where_clause = ""
    if start_date is not None:
        where_clause = "WHERE date >= %s"
        params = (start_date,)

    return sql_to_df(
        f"""
        SELECT
            date,
            security_id,
            isin,
            symbol,
            series,
            close,
            volume,
            total_value,
            adj_close,
            tr_adj_close,
            cum_price_adjustment_factor,
            cum_total_return_factor
        FROM nseindia_ohlcv_adjusted
        {where_clause}
        ORDER BY symbol, series, date
        """,
        params=params,
    )


def attach_calendar(df: pd.DataFrame) -> pd.DataFrame:
    cal = sql_to_df(
        """
        SELECT date, is_month_end, is_quarter_end, is_year_end, week_number
        FROM dim_trading_days
        """
    )
    cal["date"] = pd.to_datetime(cal["date"], utc=True)
    return df.merge(cal, on="date", how="left")


def attach_macro(df: pd.DataFrame) -> pd.DataFrame:
    macro = sql_to_df(
        """
        SELECT date, ust10y_yield, fedfunds_eff, vix_close, broad_usd_index, wti_crude_spot, inr_usd_spot
        FROM macro_usa
        ORDER BY date
        """
    )
    rates = sql_to_df(
        """
        SELECT date, repo_rate, reverse_repo_rate, bank_rate, sdf_rate, msf_rate
        FROM rbi_bank_rates
        ORDER BY date
        """
    )
    macro["date"] = pd.to_datetime(macro["date"], utc=True)
    rates["date"] = pd.to_datetime(rates["date"], utc=True)

    dates = pd.DataFrame({"date": df["date"].drop_duplicates().sort_values()})
    dates = pd.merge_asof(dates, macro.sort_values("date"), on="date", direction="backward")
    dates = pd.merge_asof(dates, rates.sort_values("date"), on="date", direction="backward")
    return df.merge(dates, on="date", how="left")


def attach_action_recency(df: pd.DataFrame) -> pd.DataFrame:
    actions = sql_to_df(
        """
        SELECT date, security_id, symbol, series, action_type
        FROM nseindia_corporate_actions_normalized
        WHERE action_type IN ('split', 'bonus', 'dividend')
        ORDER BY security_id, symbol, series, date
        """
    )
    if actions.empty:
        df["days_since_split_or_bonus"] = np.nan
        df["days_since_dividend"] = np.nan
        return df

    actions["date"] = pd.to_datetime(actions["date"], utc=True)
    outputs = []
    for security_id, group in df.groupby("security_id", dropna=False):
        action_group = actions[actions["security_id"] == security_id].copy()
        group = group.sort_values("date").copy()
        if action_group.empty:
            group["days_since_split_or_bonus"] = np.nan
            group["days_since_dividend"] = np.nan
            outputs.append(group)
            continue

        split_bonus = action_group[action_group["action_type"].isin(["split", "bonus"])][["date"]].rename(columns={"date": "split_bonus_date"})
        dividend = action_group[action_group["action_type"] == "dividend"][["date"]].rename(columns={"date": "dividend_date"})

        if not split_bonus.empty:
            group = pd.merge_asof(
                group.sort_values("date"),
                split_bonus.sort_values("split_bonus_date"),
                left_on="date",
                right_on="split_bonus_date",
                direction="backward",
            )
            group["days_since_split_or_bonus"] = (group["date"] - group["split_bonus_date"]).dt.days
            group = group.drop(columns=["split_bonus_date"])
        else:
            group["days_since_split_or_bonus"] = np.nan

        if not dividend.empty:
            group = pd.merge_asof(
                group.sort_values("date"),
                dividend.sort_values("dividend_date"),
                left_on="date",
                right_on="dividend_date",
                direction="backward",
            )
            group["days_since_dividend"] = (group["date"] - group["dividend_date"]).dt.days
            group = group.drop(columns=["dividend_date"])
        else:
            group["days_since_dividend"] = np.nan

        outputs.append(group)

    return pd.concat(outputs, ignore_index=True)


def compute_price_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(["security_id", "date"]).copy()

    def per_group(group: pd.DataFrame) -> pd.DataFrame:
        group = group.sort_values("date").copy()
        group["ret_1d"] = group["adj_close"].pct_change(1)
        group["ret_5d"] = group["adj_close"].pct_change(5)
        group["ret_20d"] = group["adj_close"].pct_change(20)
        group["tr_ret_1d"] = group["tr_adj_close"].pct_change(1)
        group["tr_ret_5d"] = group["tr_adj_close"].pct_change(5)
        group["tr_ret_20d"] = group["tr_adj_close"].pct_change(20)
        group["log_ret_1d"] = np.log(group["adj_close"] / group["adj_close"].shift(1))
        group["rolling_vol_20d"] = group["log_ret_1d"].rolling(20).std()
        group["avg_traded_value_20d"] = group["total_value"].rolling(20).mean()
        traded_value_std = group["total_value"].rolling(20).std()
        group["traded_value_z20"] = (group["total_value"] - group["avg_traded_value_20d"]) / traded_value_std.replace(0, np.nan)
        group["close_to_tr_close_ratio"] = group["adj_close"] / group["tr_adj_close"]
        return group

    outputs = [per_group(group) for _, group in df.groupby("security_id", dropna=False)]
    return pd.concat(outputs, ignore_index=True) if outputs else df.iloc[0:0].copy()


def build_price_daily_features() -> pd.DataFrame:
    max_date = get_max_date("features_price_daily", "date")
    start_date = None
    if max_date is not None:
        start_date = (max_date - pd.Timedelta(days=ROLLING_LOOKBACK_DAYS)).tz_convert("UTC")

    df = load_price_base(start_date)
    if df.empty:
        return df

    df["date"] = pd.to_datetime(df["date"], utc=True)
    df = attach_calendar(df)
    df = attach_macro(df)
    df = attach_action_recency(df)
    df = compute_price_features(df)
    if max_date is not None:
        df = df[df["date"] > max_date]
    return df.reset_index(drop=True)


def run():
    df = build_price_daily_features()
    if df.empty:
        return
    upsert_to_db(df, "features_price_daily", unique_keys=["date", "security_id", "symbol", "series"], timescaledb_column="date")


if __name__ == "__main__":
    run()
