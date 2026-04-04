from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pandas as pd

from advisory.pipeline import PIPELINE_STAGES, parse_stage, run_pipeline
from advisory.research_ledger import (
    build_data_snapshot as build_research_data_snapshot,
    build_result_metrics as build_research_result_metrics,
    finish_research_run,
    parse_validation_protocol,
    start_research_run,
)
from data.download_runner import run_all_downloads
from utils.sync import parse_datetime_arg


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DOWNLOAD_SCRIPT = REPO_ROOT / "all_downloads.sh"
HEARTBEAT_INTERVAL_SECONDS = 30.0


def _json_ready(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, pd.DataFrame):
        return value.to_dict(orient="records")
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_ready(v) for v in value]
    return value


def _emit_progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _format_elapsed(started_at: float) -> str:
    return f"{time.monotonic() - started_at:.2f}s"


def _start_heartbeat(label: str) -> tuple[float, threading.Event, threading.Thread]:
    started_at = time.monotonic()
    stop_event = threading.Event()

    def _heartbeat() -> None:
        while not stop_event.wait(HEARTBEAT_INTERVAL_SECONDS):
            _emit_progress(f"[advisory.master_pipeline] {label} running elapsed={_format_elapsed(started_at)}")

    thread = threading.Thread(target=_heartbeat, name=f"master-pipeline-{label}-heartbeat", daemon=True)
    thread.start()
    return started_at, stop_event, thread


def _stop_heartbeat(stage_state: tuple[float, threading.Event, threading.Thread]) -> float:
    started_at, stop_event, thread = stage_state
    stop_event.set()
    thread.join(timeout=0.1)
    return started_at


