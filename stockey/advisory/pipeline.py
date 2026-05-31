from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import threading
import time
from typing import Any

import pandas as pd

from advisory.announcement_watch import build_watch_updates_from_ingest, persist_watch_outputs, run_announcement_ingest
from advisory.action_recommender import build_action_recommendations, persist_action_recommendations
from advisory.adversarial_review import build_reviews as build_adversarial_reviews
from advisory.adversarial_review import persist_reviews as persist_adversarial_reviews
from advisory.execution_engine import (
    build_execution_orders,
    persist_execution_orders,
    persist_reconciliation,
    reconcile_live_orders,
    submit_live_orders,
)
from advisory.event_meta_model import (
    DEFAULT_ARTIFACT_DIR as DEFAULT_EVENT_MODEL_ARTIFACT_DIR,
    build_live_event_dataset,
    model_artifact_exists,
    persist_scores as persist_event_model_scores,
    score_events as score_event_model,
)
from advisory.event_policy import build_event_policy_actions, load_policy_inputs, persist_event_policy_actions
from advisory.exchange_events import build_exchange_events, persist_exchange_events
from advisory.exchange_features import build_exchange_features, persist_exchange_features
from advisory.fundamental_snapshot import build_fundamental_snapshot, persist_fundamental_snapshot
from advisory.intraday_features import (
    build_intraday_features,
    persist_intraday_features,
)
from advisory.llm_event_evaluator import DEFAULT_MODEL as DEFAULT_EVENT_MODEL
from advisory.llm_event_evaluator import build_outputs as build_event_evaluations
from advisory.llm_event_evaluator import persist_outputs as persist_event_evaluations
from advisory.macro_features import build_macro_features, persist_macro_features
from advisory.macro_snapshot import build_macro_snapshot, persist_macro_snapshot
from advisory.market_context import build_market_context, persist_market_context
from advisory.news_overlay_engine import build_overlay_state, persist_overlay_state
from advisory.news_theme_engine import build_theme_recommendations, load_active_theme_screener_mapping
from advisory.news_watch import persist_news_events, run_news_watch
from advisory.peer_sync import sync_peer_data
from advisory.portfolio_engine import build_portfolio_orders, persist_portfolio_orders, PortfolioConfig
from advisory.position_lifecycle import build_lifecycle_outputs, persist_outputs as persist_lifecycle_outputs
from advisory.regime_engine import build_regime_snapshot, persist_regime_snapshot
from advisory.research_ledger import (
    build_data_snapshot as build_research_data_snapshot,
    build_result_metrics as build_research_result_metrics,
    finish_research_run,
    parse_validation_protocol,
    start_research_run,
)
from advisory.risk_engine import build_allocations, persist_allocations
from advisory.rule_engine import persist_rule_outputs, run_rule_engine
from advisory.screener_parser import build_constituents, persist_constituents
from advisory.technical_features import build_technical_features, persist_technical_features
from advisory.watchlist_builder import build_watchlist, persist_watchlist
from utils.sync import parse_datetime_arg


PIPELINE_STAGES = [
    "screeners",
    "macro",
    "macro_features",
    "exchange_events",
    "exchange_features",
    "peer_sync",
    "technicals",
    "intraday",
    "fundamentals",
    "regime",
    "market_context",
    "overlay",
    "themes",
    "rules",
    "watchlist",
    "watch_ingest",
    "watch_match",
    "news",
    "evaluate",
    "event_model",
    "review",
    "event_policy",
    "risk",
    "portfolio",
    "lifecycle",
    "actions",
    "execution",
]
LEGACY_STAGE_ALIASES = {
    "watch": ("watch_ingest", "watch_match"),
}
HEARTBEAT_INTERVAL_SECONDS = 30.0


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


