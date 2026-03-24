from __future__ import annotations

import argparse
import json
from datetime import date as date_cls

import pandas as pd
from pandas.tseries.offsets import MonthBegin, MonthEnd

from features.tutils import get_max_date
from utils.db import sql_to_df, upsert_to_db
from utils.sync import parse_datetime_arg


TABLE_NAME = "advisory_macro_daily"

WPI_SERIES_MAP = {
    "(A).  FOOD ARTICLES": "wpi_food_articles",
    "(B).  NON-FOOD ARTICLES": "wpi_non_food_articles",
    "(C).  MINERALS": "wpi_minerals",
    "(D). CRUDE PETROLEUM & NATURAL GAS": "wpi_crude_petroleum_gas",
    "(A). COAL": "wpi_coal",
    "(B). MINERAL OILS": "wpi_mineral_oils",
    "(C). ELECTRICITY": "wpi_electricity",
    "(A). MANUFACTURE OF FOOD PRODUCTS": "wpi_food_products",
    "(J). MANUFACTURE OF CHEMICALS AND CHEMICAL PRODUCTS": "wpi_chemicals",
    "(K). MANUFACTURE OF PHARMACEUTICALS, MEDICINAL CHEMICAL AND BOTANICAL PRODUCTS": "wpi_pharma",
}


