from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from advisory.announcement_watch import persist_watch_outputs, run_announcement_watch
from advisory.execution_engine import (
    build_execution_orders,
    persist_execution_orders,
    persist_reconciliation,
    reconcile_live_orders,
    submit_live_orders,
)
from advisory.fundamental_snapshot import build_fundamental_snapshot, persist_fundamental_snapshot
from advisory.llm_event_evaluator import DEFAULT_MODEL as DEFAULT_EVENT_MODEL
from advisory.llm_event_evaluator import build_outputs as build_event_evaluations
from advisory.llm_event_evaluator import persist_outputs as persist_event_evaluations
from advisory.macro_snapshot import build_macro_snapshot, persist_macro_snapshot
from advisory.news_watch import persist_news_events, run_news_watch
from advisory.peer_sync import sync_peer_data
from advisory.portfolio_engine import build_portfolio_orders, persist_portfolio_orders, PortfolioConfig
from advisory.position_lifecycle import build_lifecycle_outputs, persist_outputs as persist_lifecycle_outputs
from advisory.regime_engine import build_regime_snapshot, persist_regime_snapshot
from advisory.risk_engine import build_allocations, persist_allocations
from advisory.rule_engine import persist_rule_outputs, run_rule_engine
from advisory.screener_parser import build_constituents, persist_constituents
from advisory.technical_features import build_technical_features, persist_technical_features
from advisory.watchlist_builder import build_watchlist, persist_watchlist
from utils.sync import parse_datetime_arg


PIPELINE_STAGES = [
    "screeners",
    "macro",
    "peer_sync",
    "technicals",
    "fundamentals",
    "regime",
    "rules",
    "watchlist",
    "watch",
    "news",
    "evaluate",
    "risk",
    "portfolio",
    "lifecycle",
    "execution",
]


def _json_ready(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, pd.DataFrame):
        return {
            "row_count": int(len(value)),
            "sample": value.head(5).to_dict(orient="records") if not value.empty else [],
        }
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_ready(v) for v in value]
    return value