def _emit_progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _start_stage(stage: str) -> tuple[float, threading.Event, threading.Thread]:
    _emit_progress(f"[advisory.pipeline] stage={stage} start")
    started_at = time.monotonic()
    stop_event = threading.Event()

    def _heartbeat() -> None:
        while not stop_event.wait(HEARTBEAT_INTERVAL_SECONDS):
            _emit_progress(f"[advisory.pipeline] stage={stage} running elapsed={time.monotonic() - started_at:.2f}s")

    thread = threading.Thread(target=_heartbeat, name=f"advisory-pipeline-{stage}-heartbeat", daemon=True)
    thread.start()
    return started_at, stop_event, thread


def _finish_stage(stage: str, stage_state: tuple[float, threading.Event, threading.Thread], detail: str | None = None) -> None:
    started_at, stop_event, thread = stage_state
    stop_event.set()
    thread.join(timeout=0.1)
    suffix = f" {detail}" if detail else ""
    _emit_progress(f"[advisory.pipeline] stage={stage} done elapsed={time.monotonic() - started_at:.2f}s{suffix}")


def parse_stage(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized not in PIPELINE_STAGES and normalized not in LEGACY_STAGE_ALIASES:
        supported = PIPELINE_STAGES + list(LEGACY_STAGE_ALIASES)
        raise argparse.ArgumentTypeError(f"Unsupported stage: {value}. Choose from {', '.join(supported)}")
    return normalized


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the full advisory pipeline end to end.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Pipeline asof date in YYYY-MM-DD")
    parser.add_argument("--symbols", nargs="*", help="Optional symbols")
    parser.add_argument("--setup", dest="setup_ids", nargs="*", help="Optional setup ids")
    parser.add_argument("--start-at", type=parse_stage, help="Optional stage to start from")
    parser.add_argument("--stop-at", type=parse_stage, help="Optional stage to stop after")
    parser.add_argument("--rebuild", action="store_true", help="Use rebuild semantics where supported")
    parser.add_argument("--skip-peer-sync", action="store_true", help="Skip peer preflight before technicals/fundamentals")
    parser.add_argument("--skip-intraday", action="store_true", help="Skip intraday feature sync/build")
    parser.add_argument("--skip-rule-snapshot-refresh", action="store_true", help="Skip on-demand daily/fundamental repair inside the rule engine")
    parser.add_argument("--skip-intraday-prefetch", action="store_true", help="Skip on-demand intraday feature backfill inside the rule engine")
    parser.add_argument("--intraday-lookback-days", type=int, default=180, help="How much recent intraday history to maintain for advisory pattern features")
    parser.add_argument("--rule-max-snapshot-refresh-age-days", type=int, default=7, help="Only repair missing daily/fundamental snapshots on the fly when the screener date is this recent; use -1 to always allow")
    parser.add_argument("--rule-max-intraday-prefetch-age-days", type=int, default=14, help="Only prefetch missing intraday features on the fly when the screener date is this recent; use -1 to always allow")
    parser.add_argument(
        "--intraday-intervals",
        nargs="*",
        type=int,
        default=[1],
        help="Intraday candle intervals to fetch/build for advisory intraday features",
    )
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
    parser.add_argument("--event-model-artifact-dir", default=str(DEFAULT_EVENT_MODEL_ARTIFACT_DIR), help="Directory containing trained event meta-model artifacts")
    parser.add_argument("--log-research-ledger", action="store_true", help="Record this run in the advisory research ledger")
    parser.add_argument("--ledger-label", help="Optional research-ledger label")
    parser.add_argument("--ledger-objective", help="Optional research-ledger objective")
    parser.add_argument("--ledger-validation-protocol", help="Optional JSON string describing validation protocol")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def stage_enabled(stage: str, start_at: str | None, stop_at: str | None) -> bool:
    idx = PIPELINE_STAGES.index(stage)
    normalized_start = LEGACY_STAGE_ALIASES.get(start_at, (start_at, start_at))[0] if start_at is not None else None
    normalized_stop = LEGACY_STAGE_ALIASES.get(stop_at, (stop_at, stop_at))[1] if stop_at is not None else None
    if normalized_start is not None and idx < PIPELINE_STAGES.index(normalized_start):
        return False
    if normalized_stop is not None and idx > PIPELINE_STAGES.index(normalized_stop):
        return False
    return True


def _normalize_utc_arg_timestamp(value: Any) -> pd.Timestamp | None:
    if value is None:
        return None
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        return None
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def run_pipeline(args: argparse.Namespace) -> dict[str, Any]:
    if not hasattr(args, "skip_intraday"):
        args.skip_intraday = False
    if not hasattr(args, "intraday_lookback_days"):
        args.intraday_lookback_days = 180
    if not hasattr(args, "intraday_intervals"):
        args.intraday_intervals = [1]
    if not hasattr(args, "skip_intraday_prefetch"):
        args.skip_intraday_prefetch = False
    if not hasattr(args, "skip_rule_snapshot_refresh"):
        args.skip_rule_snapshot_refresh = False
    if not hasattr(args, "rule_max_snapshot_refresh_age_days"):
        args.rule_max_snapshot_refresh_age_days = 7
    if not hasattr(args, "rule_max_intraday_prefetch_age_days"):
        args.rule_max_intraday_prefetch_age_days = 14
    if not hasattr(args, "event_model_artifact_dir"):
        args.event_model_artifact_dir = str(DEFAULT_EVENT_MODEL_ARTIFACT_DIR)
    asof_date = _normalize_utc_arg_timestamp(args.date)
    summary: dict[str, Any] = {
        "status": "ok",
        "pipeline": "advisory.pipeline",
        "asof_date": None if asof_date is None else asof_date.isoformat(),
        "dry_run": bool(args.dry_run),
        "stages": {},
    }

    symbols = args.symbols
    setup_ids = args.setup_ids
    screener_symbols: list[str] | None = None

    if stage_enabled("screeners", args.start_at, args.stop_at):
        stage_started = _start_stage("screeners")
        screener_df = build_constituents(snapshot_date=None if asof_date is None else asof_date.date(), latest_only=asof_date is None)
        if not args.dry_run:
            persist_constituents(screener_df)
        screener_symbol_col = None
        if not screener_df.empty:
            if "symbol" in screener_df.columns:
                screener_symbol_col = "symbol"
            elif "ticker" in screener_df.columns:
                screener_symbol_col = "ticker"
        if screener_symbol_col is not None:
            screener_symbols = (
                screener_df[screener_symbol_col]
                .astype("string")
                .dropna()
                .str.strip()
                .str.upper()
                .drop_duplicates()
                .tolist()
            )
        summary["stages"]["screeners"] = _json_ready(screener_df)
        _finish_stage("screeners", stage_started, f"rows={len(screener_df)}")
        advisory_symbols = symbols or screener_symbols

    advisory_symbols = symbols or screener_symbols

    if stage_enabled("macro", args.start_at, args.stop_at):
        stage_started = _start_stage("macro")
        macro_df = build_macro_snapshot(from_date=asof_date, to_date=asof_date, rebuild=bool(args.rebuild))
        if not args.dry_run:
            persist_macro_snapshot(macro_df)
        summary["stages"]["macro"] = _json_ready(macro_df)
        _finish_stage("macro", stage_started, f"rows={len(macro_df)}")

    if stage_enabled("macro_features", args.start_at, args.stop_at):
        stage_started = _start_stage("macro_features")
        macro_features_df = build_macro_features(from_date=asof_date, to_date=asof_date, rebuild=bool(args.rebuild))
        if not args.dry_run:
            persist_macro_features(macro_features_df)
        summary["stages"]["macro_features"] = _json_ready(macro_features_df)
        _finish_stage("macro_features", stage_started, f"rows={len(macro_features_df)}")

    if stage_enabled("exchange_events", args.start_at, args.stop_at):
        stage_started = _start_stage("exchange_events")
        exchange_events_df = build_exchange_events(from_date=asof_date, to_date=asof_date)
        if not args.dry_run:
            persist_exchange_events(exchange_events_df)
        summary["stages"]["exchange_events"] = _json_ready(exchange_events_df)
        _finish_stage("exchange_events", stage_started, f"rows={len(exchange_events_df)}")

    if stage_enabled("exchange_features", args.start_at, args.stop_at):
        stage_started = _start_stage("exchange_features")
        exchange_feature_symbols = advisory_symbols or None
        exchange_features_df = build_exchange_features(
            from_date=asof_date,
            to_date=asof_date,
            symbols=exchange_feature_symbols,
            rebuild=bool(args.rebuild),
        )
        if not args.dry_run:
            persist_exchange_features(exchange_features_df)
        summary["stages"]["exchange_features"] = _json_ready(exchange_features_df)
        _finish_stage(
            "exchange_features",
            stage_started,
            f"rows={len(exchange_features_df)} symbols={0 if exchange_features_df.empty else int(exchange_features_df['symbol'].nunique())}",
        )

    if stage_enabled("peer_sync", args.start_at, args.stop_at) and not args.skip_peer_sync:
        stage_started = _start_stage("peer_sync")
        peer_result = sync_peer_data(symbols=advisory_symbols, to_date=asof_date)
        summary["stages"]["peer_sync"] = _json_ready(peer_result)
        _finish_stage("peer_sync", stage_started)

    if stage_enabled("technicals", args.start_at, args.stop_at):
        stage_started = _start_stage("technicals")
        technical_df = build_technical_features(
            symbols=advisory_symbols,
            from_date=asof_date,
            to_date=asof_date,
            rebuild=bool(args.rebuild),
        )
        if not args.dry_run:
            persist_technical_features(technical_df, rebuild=bool(args.rebuild), symbols=advisory_symbols)
        summary["stages"]["technicals"] = _json_ready(technical_df)
        _finish_stage("technicals", stage_started, f"rows={len(technical_df)}")

    if stage_enabled("intraday", args.start_at, args.stop_at) and not args.skip_intraday:
        stage_started = _start_stage("intraday")
        intraday_symbols = advisory_symbols or []
        intervals = tuple(sorted({int(value) for value in (args.intraday_intervals or [1]) if int(value) > 0}))
        intraday_frames: list[pd.DataFrame] = []
        intraday_meta: list[dict[str, Any]] = []
        if intraday_symbols:
            for interval_minutes in intervals:
                interval_df, interval_meta = build_intraday_features(
                    symbols=intraday_symbols,
                    asof_date=asof_date,
                    lookback_days=int(args.intraday_lookback_days),
                    interval_minutes=interval_minutes,
                    ensure_history=not bool(args.dry_run),
                )
                intraday_frames.append(interval_df)
                intraday_meta.append(interval_meta)
            intraday_df = pd.concat([frame for frame in intraday_frames if not frame.empty], ignore_index=True) if any(not frame.empty for frame in intraday_frames) else pd.DataFrame()
            if not args.dry_run and not intraday_df.empty:
                persist_intraday_features(
                    intraday_df,
                    rebuild=bool(args.rebuild),
                    asof_date=asof_date,
                )
        else:
            intraday_df = pd.DataFrame()
        summary["stages"]["intraday"] = {
            "features": _json_ready(intraday_df),
            "meta": _json_ready(intraday_meta),
        }
        _finish_stage("intraday", stage_started, f"rows={len(intraday_df)} symbols={len(intraday_symbols)}")

    if stage_enabled("fundamentals", args.start_at, args.stop_at):
        stage_started = _start_stage("fundamentals")
        fundamental_df = build_fundamental_snapshot(
            symbols=advisory_symbols,
            from_date=asof_date,
            to_date=asof_date,
            rebuild=bool(args.rebuild),
        )
        if not args.dry_run:
            persist_fundamental_snapshot(fundamental_df)
        summary["stages"]["fundamentals"] = _json_ready(fundamental_df)
        _finish_stage("fundamentals", stage_started, f"rows={len(fundamental_df)}")

    if stage_enabled("regime", args.start_at, args.stop_at):
        stage_started = _start_stage("regime")
        regime_df = build_regime_snapshot(from_date=asof_date, to_date=asof_date)
        if not args.dry_run:
            persist_regime_snapshot(regime_df, rebuild=bool(args.rebuild))
        summary["stages"]["regime"] = _json_ready(regime_df)
        _finish_stage("regime", stage_started, f"rows={len(regime_df)}")

    if stage_enabled("market_context", args.start_at, args.stop_at):
        stage_started = _start_stage("market_context")
        market_context_universe_df, market_context_summary_df = build_market_context(asof_date=asof_date)
        if not args.dry_run:
            persist_market_context(market_context_universe_df, market_context_summary_df)
        summary["stages"]["market_context"] = {
            "universe": _json_ready(market_context_universe_df),
            "summary": _json_ready(market_context_summary_df),
        }
        _finish_stage(
            "market_context",
            stage_started,
            f"universe_rows={len(market_context_universe_df)} summary_rows={len(market_context_summary_df)}",
        )

    if stage_enabled("overlay", args.start_at, args.stop_at):
        stage_started = _start_stage("overlay")
        overlay_df = build_overlay_state(asof_date=asof_date)
        if not args.dry_run:
            persist_overlay_state(
                overlay_df,
                rebuild=bool(args.rebuild),
                asof_date=asof_date or (overlay_df["asof_date"].max() if not overlay_df.empty else None),
            )
        summary["stages"]["overlay"] = _json_ready(overlay_df)
        _finish_stage("overlay", stage_started, f"rows={len(overlay_df)}")

    if stage_enabled("themes", args.start_at, args.stop_at):
        stage_started = _start_stage("themes")
        theme_payload = build_theme_recommendations(asof_date=asof_date)
        theme_mapping = load_active_theme_screener_mapping(asof_date=asof_date)
        summary["stages"]["themes"] = {
            "meta": _json_ready(
                {
                    "asof_date": theme_payload.get("asof_date"),
                    "news_count": theme_payload.get("news_count"),
                    "error": theme_payload.get("error"),
                }
            ),
            "recommendations": _json_ready(theme_payload.get("recommendations") or []),
            "active_mapping": _json_ready(theme_mapping),
        }
        _finish_stage("themes", stage_started, f"active_themes={len(theme_payload.get('recommendations') or [])}")

    candidates = pd.DataFrame()
    rejections = pd.DataFrame()
    meta: dict[str, Any] = {}
    if stage_enabled("rules", args.start_at, args.stop_at):
        stage_started = _start_stage("rules")
        candidates, rejections, meta = run_rule_engine(
            asof_date=asof_date,
            setup_ids=setup_ids,
            config_path=None,
            skip_snapshot_refresh=bool(args.skip_rule_snapshot_refresh),
            skip_intraday_prefetch=bool(args.skip_intraday_prefetch),
            max_snapshot_refresh_age_days=int(args.rule_max_snapshot_refresh_age_days),
            max_intraday_prefetch_age_days=int(args.rule_max_intraday_prefetch_age_days),
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
        _finish_stage("rules", stage_started, f"candidates={len(candidates)} rejections={len(rejections)}")

    if args.include_watch:
        watch_ingest_state: dict[str, object] | None = None
        if stage_enabled("watchlist", args.start_at, args.stop_at):
            stage_started = _start_stage("watchlist")
            watchlist_df = build_watchlist(asof_date=asof_date, setup_ids=setup_ids, symbols=symbols)
            if not args.dry_run:
                persist_watchlist(watchlist_df, rebuild=bool(args.rebuild), asof_date=asof_date or (watchlist_df["asof_date"].max() if not watchlist_df.empty else None))
            summary["stages"]["watchlist"] = _json_ready(watchlist_df)
            _finish_stage("watchlist", stage_started, f"rows={len(watchlist_df)}")

        if stage_enabled("watch_ingest", args.start_at, args.stop_at):
            stage_started = _start_stage("watch_ingest")
            watch_ingest_state = run_announcement_ingest(
                asof_date=asof_date,
                symbols=symbols,
                setup_ids=setup_ids,
                to_date=asof_date,
            )
            watch_meta = dict(watch_ingest_state.get("meta") or {})
            summary["stages"]["watch_ingest"] = _json_ready(watch_meta)
            _finish_stage(
                "watch_ingest",
                stage_started,
                f"watch_count={watch_meta.get('watch_count', 0)} unique_targets={watch_meta.get('unique_ingest_targets', 0)}",
            )

        if stage_enabled("watch_match", args.start_at, args.stop_at):
            stage_started = _start_stage("watch_match")
            if watch_ingest_state is None:
                watch_ingest_state = run_announcement_ingest(
                    asof_date=asof_date,
                    symbols=symbols,
                    setup_ids=setup_ids,
                    to_date=asof_date,
                )
            watch_updates, watch_events, watch_match_meta = build_watch_updates_from_ingest(watch_ingest_state)
            if not args.dry_run:
                persist_watch_outputs(watch_updates, watch_events)
            watch_meta = dict(watch_ingest_state.get("meta") or {})
            watch_meta.update(watch_match_meta)
            summary["stages"]["watch_match"] = {
                "meta": _json_ready(watch_meta),
                "watch_updates": _json_ready(watch_updates),
                "watch_events": _json_ready(watch_events),
            }
            _finish_stage("watch_match", stage_started, f"updates={len(watch_updates)} events={len(watch_events)}")

        if args.include_news and stage_enabled("news", args.start_at, args.stop_at):
            stage_started = _start_stage("news")
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
            _finish_stage("news", stage_started, f"events={len(news_events)}")

        if stage_enabled("evaluate", args.start_at, args.stop_at):
            stage_started = _start_stage("evaluate")
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
            _finish_stage("evaluate", stage_started, f"evaluations={len(eval_df)} risks={len(risks_df)}")

        if stage_enabled("event_model", args.start_at, args.stop_at):
            stage_started = _start_stage("event_model")
            artifact_dir = Path(str(args.event_model_artifact_dir))
            if model_artifact_exists(artifact_dir=artifact_dir):
                model_dataset = build_live_event_dataset(
                    asof_date=asof_date,
                    symbols=symbols,
                    setup_ids=setup_ids,
                )
                score_df, score_meta = score_event_model(
                    dataset=model_dataset,
                    artifact_dir=artifact_dir,
                )
                if not args.dry_run:
                    persist_event_model_scores(score_df)
                summary["stages"]["event_model"] = {
                    "meta": _json_ready(score_meta),
                    "scores": _json_ready(score_df),
                }
                _finish_stage("event_model", stage_started, f"scores={len(score_df)}")
            else:
                summary["stages"]["event_model"] = {
                    "meta": {
                        "status": "skipped",
                        "reason": "model_artifact_missing",
                        "artifact_dir": str(artifact_dir),
                    }
                }
                _finish_stage("event_model", stage_started, "skipped=model_artifact_missing")

        if stage_enabled("review", args.start_at, args.stop_at):
            stage_started = _start_stage("review")
            from advisory.adversarial_review import load_event_evaluations as load_review_inputs

            review_inputs = load_review_inputs(
                asof_date=asof_date,
                symbols=symbols,
                setup_ids=setup_ids,
                include_reviewed=bool(args.eval_include_evaluated),
            )
            review_df, review_meta = build_adversarial_reviews(review_inputs)
            if not args.dry_run:
                persist_adversarial_reviews(review_df)
            summary["stages"]["review"] = {
                "meta": _json_ready(review_meta),
                "reviews": _json_ready(review_df),
            }
            _finish_stage("review", stage_started, f"reviews={len(review_df)}")

    if stage_enabled("event_policy", args.start_at, args.stop_at):
        stage_started = _start_stage("event_policy")
        policy_inputs = load_policy_inputs(asof_date=asof_date, symbols=symbols, setup_ids=setup_ids)
        policy_df, policy_meta = build_event_policy_actions(policy_inputs)
        if not args.dry_run:
            persist_event_policy_actions(policy_df)
        summary["stages"]["event_policy"] = {
            "meta": _json_ready(policy_meta),
            "policy_actions": _json_ready(policy_df),
        }
        _finish_stage("event_policy", stage_started, f"rows={len(policy_df)}")

    if stage_enabled("risk", args.start_at, args.stop_at):
        stage_started = _start_stage("risk")
        allocations_df = build_allocations(asof_date=asof_date, symbols=symbols, setup_ids=setup_ids, include_allocated=bool(args.rebuild))
        if not args.dry_run:
            persist_allocations(allocations_df)
        summary["stages"]["risk"] = _json_ready(allocations_df)
        _finish_stage("risk", stage_started, f"rows={len(allocations_df)}")

    if stage_enabled("portfolio", args.start_at, args.stop_at):
        stage_started = _start_stage("portfolio")
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
        _finish_stage("portfolio", stage_started, f"rows={len(portfolio_df)}")

    if args.include_lifecycle and stage_enabled("lifecycle", args.start_at, args.stop_at):
        stage_started = _start_stage("lifecycle")
        lifecycle_df, rebalance_df = build_lifecycle_outputs(asof_date=asof_date, symbols=symbols, setup_ids=setup_ids)
        if not args.dry_run:
            persist_lifecycle_outputs(lifecycle_df, rebalance_df)
        summary["stages"]["lifecycle"] = {
            "lifecycle": _json_ready(lifecycle_df),
            "rebalance": _json_ready(rebalance_df),
        }
        _finish_stage("lifecycle", stage_started, f"lifecycle_rows={len(lifecycle_df)} rebalance_rows={len(rebalance_df)}")

    if stage_enabled("actions", args.start_at, args.stop_at):
        stage_started = _start_stage("actions")
        actions_df = build_action_recommendations(asof_date=asof_date, symbols=symbols, setup_ids=setup_ids)
        if not args.dry_run:
            persist_action_recommendations(actions_df)
        summary["stages"]["actions"] = _json_ready(actions_df)
        _finish_stage("actions", stage_started, f"rows={len(actions_df)}")

    if args.include_execution and stage_enabled("execution", args.start_at, args.stop_at):
        stage_started = _start_stage("execution")
        execution_df = build_execution_orders(
            asof_date=asof_date,
            symbols=symbols,
            setup_ids=setup_ids,
            include_existing=bool(args.rebuild),
            use_broker_account=bool(args.live_execution),
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
        _finish_stage("execution", stage_started, f"planned={len(execution_df)} reconciled={len(reconcile_df)} fills={len(fills_df)}")

    return summary


def main() -> int:
    args = parse_args()
    if not hasattr(args, "log_research_ledger"):
        args.log_research_ledger = False
    if not hasattr(args, "ledger_label"):
        args.ledger_label = None
    if not hasattr(args, "ledger_objective"):
        args.ledger_objective = None
    if not hasattr(args, "ledger_validation_protocol"):
        args.ledger_validation_protocol = None
    asof_date = pd.Timestamp(args.date, tz="UTC") if args.date else None
    research_run_id: str | None = None
    if bool(args.log_research_ledger):
        research_run_id = start_research_run(
            run_type="advisory_pipeline",
            entrypoint="advisory.pipeline",
            config=vars(args),
            asof_date=asof_date,
            label=args.ledger_label,
            objective=args.ledger_objective,
            validation_protocol=parse_validation_protocol(args.ledger_validation_protocol),
        )
    try:
        summary = run_pipeline(args)
        if research_run_id:
            finish_research_run(
                research_run_id,
                status="completed",
                data_snapshot=build_research_data_snapshot(asof_date=asof_date, summary=summary),
                result_metrics=build_research_result_metrics(status="completed", summary=summary),
            )
        print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
        return 0
    except Exception as exc:
        if research_run_id:
            finish_research_run(
                research_run_id,
                status="failed",
                data_snapshot=None,
                result_metrics={"status": "failed"},
                error_text=f"{exc.__class__.__name__}: {exc}",
            )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