def normalize_timestamp(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce").dt.normalize()


def load_target_dates(
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    *,
    rebuild: bool = False,
) -> pd.DataFrame:
    effective_to_date = to_date or pd.Timestamp.now(tz="UTC").normalize()
    if rebuild:
        lower_bound = from_date
    else:
        max_date = get_max_date(TABLE_NAME, "asof_date")
        lower_bound = from_date or (
            max_date + pd.Timedelta(days=1) if max_date is not None else None
        )

    clauses = []
    params: list[object] = []
    if lower_bound is not None:
        clauses.append("date >= %s")
        params.append(lower_bound)
    if to_date is not None:
        clauses.append("date <= %s")
        params.append(effective_to_date)
    elif effective_to_date is not None:
        clauses.append("date <= %s")
        params.append(effective_to_date)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    dates = sql_to_df(
        f"""
        SELECT date
        FROM dim_trading_days
        {where_sql}
        ORDER BY date
        """,
        params=tuple(params) if params else None,
    )
    if dates.empty:
        return dates
    dates["asof_date"] = normalize_timestamp(dates["date"])
    return dates[["asof_date"]].drop_duplicates().sort_values("asof_date").reset_index(drop=True)


def merge_asof_source(base: pd.DataFrame, source: pd.DataFrame, source_prefix: str) -> pd.DataFrame:
    if source.empty:
        return base
    merged = pd.merge_asof(
        base.sort_values("asof_date"),
        source.sort_values("release_date"),
        left_on="asof_date",
        right_on="release_date",
        direction="backward",
        allow_exact_matches=True,
    )
    merged = merged.rename(columns={"release_date": f"{source_prefix}_release_date"})
    return merged


def load_macro_usa() -> pd.DataFrame:
    df = sql_to_df(
        """
        SELECT
            date,
            ust10y_yield,
            fedfunds_eff,
            vix_close,
            broad_usd_index,
            wti_crude_spot,
            inr_usd_spot
        FROM macro_usa
        ORDER BY date
        """
    )
    if df.empty:
        return df
    df["release_date"] = normalize_timestamp(df["date"])
    return df.drop(columns=["date"])


def load_bank_rates() -> pd.DataFrame:
    df = sql_to_df(
        """
        SELECT
            date,
            bank_rate,
            repo_rate,
            reverse_repo_rate,
            sdf_rate,
            msf_rate,
            crr,
            slr
        FROM rbi_bank_rates
        ORDER BY date
        """
    )
    if df.empty:
        return df
    df["release_date"] = normalize_timestamp(df["date"])
    return df.drop(columns=["date"])


def load_cpi() -> pd.DataFrame:
    df = sql_to_df(
        """
        SELECT
            reported_on,
            cpi_for_month,
            description,
            combined
        FROM mospi_cpi
        WHERE state = 'ALL India'
          AND description IN ('General Index (All Groups)', 'Consumer Food Price Index')
        ORDER BY reported_on, cpi_for_month
        """
    )
    if df.empty:
        return df
    df["release_date"] = normalize_timestamp(df["reported_on"])
    df["cpi_for_month"] = normalize_timestamp(df["cpi_for_month"])
    wide = (
        df.pivot_table(
            index=["release_date", "cpi_for_month"],
            columns="description",
            values="combined",
            aggfunc="last",
        )
        .reset_index()
        .rename(
            columns={
                "General Index (All Groups)": "india_cpi_combined",
                "Consumer Food Price Index": "india_cfpi_combined",
            }
        )
    )
    return wide


def load_wpi() -> pd.DataFrame:
    names = "', '".join(name.replace("'", "''") for name in WPI_SERIES_MAP)
    df = sql_to_df(
        f"""
        SELECT date, name, value
        FROM eaindustry_wpi
        WHERE name IN ('{names}')
        ORDER BY date, name
        """
    )
    if df.empty:
        return df
    df["period_date"] = normalize_timestamp(df["date"])
    df["release_date"] = (
        df["period_date"]
        + MonthEnd(0)
        + MonthBegin(1)
        + pd.offsets.Day(13)
    ).dt.normalize()
    wide = (
        df.pivot_table(
            index=["release_date", "period_date"],
            columns="name",
            values="value",
            aggfunc="last",
        )
        .reset_index()
        .rename(columns=WPI_SERIES_MAP)
    )
    return wide


def load_gsec_curve() -> pd.DataFrame:
    df = sql_to_df(
        """
        SELECT date, tenor_years, par_yield_sa
        FROM fbil_gsec_par
        WHERE tenor_years IN (2.0, 5.0, 10.0)
        ORDER BY date, tenor_years
        """
    )
    if df.empty:
        return df
    df["release_date"] = normalize_timestamp(df["date"])
    wide = (
        df.pivot_table(
            index=["release_date"],
            columns="tenor_years",
            values="par_yield_sa",
            aggfunc="last",
        )
        .reset_index()
        .rename(
            columns={
                2.0: "gsec_2y_yield",
                5.0: "gsec_5y_yield",
                10.0: "gsec_10y_yield",
            }
        )
    )
    return wide


def add_freshness_columns(df: pd.DataFrame) -> pd.DataFrame:
    freshness_specs = {
        "macro_usa": 7,
        "bank_rates": 45,
        "cpi": 45,
        "wpi": 45,
        "gsec_curve": 7,
    }
    for prefix, stale_after_days in freshness_specs.items():
        release_col = f"{prefix}_release_date"
        age_col = f"{prefix}_age_days"
        status_col = f"{prefix}_freshness_status"
        if release_col not in df.columns:
            df[age_col] = pd.NA
            df[status_col] = "MISSING"
            continue
        age_days = (df["asof_date"] - df[release_col]).dt.days
        age_days = age_days.where(df[release_col].notna())
        df[age_col] = age_days
        df[status_col] = pd.Series("MISSING", index=df.index, dtype="string")
        df.loc[df[release_col].notna() & (age_days <= stale_after_days), status_col] = "FRESH"
        df.loc[df[release_col].notna() & (age_days > stale_after_days), status_col] = "STALE"
    return df


def build_macro_snapshot(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    rebuild: bool = False,
) -> pd.DataFrame:
    dates = load_target_dates(from_date=from_date, to_date=to_date, rebuild=rebuild)
    if dates.empty:
        return dates

    out = dates.copy()
    out = merge_asof_source(out, load_macro_usa(), "macro_usa")
    out = merge_asof_source(out, load_bank_rates(), "bank_rates")
    out = merge_asof_source(out, load_cpi(), "cpi")
    out = merge_asof_source(out, load_wpi(), "wpi")
    out = merge_asof_source(out, load_gsec_curve(), "gsec_curve")
    out = add_freshness_columns(out)
    out["new_macro_source_required"] = "sector-linked commodity prices"
    out["load_ts"] = pd.Timestamp.utcnow()
    out = out.drop_duplicates(subset=["asof_date"], keep="last")
    return out.reset_index(drop=True)


def persist_macro_snapshot(df: pd.DataFrame) -> None:
    if df.empty:
        return
    upsert_to_db(
        df,
        TABLE_NAME,
        unique_keys=["asof_date"],
        timescaledb_column="asof_date",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a daily point-in-time advisory macro snapshot."
    )
    parser.add_argument("--from-date", type=parse_datetime_arg)
    parser.add_argument("--to-date", type=parse_datetime_arg)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def summarize(df: pd.DataFrame) -> dict[str, object]:
    if df.empty:
        return {
            "status": "ok",
            "table": TABLE_NAME,
            "row_count": 0,
            "date_min": None,
            "date_max": None,
            "sample": [],
        }
    preview_cols = [
        "asof_date",
        "ust10y_yield",
        "vix_close",
        "inr_usd_spot",
        "repo_rate",
        "india_cpi_combined",
        "wpi_crude_petroleum_gas",
        "gsec_10y_yield",
        "macro_usa_freshness_status",
        "cpi_freshness_status",
        "wpi_freshness_status",
        "gsec_curve_freshness_status",
    ]
    existing_preview_cols = [col for col in preview_cols if col in df.columns]
    return {
        "status": "ok",
        "table": TABLE_NAME,
        "row_count": int(len(df)),
        "date_min": df["asof_date"].min().date().isoformat(),
        "date_max": df["asof_date"].max().date().isoformat(),
        "sample": df[existing_preview_cols].head(5).to_dict(orient="records"),
    }


def main() -> int:
    args = parse_args()
    df = build_macro_snapshot(
        from_date=pd.Timestamp(args.from_date, tz="UTC") if args.from_date else None,
        to_date=pd.Timestamp(args.to_date, tz="UTC") if args.to_date else None,
        rebuild=args.rebuild,
    )
    if not args.dry_run:
        persist_macro_snapshot(df)
    result = summarize(df)
    result["dry_run"] = bool(args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
