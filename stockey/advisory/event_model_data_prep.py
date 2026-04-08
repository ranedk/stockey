from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import pandas as pd

from advisory.event_meta_model import build_labeled_event_dataset, load_event_rows
from advisory.pipeline import run_pipeline
from advisory.screener_parser import build_constituents, persist_constituents
from advisory.training_universe import sync_training_universes
from data.dhanlive.ohlcv import sync_many_daily
from utils.db import sql_to_df
from utils.sync import parse_datetime_arg


SNAPSHOT_TABLE = "public.screenerin_screener_snapshots"
CONSTITUENTS_TABLE = "advisory_screener_constituents"
DEFAULT_HORIZONS = [1, 3, 5]
DEFAULT_MIN_LABELED_ROWS = 20
TRAINING_SETUP_ID = "EVENT_MODEL_TRAINING_V1"


def _emit(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _format_elapsed(started_at: float) -> str:
    return f"{time.monotonic() - started_at:.2f}s"


def _normalize_day(value: pd.Timestamp | None) -> pd.Timestamp | None:
    if value is None:
        return None
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.normalize()


def _to_naive_utc_datetime(value: pd.Timestamp) -> pd.Timestamp:
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        raise ValueError("invalid timestamp supplied")
    return ts.tz_convert("UTC").tz_localize(None)


def load_distinct_dates(table_name: str, *, from_date: pd.Timestamp | None = None, to_date: pd.Timestamp | None = None) -> list[pd.Timestamp]:
    clauses = ["1 = 1"]
    params: list[object] = []
    if from_date is not None:
        clauses.append("date >= %s")
        params.append(from_date)
    if to_date is not None:
        clauses.append("date <= %s")
        params.append(to_date)
    df = sql_to_df(
        f"""
        SELECT DISTINCT date
        FROM {table_name}
        WHERE {' AND '.join(clauses)}
        ORDER BY date
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return []
    return [
        ts for ts in pd.to_datetime(df["date"], utc=True, errors="coerce").dt.normalize().tolist()
        if pd.notna(ts)
    ]


def normalize_missing_screener_dates(*, from_date: pd.Timestamp | None = None, to_date: pd.Timestamp | None = None, dry_run: bool = False) -> dict[str, Any]:
    snapshot_dates = load_distinct_dates(SNAPSHOT_TABLE, from_date=from_date, to_date=to_date)
    constituent_dates = set(load_distinct_dates(CONSTITUENTS_TABLE, from_date=from_date, to_date=to_date))
    missing_dates = [value for value in snapshot_dates if value not in constituent_dates]
    rows_written = 0
    for snapshot_date in missing_dates:
        _emit(f"[advisory.event_model_data_prep] normalize_screener_date start date={snapshot_date.date()}")
        started_at = time.monotonic()
        df = build_constituents(snapshot_date=snapshot_date.date(), latest_only=False)
        rows_written += int(len(df))
        if not dry_run and not df.empty:
            persist_constituents(df)
        _emit(
            f"[advisory.event_model_data_prep] normalize_screener_date done date={snapshot_date.date()} elapsed={_format_elapsed(started_at)} rows={len(df)}"
        )
    return {
        "snapshot_dates": [value.date().isoformat() for value in snapshot_dates],
        "missing_dates": [value.date().isoformat() for value in missing_dates],
        "normalized_dates": len(missing_dates),
        "rows_written": rows_written,
        "dry_run": bool(dry_run),
    }


def load_training_dates(*, from_date: pd.Timestamp | None = None, to_date: pd.Timestamp | None = None) -> list[pd.Timestamp]:
    return load_distinct_dates(CONSTITUENTS_TABLE, from_date=from_date, to_date=to_date)


def _pipeline_args(
    *,
    asof_date: pd.Timestamp,
    rebuild: bool,
    include_evaluated: bool,
    dry_run: bool,
    setup_ids: list[str] | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        date=_to_naive_utc_datetime(asof_date),
        symbols=None,
        setup_ids=setup_ids,
        start_at="rules",
        stop_at="review",
        rebuild=bool(rebuild),
        skip_peer_sync=False,
        skip_intraday=False,
        skip_intraday_prefetch=True,
        intraday_lookback_days=180,
        intraday_intervals=[1],
        rule_max_snapshot_refresh_age_days=0,
        rule_max_intraday_prefetch_age_days=0,
        include_watch=True,
        include_news=True,
        include_lifecycle=False,
        include_execution=False,
        live_execution=False,
        execution_reconcile=False,
        eval_include_evaluated=bool(include_evaluated),
        portfolio_capital_inr=300000.0,
        portfolio_max_positions=5,
        portfolio_single_position_cap_pct=0.35,
        portfolio_per_setup_cap_pct=0.50,
        portfolio_max_positions_per_overlap_group=1,
        event_model=None,
        event_model_artifact_dir=".cache/advisory_event_meta_model",
        dry_run=bool(dry_run),
    )


def backfill_event_history(
    dates: list[pd.Timestamp],
    *,
    rebuild: bool = False,
    include_evaluated: bool = False,
    dry_run: bool = False,
    setup_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    total = len(dates)
    for position, asof_date in enumerate(dates, start=1):
        _emit(f"[advisory.event_model_data_prep] pipeline {position}/{total} start date={asof_date.date()}")
        started_at = time.monotonic()
        if dry_run:
            result = {
                "date": asof_date.date().isoformat(),
                "elapsed_seconds": round(time.monotonic() - started_at, 4),
                "candidate_count": 0,
                "watch_event_count": 0,
                "matched_event_count": 0,
                "evaluation_count": 0,
                "error_count": 0,
                "status": "planned",
            }
            results.append(result)
            _emit(
                "[advisory.event_model_data_prep] "
                f"pipeline {position}/{total} planned date={asof_date.date()} elapsed={_format_elapsed(started_at)}"
            )
            continue
        summary = run_pipeline(
            _pipeline_args(
                asof_date=asof_date,
                rebuild=rebuild,
                include_evaluated=include_evaluated,
                dry_run=False,
                setup_ids=setup_ids,
            )
        )
        evaluate_meta = ((summary.get("stages") or {}).get("evaluate") or {}).get("meta") or {}
        watch_match_meta = ((summary.get("stages") or {}).get("watch_match") or {}).get("meta") or {}
        result = {
            "date": asof_date.date().isoformat(),
            "elapsed_seconds": round(time.monotonic() - started_at, 4),
            "candidate_count": int((((summary.get("stages") or {}).get("rules") or {}).get("candidates") or {}).get("row_count") or 0),
            "watch_event_count": int((((summary.get("stages") or {}).get("watch_match") or {}).get("watch_events") or {}).get("row_count") or 0),
            "matched_event_count": int(watch_match_meta.get("match_count") or 0),
            "evaluation_count": int(evaluate_meta.get("evaluated_count") or 0),
            "error_count": int(evaluate_meta.get("error_count") or 0),
            "status": summary.get("status"),
        }
        results.append(result)
        _emit(
            "[advisory.event_model_data_prep] "
            f"pipeline {position}/{total} done date={asof_date.date()} elapsed={_format_elapsed(started_at)} "
            f"watch_events={result['watch_event_count']} evaluations={result['evaluation_count']}"
        )
    return results


def refresh_event_symbol_prices(*, to_date: pd.Timestamp | None = None, dry_run: bool = False) -> dict[str, Any]:
    events = load_event_rows()
    if events.empty:
        return {
            "symbol_count": 0,
            "from_date": None,
            "to_date": None,
            "results": [],
            "dry_run": bool(dry_run),
        }
    symbols = sorted(events["symbol"].dropna().astype(str).str.upper().unique().tolist())
    from_date = events["published_on"].min().normalize() + pd.Timedelta(days=1)
    effective_to_date = _normalize_day(to_date) or pd.Timestamp.utcnow().normalize()
    if dry_run:
        return {
            "symbol_count": len(symbols),
            "from_date": from_date.date().isoformat(),
            "to_date": effective_to_date.date().isoformat(),
            "results": [],
            "dry_run": True,
        }
    _emit(
        "[advisory.event_model_data_prep] "
        f"price_refresh start symbols={len(symbols)} from={from_date.date()} to={effective_to_date.date()}"
    )
    started_at = time.monotonic()
    results = sync_many_daily(
        symbols,
        exchange="NSE",
        asset_type="stock",
        from_date=from_date.to_pydatetime(),
        to_date=effective_to_date.to_pydatetime(),
    )
    _emit(
        "[advisory.event_model_data_prep] "
        f"price_refresh done elapsed={_format_elapsed(started_at)} symbols={len(symbols)}"
    )
    return {
        "symbol_count": len(symbols),
        "from_date": from_date.date().isoformat(),
        "to_date": effective_to_date.date().isoformat(),
        "results": results,
        "dry_run": False,
    }


def summarize_label_coverage(*, horizons: list[int], min_labeled_rows: int) -> dict[str, Any]:
    coverage: list[dict[str, Any]] = []
    for horizon in horizons:
        dataset = build_labeled_event_dataset(horizon_days=horizon)
        labeled = dataset.dropna(subset=["target_label"]).copy() if not dataset.empty and "target_label" in dataset.columns else dataset
        coverage.append(
            {
                "horizon_days": int(horizon),
                "dataset_rows": int(len(dataset)),
                "labeled_rows": int(len(labeled)),
                "train_ready": bool(len(labeled) >= min_labeled_rows),
            }
        )
    return {
        "min_labeled_rows": int(min_labeled_rows),
        "coverage": coverage,
        "any_train_ready": any(item["train_ready"] for item in coverage),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare historical data needed for advisory event meta-model training.")
    parser.add_argument("--from-date", type=parse_datetime_arg, help="Optional lower bound in YYYY-MM-DD")
    parser.add_argument("--to-date", type=parse_datetime_arg, help="Optional upper bound in YYYY-MM-DD")
    parser.add_argument("--skip-screener-normalization", action="store_true", help="Skip filling missing advisory_screener_constituents dates from stored raw snapshots")
    parser.add_argument("--skip-training-universe", action="store_true", help="Skip syncing broad ad hoc training universes for the current date")
    parser.add_argument("--skip-event-backfill", action="store_true", help="Skip historical advisory rules->review backfill")
    parser.add_argument("--skip-price-refresh", action="store_true", help="Skip daily OHLCV refresh for evaluated event symbols")
    parser.add_argument("--rebuild", action="store_true", help="Use rebuild semantics when rerunning historical pipeline dates")
    parser.add_argument("--include-evaluated", action="store_true", help="Re-evaluate already evaluated historical event rows during backfill")
    parser.add_argument("--horizons", nargs="*", type=int, default=DEFAULT_HORIZONS, help="Forward-return horizons to summarize after prep")
    parser.add_argument("--min-labeled-rows", type=int, default=DEFAULT_MIN_LABELED_ROWS, help="Minimum labeled rows required before training is considered ready")
    parser.add_argument("--training-universe-config", help="Optional config path for broad ad hoc training universes")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def format_text(summary: dict[str, Any]) -> str:
    lines = [
        "Pipeline: advisory.event_model_data_prep",
        f"Date range: {summary.get('from_date')} -> {summary.get('to_date')}",
        f"Training dates: {summary.get('training_date_count')}",
    ]
    training_universe = summary.get("training_universe") or {}
    screener = summary.get("screener_normalization") or {}
    lines.extend(
        [
            "",
            "Training universe:",
            f"- synced_date: {training_universe.get('asof_date')}",
            f"- screener_count: {training_universe.get('screener_count')}",
            f"- row_count: {training_universe.get('row_count')}",
        ]
    )
    training_setup = summary.get("training_setup_backfill") or {}
    lines.extend(
        [
            "",
            "Training setup backfill:",
            f"- dates_run: {training_setup.get('dates_run')}",
            f"- total_watch_events: {training_setup.get('total_watch_events')}",
            f"- total_evaluations: {training_setup.get('total_evaluations')}",
        ]
    )
    lines.extend(
        [
            "",
            "Screener normalization:",
            f"- missing_dates: {screener.get('missing_dates')}",
            f"- normalized_dates: {screener.get('normalized_dates')}",
            f"- rows_written: {screener.get('rows_written')}",
        ]
    )
    backfill = summary.get("event_backfill") or {}
    lines.extend(
        [
            "",
            "Event backfill:",
            f"- dates_run: {backfill.get('dates_run')}",
            f"- total_watch_events: {backfill.get('total_watch_events')}",
            f"- total_evaluations: {backfill.get('total_evaluations')}",
        ]
    )
    prices = summary.get("price_refresh") or {}
    lines.extend(
        [
            "",
            "Price refresh:",
            f"- symbol_count: {prices.get('symbol_count')}",
            f"- from_date: {prices.get('from_date')}",
            f"- to_date: {prices.get('to_date')}",
        ]
    )
    lines.append("")
    lines.append("Label coverage:")
    for item in (summary.get("label_coverage") or {}).get("coverage") or []:
        lines.append(
            f"- horizon={item.get('horizon_days')}d rows={item.get('dataset_rows')} labeled={item.get('labeled_rows')} train_ready={item.get('train_ready')}"
        )
    lines.append(f"Any train ready: {(summary.get('label_coverage') or {}).get('any_train_ready')}")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    from_date = _normalize_day(pd.Timestamp(args.from_date, tz="UTC")) if args.from_date else None
    to_date = _normalize_day(pd.Timestamp(args.to_date, tz="UTC")) if args.to_date else None
    if from_date is not None and to_date is not None and from_date > to_date:
        raise SystemExit("--from-date cannot be after --to-date")

    summary: dict[str, Any] = {
        "status": "ok",
        "pipeline": "advisory.event_model_data_prep",
        "from_date": None if from_date is None else from_date.date().isoformat(),
        "to_date": None if to_date is None else to_date.date().isoformat(),
        "dry_run": bool(args.dry_run),
    }

    effective_training_date = to_date or pd.Timestamp.utcnow().normalize()
    today_utc = pd.Timestamp.utcnow().normalize()
    if not args.skip_training_universe and effective_training_date >= today_utc:
        _emit(
            "[advisory.event_model_data_prep] "
            f"training_universe start date={effective_training_date.date()}"
        )
        started_at = time.monotonic()
        summary["training_universe"] = sync_training_universes(
            asof_date=effective_training_date,
            config_path=args.training_universe_config,
            dry_run=bool(args.dry_run),
        )
        _emit(
            "[advisory.event_model_data_prep] "
            f"training_universe done elapsed={_format_elapsed(started_at)} "
            f"screeners={summary['training_universe'].get('screener_count')} "
            f"rows={summary['training_universe'].get('row_count')}"
        )
    else:
        summary["training_universe"] = {
            "status": "skipped",
            "reason": "skip flag or non-causal historical date range",
            "asof_date": None if effective_training_date is None else effective_training_date.date().isoformat(),
        }

    if not args.skip_screener_normalization:
        summary["screener_normalization"] = normalize_missing_screener_dates(
            from_date=from_date,
            to_date=to_date,
            dry_run=bool(args.dry_run),
        )
    else:
        summary["screener_normalization"] = {"status": "skipped"}

    training_dates = load_training_dates(from_date=from_date, to_date=to_date)
    summary["training_dates"] = [value.date().isoformat() for value in training_dates]
    summary["training_date_count"] = len(training_dates)
    _emit(
        "[advisory.event_model_data_prep] "
        f"training_dates loaded count={len(training_dates)}"
    )

    if not args.skip_event_backfill:
        results = backfill_event_history(
            training_dates,
            rebuild=bool(args.rebuild),
            include_evaluated=bool(args.include_evaluated),
            dry_run=bool(args.dry_run),
        )
        summary["event_backfill"] = {
            "dates_run": len(results),
            "results": results,
            "total_watch_events": int(sum(item.get("watch_event_count", 0) for item in results)),
            "total_evaluations": int(sum(item.get("evaluation_count", 0) for item in results)),
        }
    else:
        summary["event_backfill"] = {"status": "skipped"}

    training_setup_dates: list[pd.Timestamp] = []
    training_universe = summary.get("training_universe") or {}
    if training_universe.get("asof_date"):
        training_setup_dates = [pd.Timestamp(training_universe["asof_date"], tz="UTC")]
    if training_setup_dates and not args.skip_event_backfill:
        results = backfill_event_history(
            training_setup_dates,
            rebuild=bool(args.rebuild),
            include_evaluated=bool(args.include_evaluated),
            dry_run=bool(args.dry_run),
            setup_ids=[TRAINING_SETUP_ID],
        )
        summary["training_setup_backfill"] = {
            "setup_id": TRAINING_SETUP_ID,
            "dates_run": len(results),
            "results": results,
            "total_watch_events": int(sum(item.get("watch_event_count", 0) for item in results)),
            "total_evaluations": int(sum(item.get("evaluation_count", 0) for item in results)),
        }
    else:
        summary["training_setup_backfill"] = {"status": "skipped"}

    if not args.skip_price_refresh:
        summary["price_refresh"] = refresh_event_symbol_prices(to_date=to_date, dry_run=bool(args.dry_run))
    else:
        summary["price_refresh"] = {"status": "skipped"}

    _emit(
        "[advisory.event_model_data_prep] "
        f"label_coverage start horizons={','.join(str(value) for value in ([value for value in args.horizons if value > 0] or DEFAULT_HORIZONS))}"
    )
    coverage_started_at = time.monotonic()
    summary["label_coverage"] = summarize_label_coverage(
        horizons=[value for value in args.horizons if value > 0] or DEFAULT_HORIZONS,
        min_labeled_rows=int(args.min_labeled_rows),
    )
    _emit(
        "[advisory.event_model_data_prep] "
        f"label_coverage done elapsed={_format_elapsed(coverage_started_at)} "
        f"train_ready={(summary.get('label_coverage') or {}).get('any_train_ready')}"
    )

    if args.format == "text":
        print(format_text(summary))
    else:
        print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
