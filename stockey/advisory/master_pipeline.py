from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pandas as pd

from advisory.news_overlay_engine import build_overlay_state
from advisory.news_theme_engine import build_theme_recommendations, list_theme_screeners, load_active_theme_screener_mapping
from advisory.pipeline import PIPELINE_STAGES, parse_stage, run_pipeline
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


def build_sub_agent_workflow(*, asof_date: pd.Timestamp | None = None) -> dict[str, Any]:
    overlay_df = build_overlay_state(asof_date=asof_date)
    overlay_row = overlay_df.iloc[0].to_dict() if not overlay_df.empty else {}
    theme_payload = build_theme_recommendations(asof_date=asof_date)
    theme_mapping = load_active_theme_screener_mapping(asof_date=asof_date)
    registered = list_theme_screeners()
    if not registered.empty:
        registered["theme_id"] = registered["theme_id"].astype("string").str.upper()
        if "is_active" in registered.columns:
            registered = registered[registered["is_active"].fillna(False)]

    theme_items: list[dict[str, Any]] = []
    required_roles = {"advisory_logic_expert", "risk_portfolio_expert"}
    pipeline_branches = {"core_advisory_pipeline"}

    for item in theme_payload.get("recommendations") or []:
        theme_id = str(item.get("theme_id") or "").upper()
        registered_rows = registered[registered["theme_id"] == theme_id] if not registered.empty else pd.DataFrame()
        registered_slugs = sorted(registered_rows.get("screener_slug", pd.Series(dtype="string")).dropna().astype(str).unique().tolist())
        pending_screeners = []
        for screener in item.get("suggested_screeners") or []:
            if str(screener.get("slug") or "") not in registered_slugs:
                pending_screeners.append(
                    {
                        "slug": screener.get("slug"),
                        "screener_name": screener.get("screener_name"),
                        "screener_query": screener.get("screener_query"),
                        "register_command": f"python -m advisory.news_theme_engine register-url --theme-id {theme_id} --url <SCREENER_URL> --name \"{screener.get('screener_name')}\"",
                    }
                )
        roles = [str(value) for value in (item.get("recommended_agent_roles") or []) if value]
        branches = [str(value) for value in (item.get("recommended_pipeline_branches") or []) if value]
        required_roles.update(roles)
        pipeline_branches.update(branches)
        theme_items.append(
            {
                "theme_id": theme_id,
                "theme_name": item.get("theme_name"),
                "intensity": item.get("theme_intensity"),
                "reason": item.get("theme_reason"),
                "pipeline_branches": branches,
                "recommended_agent_roles": roles,
                "registered_screener_slugs": registered_slugs,
                "pending_screeners": pending_screeners,
            }
        )

    tasks: list[dict[str, Any]] = [
        {
            "agent_role": "data_pipeline_expert",
            "responsibility": "Refresh raw ingestion sources and derived registries before advisory stages.",
            "pipeline_branch": "core_advisory_pipeline",
        },
        {
            "agent_role": "advisory_logic_expert",
            "responsibility": "Run the ranked advisory stack from screeners through watchlist, event evaluation, risk, and portfolio.",
            "pipeline_branch": "core_advisory_pipeline",
        },
    ]
    if theme_items:
        tasks.append(
            {
                "agent_role": "news_theme_expert",
                "responsibility": "Validate active market themes against recent news and matched sources.",
                "pipeline_branch": "theme_detection_pipeline",
                "theme_ids": [item["theme_id"] for item in theme_items],
            }
        )
    if any(item["pending_screeners"] for item in theme_items):
        tasks.append(
            {
                "agent_role": "screener_designer",
                "responsibility": "Create or update Screener.in screens for active themes that do not yet have registered URLs.",
                "pipeline_branch": "event_opportunity_pipeline",
                "theme_ids": [item["theme_id"] for item in theme_items if item["pending_screeners"]],
            }
        )
    if "risk_portfolio_expert" in required_roles:
        tasks.append(
            {
                "agent_role": "risk_portfolio_expert",
                "responsibility": "Review event-driven sizing, overlap caps, and portfolio priority after candidate selection.",
                "pipeline_branch": "core_advisory_pipeline",
            }
        )

    return {
        "asof_date": theme_payload.get("asof_date") or (overlay_row or {}).get("asof_date") or asof_date,
        "base_regime": overlay_row.get("base_regime"),
        "news_overlay": overlay_row.get("overlay_name"),
        "overlay_intensity": overlay_row.get("overlay_intensity"),
        "overlay_reason": overlay_row.get("overlay_reason"),
        "pipeline_branches": sorted(pipeline_branches),
        "recommended_agent_roles": sorted(required_roles | {"data_pipeline_expert"} | ({task["agent_role"] for task in tasks})),
        "active_theme_ids": theme_mapping.get("theme_ids") or [],
        "registered_theme_screeners": theme_mapping.get("screener_slugs") or [],
        "themes": theme_items,
        "tasks": tasks,
    }


