from __future__ import annotations

import argparse
import json
from typing import Any, Iterable

import pandas as pd

from advisory.action_recommender import TABLE_NAME as ACTION_TABLE
from advisory.fallback_telemetry import record_local_fallback_event
from advisory.research_ledger import finish_research_run, start_research_run
from advisory.ts_forecast_features import TABLE_NAME as FORECAST_TABLE
from advisory.ts_forecast_evaluator import DEFAULT_COST_BPS, load_price_window
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


PAPER_TABLE = "advisory_ts_forecast_paper_portfolio"
TS_FORECAST_PAPER_SCHEMA_MIGRATION_ID = "20260612_advisory_ts_forecast_paper_portfolio_base"
DEFAULT_MIN_PROBABILITY_POSITIVE = 0.58
DEFAULT_MIN_SIGNAL_QUALITY = 0.35
DEFAULT_MIN_FORECAST_RETURN = 0.0

TS_FORECAST_PAPER_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {PAPER_TABLE} (
        asof_date TIMESTAMPTZ NOT NULL,
        symbol TEXT NOT NULL,
        model_name TEXT NOT NULL,
        forecast_horizon_days BIGINT NOT NULL,
        paper_decision TEXT NOT NULL,
        paper_policy_version TEXT NOT NULL,
        action_hint TEXT,
        forecast_return DOUBLE PRECISION,
        probability_positive DOUBLE PRECISION,
        signal_quality DOUBLE PRECISION,
        momentum_return_20d DOUBLE PRECISION,
        momentum_return_60d DOUBLE PRECISION,
        momentum_baseline_decision TEXT,
        advisory_action_code TEXT,
        advisory_execution_mode TEXT,
        advisory_alignment TEXT,
        entry_date TIMESTAMPTZ,
        exit_date TIMESTAMPTZ,
        entry_price DOUBLE PRECISION,
        exit_price DOUBLE PRECISION,
        realized_return DOUBLE PRECISION,
        cost_adjusted_return DOUBLE PRECISION,
        baseline_cost_adjusted_return DOUBLE PRECISION,
        evaluation_status TEXT,
        decision_reason TEXT,
        config_json TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (asof_date, symbol, model_name, forecast_horizon_days)
    )
    """,
]


def _record_ts_paper_fallback(
    *,
    fallback_type: str,
    source: str,
    reason: str,
    error: Exception,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.ts_forecast_paper_portfolio",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def _normalize_timestamp(value: Any | None) -> pd.Timestamp | None:
    if value is None:
        return None
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.normalize()


def _as_symbol_list(values: Iterable[str] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        for part in str(value).split(","):
            symbol = part.strip().upper()
            if symbol:
                out.append(symbol)
    return sorted(set(out))


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _finite_float(value: Any, default: float = 0.0) -> float:
    num = pd.to_numeric(value, errors="coerce")
    if pd.isna(num):
        return float(default)
    return float(num)


def ensure_tables() -> None:
    apply_schema_migration(
        migration_id=TS_FORECAST_PAPER_SCHEMA_MIGRATION_ID,
        description="Create research-only TS forecast paper portfolio table.",
        statements=TS_FORECAST_PAPER_SCHEMA_STATEMENTS,
        metadata={"module": "advisory.ts_forecast_paper_portfolio", "tables": [PAPER_TABLE]},
    )


def load_forecast_candidates(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    model_names: list[str] | None = None,
) -> pd.DataFrame:
    clauses = ["1 = 1"]
    params: list[object] = []
    if from_date is not None:
        clauses.append("asof_date >= %s")
        params.append(from_date)
    if to_date is not None:
        clauses.append("asof_date <= %s")
        params.append(to_date)
    if symbols:
        clauses.append("symbol = ANY(%s)")
        params.append(_as_symbol_list(symbols))
    if model_names:
        clauses.append("model_name = ANY(%s)")
        params.append([str(value).strip().lower() for value in model_names if str(value).strip()])
    try:
        df = sql_to_df(
            f"""
            SELECT
                asof_date,
                symbol,
                model_name,
                forecast_horizon_days,
                action_hint,
                forecast_return,
                probability_positive,
                signal_quality,
                momentum_return_20d,
                momentum_return_60d
            FROM {FORECAST_TABLE}
            WHERE {' AND '.join(clauses)}
            ORDER BY asof_date, model_name, forecast_horizon_days, symbol
            """,
            params=tuple(params) if params else None,
        )
    except Exception as exc:
        _record_ts_paper_fallback(
            fallback_type="ts_forecast_paper_forecast_load_failed",
            source=FORECAST_TABLE,
            reason="TS forecast paper portfolio evaluator could not load forecast candidates.",
            error=exc,
            metadata={
                "from_date": str(from_date) if from_date is not None else None,
                "to_date": str(to_date) if to_date is not None else None,
                "symbol_count": len(symbols or []),
                "model_count": len(model_names or []),
            },
        )
        raise
    if df.empty:
        return df
    df["asof_date"] = pd.to_datetime(df["asof_date"], utc=True, errors="coerce").dt.normalize()
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    df["model_name"] = df["model_name"].astype("string").str.strip().str.lower()
    for col in [
        "forecast_horizon_days",
        "forecast_return",
        "probability_positive",
        "signal_quality",
        "momentum_return_20d",
        "momentum_return_60d",
    ]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["asof_date", "symbol", "model_name", "forecast_horizon_days", "forecast_return"])


def load_advisory_actions(
    *,
    symbols: list[str],
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> pd.DataFrame:
    normalized_symbols = _as_symbol_list(symbols)
    if not normalized_symbols:
        return pd.DataFrame()
    try:
        df = sql_to_df(
            f"""
            SELECT asof_date, symbol, action_code, execution_mode
            FROM {ACTION_TABLE}
            WHERE symbol = ANY(%s)
              AND asof_date >= %s
              AND asof_date <= %s
            ORDER BY asof_date DESC, load_ts DESC
            """,
            params=(normalized_symbols, start_date, end_date),
        )
    except Exception as exc:
        _record_ts_paper_fallback(
            fallback_type="ts_forecast_paper_advisory_action_load_failed",
            source=ACTION_TABLE,
            reason="TS forecast paper portfolio evaluator could not load advisory action alignment; continuing without alignment.",
            error=exc,
            metadata={"symbol_count": len(normalized_symbols), "from_date": str(start_date), "to_date": str(end_date)},
        )
        return pd.DataFrame()
    if df.empty:
        return df
    df["asof_date"] = pd.to_datetime(df["asof_date"], utc=True, errors="coerce").dt.normalize()
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    return df.dropna(subset=["asof_date", "symbol"]).drop_duplicates(subset=["asof_date", "symbol"], keep="first")


def _nth_future_price(price_group: pd.DataFrame, asof_date: pd.Timestamp, horizon_days: int) -> tuple[pd.Timestamp | None, float | None, float | None, str]:
    future = price_group[price_group["date"].ge(asof_date)].sort_values("date").reset_index(drop=True)
    if future.empty:
        return None, None, None, "missing_entry_price"
    entry_row = future.iloc[0]
    target_idx = int(horizon_days)
    if len(future) <= target_idx:
        return None, float(entry_row["close"]), None, "not_matured"
    exit_row = future.iloc[target_idx]
    return pd.Timestamp(exit_row["date"]), float(entry_row["close"]), float(exit_row["close"]), "evaluated"


def _paper_decision(
    row: pd.Series,
    *,
    min_probability_positive: float,
    min_signal_quality: float,
    min_forecast_return: float,
) -> tuple[str, str]:
    probability_positive = _finite_float(row.get("probability_positive"))
    signal_quality = _finite_float(row.get("signal_quality"))
    forecast_return = _finite_float(row.get("forecast_return"))
    action_hint = str(row.get("action_hint") or "").upper()
    if action_hint != "EXPERIMENTAL_POSITIVE":
        return "PAPER_SKIP", f"Skipped because action_hint={action_hint or 'UNKNOWN'} is not EXPERIMENTAL_POSITIVE."
    if forecast_return <= float(min_forecast_return):
        return "PAPER_SKIP", f"Skipped because forecast_return={forecast_return:.2%} is below threshold."
    if probability_positive < float(min_probability_positive):
        return "PAPER_SKIP", f"Skipped because probability_positive={probability_positive:.2f} is below threshold."
    if signal_quality < float(min_signal_quality):
        return "PAPER_SKIP", f"Skipped because signal_quality={signal_quality:.2f} is below threshold."
    return (
        "PAPER_BUY",
        (
            "Research-only paper buy: forecast is positive, probability and signal-quality gates passed. "
            "This does not create action, portfolio, or broker execution authority."
        ),
    )


def _momentum_baseline_decision(row: pd.Series) -> str:
    mom20 = pd.to_numeric(row.get("momentum_return_20d"), errors="coerce")
    mom60 = pd.to_numeric(row.get("momentum_return_60d"), errors="coerce")
    if pd.notna(mom20) and pd.notna(mom60) and float(mom20) > 0 and float(mom60) > 0:
        return "MOMENTUM_BUY"
    return "MOMENTUM_SKIP"


def _advisory_alignment(paper_decision: str, action_code: Any) -> str:
    action = str(action_code or "").upper()
    if not action:
        return "NO_ADVISORY_ACTION"
    if paper_decision == "PAPER_BUY" and action in {"BUY", "BUY_MORE", "WATCH"}:
        return "ALIGNED_POSITIVE"
    if paper_decision == "PAPER_BUY" and action in {"SELL", "PARTIAL_SELL"}:
        return "CONFLICT_EXIT"
    if paper_decision == "PAPER_SKIP" and action in {"BUY", "BUY_MORE"}:
        return "ADVISORY_POSITIVE_TS_SKIP"
    return "NEUTRAL_OR_UNRELATED"


def build_paper_portfolio(
    *,
    forecasts: pd.DataFrame | None = None,
    prices: pd.DataFrame | None = None,
    advisory_actions: pd.DataFrame | None = None,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    model_names: list[str] | None = None,
    cost_bps: float = DEFAULT_COST_BPS,
    min_probability_positive: float = DEFAULT_MIN_PROBABILITY_POSITIVE,
    min_signal_quality: float = DEFAULT_MIN_SIGNAL_QUALITY,
    min_forecast_return: float = DEFAULT_MIN_FORECAST_RETURN,
) -> pd.DataFrame:
    forecast_df = forecasts if forecasts is not None else load_forecast_candidates(
        from_date=from_date,
        to_date=to_date,
        symbols=symbols,
        model_names=model_names,
    )
    if forecast_df.empty:
        return pd.DataFrame()
    forecast_df = forecast_df.copy()
    forecast_df["asof_date"] = pd.to_datetime(forecast_df["asof_date"], utc=True, errors="coerce").dt.normalize()
    forecast_df["symbol"] = forecast_df["symbol"].astype("string").str.strip().str.upper()
    forecast_df["model_name"] = forecast_df["model_name"].astype("string").str.strip().str.lower()
    for col in ["forecast_horizon_days", "forecast_return", "probability_positive", "signal_quality", "momentum_return_20d", "momentum_return_60d"]:
        forecast_df[col] = pd.to_numeric(forecast_df.get(col), errors="coerce")
    max_horizon = int(forecast_df["forecast_horizon_days"].max())
    min_date = forecast_df["asof_date"].min()
    max_date = forecast_df["asof_date"].max() + pd.Timedelta(days=max_horizon * 3)
    price_df = prices if prices is not None else load_price_window(
        symbols=forecast_df["symbol"].dropna().astype(str).unique().tolist(),
        start_date=min_date,
        end_date=max_date,
    )
    if price_df.empty:
        return pd.DataFrame()
    price_df = price_df.copy()
    price_df["symbol"] = price_df["symbol"].astype("string").str.strip().str.upper()
    price_df["date"] = pd.to_datetime(price_df["date"], utc=True, errors="coerce").dt.normalize()
    price_df["close"] = pd.to_numeric(price_df["close"], errors="coerce")
    price_map = {symbol: group.sort_values("date").reset_index(drop=True) for symbol, group in price_df.groupby("symbol", sort=False)}

    actions_df = advisory_actions
    if actions_df is None:
        actions_df = load_advisory_actions(
            symbols=forecast_df["symbol"].dropna().astype(str).unique().tolist(),
            start_date=forecast_df["asof_date"].min(),
            end_date=forecast_df["asof_date"].max(),
        )
    actions_lookup: dict[tuple[pd.Timestamp, str], dict[str, Any]] = {}
    if actions_df is not None and not actions_df.empty:
        actions = actions_df.copy()
        actions["asof_date"] = pd.to_datetime(actions["asof_date"], utc=True, errors="coerce").dt.normalize()
        actions["symbol"] = actions["symbol"].astype("string").str.strip().str.upper()
        for _, action_row in actions.dropna(subset=["asof_date", "symbol"]).iterrows():
            actions_lookup[(pd.Timestamp(action_row["asof_date"]), str(action_row["symbol"]))] = action_row.to_dict()

    cost_rate = float(cost_bps) / 10000.0
    config = {
        "cost_bps": float(cost_bps),
        "min_probability_positive": float(min_probability_positive),
        "min_signal_quality": float(min_signal_quality),
        "min_forecast_return": float(min_forecast_return),
    }
    policy_version = "ts_forecast_paper_v1"
    now = pd.Timestamp.utcnow()
    rows: list[dict[str, Any]] = []
    for _, forecast in forecast_df.iterrows():
        symbol = str(forecast["symbol"]).upper()
        asof_date = pd.Timestamp(forecast["asof_date"])
        horizon_days = int(forecast["forecast_horizon_days"])
        exit_date, entry_price, exit_price, status = _nth_future_price(
            price_map.get(symbol, pd.DataFrame(columns=["date", "close"])),
            asof_date,
            horizon_days,
        )
        decision, reason = _paper_decision(
            forecast,
            min_probability_positive=min_probability_positive,
            min_signal_quality=min_signal_quality,
            min_forecast_return=min_forecast_return,
        )
        realized_return = None
        cost_adjusted_return = None
        baseline_cost_adjusted_return = None
        if status == "evaluated" and entry_price and exit_price is not None:
            realized_return = (float(exit_price) / float(entry_price)) - 1.0
            if decision == "PAPER_BUY":
                cost_adjusted_return = realized_return - cost_rate
            if _momentum_baseline_decision(forecast) == "MOMENTUM_BUY":
                baseline_cost_adjusted_return = realized_return - cost_rate
        action_row = actions_lookup.get((asof_date, symbol), {})
        rows.append(
            {
                "asof_date": asof_date,
                "symbol": symbol,
                "model_name": str(forecast["model_name"]),
                "forecast_horizon_days": horizon_days,
                "paper_decision": decision,
                "paper_policy_version": policy_version,
                "action_hint": forecast.get("action_hint"),
                "forecast_return": forecast.get("forecast_return"),
                "probability_positive": forecast.get("probability_positive"),
                "signal_quality": forecast.get("signal_quality"),
                "momentum_return_20d": forecast.get("momentum_return_20d"),
                "momentum_return_60d": forecast.get("momentum_return_60d"),
                "momentum_baseline_decision": _momentum_baseline_decision(forecast),
                "advisory_action_code": action_row.get("action_code"),
                "advisory_execution_mode": action_row.get("execution_mode"),
                "advisory_alignment": _advisory_alignment(decision, action_row.get("action_code")),
                "entry_date": asof_date if entry_price is not None else None,
                "exit_date": exit_date,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "realized_return": realized_return,
                "cost_adjusted_return": cost_adjusted_return,
                "baseline_cost_adjusted_return": baseline_cost_adjusted_return,
                "evaluation_status": status,
                "decision_reason": reason,
                "config_json": _json_text(config),
                "load_ts": now,
            }
        )
    return pd.DataFrame(rows)


def build_summary(rows: pd.DataFrame, *, cost_bps: float = DEFAULT_COST_BPS) -> pd.DataFrame:
    if rows.empty:
        return pd.DataFrame()
    evaluated = rows[rows["evaluation_status"].eq("evaluated")].copy()
    if evaluated.empty:
        return pd.DataFrame()
    out_rows: list[dict[str, Any]] = []
    for keys, group in evaluated.groupby(["model_name", "forecast_horizon_days", "paper_decision"], dropna=False, sort=True):
        model_name, horizon_days, paper_decision = keys
        adjusted = pd.to_numeric(group["cost_adjusted_return"], errors="coerce").dropna()
        baseline = pd.to_numeric(group["baseline_cost_adjusted_return"], errors="coerce").dropna()
        realized = pd.to_numeric(group["realized_return"], errors="coerce").dropna()
        out_rows.append(
            {
                "model_name": str(model_name),
                "forecast_horizon_days": int(horizon_days),
                "paper_decision": str(paper_decision),
                "row_count": int(len(group)),
                "evaluated_trades": int(len(adjusted)),
                "win_rate": float((adjusted > 0).mean()) if len(adjusted) else None,
                "avg_cost_adjusted_return": float(adjusted.mean()) if len(adjusted) else None,
                "median_cost_adjusted_return": float(adjusted.median()) if len(adjusted) else None,
                "avg_realized_return_all_rows": float(realized.mean()) if len(realized) else None,
                "baseline_trade_count": int(len(baseline)),
                "baseline_avg_cost_adjusted_return": float(baseline.mean()) if len(baseline) else None,
                "aligned_positive_count": int(group["advisory_alignment"].eq("ALIGNED_POSITIVE").sum()),
                "conflict_exit_count": int(group["advisory_alignment"].eq("CONFLICT_EXIT").sum()),
                "cost_bps": float(cost_bps),
            }
        )
    return pd.DataFrame(out_rows)


def _prepare_for_persist(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    for col in ["asof_date", "entry_date", "exit_date", "load_ts"]:
        if col in out.columns:
            out[col] = pd.to_datetime(out[col], utc=True, errors="coerce")
    for col in ["forecast_horizon_days"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").astype("Int64")
    for col in [
        "forecast_return",
        "probability_positive",
        "signal_quality",
        "momentum_return_20d",
        "momentum_return_60d",
        "entry_price",
        "exit_price",
        "realized_return",
        "cost_adjusted_return",
        "baseline_cost_adjusted_return",
    ]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    for col in [
        "symbol",
        "model_name",
        "paper_decision",
        "paper_policy_version",
        "action_hint",
        "momentum_baseline_decision",
        "advisory_action_code",
        "advisory_execution_mode",
        "advisory_alignment",
        "evaluation_status",
        "decision_reason",
        "config_json",
    ]:
        if col in out.columns:
            out[col] = out[col].astype("string")
    return out


def persist_paper_portfolio(df: pd.DataFrame) -> None:
    ensure_tables()
    if df.empty:
        return
    upsert_to_db(
        _prepare_for_persist(df),
        PAPER_TABLE,
        unique_keys=["asof_date", "symbol", "model_name", "forecast_horizon_days"],
        timescaledb_column="asof_date",
    )


def summarize(rows: pd.DataFrame, summary: pd.DataFrame) -> dict[str, Any]:
    return {
        "status": "ok",
        "paper_table": PAPER_TABLE,
        "row_count": int(len(rows)),
        "paper_buy_rows": int(rows["paper_decision"].eq("PAPER_BUY").sum()) if not rows.empty else 0,
        "evaluated_rows": int(rows["evaluation_status"].eq("evaluated").sum()) if not rows.empty else 0,
        "not_matured_rows": int(rows["evaluation_status"].eq("not_matured").sum()) if not rows.empty else 0,
        "summary_rows": int(len(summary)),
        "summary_sample": summary.head(20).to_dict(orient="records") if not summary.empty else [],
        "sample": rows.head(10).to_dict(orient="records") if not rows.empty else [],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a research-only paper portfolio from experimental TS forecast rows.")
    parser.add_argument("--from-date", type=parse_datetime_arg)
    parser.add_argument("--to-date", type=parse_datetime_arg)
    parser.add_argument("--symbols", nargs="*", help="Symbols, comma-separated or repeated")
    parser.add_argument("--model-name", dest="model_names", nargs="*", help="Model names to evaluate")
    parser.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    parser.add_argument("--min-probability-positive", type=float, default=DEFAULT_MIN_PROBABILITY_POSITIVE)
    parser.add_argument("--min-signal-quality", type=float, default=DEFAULT_MIN_SIGNAL_QUALITY)
    parser.add_argument("--min-forecast-return", type=float, default=DEFAULT_MIN_FORECAST_RETURN)
    parser.add_argument("--log-research-ledger", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    from_date = _normalize_timestamp(args.from_date)
    to_date = _normalize_timestamp(args.to_date)
    config = {
        "from_date": str(from_date) if from_date is not None else None,
        "to_date": str(to_date) if to_date is not None else None,
        "symbols": _as_symbol_list(args.symbols),
        "model_names": args.model_names or [],
        "cost_bps": float(args.cost_bps),
        "min_probability_positive": float(args.min_probability_positive),
        "min_signal_quality": float(args.min_signal_quality),
        "min_forecast_return": float(args.min_forecast_return),
    }
    run_id = None
    if bool(args.log_research_ledger) and not bool(args.dry_run):
        run_id = start_research_run(
            run_type="ts_forecast_paper_portfolio",
            entrypoint="python -m advisory.ts_forecast_paper_portfolio",
            label="TS forecast paper portfolio",
            objective="Evaluate forecast-only paper decisions against naive momentum and current advisory alignment before any policy integration.",
            config=config,
            asof_date=to_date,
            validation_protocol={
                "authority": "research_only",
                "no_broker_execution": True,
                "no_action_queue_write": True,
                "comparison": ["forecast_only", "naive_momentum", "current_advisory_action_alignment"],
            },
        )
    try:
        rows = build_paper_portfolio(
            from_date=from_date,
            to_date=to_date,
            symbols=_as_symbol_list(args.symbols),
            model_names=args.model_names,
            cost_bps=float(args.cost_bps),
            min_probability_positive=float(args.min_probability_positive),
            min_signal_quality=float(args.min_signal_quality),
            min_forecast_return=float(args.min_forecast_return),
        )
        summary = build_summary(rows, cost_bps=float(args.cost_bps))
        if not args.dry_run:
            persist_paper_portfolio(rows)
        result = summarize(rows, summary)
        result["dry_run"] = bool(args.dry_run)
        result["research_run_id"] = run_id
        if run_id:
            finish_research_run(
                run_id,
                status="completed",
                data_snapshot={
                    "row_count": result["row_count"],
                    "paper_buy_rows": result["paper_buy_rows"],
                    "evaluated_rows": result["evaluated_rows"],
                    "from_date": config["from_date"],
                    "to_date": config["to_date"],
                },
                result_metrics={
                    "summary": summary.to_dict(orient="records") if not summary.empty else [],
                    "paper_buy_rows": result["paper_buy_rows"],
                    "evaluated_rows": result["evaluated_rows"],
                },
                notes={"authority": "research_only", "paper_table": PAPER_TABLE},
            )
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0
    except Exception as exc:
        if run_id:
            finish_research_run(run_id, status="failed", error_text=f"{type(exc).__name__}: {exc}")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
