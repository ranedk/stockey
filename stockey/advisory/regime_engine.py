from __future__ import annotations

import argparse
import json
from typing import Any

import numpy as np
import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


TABLE_NAME = "advisory_market_regime"
DEFAULT_BENCHMARK_NAME = "NIFTY"
MACRO_FEATURES_TABLE = "advisory_macro_features_daily"
REGIME_SCHEMA_MIGRATION_ID = "20260611_advisory_market_regime_base"
REGIME_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        asof_date TIMESTAMPTZ NOT NULL,
        benchmark_name TEXT,
        benchmark_close DOUBLE PRECISION,
        benchmark_ret_20d DOUBLE PRECISION,
        benchmark_ret_60d DOUBLE PRECISION,
        benchmark_dma_50 DOUBLE PRECISION,
        benchmark_dma_200 DOUBLE PRECISION,
        benchmark_realized_vol_20d DOUBLE PRECISION,
        benchmark_drawdown_60d DOUBLE PRECISION,
        vix_close DOUBLE PRECISION,
        broad_usd_index_ret_20d DOUBLE PRECISION,
        wti_crude_spot_ret_20d DOUBLE PRECISION,
        inr_usd_spot_ret_20d DOUBLE PRECISION,
        gsec_10y_change_20d_bps DOUBLE PRECISION,
        macro_stress_score DOUBLE PRECISION,
        macro_risk_state TEXT,
        macro_sizing_multiplier DOUBLE PRECISION,
        repo_rate DOUBLE PRECISION,
        macro_usa_freshness_status TEXT,
        bank_rates_freshness_status TEXT,
        cpi_freshness_status TEXT,
        wpi_freshness_status TEXT,
        gsec_curve_freshness_status TEXT,
        new_macro_source_required BOOLEAN,
        tariff_pressure_flag BOOLEAN,
        shock_flag BOOLEAN,
        risk_off_flag BOOLEAN,
        regime_name TEXT,
        regime_notes TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (asof_date)
    )
    """,
]


def _record_regime_fallback(
    *,
    fallback_type: str,
    source: str,
    reason: str,
    error: Exception,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.regime_engine",
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
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = %s AND table_name = %s
            ) AS exists
            """,
            params=(schema_name, base_table_name),
        )
    except Exception as exc:
        _record_regime_fallback(
            fallback_type="regime_engine_table_lookup_failed",
            source=table_name,
            reason="Regime engine could not inspect whether a source table exists.",
            error=exc,
            metadata={"table_name": table_name},
        )
        return False
    if df.empty:
        return False
    return bool(df.iloc[0]["exists"])


def ensure_regime_table() -> None:
    apply_schema_migration(
        migration_id=REGIME_SCHEMA_MIGRATION_ID,
        statements=REGIME_SCHEMA_STATEMENTS,
        owner="advisory.regime_engine",
        description="Create advisory market regime snapshot table.",
        metadata={"tables": [TABLE_NAME], "workflow": "market_regime"},
    )


