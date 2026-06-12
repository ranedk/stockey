from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event
from features.tutils import get_max_date
from utils.db import sql_to_df, upsert_to_db
from utils.sync import parse_datetime_arg


TABLE_NAME = "advisory_macro_features_daily"
SOURCE_TABLE = "advisory_macro_daily"


def _record_macro_features_fallback(
    *,
    fallback_type: str,
    source: str,
    reason: str,
    error: Exception,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.macro_features",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def normalize_timestamp(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce").dt.normalize()


def table_exists(table_name: str) -> bool:
    schema_name, base_table_name = (
        table_name.split(".", 1) if "." in table_name else ("public", table_name)
    )
    try:
        df = sql_to_df(
            """
            SELECT 1 AS exists_flag
            FROM information_schema.tables
            WHERE table_schema = %s
              AND table_name = %s
            LIMIT 1
            """,
            params=(schema_name, base_table_name),
        )
    except Exception as exc:
        _record_macro_features_fallback(
            fallback_type="macro_features_table_lookup_failed",
            source=table_name,
            reason="Macro feature builder could not inspect whether a source/output table exists.",
            error=exc,
            metadata={"table_name": table_name},
        )
        return False
    return not df.empty


def _as_utc_timestamp(value: pd.Timestamp | None) -> pd.Timestamp | None:
    if value is None:
        return None
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC").normalize()
    return ts.tz_convert("UTC").normalize()


def _numeric(frame: pd.DataFrame, col: str) -> pd.Series:
    if col not in frame.columns:
        return pd.Series(pd.NA, index=frame.index, dtype="Float64")
    return pd.to_numeric(frame[col], errors="coerce")


def _pct_change(frame: pd.DataFrame, col: str, periods: int) -> pd.Series:
    return _numeric(frame, col).pct_change(periods, fill_method=None)


def _diff_bps(frame: pd.DataFrame, col: str, periods: int) -> pd.Series:
    return _numeric(frame, col).diff(periods) * 100.0


def _diff_level(frame: pd.DataFrame, col: str, periods: int) -> pd.Series:
    return _numeric(frame, col).diff(periods)


def _risk_state(score: float | None) -> str:
    if pd.isna(score):
        return "UNKNOWN"
    if float(score) >= 0.65:
        return "STRESS"
    if float(score) >= 0.40:
        return "ELEVATED"
    if float(score) >= 0.20:
        return "WATCH"
    return "NORMAL"


def _sizing_multiplier(state: str) -> float:
    return {
        "NORMAL": 1.00,
        "WATCH": 0.90,
        "ELEVATED": 0.75,
        "STRESS": 0.55,
    }.get(str(state), 1.00)


def load_macro_daily(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    rebuild: bool = False,
    lookback_days: int = 140,
) -> pd.DataFrame:
    if not table_exists(SOURCE_TABLE):
        return pd.DataFrame()

    effective_from = _as_utc_timestamp(from_date)
    effective_to = _as_utc_timestamp(to_date)
    if not rebuild and from_date is None and table_exists(TABLE_NAME):
        max_date = get_max_date(TABLE_NAME, "asof_date")
        if max_date is not None:
            next_date = _as_utc_timestamp(pd.Timestamp(max_date) + pd.Timedelta(days=1))
            effective_from = max(effective_from, next_date) if effective_from is not None else next_date

    source_from = effective_from - pd.Timedelta(days=int(lookback_days)) if effective_from is not None else None
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if source_from is not None:
        clauses.append("asof_date >= %(from_date)s")
        params["from_date"] = source_from
    if effective_to is not None:
        clauses.append("asof_date <= %(to_date)s")
        params["to_date"] = effective_to
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        df = sql_to_df(
            f"""
            SELECT *
            FROM {SOURCE_TABLE}
            {where_sql}
            ORDER BY asof_date
            """,
            params=params or None,
        )
    except Exception as exc:
        _record_macro_features_fallback(
            fallback_type="macro_features_source_load_failed",
            source=SOURCE_TABLE,
            reason="Macro feature builder could not load point-in-time macro source rows.",
            error=exc,
            metadata={
                "from_date": str(source_from) if source_from is not None else None,
                "to_date": str(effective_to) if effective_to is not None else None,
                "target_from": str(effective_from) if effective_from is not None else None,
                "target_to": str(effective_to) if effective_to is not None else None,
                "rebuild": bool(rebuild),
                "lookback_days": int(lookback_days),
            },
        )
        raise
    if df.empty:
        return df
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    out = df.sort_values("asof_date").drop_duplicates(subset=["asof_date"], keep="last").reset_index(drop=True)
    out.attrs["target_from"] = effective_from
    out.attrs["target_to"] = effective_to
    return out


