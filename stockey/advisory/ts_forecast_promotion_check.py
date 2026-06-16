from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event
from advisory.ts_forecast_paper_portfolio import PAPER_TABLE
from utils.db import sql_to_df
from utils.sync import parse_datetime_arg


DEFAULT_MIN_EVALUATED_TRADES = 50
DEFAULT_MIN_WIN_RATE = 0.52
DEFAULT_MIN_AVG_COST_ADJUSTED_RETURN = 0.01
DEFAULT_MIN_LIFT_VS_MOMENTUM = 0.005
DEFAULT_MAX_EXIT_CONFLICT_RATE = 0.05
DEFAULT_MIN_DISTINCT_DATES = 10
DEFAULT_MIN_SYMBOLS = 20


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    return str(value)


def _number(value: Any) -> float | None:
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return None
    return float(numeric)


def _timestamp_or_none(value: Any) -> pd.Timestamp | None:
    if value is None:
        return None
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.normalize()


def _int_or_zero(value: Any) -> int:
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return 0
    return int(numeric)


def _record_ts_promotion_fallback(
    *,
    fallback_type: str,
    source: str,
    reason: str,
    error: Exception,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.ts_forecast_promotion_check",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def _gate_gte(name: str, value: float | int | None, threshold: float | int, gates: list[dict[str, Any]]) -> bool:
    passed = value is not None and float(value) >= float(threshold)
    gates.append({"gate": name, "passed": bool(passed), "value": value, "threshold": threshold})
    return bool(passed)


def _gate_lte(name: str, value: float | int | None, threshold: float | int, gates: list[dict[str, Any]]) -> bool:
    passed = value is not None and float(value) <= float(threshold)
    gates.append({"gate": name, "passed": bool(passed), "value": value, "threshold": threshold})
    return bool(passed)


def load_paper_evidence(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    model_name: str | None = None,
    horizon_days: int | None = None,
) -> pd.DataFrame:
    clauses = ["paper_decision = 'PAPER_BUY'", "evaluation_status = 'evaluated'"]
    params: list[object] = []
    if from_date is not None:
        clauses.append("asof_date >= %s")
        params.append(from_date)
    if to_date is not None:
        clauses.append("asof_date <= %s")
        params.append(to_date)
    if model_name:
        clauses.append("model_name = %s")
        params.append(str(model_name).strip().lower())
    if horizon_days is not None:
        clauses.append("forecast_horizon_days = %s")
        params.append(int(horizon_days))
    try:
        df = sql_to_df(
            f"""
            SELECT
                asof_date,
                symbol,
                model_name,
                forecast_horizon_days,
                paper_decision,
                cost_adjusted_return,
                baseline_cost_adjusted_return,
                realized_return,
                advisory_alignment,
                load_ts
            FROM {PAPER_TABLE}
            WHERE {' AND '.join(clauses)}
            ORDER BY asof_date, model_name, forecast_horizon_days, symbol
            """,
            params=tuple(params) if params else None,
        )
    except Exception as exc:
        _record_ts_promotion_fallback(
            fallback_type="ts_forecast_promotion_paper_evidence_load_failed",
            source=PAPER_TABLE,
            reason="TS forecast promotion check could not load paper-portfolio evidence.",
            error=exc,
            metadata={
                "from_date": str(from_date) if from_date is not None else None,
                "to_date": str(to_date) if to_date is not None else None,
                "model_name": model_name,
                "horizon_days": horizon_days,
            },
        )
        raise
    if df.empty:
        return df
    df["asof_date"] = pd.to_datetime(df["asof_date"], utc=True, errors="coerce").dt.normalize()
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    df["model_name"] = df["model_name"].astype("string").str.strip().str.lower()
    df["forecast_horizon_days"] = pd.to_numeric(df["forecast_horizon_days"], errors="coerce")
    for column in ["cost_adjusted_return", "baseline_cost_adjusted_return", "realized_return"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    return df.dropna(subset=["asof_date", "symbol", "model_name", "forecast_horizon_days", "cost_adjusted_return"])


def summarize_groups(evidence: pd.DataFrame) -> list[dict[str, Any]]:
    if evidence.empty:
        return []
    rows: list[dict[str, Any]] = []
    for keys, group in evidence.groupby(["model_name", "forecast_horizon_days"], sort=True, dropna=False):
        model_name, horizon = keys
        adjusted = pd.to_numeric(group["cost_adjusted_return"], errors="coerce").dropna()
        baseline = pd.to_numeric(group["baseline_cost_adjusted_return"], errors="coerce").dropna()
        avg_adjusted = _number(adjusted.mean()) if len(adjusted) else None
        avg_baseline = _number(baseline.mean()) if len(baseline) else None
        lift = None
        if avg_adjusted is not None and avg_baseline is not None:
            lift = round(float(avg_adjusted - avg_baseline), 6)
        conflict_count = int(group["advisory_alignment"].astype("string").str.upper().eq("CONFLICT_EXIT").sum()) if "advisory_alignment" in group.columns else 0
        evaluated_trades = int(len(adjusted))
        rows.append(
            {
                "model_name": str(model_name),
                "horizon_days": int(horizon),
                "evaluated_trades": evaluated_trades,
                "win_rate": _number((adjusted > 0).mean()) if len(adjusted) else None,
                "avg_cost_adjusted_return": avg_adjusted,
                "median_cost_adjusted_return": _number(adjusted.median()) if len(adjusted) else None,
                "baseline_trade_count": int(len(baseline)),
                "baseline_avg_cost_adjusted_return": avg_baseline,
                "lift_vs_momentum": lift,
                "exit_conflict_count": conflict_count,
                "exit_conflict_rate": round(float(conflict_count / evaluated_trades), 6) if evaluated_trades else None,
                "distinct_dates": int(group["asof_date"].nunique()),
                "symbol_count": int(group["symbol"].nunique()),
                "from_date": group["asof_date"].min(),
                "to_date": group["asof_date"].max(),
                "latest_load_ts": pd.to_datetime(group.get("load_ts"), utc=True, errors="coerce").max() if "load_ts" in group.columns else None,
            }
        )
    return rows


def score_group(group: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    gates: list[dict[str, Any]] = []
    _gate_gte("evaluated_trades", group.get("evaluated_trades"), args.min_evaluated_trades, gates)
    _gate_gte("win_rate", group.get("win_rate"), args.min_win_rate, gates)
    _gate_gte("avg_cost_adjusted_return", group.get("avg_cost_adjusted_return"), args.min_avg_cost_adjusted_return, gates)
    _gate_gte("lift_vs_momentum", group.get("lift_vs_momentum"), args.min_lift_vs_momentum, gates)
    _gate_lte("exit_conflict_rate", group.get("exit_conflict_rate"), args.max_exit_conflict_rate, gates)
    _gate_gte("distinct_dates", group.get("distinct_dates"), args.min_distinct_dates, gates)
    _gate_gte("symbol_count", group.get("symbol_count"), args.min_symbols, gates)
    failed = [gate["gate"] for gate in gates if not gate.get("passed")]
    decision = "review_candidate" if not failed else "hold_research_only"
    return {
        **group,
        "decision": decision,
        "ready_for_operator_review": decision == "review_candidate",
        "failed_gates": failed,
        "failed_gate_count": len(failed),
        "gates": gates,
        "authority": "research_only_manual_review",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "operator_action": (
            "Open a manual promotion review before allowing TS forecasts as low-weight inputs."
            if decision == "review_candidate"
            else "Keep TS forecasts research-only; collect more matured paper evidence or improve performance versus momentum."
        ),
    }


def build_scorecard(
    *,
    groups: list[dict[str, Any]],
    args: argparse.Namespace,
) -> dict[str, Any]:
    scored = [score_group(group, args) for group in groups]
    scored.sort(
        key=lambda row: (
            bool(row.get("ready_for_operator_review")),
            _number(row.get("avg_cost_adjusted_return")) or -999.0,
            _number(row.get("lift_vs_momentum")) or -999.0,
            _int_or_zero(row.get("evaluated_trades")),
        ),
        reverse=True,
    )
    best = scored[0] if scored else None
    ready = [row for row in scored if row.get("ready_for_operator_review")]
    decision = "review_candidate" if ready else "hold_research_only"
    return {
        "decision": decision,
        "ready_for_operator_review": bool(ready),
        "status": "usable_for_manual_review" if ready else "not_usable",
        "headline": (
            "At least one TS forecast paper group passed promotion gates."
            if ready
            else "TS forecast paper evidence has not passed promotion gates."
        ),
        "operator_action": (
            "Create a manual promotion review for the best passing TS group; do not auto-promote."
            if ready
            else "Keep TS forecasts research-only until paper evidence beats momentum with enough breadth."
        ),
        "authority": "research_only_manual_review",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
        "group_count": len(scored),
        "ready_group_count": len(ready),
        "best_group": best,
        "groups": scored,
        "configured_gates": {
            "min_evaluated_trades": int(args.min_evaluated_trades),
            "min_win_rate": float(args.min_win_rate),
            "min_avg_cost_adjusted_return": float(args.min_avg_cost_adjusted_return),
            "min_lift_vs_momentum": float(args.min_lift_vs_momentum),
            "max_exit_conflict_rate": float(args.max_exit_conflict_rate),
            "min_distinct_dates": int(args.min_distinct_dates),
            "min_symbols": int(args.min_symbols),
        },
    }


def _is_missing_table_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return exc.__class__.__name__ in {"UndefinedTable", "ProgrammingError"} and (
        "does not exist" in text or "undefinedtable" in text
    )


def _build_unavailable_payload(*, args: argparse.Namespace, error: Exception) -> dict[str, Any]:
    scorecard = build_scorecard(groups=[], args=args)
    scorecard.update(
        {
            "status": "evidence_unavailable",
            "headline": "TS forecast paper evidence table is unavailable.",
            "operator_action": "Run the TS forecast paper portfolio workflow before reviewing TS forecasts for promotion.",
            "evidence_unavailable": True,
            "evidence_error_type": error.__class__.__name__,
        }
    )
    return {
        "status": "blocked",
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "decision": "hold_research_only",
        "ready_for_operator_review": False,
        "promotion_mode": "manual_low_weight_input_only",
        "scorecard": scorecard,
        "evidence": {
            "paper_table": PAPER_TABLE,
            "row_count": 0,
            "from_date": None,
            "to_date": None,
            "model_name_filter": args.model_name,
            "horizon_days_filter": args.horizon_days,
            "available": False,
            "error_type": error.__class__.__name__,
            "error": str(error)[:500],
        },
        "notes": [
            "This check never promotes TS forecasts automatically.",
            "TS forecast paper evidence is unavailable, so forecasts remain research-only.",
            "Run advisory.ts_forecast_paper_portfolio after forecasts/evaluations are available.",
        ],
    }


def build_promotion_check(args: argparse.Namespace) -> dict[str, Any]:
    try:
        evidence = load_paper_evidence(
            from_date=_timestamp_or_none(args.from_date),
            to_date=_timestamp_or_none(args.to_date),
            model_name=args.model_name,
            horizon_days=args.horizon_days,
        )
    except Exception as exc:
        if _is_missing_table_error(exc):
            return _build_unavailable_payload(args=args, error=exc)
        raise
    groups = summarize_groups(evidence)
    scorecard = build_scorecard(groups=groups, args=args)
    return {
        "status": "ok",
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "decision": scorecard["decision"],
        "ready_for_operator_review": scorecard["ready_for_operator_review"],
        "promotion_mode": "manual_low_weight_input_only",
        "scorecard": scorecard,
        "evidence": {
            "paper_table": PAPER_TABLE,
            "row_count": int(len(evidence)),
            "from_date": None if evidence.empty else evidence["asof_date"].min(),
            "to_date": None if evidence.empty else evidence["asof_date"].max(),
            "model_name_filter": args.model_name,
            "horizon_days_filter": args.horizon_days,
        },
        "notes": [
            "This check never promotes TS forecasts automatically.",
            "Passing gates means the forecast group can be reviewed manually as a low-weight research input.",
            "TS forecasts still cannot create action recommendations, portfolio rows, or broker orders.",
        ],
    }


def format_text(payload: dict[str, Any]) -> str:
    scorecard = payload.get("scorecard") or {}
    best = scorecard.get("best_group") or {}
    lines = [
        "TS Forecast Promotion Check",
        f"Decision: {payload.get('decision')}",
        f"Ready for operator review: {payload.get('ready_for_operator_review')}",
        f"Status: {scorecard.get('status')}",
        "",
        "Best group:",
        f"- model/horizon: {best.get('model_name')}/{best.get('horizon_days')}d",
        f"- evaluated trades: {best.get('evaluated_trades')}",
        f"- win rate: {best.get('win_rate')}",
        f"- avg cost-adjusted return: {best.get('avg_cost_adjusted_return')}",
        f"- lift vs momentum: {best.get('lift_vs_momentum')}",
        f"- exit conflict rate: {best.get('exit_conflict_rate')}",
        f"- failed gates: {', '.join(best.get('failed_gates') or []) or 'none'}",
        "",
        f"Operator action: {scorecard.get('operator_action')}",
    ]
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only promotion gate for TS forecast paper evidence.")
    parser.add_argument("--format", choices=["json", "text"], default="text")
    parser.add_argument("--from-date", type=parse_datetime_arg)
    parser.add_argument("--to-date", type=parse_datetime_arg)
    parser.add_argument("--model-name")
    parser.add_argument("--horizon-days", type=int)
    parser.add_argument("--min-evaluated-trades", type=int, default=DEFAULT_MIN_EVALUATED_TRADES)
    parser.add_argument("--min-win-rate", type=float, default=DEFAULT_MIN_WIN_RATE)
    parser.add_argument("--min-avg-cost-adjusted-return", type=float, default=DEFAULT_MIN_AVG_COST_ADJUSTED_RETURN)
    parser.add_argument("--min-lift-vs-momentum", type=float, default=DEFAULT_MIN_LIFT_VS_MOMENTUM)
    parser.add_argument("--max-exit-conflict-rate", type=float, default=DEFAULT_MAX_EXIT_CONFLICT_RATE)
    parser.add_argument("--min-distinct-dates", type=int, default=DEFAULT_MIN_DISTINCT_DATES)
    parser.add_argument("--min-symbols", type=int, default=DEFAULT_MIN_SYMBOLS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = build_promotion_check(args)
    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default))
    else:
        print(format_text(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