def run_downloads(*, dry_run: bool, download_script: Path, continue_on_error: bool) -> dict[str, Any]:
    command = [str(download_script)]
    if dry_run:
        return {"status": "skipped_dry_run", "command": command}
    heartbeat = _start_heartbeat("downloads")
    try:
        _emit_progress(f"[advisory.master_pipeline] downloads start command={' '.join(command)}")
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            check=True,
        )
        started_at = _stop_heartbeat(heartbeat)
        _emit_progress(f"[advisory.master_pipeline] downloads done elapsed={_format_elapsed(started_at)}")
        return {
            "status": "ok",
            "command": command,
            "returncode": completed.returncode,
        }
    except subprocess.CalledProcessError as exc:
        started_at = _stop_heartbeat(heartbeat)
        _emit_progress(f"[advisory.master_pipeline] downloads failed returncode={exc.returncode}")
        result = {
            "status": "failed",
            "command": command,
            "returncode": exc.returncode,
            "elapsed_seconds": round(time.monotonic() - started_at, 4),
        }
        if not continue_on_error:
            raise
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
    parser.add_argument("--download-script", default=str(DEFAULT_DOWNLOAD_SCRIPT))
    parser.add_argument("--continue-on-download-error", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    return parser.parse_args()


def format_text(summary: dict[str, Any]) -> str:
    workflow = summary.get("sub_agent_workflow") or {}
    lines = [
        f"Pipeline: {summary.get('pipeline')}",
        f"Asof date: {summary.get('asof_date')}",
        f"Downloads: {(summary.get('downloads') or {}).get('status')}",
        f"Base regime: {workflow.get('base_regime') or '-'}",
        f"Overlay: {workflow.get('news_overlay') or '-'} | intensity={workflow.get('overlay_intensity')}",
        f"Pipeline branches: {', '.join(workflow.get('pipeline_branches') or []) or '-'}",
        f"Recommended agent roles: {', '.join(workflow.get('recommended_agent_roles') or []) or '-'}",
        "",
        "Active themes:",
    ]
    for item in workflow.get("themes") or []:
        lines.append(
            f"- {item.get('theme_id')} | intensity={item.get('intensity')} | screeners={item.get('registered_screener_slugs') or []}"
        )
        if item.get("pending_screeners"):
            lines.append(f"  pending_screeners={len(item.get('pending_screeners') or [])}")
    lines.extend(["", "Tasks:"])
    for task in workflow.get("tasks") or []:
        lines.append(f"- {task.get('agent_role')}: {task.get('responsibility')}")
    lines.extend(
        [
            "",
            "Advisory stages:",
            f"- stages_run={sorted((summary.get('advisory') or {}).get('stages', {}).keys())}",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    asof_date = pd.Timestamp(args.date, tz="UTC") if args.date else None

    pipeline_started = time.monotonic()
    download_summary = {"status": "skipped"}
    if not args.skip_downloads:
        download_summary = run_downloads(
            dry_run=bool(args.dry_run),
            download_script=Path(args.download_script),
            continue_on_error=bool(args.continue_on_download_error),
        )

    workflow_heartbeat = _start_heartbeat("workflow planning")
    _emit_progress("[advisory.master_pipeline] workflow planning start")
    workflow = build_sub_agent_workflow(asof_date=asof_date)
    workflow_started = _stop_heartbeat(workflow_heartbeat)
    _emit_progress(
        f"[advisory.master_pipeline] workflow planning done elapsed={_format_elapsed(workflow_started)} themes={len(workflow.get('themes') or [])} roles={len(workflow.get('recommended_agent_roles') or [])}"
    )
    advisory_args = argparse.Namespace(
        date=args.date,
        symbols=args.symbols,
        setup_ids=args.setup_ids,
        start_at=args.start_at,
        stop_at=args.stop_at,
        rebuild=bool(args.rebuild),
        skip_peer_sync=bool(args.skip_peer_sync),
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
        "sub_agent_workflow": _json_ready(workflow),
        "advisory": _json_ready(advisory_summary),
    }

    if args.format == "text":
        print(format_text(summary))
    else:
        print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