def compute_macro_feature_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    out = df.copy()
    out["asof_date"] = normalize_timestamp(out["asof_date"])
    out = out.sort_values("asof_date").drop_duplicates(subset=["asof_date"], keep="last").reset_index(drop=True)

    level_cols = [
        "vix_close",
        "ust10y_yield",
        "fedfunds_eff",
        "broad_usd_index",
        "wti_crude_spot",
        "inr_usd_spot",
        "repo_rate",
        "bank_rate",
        "reverse_repo_rate",
        "sdf_rate",
        "msf_rate",
        "crr",
        "slr",
        "india_cpi_combined",
        "india_cfpi_combined",
        "wpi_food_articles",
        "wpi_non_food_articles",
        "wpi_minerals",
        "wpi_crude_petroleum_gas",
        "wpi_coal",
        "wpi_mineral_oils",
        "wpi_electricity",
        "wpi_food_products",
        "wpi_chemicals",
        "wpi_pharma",
        "gsec_2y_yield",
        "gsec_5y_yield",
        "gsec_10y_yield",
    ]
    for col in level_cols:
        out[col] = _numeric(out, col)

    out["vix_change_20d"] = _diff_level(out, "vix_close", 20)
    out["ust10y_change_20d_bps"] = _diff_bps(out, "ust10y_yield", 20)
    out["gsec_10y_change_20d_bps"] = _diff_bps(out, "gsec_10y_yield", 20)
    out["gsec_10y_change_60d_bps"] = _diff_bps(out, "gsec_10y_yield", 60)
    out["repo_rate_change_90d_bps"] = _diff_bps(out, "repo_rate", 90)
    out["gsec_curve_10y_2y_bps"] = (out["gsec_10y_yield"] - out["gsec_2y_yield"]) * 100.0
    out["broad_usd_ret_20d"] = _pct_change(out, "broad_usd_index", 20)
    out["wti_ret_20d"] = _pct_change(out, "wti_crude_spot", 20)
    out["inr_usd_ret_20d"] = _pct_change(out, "inr_usd_spot", 20)
    out["india_cpi_change_60d"] = _diff_level(out, "india_cpi_combined", 60)
    out["india_cfpi_change_60d"] = _diff_level(out, "india_cfpi_combined", 60)
    out["wpi_food_articles_change_60d"] = _diff_level(out, "wpi_food_articles", 60)
    out["wpi_crude_petroleum_gas_change_60d"] = _diff_level(out, "wpi_crude_petroleum_gas", 60)
    out["wpi_chemicals_change_60d"] = _diff_level(out, "wpi_chemicals", 60)
    out["wpi_pharma_change_60d"] = _diff_level(out, "wpi_pharma", 60)

    score = pd.Series(0.0, index=out.index, dtype="float64")
    vix = out["vix_close"]
    score += (vix >= 24.0).fillna(False).astype(float) * 0.15
    score += (vix >= 30.0).fillna(False).astype(float) * 0.20
    score += (out["gsec_10y_change_20d_bps"] >= 25.0).fillna(False).astype(float) * 0.15
    score += (out["ust10y_change_20d_bps"] >= 25.0).fillna(False).astype(float) * 0.10
    score += (out["wti_ret_20d"] >= 0.12).fillna(False).astype(float) * 0.15
    score += (out["broad_usd_ret_20d"] >= 0.03).fillna(False).astype(float) * 0.10
    score += (out["inr_usd_ret_20d"] >= 0.025).fillna(False).astype(float) * 0.10
    score += (out["india_cfpi_change_60d"] >= 2.0).fillna(False).astype(float) * 0.05
    score += (out["wpi_crude_petroleum_gas_change_60d"] >= 5.0).fillna(False).astype(float) * 0.05
    out["macro_stress_score"] = score.clip(lower=0.0, upper=1.0)
    out["macro_risk_state"] = out["macro_stress_score"].map(_risk_state)
    out["macro_sizing_multiplier"] = out["macro_risk_state"].map(_sizing_multiplier)

    freshness_cols = [col for col in out.columns if col.endswith("_freshness_status")]
    if freshness_cols:
        out["macro_missing_source_count"] = out[freshness_cols].eq("MISSING").sum(axis=1)
        out["macro_stale_source_count"] = out[freshness_cols].eq("STALE").sum(axis=1)
    else:
        out["macro_missing_source_count"] = 0
        out["macro_stale_source_count"] = 0

    out["load_ts"] = pd.Timestamp.utcnow()
    return out


def build_macro_features(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    rebuild: bool = False,
) -> pd.DataFrame:
    macro = load_macro_daily(from_date=from_date, to_date=to_date, rebuild=rebuild)
    if macro.empty:
        return macro
    target_from = macro.attrs.get("target_from")
    target_to = macro.attrs.get("target_to")
    features = compute_macro_feature_columns(macro)
    if target_from is not None:
        features = features[features["asof_date"] >= target_from]
    if target_to is not None:
        features = features[features["asof_date"] <= target_to]
    return features.reset_index(drop=True)


def persist_macro_features(df: pd.DataFrame) -> None:
    if df.empty:
        return
    upsert_to_db(
        df,
        TABLE_NAME,
        unique_keys=["asof_date"],
        timescaledb_column="asof_date",
    )


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
        "macro_stress_score",
        "macro_risk_state",
        "macro_sizing_multiplier",
        "vix_close",
        "gsec_10y_change_20d_bps",
        "wti_ret_20d",
        "inr_usd_ret_20d",
        "india_cfpi_change_60d",
    ]
    existing_preview_cols = [col for col in preview_cols if col in df.columns]
    return {
        "status": "ok",
        "table": TABLE_NAME,
        "row_count": int(len(df)),
        "date_min": df["asof_date"].min().date().isoformat(),
        "date_max": df["asof_date"].max().date().isoformat(),
        "risk_state_counts": {
            str(key): int(value)
            for key, value in df["macro_risk_state"].value_counts(dropna=False).to_dict().items()
        },
        "sample": df[existing_preview_cols].tail(5).to_dict(orient="records"),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build point-in-time macro features for advisory models and regime logic.")
    parser.add_argument("--from-date", type=parse_datetime_arg)
    parser.add_argument("--to-date", type=parse_datetime_arg)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    df = build_macro_features(
        from_date=pd.Timestamp(args.from_date, tz="UTC") if args.from_date else None,
        to_date=pd.Timestamp(args.to_date, tz="UTC") if args.to_date else None,
        rebuild=bool(args.rebuild),
    )
    if not args.dry_run:
        persist_macro_features(df)
    result = summarize(df)
    result["dry_run"] = bool(args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