def load_benchmark_history(
    *,
    benchmark_name: str = DEFAULT_BENCHMARK_NAME,
    start_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
) -> pd.DataFrame:
    if benchmark_name.upper() == "NIFTY":
        clauses = ["index_name = %(benchmark_name)s"]
        params: dict[str, object] = {"benchmark_name": "Nifty 50"}
        if start_date is not None:
            clauses.append("date >= %(start_date)s")
            params["start_date"] = start_date
        if to_date is not None:
            clauses.append("date <= %(to_date)s")
            params["to_date"] = to_date
        try:
            nse_df = sql_to_df(
                f"""
                SELECT date, close
                FROM nseindia_indices
                WHERE {' AND '.join(clauses)}
                ORDER BY date
                """,
                params=params,
            )
        except Exception as exc:
            _record_regime_fallback(
                fallback_type="regime_engine_nse_benchmark_load_failed",
                source="nseindia_indices",
                reason="Regime engine could not load the preferred NSE NIFTY benchmark history and will try the Dhan benchmark table.",
                error=exc,
                metadata={
                    "benchmark_name": benchmark_name,
                    "start_date": str(start_date) if start_date is not None else None,
                    "to_date": str(to_date) if to_date is not None else None,
                },
            )
            nse_df = pd.DataFrame()
        if not nse_df.empty:
            df = nse_df
            df["date"] = normalize_timestamp(df["date"])
            df["benchmark_close"] = pd.to_numeric(df["close"], errors="coerce")
            df = df[["date", "benchmark_close"]].sort_values("date").reset_index(drop=True)
            df["benchmark_ret_20d"] = df["benchmark_close"].pct_change(20)
            df["benchmark_ret_60d"] = df["benchmark_close"].pct_change(60)
            df["benchmark_dma_50"] = df["benchmark_close"].rolling(50, min_periods=50).mean()
            df["benchmark_dma_200"] = df["benchmark_close"].rolling(200, min_periods=150).mean()
            returns = df["benchmark_close"].pct_change()
            df["benchmark_realized_vol_20d"] = returns.rolling(20, min_periods=15).std() * np.sqrt(252)
            df["benchmark_drawdown_60d"] = (
                df["benchmark_close"] / df["benchmark_close"].rolling(60, min_periods=20).max() - 1.0
            )
            return df

    clauses = ["ticker = %(benchmark_name)s", "asset_type = 'benchmark'"]
    params = {"benchmark_name": benchmark_name}
    if start_date is not None:
        clauses.append("date >= %(start_date)s")
        params["start_date"] = start_date
    if to_date is not None:
        clauses.append("date <= %(to_date)s")
        params["to_date"] = to_date
    try:
        df = sql_to_df(
            f"""
            SELECT date, close
            FROM dhan_ohlcv_daily
            WHERE {' AND '.join(clauses)}
            ORDER BY date
            """,
            params=params,
        )
    except Exception as exc:
        _record_regime_fallback(
            fallback_type="regime_engine_dhan_benchmark_load_failed",
            source="dhan_ohlcv_daily",
            reason="Regime engine could not load benchmark history from Dhan OHLCV.",
            error=exc,
            metadata={
                "benchmark_name": benchmark_name,
                "start_date": str(start_date) if start_date is not None else None,
                "to_date": str(to_date) if to_date is not None else None,
            },
        )
        raise
    if df.empty:
        return df
    df["date"] = normalize_timestamp(df["date"])
    df["benchmark_close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df[["date", "benchmark_close"]].sort_values("date").reset_index(drop=True)
    df["benchmark_ret_20d"] = df["benchmark_close"].pct_change(20)
    df["benchmark_ret_60d"] = df["benchmark_close"].pct_change(60)
    df["benchmark_dma_50"] = df["benchmark_close"].rolling(50, min_periods=50).mean()
    df["benchmark_dma_200"] = df["benchmark_close"].rolling(200, min_periods=150).mean()
    returns = df["benchmark_close"].pct_change()
    df["benchmark_realized_vol_20d"] = returns.rolling(20, min_periods=15).std() * np.sqrt(252)
    df["benchmark_drawdown_60d"] = (
        df["benchmark_close"] / df["benchmark_close"].rolling(60, min_periods=20).max() - 1.0
    )
    return df


def load_macro_history(
    *,
    start_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
) -> pd.DataFrame:
    clauses = []
    params: dict[str, object] = {}
    if start_date is not None:
        clauses.append("asof_date >= %(start_date)s")
        params["start_date"] = start_date
    if to_date is not None:
        clauses.append("asof_date <= %(to_date)s")
        params["to_date"] = to_date
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    if table_exists(MACRO_FEATURES_TABLE):
        try:
            df = sql_to_df(
                f"""
                SELECT
                    asof_date,
                    vix_close,
                    broad_usd_index,
                    wti_crude_spot,
                    inr_usd_spot,
                    gsec_10y_yield,
                    repo_rate,
                    macro_usa_freshness_status,
                    bank_rates_freshness_status,
                    cpi_freshness_status,
                    wpi_freshness_status,
                    gsec_curve_freshness_status,
                    new_macro_source_required,
                    broad_usd_ret_20d,
                    wti_ret_20d,
                    inr_usd_ret_20d,
                    gsec_10y_change_20d_bps,
                    macro_stress_score,
                    macro_risk_state,
                    macro_sizing_multiplier
                FROM {MACRO_FEATURES_TABLE}
                {where_sql}
                ORDER BY asof_date
                """,
                params=params or None,
            )
        except Exception as exc:
            _record_regime_fallback(
                fallback_type="regime_engine_macro_features_load_failed",
                source=MACRO_FEATURES_TABLE,
                reason="Regime engine could not load macro feature rows for regime classification.",
                error=exc,
                metadata={
                    "start_date": str(start_date) if start_date is not None else None,
                    "to_date": str(to_date) if to_date is not None else None,
                },
            )
            raise
    else:
        try:
            df = sql_to_df(
                f"""
                SELECT
                    asof_date,
                    vix_close,
                    broad_usd_index,
                    wti_crude_spot,
                    inr_usd_spot,
                    gsec_10y_yield,
                    repo_rate,
                    macro_usa_freshness_status,
                    bank_rates_freshness_status,
                    cpi_freshness_status,
                    wpi_freshness_status,
                    gsec_curve_freshness_status,
                    new_macro_source_required
                FROM advisory_macro_daily
                {where_sql}
                ORDER BY asof_date
                """,
                params=params or None,
            )
        except Exception as exc:
            _record_regime_fallback(
                fallback_type="regime_engine_macro_daily_load_failed",
                source="advisory_macro_daily",
                reason="Regime engine could not load raw macro rows for regime classification.",
                error=exc,
                metadata={
                    "start_date": str(start_date) if start_date is not None else None,
                    "to_date": str(to_date) if to_date is not None else None,
                },
            )
            raise
    if df.empty:
        return df
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    numeric_cols = [
        "vix_close",
        "broad_usd_index",
        "wti_crude_spot",
        "inr_usd_spot",
        "gsec_10y_yield",
        "repo_rate",
        "broad_usd_ret_20d",
        "wti_ret_20d",
        "inr_usd_ret_20d",
        "gsec_10y_change_20d_bps",
        "macro_stress_score",
        "macro_sizing_multiplier",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "broad_usd_ret_20d" not in df.columns:
        df["broad_usd_ret_20d"] = pd.NA
    if "wti_ret_20d" not in df.columns:
        df["wti_ret_20d"] = pd.NA
    if "inr_usd_ret_20d" not in df.columns:
        df["inr_usd_ret_20d"] = pd.NA
    for source_col, out_col in [
        ("broad_usd_index", "broad_usd_ret_20d"),
        ("wti_crude_spot", "wti_ret_20d"),
        ("inr_usd_spot", "inr_usd_ret_20d"),
    ]:
        if df[out_col].isna().all():
            df[out_col] = df[source_col].pct_change(20)
        df[f"{source_col}_ret_20d"] = df[out_col]
    if "gsec_10y_change_20d_bps" not in df.columns or df["gsec_10y_change_20d_bps"].isna().all():
        df["gsec_10y_change_20d_bps"] = df["gsec_10y_yield"].diff(20) * 100.0
    if "macro_stress_score" not in df.columns:
        df["macro_stress_score"] = pd.NA
    if "macro_risk_state" not in df.columns:
        df["macro_risk_state"] = pd.NA
    if "macro_sizing_multiplier" not in df.columns:
        df["macro_sizing_multiplier"] = pd.NA
    return df


def classify_regime(row: pd.Series) -> tuple[str, list[str]]:
    notes: list[str] = []
    close = row.get("benchmark_close")
    dma50 = row.get("benchmark_dma_50")
    dma200 = row.get("benchmark_dma_200")
    ret20 = row.get("benchmark_ret_20d")
    ret60 = row.get("benchmark_ret_60d")
    drawdown60 = row.get("benchmark_drawdown_60d")
    vix = row.get("vix_close")
    realized_vol = row.get("benchmark_realized_vol_20d")
    usd_ret20 = row.get("broad_usd_index_ret_20d")
    wti_ret20 = row.get("wti_crude_spot_ret_20d")
    inr_ret20 = row.get("inr_usd_spot_ret_20d")
    gsec_change_20d = row.get("gsec_10y_change_20d_bps")
    macro_stress = row.get("macro_stress_score")

    above_dma200 = pd.notna(close) and pd.notna(dma200) and close >= dma200
    above_dma50 = pd.notna(close) and pd.notna(dma50) and close >= dma50
    bullish_stack = pd.notna(dma50) and pd.notna(dma200) and dma50 >= dma200

    tariff_pressure = bool(
        (pd.notna(usd_ret20) and usd_ret20 >= 0.03)
        or (pd.notna(wti_ret20) and wti_ret20 >= 0.12)
        or (pd.notna(inr_ret20) and inr_ret20 >= 0.025)
        or (pd.notna(gsec_change_20d) and gsec_change_20d >= 25.0)
        or (pd.notna(macro_stress) and macro_stress >= 0.40)
    )
    shock_flag = bool(
        (pd.notna(vix) and vix >= 30.0)
        or (pd.notna(ret20) and ret20 <= -0.10)
        or (pd.notna(drawdown60) and drawdown60 <= -0.15)
        or (pd.notna(macro_stress) and macro_stress >= 0.75)
    )
    risk_off_flag = bool(
        shock_flag
        or (pd.notna(vix) and vix >= 24.0)
        or (not above_dma200 and pd.notna(ret60) and ret60 < -0.03)
        or (pd.notna(drawdown60) and drawdown60 <= -0.10)
        or (pd.notna(macro_stress) and macro_stress >= 0.60)
    )

    if shock_flag:
        notes.append("volatility or drawdown breach")
        if tariff_pressure:
            notes.append("macro shock pressure rising")
        return "SHOCK", notes

    if risk_off_flag:
        notes.append("benchmark below risk tolerance")
        if tariff_pressure:
            notes.append("macro pressure still elevated")
        return "RISK_OFF", notes

    if (
        above_dma200
        and bullish_stack
        and pd.notna(ret20)
        and ret20 >= 0.03
        and pd.notna(vix)
        and vix < 18.0
        and pd.notna(realized_vol)
        and realized_vol < 0.22
    ):
        notes.append("trend and volatility supportive")
        return "BULL_RISK_ON", notes

    if tariff_pressure:
        notes.append("tariff or macro pressure rising")
        if above_dma200:
            notes.append("trend intact despite pressure")
        return "STABLE_BUT_TARIFF_RISING", notes

    if above_dma200 and above_dma50 and pd.notna(vix) and vix < 24.0:
        notes.append("trend positive but not broad risk-on")
        return "BULL_NARROW", notes

    notes.append("mixed trend and macro backdrop")
    return "STABLE", notes


def build_regime_snapshot(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    benchmark_name: str = DEFAULT_BENCHMARK_NAME,
) -> pd.DataFrame:
    macro = load_macro_history(start_date=from_date, to_date=to_date)
    benchmark = load_benchmark_history(
        benchmark_name=benchmark_name,
        start_date=from_date,
        to_date=to_date,
    )
    if macro.empty or benchmark.empty:
        return pd.DataFrame()

    out = macro.merge(benchmark, left_on="asof_date", right_on="date", how="inner").drop(
        columns=["date"], errors="ignore"
    )
    labels = out.apply(classify_regime, axis=1)
    out["regime_name"] = [label for label, _ in labels]
    out["regime_notes"] = ["; ".join(notes) for _, notes in labels]
    out["benchmark_name"] = benchmark_name
    out["tariff_pressure_flag"] = (
        (out["broad_usd_index_ret_20d"] >= 0.03)
        | (out["wti_crude_spot_ret_20d"] >= 0.12)
        | (out["inr_usd_spot_ret_20d"] >= 0.025)
        | (out["gsec_10y_change_20d_bps"] >= 25.0)
        | (out["macro_stress_score"] >= 0.40)
    ).fillna(False)
    out["shock_flag"] = out["regime_name"].eq("SHOCK")
    out["risk_off_flag"] = out["regime_name"].isin(["SHOCK", "RISK_OFF"])
    out["load_ts"] = pd.Timestamp.utcnow()
    ordered_cols = [
        "asof_date",
        "benchmark_name",
        "benchmark_close",
        "benchmark_ret_20d",
        "benchmark_ret_60d",
        "benchmark_dma_50",
        "benchmark_dma_200",
        "benchmark_realized_vol_20d",
        "benchmark_drawdown_60d",
        "vix_close",
        "broad_usd_index_ret_20d",
        "wti_crude_spot_ret_20d",
        "inr_usd_spot_ret_20d",
        "gsec_10y_change_20d_bps",
        "macro_stress_score",
        "macro_risk_state",
        "macro_sizing_multiplier",
        "repo_rate",
        "macro_usa_freshness_status",
        "bank_rates_freshness_status",
        "cpi_freshness_status",
        "wpi_freshness_status",
        "gsec_curve_freshness_status",
        "new_macro_source_required",
        "tariff_pressure_flag",
        "shock_flag",
        "risk_off_flag",
        "regime_name",
        "regime_notes",
        "load_ts",
    ]
    return out[ordered_cols].drop_duplicates(subset=["asof_date"], keep="last").reset_index(drop=True)


def persist_regime_snapshot(df: pd.DataFrame, *, rebuild: bool = False) -> None:
    if df.empty:
        return
    ensure_regime_table()
    if rebuild:
        def _delete_existing_regime_rows() -> None:
            with db_session() as (_, cur):
                cur.execute(f"DELETE FROM {TABLE_NAME}")

        execute_db_operation(
            _delete_existing_regime_rows,
            operation_name="regime_engine:delete_rebuild_snapshot",
        )
    upsert_to_db(
        df,
        TABLE_NAME,
        unique_keys=["asof_date"],
        timescaledb_column="asof_date",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build REGIME_ALGO_V1 market regime labels.")
    parser.add_argument("--from-date", type=parse_datetime_arg)
    parser.add_argument("--to-date", type=parse_datetime_arg)
    parser.add_argument("--benchmark", default=DEFAULT_BENCHMARK_NAME)
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
            "regime_counts": {},
            "sample": [],
        }
    preview_cols = [
        "asof_date",
        "regime_name",
        "benchmark_ret_20d",
        "vix_close",
        "tariff_pressure_flag",
        "regime_notes",
    ]
    return {
        "status": "ok",
        "table": TABLE_NAME,
        "row_count": int(len(df)),
        "date_min": df["asof_date"].min().date().isoformat(),
        "date_max": df["asof_date"].max().date().isoformat(),
        "regime_counts": {
            str(key): int(value)
            for key, value in df["regime_name"].value_counts(dropna=False).to_dict().items()
        },
        "sample": df[preview_cols].tail(5).to_dict(orient="records"),
    }


def main() -> int:
    args = parse_args()
    df = build_regime_snapshot(
        from_date=pd.Timestamp(args.from_date, tz="UTC") if args.from_date else None,
        to_date=pd.Timestamp(args.to_date, tz="UTC") if args.to_date else None,
        benchmark_name=args.benchmark,
    )
    if not args.dry_run:
        persist_regime_snapshot(df, rebuild=args.rebuild)
    result = summarize(df)
    result["dry_run"] = bool(args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