def parse_stage(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized not in PIPELINE_STAGES:
        raise argparse.ArgumentTypeError(f"Unsupported stage: {value}. Choose from {', '.join(PIPELINE_STAGES)}")
    return normalized


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the full advisory pipeline end to end.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Pipeline asof date in YYYY-MM-DD")
    parser.add_argument("--symbols", nargs="*", help="Optional symbols")
    parser.add_argument("--setup", dest="setup_ids", nargs="*", help="Optional setup ids")
    parser.add_argument("--start-at", type=parse_stage, choices=PIPELINE_STAGES, help="Optional stage to start from")
    parser.add_argument("--stop-at", type=parse_stage, choices=PIPELINE_STAGES, help="Optional stage to stop after")
    parser.add_argument("--rebuild", action="store_true", help="Use rebuild semantics where supported")
    parser.add_argument("--skip-peer-sync", action="store_true", help="Skip peer preflight before technicals/fundamentals")
    parser.add_argument("--include-watch", action="store_true", help="Run watch/event stages after rules")
    parser.add_argument("--include-news", action="store_true", help="Run ET RSS ingest and watch matching after watchlist")
    parser.add_argument("--include-lifecycle", action="store_true", help="Run paper-position lifecycle after portfolio planning")
    parser.add_argument("--include-execution", action="store_true", help="Build execution orders after portfolio planning")
    parser.add_argument("--live-execution", action="store_true", help="Submit execution orders live through Dhan when execution stage runs")
    parser.add_argument("--execution-reconcile", action="store_true", help="Reconcile broker execution state after execution planning")
    parser.add_argument("--eval-include-evaluated", action="store_true", help="Re-evaluate already evaluated watch events")
    parser.add_argument("--portfolio-capital-inr", type=float, default=300000.0)
    parser.add_argument("--portfolio-max-positions", type=int, default=5)
    parser.add_argument("--portfolio-single-position-cap-pct", type=float, default=0.35)
    parser.add_argument("--portfolio-per-setup-cap-pct", type=float, default=0.50)
    parser.add_argument("--portfolio-max-positions-per-overlap-group", type=int, default=1)
    parser.add_argument("--event-model", help="Override advisory LLM event evaluation model")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def stage_enabled(stage: str, start_at: str | None, stop_at: str | None) -> bool:
    idx = PIPELINE_STAGES.index(stage)
    if start_at is not None and idx < PIPELINE_STAGES.index(start_at):
        return False
    if stop_at is not None and idx > PIPELINE_STAGES.index(stop_at):
        return False
    return True


def main() -> int:
    args = parse_args()
    asof_date = pd.Timestamp(args.date, tz="UTC") if args.date else None
    summary: dict[str, Any] = {
        "status": "ok",
        "pipeline": "advisory.pipeline",
        "asof_date": None if asof_date is None else asof_date.isoformat(),
        "dry_run": bool(args.dry_run),
        "stages": {},
    }

    symbols = args.symbols
    setup_ids = args.setup_ids

    if stage_enabled("screeners", args.start_at, args.stop_at):
        screener_df = build_constituents(snapshot_date=None if asof_date is None else asof_date.date(), latest_only=asof_date is None)
        if not args.dry_run:
            persist_constituents(screener_df)
        summary["stages"]["screeners"] = _json_ready(screener_df)

    if stage_enabled("macro", args.start_at, args.stop_at):
        macro_df = build_macro_snapshot(from_date=asof_date, to_date=asof_date, rebuild=bool(args.rebuild))
        if not args.dry_run:
            persist_macro_snapshot(macro_df, rebuild=bool(args.rebuild))
        summary["stages"]["macro"] = _json_ready(macro_df)

    if stage_enabled("peer_sync", args.start_at, args.stop_at) and not args.skip_peer_sync:
        peer_result = sync_peer_data(symbols=symbols, to_date=asof_date)
        summary["stages"]["peer_sync"] = _json_ready(peer_result)

    if stage_enabled("technicals", args.start_at, args.stop_at):
        technical_df = build_technical_features(
            symbols=symbols,
            from_date=asof_date,
            to_date=asof_date,
            rebuild=bool(args.rebuild),
        )
        if not args.dry_run:
            persist_technical_features(technical_df, rebuild=bool(args.rebuild), symbols=symbols)
        summary["stages"]["technicals"] = _json_ready(technical_df)

    if stage_enabled("fundamentals", args.start_at, args.stop_at):
        fundamental_df = build_fundamental_snapshot(
            symbols=symbols,
            from_date=asof_date,
            to_date=asof_date,
            rebuild=bool(args.rebuild),
        )
        if not args.dry_run:
            persist_fundamental_snapshot(fundamental_df)
        summary["stages"]["fundamentals"] = _json_ready(fundamental_df)

    if stage_enabled("regime", args.start_at, args.stop_at):
        regime_df = build_regime_snapshot(from_date=asof_date, to_date=asof_date)
        if not args.dry_run:
            persist_regime_snapshot(regime_df, rebuild=bool(args.rebuild))
        summary["stages"]["regime"] = _json_ready(regime_df)

    candidates = pd.DataFrame()
    rejections = pd.DataFrame()
    meta: dict[str, Any] = {}
    if stage_enabled("rules", args.start_at, args.stop_at):
        candidates, rejections, meta = run_rule_engine(
            asof_date=asof_date,
            setup_ids=setup_ids,
            config_path=None,
        )
        effective_date = pd.to_datetime(meta.get("effective_date"), utc=True, errors="coerce")
        if not args.dry_run:
            persist_rule_outputs(
                candidates,
                rejections,
                asof_date=None if pd.isna(effective_date) else effective_date,
                rebuild=bool(args.rebuild),
            )
        summary["stages"]["rules"] = {
            "meta": _json_ready(meta),
            "candidates": _json_ready(candidates),
            "rejections": _json_ready(rejections),
        }

    if args.include_watch:
        if stage_enabled("watchlist", args.start_at, args.stop_at):
            watchlist_df = build_watchlist(asof_date=asof_date, setup_ids=setup_ids, symbols=symbols)
            if not args.dry_run:
                persist_watchlist(watchlist_df, rebuild=bool(args.rebuild), asof_date=asof_date or (watchlist_df["asof_date"].max() if not watchlist_df.empty else None))
            summary["stages"]["watchlist"] = _json_ready(watchlist_df)

        if stage_enabled("watch", args.start_at, args.stop_at):
            watch_updates, watch_events, watch_meta = run_announcement_watch(
                asof_date=asof_date,
                symbols=symbols,
                setup_ids=setup_ids,
                to_date=asof_date,
            )
            if not args.dry_run:
                persist_watch_outputs(watch_updates, watch_events)
            summary["stages"]["watch"] = {
                "meta": _json_ready(watch_meta),
                "watch_updates": _json_ready(watch_updates),
                "watch_events": _json_ready(watch_events),
            }

        if args.include_news and stage_enabled("news", args.start_at, args.stop_at):
            news_events, news_meta = run_news_watch(
                asof_date=asof_date,
                symbols=symbols,
                setup_ids=setup_ids,
                to_date=asof_date,
                refresh_feeds=not args.dry_run,
            )
            if not args.dry_run:
                persist_news_events(news_events)
            summary["stages"]["news"] = {
                "meta": _json_ready(news_meta),
                "news_events": _json_ready(news_events),
            }

        if stage_enabled("evaluate", args.start_at, args.stop_at):
            from advisory.llm_event_evaluator import load_watch_events

            events = load_watch_events(
                asof_date=asof_date,
                symbols=symbols,
                setup_ids=setup_ids,
                include_evaluated=bool(args.eval_include_evaluated),
                limit=None,
            )
            eval_df, risks_df, eval_meta = build_event_evaluations(
                events,
                model=args.event_model or DEFAULT_EVENT_MODEL,
            )
            if not args.dry_run:
                persist_event_evaluations(eval_df, risks_df)
            summary["stages"]["evaluate"] = {
                "meta": _json_ready(eval_meta),
                "evaluations": _json_ready(eval_df),
                "risks": _json_ready(risks_df),
            }

    if stage_enabled("risk", args.start_at, args.stop_at):
        allocations_df = build_allocations(asof_date=asof_date, symbols=symbols, setup_ids=setup_ids, include_allocated=bool(args.rebuild))
        if not args.dry_run:
            persist_allocations(allocations_df)
        summary["stages"]["risk"] = _json_ready(allocations_df)

    if stage_enabled("portfolio", args.start_at, args.stop_at):
        portfolio_df = build_portfolio_orders(
            asof_date=asof_date,
            symbols=symbols,
            setup_ids=setup_ids,
            include_planned=bool(args.rebuild),
            config=PortfolioConfig(
                capital_inr=float(args.portfolio_capital_inr),
                max_positions=int(args.portfolio_max_positions),
                single_position_cap_pct=float(args.portfolio_single_position_cap_pct),
                per_setup_cap_pct=float(args.portfolio_per_setup_cap_pct),
                max_positions_per_overlap_group=int(args.portfolio_max_positions_per_overlap_group),
            ),
        )
        if not args.dry_run:
            persist_portfolio_orders(portfolio_df)
        summary["stages"]["portfolio"] = _json_ready(portfolio_df)

    if args.include_lifecycle and stage_enabled("lifecycle", args.start_at, args.stop_at):
        lifecycle_df, rebalance_df = build_lifecycle_outputs(asof_date=asof_date, symbols=symbols, setup_ids=setup_ids)
        if not args.dry_run:
            persist_lifecycle_outputs(lifecycle_df, rebalance_df)
        summary["stages"]["lifecycle"] = {
            "lifecycle": _json_ready(lifecycle_df),
            "rebalance": _json_ready(rebalance_df),
        }

    if args.include_execution and stage_enabled("execution", args.start_at, args.stop_at):
        execution_df = build_execution_orders(
            asof_date=asof_date,
            symbols=symbols,
            setup_ids=setup_ids,
            include_existing=bool(args.rebuild),
        )
        if args.live_execution:
            execution_df = submit_live_orders(execution_df)
        if not args.dry_run:
            persist_execution_orders(execution_df)

        reconcile_df = pd.DataFrame()
        fills_df = pd.DataFrame()
        if args.live_execution or args.execution_reconcile:
            reconcile_df, fills_df = reconcile_live_orders(asof_date=asof_date, symbols=symbols, setup_ids=setup_ids)
            if not args.dry_run:
                persist_reconciliation(reconcile_df, fills_df)
        summary["stages"]["execution"] = {
            "planned": _json_ready(execution_df),
            "reconciled": _json_ready(reconcile_df),
            "fills": _json_ready(fills_df),
        }

    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