def run_downloads(*, dry_run: bool, download_script: Path, continue_on_error: bool) -> dict[str, Any]:
    started_at = time.monotonic()
    _emit_progress("[advisory.master_pipeline] downloads start mode=in_process_python_runner")
    result = run_all_downloads(
        continue_on_error=continue_on_error,
        dry_run=dry_run,
    )
    _emit_progress(
        f"[advisory.master_pipeline] downloads done elapsed={_format_elapsed(started_at)} status={result.get('status')}"
    )
    result["download_script"] = str(download_script)
    result["elapsed_seconds"] = round(time.monotonic() - started_at, 4)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run raw downloads plus the full advisory pipeline with theme-based workflow routing.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Pipeline asof date in YYYY-MM-DD")
    parser.add_argument("--symbols", nargs="*", help="Optional symbols")
    parser.add_argument("--setup", dest="setup_ids", nargs="*", help="Optional setup ids")
    parser.add_argument("--start-at", type=parse_stage, choices=PIPELINE_STAGES, help="Optional advisory stage to start from")
    parser.add_argument("--stop-at", type=parse_stage, choices=PIPELINE_STAGES, help="Optional advisory stage to stop after")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--skip-peer-sync", action="store_true")
    parser.add_argument("--skip-intraday", action="store_true", help="Skip intraday feature sync/build")
    parser.add_argument("--skip-intraday-prefetch", action="store_true", help="Skip on-demand intraday feature backfill inside the rule engine")
    parser.add_argument("--intraday-lookback-days", type=int, default=180, help="How much recent intraday history to maintain for advisory pattern features")
    parser.add_argument(
        "--intraday-intervals",
        nargs="*",
        type=int,
        default=[1],
        help="Intraday candle intervals to fetch/build for advisory intraday features",
    )
    parser.add_argument("--skip-downloads", action="store_true", help="Skip the raw download shell script")
    parser.add_argument("--skip-watch", action="store_true", help="Skip announcement watchlist stages")
    parser.add_argument("--skip-news", action="store_true", help="Skip ET RSS watch matching stages")
    parser.add_argument("--skip-lifecycle", action="store_true", help="Skip lifecycle stage")
    parser.add_argument("--include-execution", action="store_true", help="Include execution planning stage")
    parser.add_argument("--live-execution", action="store_true", help="Submit execution orders live through Dhan when execution stage runs")
    parser.add_argument("--execution-reconcile", action="store_true", help="Reconcile broker execution state after execution planning")
    parser.add_argument("--eval-include-evaluated", action="store_true")
    parser.add_argument("--portfolio-capital-inr", type=float, default=300000.0)
    parser.add_argument("--portfolio-max-positions", type=int, default=5)
    parser.add_argument("--portfolio-single-position-cap-pct", type=float, default=0.35)
    parser.add_argument("--portfolio-per-setup-cap-pct", type=float, default=0.50)
    parser.add_argument("--portfolio-max-positions-per-overlap-group", type=int, default=1)
    parser.add_argument("--event-model")
    parser.add_argument("--event-model-artifact-dir", default=".cache/advisory_event_meta_model")
    parser.add_argument("--log-research-ledger", action="store_true", help="Record this run in the advisory research ledger")
    parser.add_argument("--ledger-label", help="Optional research-ledger label")
    parser.add_argument("--ledger-objective", help="Optional research-ledger objective")
    parser.add_argument("--ledger-validation-protocol", help="Optional JSON string describing validation protocol")
    parser.add_argument("--download-script", default=str(DEFAULT_DOWNLOAD_SCRIPT))
    parser.add_argument("--continue-on-download-error", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    return parser.parse_args()


def format_text(summary: dict[str, Any]) -> str:
    lines = [
        f"Pipeline: {summary.get('pipeline')}",
        f"Asof date: {summary.get('asof_date')}",
        f"Downloads: {(summary.get('downloads') or {}).get('status')}",
        "",
        "Advisory stages:",
        f"- stages_run={sorted((summary.get('advisory') or {}).get('stages', {}).keys())}",
    ]
    return "\n".join(lines)


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
            run_type="master_advisory_pipeline",
            entrypoint="advisory.master_pipeline",
            config=vars(args),
            asof_date=asof_date,
            label=args.ledger_label,
            objective=args.ledger_objective,
            validation_protocol=parse_validation_protocol(args.ledger_validation_protocol),
        )

    try:
        pipeline_started = time.monotonic()
        download_summary = {"status": "skipped"}
        if not args.skip_downloads:
            download_summary = run_downloads(
                dry_run=bool(args.dry_run),
                download_script=Path(args.download_script),
                continue_on_error=bool(args.continue_on_download_error),
            )

        advisory_args = argparse.Namespace(
            date=args.date,
            symbols=args.symbols,
            setup_ids=args.setup_ids,
            start_at=args.start_at,
            stop_at=args.stop_at,
            rebuild=bool(args.rebuild),
            skip_peer_sync=bool(args.skip_peer_sync),
            skip_intraday=bool(args.skip_intraday),
            skip_intraday_prefetch=bool(args.skip_intraday_prefetch),
            intraday_lookback_days=int(args.intraday_lookback_days),
            intraday_intervals=list(args.intraday_intervals or [1]),
            include_watch=not bool(args.skip_watch),
            include_news=not bool(args.skip_news),
            include_lifecycle=not bool(args.skip_lifecycle),
            include_execution=bool(args.include_execution),
            live_execution=bool(args.live_execution),
            execution_reconcile=bool(args.execution_reconcile),
            eval_include_evaluated=bool(args.eval_include_evaluated),
            portfolio_capital_inr=float(args.portfolio_capital_inr),
            portfolio_max_positions=int(args.portfolio_max_positions),
            portfolio_single_position_cap_pct=float(args.portfolio_single_position_cap_pct),
            portfolio_per_setup_cap_pct=float(args.portfolio_per_setup_cap_pct),
            portfolio_max_positions_per_overlap_group=int(args.portfolio_max_positions_per_overlap_group),
            event_model=args.event_model,
            event_model_artifact_dir=args.event_model_artifact_dir,
            dry_run=bool(args.dry_run),
        )
        advisory_heartbeat = _start_heartbeat("advisory run")
        _emit_progress("[advisory.master_pipeline] advisory run start")
        advisory_summary = run_pipeline(advisory_args)
        advisory_started = _stop_heartbeat(advisory_heartbeat)
        _emit_progress(
            f"[advisory.master_pipeline] advisory run done elapsed={_format_elapsed(advisory_started)} stages={len((advisory_summary.get('stages') or {}))}"
        )
        _emit_progress(f"[advisory.master_pipeline] pipeline done elapsed={_format_elapsed(pipeline_started)}")

        summary = {
            "status": "ok" if download_summary.get("status") != "failed" else "warning",
            "pipeline": "advisory.master_pipeline",
            "asof_date": None if asof_date is None else asof_date.isoformat(),
            "dry_run": bool(args.dry_run),
            "downloads": download_summary,
            "advisory": _json_ready(advisory_summary),
        }
        if research_run_id:
            finish_research_run(
                research_run_id,
                status="completed",
                data_snapshot=build_research_data_snapshot(asof_date=asof_date, summary=advisory_summary),
                result_metrics={
                    **build_research_result_metrics(status="completed", summary=advisory_summary),
                    "download_status": download_summary.get("status"),
                },
            )
        if args.format == "text":
            print(format_text(summary))
        else:
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
