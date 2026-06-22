from __future__ import annotations

import argparse
from datetime import date, timedelta
import json
import math
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from environs import Env

from advisory.fallback_telemetry import record_local_fallback_event


REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON_BIN = Path(sys.executable)
env = Env()
env.read_env()


DEFAULT_S3_UPLOAD_ENABLED = env.bool("EVENT_MODEL_ARTIFACT_UPLOAD_ENABLED", True)
DEFAULT_S3_PREFIX = env.str("EVENT_MODEL_ARTIFACT_S3_PREFIX", "models/advisory_event_meta_model")
DEFAULT_SIGNAL_QUALITY_HORIZONS = [5, 10, 20]
DEFAULT_SIGNAL_QUALITY_WINDOW_DAYS = 180
DEFAULT_SIGNAL_QUALITY_STEP_DAYS = 90
DEFAULT_SIGNAL_QUALITY_WINDOWS = 4
DEFAULT_SIGNAL_QUALITY_MIN_MATURED_ROWS = 10
DEFAULT_SIGNAL_QUALITY_MIN_STABLE_WINDOWS = 2
DEFAULT_SIGNAL_QUALITY_MIN_STABLE_WINDOW_RATE = 0.5
DEFAULT_SIGNAL_QUALITY_SPLIT_REPORT_MIN_MATURED_ROWS = 10
DEFAULT_SIGNAL_QUALITY_SPLIT_REPORT_TOP_N = 10
DEFAULT_NEGATIVE_PRESSURE_MIN_MATURED_ROWS = 10
DEFAULT_CONTEXT_WATCH_MIN_MATURED_ROWS = 10
DEFAULT_ADVERSARIAL_REVIEW_MIN_MATURED_ROWS = 10
DEFAULT_CAUSAL_EVENT_MEMORY_MIN_MATURED_ROWS = 10
DEFAULT_ACTION_TRANSITION_MIN_MATURED_ROWS = 10
DEFAULT_CAUSAL_EVENT_MEMORY_LIMIT_PER_SOURCE = env.int("CAUSAL_EVENT_MEMORY_LIMIT_PER_SOURCE", default=5000)
DEFAULT_CONTEXT_OVERLAY_SIGNAL_BACKFILL_DAYS = env.int("CONTEXT_OVERLAY_SIGNAL_BACKFILL_DAYS", default=45)
DEFAULT_CONTEXT_OVERLAY_SIGNAL_BACKFILL_STEP_DAYS = env.int("CONTEXT_OVERLAY_SIGNAL_BACKFILL_STEP_DAYS", default=1)
DEFAULT_CONTEXT_OVERLAY_SIGNAL_BACKFILL_LIMIT = env.int("CONTEXT_OVERLAY_SIGNAL_BACKFILL_LIMIT", default=50)
DEFAULT_CONTEXT_OVERLAY_SIGNAL_BACKFILL_MATURITY_BUFFER_DAYS = env.int("CONTEXT_OVERLAY_SIGNAL_BACKFILL_MATURITY_BUFFER_DAYS", default=-1)


def _emit(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _run_json_command(args: list[str]) -> dict[str, Any]:
    proc = subprocess.Popen(
        args,
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=None,
    )
    stdout, _ = proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(args)}")
    return _parse_json_command_stdout(stdout or "", args)


def _parse_json_command_stdout(stdout: str, args: list[str] | None = None) -> dict[str, Any]:
    command = "" if args is None else " from " + " ".join(args)
    try:
        parsed = json.loads(stdout or "")
    except json.JSONDecodeError as exc:
        decoder = json.JSONDecoder()
        best: dict[str, Any] | None = None
        malformed_candidate_count = 0
        for idx, char in enumerate(stdout):
            if char != "{":
                continue
            try:
                value, end = decoder.raw_decode(stdout, idx)
            except json.JSONDecodeError as candidate_exc:
                malformed_candidate_count += 1
                if malformed_candidate_count == 1:
                    record_local_fallback_event(
                        module="advisory.model_training_runner",
                        fallback_type="model_training_runner_json_stdout_candidate_parse_failed",
                        source="subprocess_stdout",
                        severity="warn",
                        reason=(
                            "Model training runner ignored a malformed JSON-looking stdout "
                            "candidate while scanning for the final JSON object."
                        ),
                        error=candidate_exc,
                        metadata={
                            "command": " ".join(args or []),
                            "candidate_offset": idx,
                            "stdout_length": len(stdout or ""),
                            "stdout_tail": (stdout or "")[-500:],
                        },
                    )
                continue
            if isinstance(value, dict):
                best = value
                if not stdout[end:].strip():
                    return value
        if best is not None:
            return best
        excerpt = stdout[-2000:] if stdout else "<empty stdout>"
        raise RuntimeError(f"expected JSON output{command}; stdout_tail={excerpt!r}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"expected JSON object output{command}; got {type(parsed).__name__}")
    return parsed


def _run_streaming_command(args: list[str]) -> int:
    proc = subprocess.run(args, cwd=REPO_ROOT)
    return int(proc.returncode)


def _horizon_ready(prep_summary: dict[str, Any], horizon_days: int) -> bool:
    coverage = ((prep_summary.get("label_coverage") or {}).get("coverage") or [])
    for item in coverage:
        if int(item.get("horizon_days") or 0) == int(horizon_days):
            return bool(item.get("train_ready"))
    return False


def _build_signal_quality_command(args: argparse.Namespace) -> list[str]:
    horizons = [int(value) for value in (getattr(args, "signal_quality_horizons", None) or DEFAULT_SIGNAL_QUALITY_HORIZONS)]
    cmd = [
        str(PYTHON_BIN),
        "-m",
        "advisory.signal_quality_evaluator",
        "--horizons",
        *[str(value) for value in horizons],
        "--cost-bps",
        str(args.cost_bps),
        "--return-threshold",
        str(args.return_threshold),
    ]
    if args.from_date:
        cmd.extend(["--from-date", args.from_date])
    if args.to_date:
        cmd.extend(["--to-date", args.to_date])
    return cmd


def _build_signal_quality_family_report_command(args: argparse.Namespace) -> list[str]:
    horizons = [int(value) for value in (getattr(args, "signal_quality_horizons", None) or DEFAULT_SIGNAL_QUALITY_HORIZONS)]
    return [
        str(PYTHON_BIN),
        "-m",
        "advisory.signal_quality_family_report",
        "--format",
        "json",
        "--horizons",
        *[str(value) for value in horizons],
    ]


def _build_signal_quality_auto_promotion_command(args: argparse.Namespace) -> list[str]:
    horizons = [int(value) for value in (getattr(args, "signal_quality_horizons", None) or DEFAULT_SIGNAL_QUALITY_HORIZONS)]
    return [
        str(PYTHON_BIN),
        "-m",
        "advisory.signal_quality_promotion",
        "--auto-family-candidates",
        "--horizons",
        *[str(value) for value in horizons],
    ]


def _build_signal_quality_split_queue_command(args: argparse.Namespace) -> list[str]:
    horizons = [int(value) for value in (getattr(args, "signal_quality_horizons", None) or DEFAULT_SIGNAL_QUALITY_HORIZONS)]
    cmd = [
        str(PYTHON_BIN),
        "-m",
        "advisory.signal_quality_split_report",
        "--queue-only",
        "--format",
        "json",
        "--horizons",
        *[str(value) for value in horizons],
        "--min-matured-rows",
        str(getattr(args, "signal_quality_split_report_min_matured_rows", DEFAULT_SIGNAL_QUALITY_SPLIT_REPORT_MIN_MATURED_ROWS)),
        "--top-n",
        str(getattr(args, "signal_quality_split_report_top_n", DEFAULT_SIGNAL_QUALITY_SPLIT_REPORT_TOP_N)),
    ]
    return cmd


def _build_signal_quality_split_evaluator_command(args: argparse.Namespace) -> list[str]:
    horizons = [int(value) for value in (getattr(args, "signal_quality_horizons", None) or DEFAULT_SIGNAL_QUALITY_HORIZONS)]
    return [
        str(PYTHON_BIN),
        "-m",
        "advisory.signal_quality_split_evaluator",
        "--format",
        "json",
        "--horizons",
        *[str(value) for value in horizons],
        "--min-matured-rows",
        str(getattr(args, "signal_quality_split_report_min_matured_rows", DEFAULT_SIGNAL_QUALITY_SPLIT_REPORT_MIN_MATURED_ROWS)),
        "--top-n",
        str(getattr(args, "signal_quality_split_report_top_n", DEFAULT_SIGNAL_QUALITY_SPLIT_REPORT_TOP_N)),
    ]


def _build_negative_pressure_command(args: argparse.Namespace) -> list[str]:
    horizons = [int(value) for value in (getattr(args, "signal_quality_horizons", None) or DEFAULT_SIGNAL_QUALITY_HORIZONS)]
    cmd = [
        str(PYTHON_BIN),
        "-m",
        "advisory.negative_pressure_evaluator",
        "--format",
        "json",
        "--horizons",
        *[str(value) for value in horizons],
        "--cost-bps",
        str(args.cost_bps),
        "--return-threshold",
        str(args.return_threshold),
        "--min-matured-rows",
        str(getattr(args, "negative_pressure_min_matured_rows", DEFAULT_NEGATIVE_PRESSURE_MIN_MATURED_ROWS)),
    ]
    if args.from_date:
        cmd.extend(["--from-date", args.from_date])
    if args.to_date:
        cmd.extend(["--to-date", args.to_date])
    return cmd


def _build_context_watch_command(args: argparse.Namespace) -> list[str]:
    horizons = [int(value) for value in (getattr(args, "signal_quality_horizons", None) or DEFAULT_SIGNAL_QUALITY_HORIZONS)]
    cmd = [
        str(PYTHON_BIN),
        "-m",
        "advisory.context_watch_evaluator",
        "--format",
        "json",
        "--horizons",
        *[str(value) for value in horizons],
        "--cost-bps",
        str(args.cost_bps),
        "--return-threshold",
        str(args.return_threshold),
        "--min-matured-rows",
        str(getattr(args, "context_watch_min_matured_rows", DEFAULT_CONTEXT_WATCH_MIN_MATURED_ROWS)),
    ]
    if args.from_date:
        cmd.extend(["--from-date", args.from_date])
    if args.to_date:
        cmd.extend(["--to-date", args.to_date])
    return cmd


def _build_adversarial_review_evaluator_command(args: argparse.Namespace) -> list[str]:
    horizons = [int(value) for value in (getattr(args, "signal_quality_horizons", None) or DEFAULT_SIGNAL_QUALITY_HORIZONS)]
    cmd = [
        str(PYTHON_BIN),
        "-m",
        "advisory.adversarial_review_evaluator",
        "--horizons",
        *[str(value) for value in horizons],
        "--cost-bps",
        str(args.cost_bps),
        "--min-matured-rows",
        str(getattr(args, "adversarial_review_min_matured_rows", DEFAULT_ADVERSARIAL_REVIEW_MIN_MATURED_ROWS)),
        "--queue-top-n",
        "20",
    ]
    if args.from_date:
        cmd.extend(["--from-date", args.from_date])
    if args.to_date:
        cmd.extend(["--to-date", args.to_date])
    return cmd


def _build_action_transition_evaluator_command(args: argparse.Namespace) -> list[str]:
    horizons = [int(value) for value in (getattr(args, "signal_quality_horizons", None) or DEFAULT_SIGNAL_QUALITY_HORIZONS)]
    cmd = [
        str(PYTHON_BIN),
        "-m",
        "advisory.action_transition_evaluator",
        "--format",
        "json",
        "--horizons",
        *[str(value) for value in horizons],
        "--cost-bps",
        str(args.cost_bps),
        "--return-threshold",
        str(args.return_threshold),
        "--min-matured-rows",
        str(getattr(args, "action_transition_min_matured_rows", DEFAULT_ACTION_TRANSITION_MIN_MATURED_ROWS)),
    ]
    if args.from_date:
        cmd.extend(["--from-date", args.from_date])
    if args.to_date:
        cmd.extend(["--to-date", args.to_date])
    return cmd


def _build_action_evidence_provenance_command(args: argparse.Namespace) -> list[str]:
    cmd = [
        str(PYTHON_BIN),
        "-m",
        "advisory.action_evidence_provenance",
        "--format",
        "json",
        "--limit",
        str(max(1, int(getattr(args, "action_evidence_provenance_limit", DEFAULT_CAUSAL_EVENT_MEMORY_LIMIT_PER_SOURCE) or 1) * 3)),
    ]
    if args.from_date:
        cmd.extend(["--from-date", args.from_date])
    if args.to_date:
        cmd.extend(["--to-date", args.to_date])
    return cmd


def _build_event_evidence_store_command(args: argparse.Namespace) -> list[str]:
    cmd = [
        str(PYTHON_BIN),
        "-m",
        "advisory.event_evidence_store",
    ]
    if args.from_date:
        cmd.extend(["--from-date", args.from_date])
    if args.to_date:
        cmd.extend(["--to-date", args.to_date])
    return cmd


def _build_causal_event_memory_command(args: argparse.Namespace) -> list[str]:
    cmd = [
        str(PYTHON_BIN),
        "-m",
        "advisory.causal_event_memory",
        "--format",
        "json",
        "--limit-per-source",
        str(max(1, int(getattr(args, "causal_event_memory_limit_per_source", DEFAULT_CAUSAL_EVENT_MEMORY_LIMIT_PER_SOURCE)))),
    ]
    if args.from_date:
        cmd.extend(["--from-date", args.from_date])
    if args.to_date:
        cmd.extend(["--to-date", args.to_date])
    return cmd


def _build_causal_event_memory_evaluator_command(args: argparse.Namespace) -> list[str]:
    horizons = [int(value) for value in (getattr(args, "signal_quality_horizons", None) or DEFAULT_SIGNAL_QUALITY_HORIZONS)]
    cmd = [
        str(PYTHON_BIN),
        "-m",
        "advisory.causal_event_memory_evaluator",
        "--format",
        "json",
        "--horizons",
        *[str(value) for value in horizons],
        "--cost-bps",
        str(args.cost_bps),
        "--return-threshold",
        str(args.return_threshold),
        "--min-matured-rows",
        str(getattr(args, "causal_event_memory_min_matured_rows", DEFAULT_CAUSAL_EVENT_MEMORY_MIN_MATURED_ROWS)),
    ]
    if getattr(args, "generate_causal_event_memory_config_previews", False):
        cmd.extend(
            [
                "--generate-config-previews",
                "--max-config-previews",
                str(max(1, int(getattr(args, "max_causal_event_memory_config_previews", 5)))),
            ]
        )
        if getattr(args, "include_causal_event_memory_suppression_previews", False):
            cmd.append("--include-suppression-config-previews")
    if args.from_date:
        cmd.extend(["--from-date", args.from_date])
    if args.to_date:
        cmd.extend(["--to-date", args.to_date])
    return cmd


def _build_causal_event_provenance_command(args: argparse.Namespace) -> list[str]:
    cmd = [
        str(PYTHON_BIN),
        "-m",
        "advisory.causal_event_provenance",
        "--format",
        "json",
        "--limit",
        str(max(1, int(getattr(args, "causal_event_memory_limit_per_source", DEFAULT_CAUSAL_EVENT_MEMORY_LIMIT_PER_SOURCE)) * 3)),
    ]
    if args.from_date:
        cmd.extend(["--from-date", args.from_date])
    if args.to_date:
        cmd.extend(["--to-date", args.to_date])
    return cmd


def _parse_iso_date(value: Any | None, *, default: date) -> date:
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    return date.fromisoformat(text[:10])


def _context_overlay_maturity_buffer_days(args: argparse.Namespace) -> int:
    configured_buffer = int(getattr(args, "context_overlay_signal_backfill_maturity_buffer_days", DEFAULT_CONTEXT_OVERLAY_SIGNAL_BACKFILL_MATURITY_BUFFER_DAYS))
    if configured_buffer >= 0:
        return max(0, configured_buffer)
    horizons = [int(value) for value in (getattr(args, "signal_quality_horizons", None) or DEFAULT_SIGNAL_QUALITY_HORIZONS)]
    max_horizon = max(horizons or DEFAULT_SIGNAL_QUALITY_HORIZONS)
    return max(0, int(math.ceil(max_horizon * 7 / 5) + 3))


def _context_overlay_signal_backfill_dates(args: argparse.Namespace) -> list[date]:
    days = max(0, int(getattr(args, "context_overlay_signal_backfill_days", DEFAULT_CONTEXT_OVERLAY_SIGNAL_BACKFILL_DAYS)))
    if days <= 0:
        return []
    step_days = max(1, int(getattr(args, "context_overlay_signal_backfill_step_days", DEFAULT_CONTEXT_OVERLAY_SIGNAL_BACKFILL_STEP_DAYS)))
    raw_end_date = _parse_iso_date(getattr(args, "to_date", None), default=date.today())
    maturity_buffer_days = _context_overlay_maturity_buffer_days(args)
    end_date = raw_end_date - timedelta(days=maturity_buffer_days)
    bounded_start = end_date - timedelta(days=max(0, days - 1))
    requested_start = _parse_iso_date(getattr(args, "from_date", None), default=bounded_start)
    start_date = max(requested_start, bounded_start)
    if start_date > end_date:
        return []
    out: list[date] = []
    current = start_date
    while current <= end_date:
        out.append(current)
        current += timedelta(days=step_days)
    return out


def _build_context_overlay_signal_backfill_command(args: argparse.Namespace, asof_date: date) -> list[str]:
    return [
        str(PYTHON_BIN),
        "-m",
        "advisory.signal_refresh",
        "--from-context-overlays",
        "--asof-date",
        asof_date.isoformat(),
        "--limit",
        str(max(1, int(getattr(args, "context_overlay_signal_backfill_limit", DEFAULT_CONTEXT_OVERLAY_SIGNAL_BACKFILL_LIMIT)))),
        "--format",
        "json",
    ]


def _run_context_overlay_signal_backfill(args: argparse.Namespace) -> dict[str, Any]:
    dates = _context_overlay_signal_backfill_dates(args)
    maturity_buffer_days = _context_overlay_maturity_buffer_days(args)
    if not dates:
        return {
            "status": "skipped",
            "reason": "context_overlay_signal_backfill_no_mature_eligible_dates",
            "maturity_buffer_days": maturity_buffer_days,
            "authority": "research_only_review_signal_rows",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
        }
    rows: list[dict[str, Any]] = []
    for asof_date in dates:
        payload = _run_json_command(_build_context_overlay_signal_backfill_command(args, asof_date))
        rows.append(
            {
                "asof_date": asof_date.isoformat(),
                "status": payload.get("status"),
                "signal_rows": int(payload.get("signal_rows") or 0),
                "target_rows": int(payload.get("target_rows") or 0),
                "empty_reason": payload.get("empty_reason"),
            }
        )
    return {
        "status": "ok",
        "date_count": len(dates),
        "from_date": dates[0].isoformat(),
        "to_date": dates[-1].isoformat(),
        "step_days": max(1, int(getattr(args, "context_overlay_signal_backfill_step_days", DEFAULT_CONTEXT_OVERLAY_SIGNAL_BACKFILL_STEP_DAYS))),
        "limit": max(1, int(getattr(args, "context_overlay_signal_backfill_limit", DEFAULT_CONTEXT_OVERLAY_SIGNAL_BACKFILL_LIMIT))),
        "maturity_buffer_days": maturity_buffer_days,
        "signal_rows": sum(int(row.get("signal_rows") or 0) for row in rows),
        "target_rows": sum(int(row.get("target_rows") or 0) for row in rows),
        "empty_date_count": sum(1 for row in rows if int(row.get("target_rows") or 0) <= 0),
        "rows": rows,
        "authority": "research_only_review_signal_rows",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
    }


def _build_context_overlay_reliability_command(args: argparse.Namespace) -> list[str]:
    horizons = [int(value) for value in (getattr(args, "signal_quality_horizons", None) or DEFAULT_SIGNAL_QUALITY_HORIZONS)]
    cmd = [
        str(PYTHON_BIN),
        "-m",
        "advisory.context_overlay_reliability_report",
        "--format",
        "json",
        "--horizons",
        *[str(value) for value in horizons],
        "--min-matured-rows",
        str(getattr(args, "context_watch_min_matured_rows", DEFAULT_CONTEXT_WATCH_MIN_MATURED_ROWS)),
    ]
    if args.to_date:
        cmd.extend(["--asof-date", args.to_date])
    return cmd


def _build_signal_quality_window_command(args: argparse.Namespace, *, preflight_only: bool = False) -> list[str]:
    horizons = [int(value) for value in (getattr(args, "signal_quality_horizons", None) or DEFAULT_SIGNAL_QUALITY_HORIZONS)]
    to_date = getattr(args, "to_date", None) or date.today().isoformat()
    cmd = [
        str(PYTHON_BIN),
        "-m",
        "advisory.signal_quality_window_runner",
        "--to-date",
        str(to_date),
        "--window-days",
        str(getattr(args, "signal_quality_window_days", DEFAULT_SIGNAL_QUALITY_WINDOW_DAYS)),
        "--step-days",
        str(getattr(args, "signal_quality_step_days", DEFAULT_SIGNAL_QUALITY_STEP_DAYS)),
        "--windows",
        str(getattr(args, "signal_quality_windows", DEFAULT_SIGNAL_QUALITY_WINDOWS)),
        "--horizons",
        *[str(value) for value in horizons],
        "--cost-bps",
        str(args.cost_bps),
        "--return-threshold",
        str(args.return_threshold),
        "--min-matured-rows",
        str(getattr(args, "signal_quality_min_matured_rows", DEFAULT_SIGNAL_QUALITY_MIN_MATURED_ROWS)),
        "--min-stable-windows",
        str(getattr(args, "signal_quality_min_stable_windows", DEFAULT_SIGNAL_QUALITY_MIN_STABLE_WINDOWS)),
        "--min-stable-window-rate",
        str(getattr(args, "signal_quality_min_stable_window_rate", DEFAULT_SIGNAL_QUALITY_MIN_STABLE_WINDOW_RATE)),
    ]
    if getattr(args, "from_date", None):
        cmd.extend(["--from-date", str(args.from_date)])
    if preflight_only:
        cmd.append("--preflight-only")
    elif getattr(args, "include_signal_quality_split_reports", False):
        cmd.extend(
            [
                "--include-split-reports",
                "--split-report-min-matured-rows",
                str(
                    getattr(
                        args,
                        "signal_quality_split_report_min_matured_rows",
                        DEFAULT_SIGNAL_QUALITY_SPLIT_REPORT_MIN_MATURED_ROWS,
                    )
                ),
                "--split-report-top-n",
                str(getattr(args, "signal_quality_split_report_top_n", DEFAULT_SIGNAL_QUALITY_SPLIT_REPORT_TOP_N)),
            ]
        )
    if getattr(args, "skip_signal_quality_promotion", False):
        cmd.append("--skip-auto-promotion")
    return cmd


def _run_signal_quality_window_with_preflight(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any] | None]:
    preflight_started_at = time.monotonic()
    _emit("[all_model_training] signal_quality_window_runner_preflight start")
    window_preflight = _run_json_command(_build_signal_quality_window_command(args, preflight_only=True))
    _emit(
        "[all_model_training] "
        f"signal_quality_window_runner_preflight done elapsed={time.monotonic() - preflight_started_at:.2f}s "
        f"decision={window_preflight.get('decision')} ready={window_preflight.get('ready_for_persisted_window_run')}"
    )
    if not window_preflight.get("ready_for_persisted_window_run"):
        _emit("[all_model_training] signal_quality_window_runner skipped; preflight not ready for persisted window run")
        return window_preflight, None

    window_started_at = time.monotonic()
    _emit("[all_model_training] signal_quality_window_runner start")
    window_runner = _run_json_command(_build_signal_quality_window_command(args))
    stability = window_runner.get("stability_report") or {}
    split_diagnostics = window_runner.get("split_diagnostics") or {}
    _emit(
        "[all_model_training] "
        f"signal_quality_window_runner done elapsed={time.monotonic() - window_started_at:.2f}s "
        f"stable_candidate_family_count={int(stability.get('stable_candidate_count') or 0)} "
        f"split_diagnostics_status={split_diagnostics.get('status') if split_diagnostics else 'not_requested'} "
        f"split_candidate_report_count={int(split_diagnostics.get('candidate_report_count') or 0) if split_diagnostics else 0}"
    )
    return window_preflight, window_runner


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError) as exc:
        record_local_fallback_event(
            module="advisory.model_training_runner",
            fallback_type="model_training_runner_int_parse_failed",
            source="research_evidence_component_rows",
            severity="warn",
            reason="Model training runner could not parse an integer component count and used zero.",
            error=exc,
            metadata={"value_type": type(value).__name__, "value_excerpt": str(value)[:200]},
        )
        return 0


def _nested_dict(value: Any, key: str) -> dict[str, Any]:
    if isinstance(value, dict) and isinstance(value.get(key), dict):
        return value[key]
    return {}


def _list_len(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _status_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _component_rows(payload: dict[str, Any]) -> dict[str, int]:
    meta = _nested_dict(payload, "meta")
    readiness = _nested_dict(payload, "readiness_contract")
    research_queue = _nested_dict(payload, "research_queue")
    promotion_readiness = _nested_dict(payload, "promotion_readiness")
    return {
        "evaluation_rows": max(
            _int_value(payload.get("evaluation_rows")),
            _int_value(meta.get("evaluation_rows")),
            _int_value(payload.get("provenance_rows")),
            _int_value(payload.get("signal_rows")),
            _int_value(payload.get("memory_rows")),
            _int_value(payload.get("rows")),
        ),
        "summary_rows": max(_int_value(payload.get("summary_rows")), _int_value(meta.get("summary_rows")), _int_value(payload.get("family_count"))),
        "evaluated_rows": max(_int_value(payload.get("evaluated_rows")), _int_value(meta.get("evaluated_rows"))),
        "matured_rows": max(_int_value(payload.get("matured_rows")), _int_value(readiness.get("matured_rows"))),
        "candidate_count": max(
            _int_value(payload.get("candidate_helpful_count")),
            _int_value(payload.get("protective_candidate_count")),
            _int_value(payload.get("candidate_count")),
            _int_value(payload.get("candidate_build_count")),
            _int_value(meta.get("candidate_count")),
            _list_len(promotion_readiness.get("candidate_helpful_families")),
            _int_value(research_queue.get("candidate_build_count")),
            _int_value(research_queue.get("candidate_count")),
        ),
        "beta_only_count": max(
            _int_value(payload.get("benchmark_beta_not_overlay_alpha_count")),
            _int_value(payload.get("benchmark_beta_not_transition_alpha_count")),
            _int_value(payload.get("benchmark_beta_not_memory_alpha_count")),
            _int_value(payload.get("benchmark_beta_not_veto_alpha_count")),
            _int_value(payload.get("benchmark_beta_not_derisk_alpha_count")),
            _int_value(readiness.get("benchmark_beta_not_transition_alpha_count")),
            _list_len(promotion_readiness.get("benchmark_or_attribution_blocked_families")),
        ),
    }


def _flag_is_true(value: Any) -> bool:
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on", "allowed", "enabled"}
    return False


def _authority_violations(value: Any, *, path: str = "$") -> list[dict[str, Any]]:
    flagged_keys = {
        "broker_execution_allowed": "broker",
        "policy_auto_promotion_allowed": "policy",
        "portfolio_mutation_allowed": "portfolio",
        "portfolio_mutation": "portfolio",
        "config_mutation_allowed": "config",
        "runtime_consumer_created": "runtime_consumer",
    }
    violations: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            item_path = f"{path}.{key}"
            if key in flagged_keys and _flag_is_true(item):
                violations.append(
                    {
                        "path": item_path,
                        "key": key,
                        "authority_type": flagged_keys[key],
                        "value": item,
                    }
                )
            if isinstance(item, (dict, list)):
                violations.extend(_authority_violations(item, path=item_path))
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            if isinstance(item, (dict, list)):
                violations.extend(_authority_violations(item, path=f"{path}[{idx}]"))
    return violations


def _component_status(name: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    if payload is None:
        return {
            "component": name,
            "status": "skipped",
            "usable_for_review": False,
            "reason": "component_not_requested_or_skipped",
            "rows": {},
        }
    if not isinstance(payload, dict):
        return {
            "component": name,
            "status": "error",
            "usable_for_review": False,
            "reason": "component_payload_not_dict",
            "rows": {},
        }
    rows = _component_rows(payload)
    status = _status_text(payload.get("status"))
    readiness = _nested_dict(payload, "readiness_contract")
    promotion_readiness = _nested_dict(payload, "promotion_readiness")
    readiness_status = _status_text(payload.get("readiness_status") or readiness.get("status"))
    promotion_readiness_status = _status_text(promotion_readiness.get("status"))
    empty_reason = _status_text(payload.get("empty_reason"))
    family_count = _int_value(payload.get("family_count"))
    has_rows = any(rows[key] > 0 for key in ["evaluation_rows", "summary_rows", "evaluated_rows", "matured_rows"]) or family_count > 0
    candidate_count = rows["candidate_count"]
    beta_only_count = rows["beta_only_count"]
    ready_for_policy_review = bool(payload.get("ready_for_policy_review") or readiness.get("ready_for_policy_review"))
    authority_violations = _authority_violations(payload)

    text_blob = " ".join(item for item in [status, readiness_status, promotion_readiness_status, empty_reason] if item)
    if authority_violations:
        normalized_status = "authority_violation"
        reason = "research_component_reported_policy_portfolio_or_broker_authority"
    elif status in {"error", "failed", "partial_error"}:
        normalized_status = "error"
        reason = status or "component_error"
    elif "baseline_unavailable" in text_blob or "under_baseline" in text_blob or "under-baseline" in text_blob:
        normalized_status = "under_baselined"
        reason = readiness_status or status or "technical_baseline_unavailable"
    elif status in {"evidence_unavailable", "unavailable"} or "unavailable" in text_blob:
        normalized_status = "evidence_unavailable"
        reason = status or readiness_status or "evidence_unavailable"
    elif beta_only_count > 0 or "benchmark_beta" in text_blob or "benchmark_or_attribution" in text_blob:
        normalized_status = "benchmark_beta_only"
        reason = promotion_readiness_status or readiness_status or status or "benchmark_beta_only"
    elif ready_for_policy_review or candidate_count > 0 or status in {"candidate_helpful", "protective_candidate", "usable_for_manual_review"}:
        normalized_status = "candidate_for_review"
        reason = promotion_readiness_status or readiness_status or status or "candidate_evidence_present"
    elif not has_rows or status in {"no_data", "no_summary_rows", "no_memory_rows"}:
        normalized_status = "empty"
        reason = empty_reason or status or "no_evidence_rows"
    elif "no_matured" in text_blob or "collect_more" in text_blob or status in {"needs_more_data", "not_usable"}:
        normalized_status = "needs_more_data"
        reason = readiness_status or status or "needs_more_data"
    else:
        normalized_status = "monitor"
        reason = readiness_status or status or "rows_present_no_candidate"

    return {
        "component": name,
        "status": normalized_status,
        "usable_for_review": bool(normalized_status == "candidate_for_review"),
        "reason": reason,
        "rows": rows,
        "source_status": status or None,
        "readiness_status": readiness_status or None,
        "ready_for_policy_review": ready_for_policy_review,
        "authority_violation_count": len(authority_violations),
        "authority_violations": authority_violations[:10],
    }


def _build_research_evidence_readiness_summary(components: list[dict[str, Any]]) -> dict[str, Any]:
    active = [row for row in components if row.get("status") != "skipped"]
    status_counts: dict[str, int] = {}
    for row in components:
        key = str(row.get("status") or "unknown")
        status_counts[key] = status_counts.get(key, 0) + 1
    usable = [row for row in active if row.get("usable_for_review")]
    blockers = [
        row
        for row in active
        if row.get("status")
        in {
            "error",
            "evidence_unavailable",
            "under_baselined",
            "benchmark_beta_only",
            "empty",
            "needs_more_data",
            "authority_violation",
        }
    ]
    authority_violations = [row for row in active if row.get("status") == "authority_violation"]
    if authority_violations:
        status = "error"
        operator_action = "Stop using this research-evidence run until authority-boundary violations are fixed."
    elif any(row.get("status") == "error" for row in active):
        status = "error"
        operator_action = "Fix failed research-evidence components before trusting context/event policy research."
    elif usable:
        status = "candidate_evidence_available"
        operator_action = "Review candidate components manually; evidence remains research-only and cannot change live policy by itself."
    elif active and all(row.get("status") in {"empty", "needs_more_data", "benchmark_beta_only", "under_baselined", "evidence_unavailable"} for row in active):
        status = "not_ready"
        operator_action = "Keep context/event overlays research-only; collect more matured benchmark-attributed labels."
    elif active:
        status = "monitor"
        operator_action = "Monitor research evidence; no component is ready for policy review yet."
    else:
        status = "all_skipped"
        operator_action = "No research-evidence components ran; rerun without skip flags when evidence refresh is required."
    return {
        "status": status,
        "component_count": len(components),
        "active_component_count": len(active),
        "candidate_component_count": len(usable),
        "blocker_count": len(blockers),
        "status_counts": status_counts,
        "candidate_components": [row["component"] for row in usable],
        "blocker_components": [
            {
                "component": row["component"],
                "status": row["status"],
                "reason": row.get("reason"),
                "authority_violation_count": row.get("authority_violation_count", 0),
                "authority_violations": row.get("authority_violations", []),
            }
            for row in blockers
        ],
        "authority_violation_count": sum(int(row.get("authority_violation_count") or 0) for row in active),
        "authority_violation_components": [
            {
                "component": row["component"],
                "authority_violation_count": row.get("authority_violation_count", 0),
                "authority_violations": row.get("authority_violations", []),
            }
            for row in authority_violations
        ],
        "operator_action": operator_action,
        "authority": "research_only_no_policy_or_broker_authority",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
    }


def _build_research_evidence_payload(
    *,
    args: argparse.Namespace,
    signal_summary: dict[str, Any],
    family_report: dict[str, Any],
    split_queue: dict[str, Any] | None,
    split_evaluator: dict[str, Any] | None,
    context_overlay_signal_backfill: dict[str, Any] | None,
    negative_pressure: dict[str, Any] | None,
    context_watch: dict[str, Any] | None,
    adversarial_review: dict[str, Any] | None,
    action_transition: dict[str, Any] | None,
    action_evidence_provenance: dict[str, Any] | None,
    event_evidence_store: dict[str, Any] | None,
    causal_event_memory: dict[str, Any] | None,
    causal_event_memory_evaluator: dict[str, Any] | None,
    causal_event_provenance: dict[str, Any] | None,
    context_overlay_reliability: dict[str, Any] | None,
    window_preflight: dict[str, Any] | None,
    window_runner: dict[str, Any] | None,
    promotion_review: dict[str, Any] | None,
) -> dict[str, Any]:
    if args.skip_signal_quality_promotion:
        manual_review_rows_may_be_created = False
    elif args.run_signal_quality_window_runner:
        manual_review_rows_may_be_created = window_runner is not None
    else:
        manual_review_rows_may_be_created = True
    causal_preview_generation = (
        causal_event_memory_evaluator.get("config_preview_generation")
        if isinstance(causal_event_memory_evaluator, dict) and isinstance(causal_event_memory_evaluator.get("config_preview_generation"), dict)
        else {}
    )
    causal_preview_summary = {
        "status": causal_preview_generation.get("status") or "not_requested",
        "generated_count": int(causal_preview_generation.get("generated_count") or 0),
        "helpful_generated_count": int(causal_preview_generation.get("helpful_generated_count") or 0),
        "suppression_generated_count": int(causal_preview_generation.get("suppression_generated_count") or 0),
        "error_count": int(causal_preview_generation.get("error_count") or 0),
        "candidate_count": int(causal_preview_generation.get("candidate_count") or 0),
        "suppression_candidate_count": int(causal_preview_generation.get("suppression_candidate_count") or 0),
        "persisted": bool(causal_preview_generation.get("persisted", False)),
        "source_type": "causal_event_memory_rule" if int(causal_preview_generation.get("generated_count") or 0) > 0 else None,
        "runtime_consumer_created": bool(causal_preview_generation.get("runtime_consumer_created", False)),
        "authority": causal_preview_generation.get("authority") or "review_input_only",
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
    }
    components = [
        _component_status("signal_quality", None if getattr(args, "skip_signal_quality", False) else signal_summary),
        _component_status("signal_quality_family_report", None if getattr(args, "skip_signal_quality", False) else family_report),
        _component_status("signal_quality_split_queue", None if getattr(args, "skip_signal_quality", False) else split_queue),
        _component_status("signal_quality_split_evaluator", split_evaluator),
        _component_status("context_overlay_signal_backfill", context_overlay_signal_backfill),
        _component_status("negative_pressure_evaluator", negative_pressure),
        _component_status("context_watch_evaluator", context_watch),
        _component_status("adversarial_review_evaluator", adversarial_review),
        _component_status("action_transition_evaluator", action_transition),
        _component_status("action_evidence_provenance", action_evidence_provenance),
        _component_status("event_evidence_store", event_evidence_store),
        _component_status("causal_event_memory", causal_event_memory),
        _component_status("causal_event_memory_evaluator", causal_event_memory_evaluator),
        _component_status("causal_event_provenance", causal_event_provenance),
        _component_status("context_overlay_reliability_report", context_overlay_reliability),
        _component_status("signal_quality_window_preflight", window_preflight),
        _component_status("signal_quality_window_runner", window_runner),
        _component_status("signal_quality_auto_promotion", promotion_review),
    ]
    return {
        "research_evidence": {
            "readiness_summary": _build_research_evidence_readiness_summary(components),
            "component_status": components,
            "signal_quality": signal_summary,
            "signal_quality_family_report": family_report,
            "signal_quality_split_queue": split_queue,
            "signal_quality_split_evaluator": split_evaluator,
            "context_overlay_signal_backfill": context_overlay_signal_backfill,
            "negative_pressure_evaluator": negative_pressure,
            "context_watch_evaluator": context_watch,
            "adversarial_review_evaluator": adversarial_review,
            "action_transition_evaluator": action_transition,
            "action_evidence_provenance": action_evidence_provenance,
            "event_evidence_store": event_evidence_store,
            "causal_event_memory": causal_event_memory,
            "causal_event_memory_evaluator": causal_event_memory_evaluator,
            "causal_event_memory_config_preview_generation": causal_preview_summary,
            "causal_event_provenance": causal_event_provenance,
            "context_overlay_reliability_report": context_overlay_reliability,
            "signal_quality_window_preflight": window_preflight,
            "signal_quality_window_runner": window_runner,
            "signal_quality_auto_promotion": promotion_review,
            "authority": "research_only",
            "broker_execution_allowed": False,
            "policy_auto_promotion_allowed": False,
            "manual_review_rows_may_be_created": manual_review_rows_may_be_created,
            "manual_review_row_reason": (
                "disabled_by_skip_signal_quality_promotion"
                if args.skip_signal_quality_promotion
                else "preflight_not_ready_for_persisted_window_run"
                if args.run_signal_quality_window_runner and window_runner is None
                else "stable_window_runner_can_create_review_rows"
                if args.run_signal_quality_window_runner
                else "single_window_auto_promotion_can_create_review_rows"
            ),
        }
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare, train, and score the advisory event meta-model when label coverage is sufficient.")
    parser.add_argument("--horizon-days", type=int, default=1)
    parser.add_argument("--min-labeled-rows", type=int, default=20)
    parser.add_argument("--return-threshold", type=float, default=0.02)
    parser.add_argument("--cost-bps", type=float, default=25.0)
    parser.add_argument("--artifact-dir", default=".cache/advisory_event_meta_model")
    parser.add_argument("--model-basename", default="event_meta_model")
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--include-evaluated", action="store_true")
    parser.add_argument("--skip-training-universe", action="store_true")
    parser.add_argument("--skip-screener-normalization", action="store_true")
    parser.add_argument("--skip-event-backfill", action="store_true")
    parser.add_argument("--skip-price-refresh", action="store_true")
    parser.add_argument("--skip-score", action="store_true")
    parser.add_argument("--skip-signal-quality", action="store_true")
    parser.add_argument("--skip-signal-quality-promotion", action="store_true")
    parser.add_argument("--signal-quality-horizons", nargs="*", type=int, default=DEFAULT_SIGNAL_QUALITY_HORIZONS)
    parser.add_argument("--run-signal-quality-window-runner", action="store_true")
    parser.add_argument("--signal-quality-window-days", type=int, default=DEFAULT_SIGNAL_QUALITY_WINDOW_DAYS)
    parser.add_argument("--signal-quality-step-days", type=int, default=DEFAULT_SIGNAL_QUALITY_STEP_DAYS)
    parser.add_argument("--signal-quality-windows", type=int, default=DEFAULT_SIGNAL_QUALITY_WINDOWS)
    parser.add_argument("--signal-quality-min-matured-rows", type=int, default=DEFAULT_SIGNAL_QUALITY_MIN_MATURED_ROWS)
    parser.add_argument("--signal-quality-min-stable-windows", type=int, default=DEFAULT_SIGNAL_QUALITY_MIN_STABLE_WINDOWS)
    parser.add_argument("--signal-quality-min-stable-window-rate", type=float, default=DEFAULT_SIGNAL_QUALITY_MIN_STABLE_WINDOW_RATE)
    parser.add_argument(
        "--include-signal-quality-split-reports",
        action="store_true",
        help="Attach research-only split diagnostics and run narrowed split evaluation for unstable source families.",
    )
    parser.add_argument(
        "--signal-quality-split-report-min-matured-rows",
        type=int,
        default=DEFAULT_SIGNAL_QUALITY_SPLIT_REPORT_MIN_MATURED_ROWS,
    )
    parser.add_argument("--signal-quality-split-report-top-n", type=int, default=DEFAULT_SIGNAL_QUALITY_SPLIT_REPORT_TOP_N)
    parser.add_argument("--skip-negative-pressure-evaluator", action="store_true")
    parser.add_argument("--negative-pressure-min-matured-rows", type=int, default=DEFAULT_NEGATIVE_PRESSURE_MIN_MATURED_ROWS)
    parser.add_argument("--skip-context-watch-evaluator", action="store_true")
    parser.add_argument("--context-watch-min-matured-rows", type=int, default=DEFAULT_CONTEXT_WATCH_MIN_MATURED_ROWS)
    parser.add_argument("--skip-adversarial-review-evaluator", action="store_true")
    parser.add_argument("--adversarial-review-min-matured-rows", type=int, default=DEFAULT_ADVERSARIAL_REVIEW_MIN_MATURED_ROWS)
    parser.add_argument("--skip-action-transition-evaluator", action="store_true")
    parser.add_argument("--action-transition-min-matured-rows", type=int, default=DEFAULT_ACTION_TRANSITION_MIN_MATURED_ROWS)
    parser.add_argument("--skip-action-evidence-provenance", action="store_true")
    parser.add_argument("--action-evidence-provenance-limit", type=int, default=DEFAULT_CAUSAL_EVENT_MEMORY_LIMIT_PER_SOURCE)
    parser.add_argument("--skip-event-evidence-store", action="store_true")
    parser.add_argument("--skip-causal-event-memory", action="store_true")
    parser.add_argument("--skip-causal-event-memory-evaluator", action="store_true")
    parser.add_argument("--skip-causal-event-provenance", action="store_true")
    parser.add_argument("--causal-event-memory-min-matured-rows", type=int, default=DEFAULT_CAUSAL_EVENT_MEMORY_MIN_MATURED_ROWS)
    parser.add_argument("--causal-event-memory-limit-per-source", type=int, default=DEFAULT_CAUSAL_EVENT_MEMORY_LIMIT_PER_SOURCE)
    parser.add_argument(
        "--generate-causal-event-memory-config-previews",
        action="store_true",
        help="Persist disabled config-preview audit rows for causal-memory readiness candidates. Does not create live policy, portfolio rows, or broker orders.",
    )
    parser.add_argument(
        "--include-causal-event-memory-suppression-previews",
        action="store_true",
        help="When generating causal-memory config previews, also include disabled suppression-review previews for hurts_or_no_lift groups.",
    )
    parser.add_argument("--max-causal-event-memory-config-previews", type=int, default=5)
    parser.add_argument(
        "--skip-context-overlay-signal-backfill",
        action="store_true",
        help="Skip bounded research-only backfill of context-overlay signal-refresh rows before fast context evaluators.",
    )
    parser.add_argument("--context-overlay-signal-backfill-days", type=int, default=DEFAULT_CONTEXT_OVERLAY_SIGNAL_BACKFILL_DAYS)
    parser.add_argument("--context-overlay-signal-backfill-step-days", type=int, default=DEFAULT_CONTEXT_OVERLAY_SIGNAL_BACKFILL_STEP_DAYS)
    parser.add_argument("--context-overlay-signal-backfill-limit", type=int, default=DEFAULT_CONTEXT_OVERLAY_SIGNAL_BACKFILL_LIMIT)
    parser.add_argument(
        "--context-overlay-signal-backfill-maturity-buffer-days",
        type=int,
        default=DEFAULT_CONTEXT_OVERLAY_SIGNAL_BACKFILL_MATURITY_BUFFER_DAYS,
        help="Calendar-day buffer subtracted from --to-date for context-overlay signal backfill. Default -1 uses the largest signal-quality horizon.",
    )
    parser.add_argument("--skip-s3-upload", action="store_true")
    parser.add_argument("--s3-prefix", default=DEFAULT_S3_PREFIX)
    parser.add_argument("--prep-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    started_at = time.monotonic()

    prep_cmd = [
        str(PYTHON_BIN),
        "-m",
        "advisory.event_model_data_prep",
        "--format",
        "json",
        "--min-labeled-rows",
        str(args.min_labeled_rows),
        "--horizons",
        str(args.horizon_days),
    ]
    if args.from_date:
        prep_cmd.extend(["--from-date", args.from_date])
    if args.to_date:
        prep_cmd.extend(["--to-date", args.to_date])
    if args.rebuild:
        prep_cmd.append("--rebuild")
    if args.include_evaluated:
        prep_cmd.append("--include-evaluated")
    if args.skip_training_universe:
        prep_cmd.append("--skip-training-universe")
    if args.skip_screener_normalization:
        prep_cmd.append("--skip-screener-normalization")
    if args.skip_event_backfill:
        prep_cmd.append("--skip-event-backfill")
    if args.skip_price_refresh:
        prep_cmd.append("--skip-price-refresh")

    prep_started_at = time.monotonic()
    _emit("[all_model_training] prep start")
    prep_summary = _run_json_command(prep_cmd)
    _emit(f"[all_model_training] prep done elapsed={time.monotonic() - prep_started_at:.2f}s")
    print(json.dumps({"prep_summary": prep_summary}, indent=2, ensure_ascii=False), flush=True)

    if args.prep_only:
        _emit(f"[all_model_training] prep_only requested; skipping train/score total_elapsed={time.monotonic() - started_at:.2f}s")
        return 0

    if not _horizon_ready(prep_summary, args.horizon_days):
        _emit(
            f"[all_model_training] horizon={args.horizon_days}d not ready; skipping train/score",
        )
        return 0

    train_cmd = [
        str(PYTHON_BIN),
        "-m",
        "advisory.event_meta_model",
        "train",
        "--horizon-days",
        str(args.horizon_days),
        "--return-threshold",
        str(args.return_threshold),
        "--cost-bps",
        str(args.cost_bps),
        "--artifact-dir",
        args.artifact_dir,
        "--model-basename",
        args.model_basename,
    ]
    if args.to_date:
        train_cmd.extend(["--date", args.to_date])

    train_started_at = time.monotonic()
    _emit("[all_model_training] train start")
    train_code = _run_streaming_command(train_cmd)
    if train_code != 0:
        return train_code
    _emit(f"[all_model_training] train done elapsed={time.monotonic() - train_started_at:.2f}s")

    should_upload = bool(DEFAULT_S3_UPLOAD_ENABLED) and not bool(args.skip_s3_upload)
    if should_upload:
        upload_cmd = [
            str(PYTHON_BIN),
            "-m",
            "advisory.event_model_artifact_store",
            "--artifact-dir",
            args.artifact_dir,
            "--model-basename",
            args.model_basename,
            "--s3-prefix",
            args.s3_prefix,
        ]
        upload_started_at = time.monotonic()
        _emit("[all_model_training] s3_upload start")
        upload_code = _run_streaming_command(upload_cmd)
        if upload_code != 0:
            return upload_code
        _emit(f"[all_model_training] s3_upload done elapsed={time.monotonic() - upload_started_at:.2f}s")
    else:
        _emit("[all_model_training] s3_upload skipped")

    if args.skip_score:
        _emit(f"[all_model_training] skip_score requested total_elapsed={time.monotonic() - started_at:.2f}s")
        return 0

    score_cmd = [
        str(PYTHON_BIN),
        "-m",
        "advisory.event_meta_model",
        "score",
        "--artifact-dir",
        args.artifact_dir,
        "--model-basename",
        args.model_basename,
    ]
    if args.to_date:
        score_cmd.extend(["--date", args.to_date])

    score_started_at = time.monotonic()
    _emit("[all_model_training] score start")
    score_code = _run_streaming_command(score_cmd)
    if score_code != 0:
        return score_code
    _emit(f"[all_model_training] score done elapsed={time.monotonic() - score_started_at:.2f}s")

    if args.skip_signal_quality:
        _emit("[all_model_training] signal_quality skipped")
        _emit(f"[all_model_training] complete total_elapsed={time.monotonic() - started_at:.2f}s")
        return 0

    signal_started_at = time.monotonic()
    _emit("[all_model_training] signal_quality start")
    signal_summary = _run_json_command(_build_signal_quality_command(args))
    _emit(
        "[all_model_training] "
        f"signal_quality done elapsed={time.monotonic() - signal_started_at:.2f}s "
        f"summary_rows={signal_summary.get('summary_rows')}"
    )
    family_started_at = time.monotonic()
    _emit("[all_model_training] signal_quality_family_report start")
    family_report = _run_json_command(_build_signal_quality_family_report_command(args))
    _emit(
        "[all_model_training] "
        f"signal_quality_family_report done elapsed={time.monotonic() - family_started_at:.2f}s "
        f"status={family_report.get('status')} candidate_helpful_count={family_report.get('candidate_helpful_count')}"
    )
    split_queue_started_at = time.monotonic()
    _emit("[all_model_training] signal_quality_split_queue start")
    split_queue = _run_json_command(_build_signal_quality_split_queue_command(args))
    queue = split_queue.get("research_queue") if isinstance(split_queue.get("research_queue"), dict) else {}
    _emit(
        "[all_model_training] "
        f"signal_quality_split_queue done elapsed={time.monotonic() - split_queue_started_at:.2f}s "
        f"status={split_queue.get('status')} candidate_build_count={int(queue.get('candidate_build_count') or 0)} "
        f"negative_control_count={int(queue.get('negative_control_count') or 0)}"
    )
    split_evaluator = None
    if args.include_signal_quality_split_reports:
        split_eval_started_at = time.monotonic()
        _emit("[all_model_training] signal_quality_split_evaluator start")
        split_evaluator = _run_json_command(_build_signal_quality_split_evaluator_command(args))
        split_eval_meta = split_evaluator.get("meta") if isinstance(split_evaluator.get("meta"), dict) else {}
        _emit(
            "[all_model_training] "
            f"signal_quality_split_evaluator done elapsed={time.monotonic() - split_eval_started_at:.2f}s "
            f"spec_count={int(split_eval_meta.get('spec_count') or 0)} "
            f"summary_rows={int(split_eval_meta.get('summary_rows') or 0)}"
        )
    else:
        _emit("[all_model_training] signal_quality_split_evaluator skipped; include_signal_quality_split_reports=false")
    context_overlay_signal_backfill = None
    if args.skip_context_overlay_signal_backfill or (args.skip_negative_pressure_evaluator and args.skip_context_watch_evaluator):
        _emit("[all_model_training] context_overlay_signal_backfill skipped")
    else:
        backfill_started_at = time.monotonic()
        _emit("[all_model_training] context_overlay_signal_backfill start")
        context_overlay_signal_backfill = _run_context_overlay_signal_backfill(args)
        _emit(
            "[all_model_training] "
            f"context_overlay_signal_backfill done elapsed={time.monotonic() - backfill_started_at:.2f}s "
            f"date_count={int(context_overlay_signal_backfill.get('date_count') or 0)} "
            f"signal_rows={int(context_overlay_signal_backfill.get('signal_rows') or 0)}"
        )
    event_evidence_store = None
    if args.skip_event_evidence_store or args.skip_causal_event_memory:
        _emit("[all_model_training] event_evidence_store skipped")
    else:
        evidence_started_at = time.monotonic()
        _emit("[all_model_training] event_evidence_store start")
        event_evidence_store = _run_json_command(_build_event_evidence_store_command(args))
        bhavcopy_evidence = event_evidence_store.get("bhavcopy") if isinstance(event_evidence_store.get("bhavcopy"), dict) else {}
        announcement_evidence = event_evidence_store.get("announcements") if isinstance(event_evidence_store.get("announcements"), dict) else {}
        _emit(
            "[all_model_training] "
            f"event_evidence_store done elapsed={time.monotonic() - evidence_started_at:.2f}s "
            f"bhavcopy_overlays={int(bhavcopy_evidence.get('context_overlay_rows') or 0)} "
            f"announcement_overlays={int(announcement_evidence.get('context_overlay_rows') or 0)}"
        )
    causal_event_memory = None
    if args.skip_causal_event_memory:
        _emit("[all_model_training] causal_event_memory skipped")
    else:
        causal_started_at = time.monotonic()
        _emit("[all_model_training] causal_event_memory start")
        causal_event_memory = _run_json_command(_build_causal_event_memory_command(args))
        coverage = causal_event_memory.get("source_coverage") if isinstance(causal_event_memory.get("source_coverage"), dict) else {}
        _emit(
            "[all_model_training] "
            f"causal_event_memory done elapsed={time.monotonic() - causal_started_at:.2f}s "
            f"memory_rows={int(causal_event_memory.get('memory_rows') or 0)} "
            f"symbol_scoped={int(causal_event_memory.get('symbol_scoped_memory_rows') or 0)} "
            f"source_count={len(coverage)}"
        )
    causal_event_memory_evaluator = None
    if args.skip_causal_event_memory_evaluator:
        _emit("[all_model_training] causal_event_memory_evaluator skipped")
    elif args.skip_causal_event_memory:
        _emit("[all_model_training] causal_event_memory_evaluator skipped; causal_event_memory skipped")
    else:
        causal_eval_started_at = time.monotonic()
        _emit("[all_model_training] causal_event_memory_evaluator start")
        causal_event_memory_evaluator = _run_json_command(_build_causal_event_memory_evaluator_command(args))
        causal_eval_meta = causal_event_memory_evaluator.get("meta") if isinstance(causal_event_memory_evaluator.get("meta"), dict) else {}
        causal_preview_generation = (
            causal_event_memory_evaluator.get("config_preview_generation")
            if isinstance(causal_event_memory_evaluator.get("config_preview_generation"), dict)
            else {}
        )
        _emit(
            "[all_model_training] "
            f"causal_event_memory_evaluator done elapsed={time.monotonic() - causal_eval_started_at:.2f}s "
            f"evaluation_rows={int(causal_event_memory_evaluator.get('evaluation_rows') or 0)} "
            f"summary_rows={int(causal_event_memory_evaluator.get('summary_rows') or 0)} "
            f"memory_rows={int(causal_eval_meta.get('memory_rows') or 0)} "
            f"preview_status={causal_preview_generation.get('status')} "
            f"preview_generated={int(causal_preview_generation.get('generated_count') or 0)}"
        )
    causal_event_provenance = None
    if args.skip_causal_event_provenance:
        _emit("[all_model_training] causal_event_provenance skipped")
    elif args.skip_causal_event_memory:
        _emit("[all_model_training] causal_event_provenance skipped; causal_event_memory skipped")
    else:
        provenance_started_at = time.monotonic()
        _emit("[all_model_training] causal_event_provenance start")
        causal_event_provenance = _run_json_command(_build_causal_event_provenance_command(args))
        _emit(
            "[all_model_training] "
            f"causal_event_provenance done elapsed={time.monotonic() - provenance_started_at:.2f}s "
            f"provenance_rows={int(causal_event_provenance.get('provenance_rows') or 0)} "
            f"memory_rows={int(causal_event_provenance.get('memory_rows') or 0)}"
        )
    negative_pressure = None
    if args.skip_negative_pressure_evaluator:
        _emit("[all_model_training] negative_pressure_evaluator skipped")
    else:
        negative_started_at = time.monotonic()
        _emit("[all_model_training] negative_pressure_evaluator start")
        negative_pressure = _run_json_command(_build_negative_pressure_command(args))
        meta = negative_pressure.get("meta") if isinstance(negative_pressure.get("meta"), dict) else {}
        _emit(
            "[all_model_training] "
            f"negative_pressure_evaluator done elapsed={time.monotonic() - negative_started_at:.2f}s "
            f"evaluation_rows={int(negative_pressure.get('evaluation_rows') or 0)} "
            f"summary_rows={int(negative_pressure.get('summary_rows') or 0)} "
            f"signal_rows={int(meta.get('signal_rows') or 0)}"
        )
    context_watch = None
    if args.skip_context_watch_evaluator:
        _emit("[all_model_training] context_watch_evaluator skipped")
    else:
        context_watch_started_at = time.monotonic()
        _emit("[all_model_training] context_watch_evaluator start")
        context_watch = _run_json_command(_build_context_watch_command(args))
        meta = context_watch.get("meta") if isinstance(context_watch.get("meta"), dict) else {}
        _emit(
            "[all_model_training] "
            f"context_watch_evaluator done elapsed={time.monotonic() - context_watch_started_at:.2f}s "
            f"evaluation_rows={int(context_watch.get('evaluation_rows') or 0)} "
            f"summary_rows={int(context_watch.get('summary_rows') or 0)} "
            f"signal_rows={int(meta.get('signal_rows') or 0)}"
        )
    adversarial_review_evidence = None
    if args.skip_adversarial_review_evaluator:
        _emit("[all_model_training] adversarial_review_evaluator skipped")
    else:
        adversarial_started_at = time.monotonic()
        _emit("[all_model_training] adversarial_review_evaluator start")
        adversarial_review_evidence = _run_json_command(_build_adversarial_review_evaluator_command(args))
        _emit(
            "[all_model_training] "
            f"adversarial_review_evaluator done elapsed={time.monotonic() - adversarial_started_at:.2f}s "
            f"evaluation_rows={int(adversarial_review_evidence.get('evaluation_rows') or 0)} "
            f"summary_rows={int(adversarial_review_evidence.get('summary_rows') or 0)} "
            f"review_rows={int(adversarial_review_evidence.get('review_rows') or 0)}"
        )
    action_transition_evidence = None
    if args.skip_action_transition_evaluator:
        _emit("[all_model_training] action_transition_evaluator skipped")
    else:
        transition_started_at = time.monotonic()
        _emit("[all_model_training] action_transition_evaluator start")
        action_transition_evidence = _run_json_command(_build_action_transition_evaluator_command(args))
        transition_meta = action_transition_evidence.get("meta") if isinstance(action_transition_evidence.get("meta"), dict) else {}
        _emit(
            "[all_model_training] "
            f"action_transition_evaluator done elapsed={time.monotonic() - transition_started_at:.2f}s "
            f"evaluation_rows={int(action_transition_evidence.get('evaluation_rows') or 0)} "
            f"summary_rows={int(action_transition_evidence.get('summary_rows') or 0)} "
            f"signal_rows={int(transition_meta.get('signal_rows') or 0)}"
        )
    action_evidence_provenance = None
    if args.skip_action_evidence_provenance:
        _emit("[all_model_training] action_evidence_provenance skipped")
    else:
        action_provenance_started_at = time.monotonic()
        _emit("[all_model_training] action_evidence_provenance start")
        action_evidence_provenance = _run_json_command(_build_action_evidence_provenance_command(args))
        _emit(
            "[all_model_training] "
            f"action_evidence_provenance done elapsed={time.monotonic() - action_provenance_started_at:.2f}s "
            f"provenance_rows={int(action_evidence_provenance.get('provenance_rows') or 0)} "
            f"action_rows={int(action_evidence_provenance.get('action_rows') or 0)}"
        )
    context_overlay_reliability = None
    if args.skip_context_watch_evaluator and args.skip_negative_pressure_evaluator:
        _emit("[all_model_training] context_overlay_reliability_report skipped; both fast context evaluators skipped")
    else:
        context_reliability_started_at = time.monotonic()
        _emit("[all_model_training] context_overlay_reliability_report start")
        context_overlay_reliability = _run_json_command(_build_context_overlay_reliability_command(args))
        _emit(
            "[all_model_training] "
            f"context_overlay_reliability_report done elapsed={time.monotonic() - context_reliability_started_at:.2f}s "
            f"status={context_overlay_reliability.get('status')} "
            f"family_count={int(context_overlay_reliability.get('family_count') or 0)} "
            f"candidate_helpful_count={int(context_overlay_reliability.get('candidate_helpful_count') or 0)}"
        )
    window_runner = None
    window_preflight = None
    if args.run_signal_quality_window_runner:
        window_preflight, window_runner = _run_signal_quality_window_with_preflight(args)
    promotion_review = None
    if args.skip_signal_quality_promotion:
        _emit("[all_model_training] signal_quality_auto_promotion skipped")
    elif args.run_signal_quality_window_runner:
        _emit("[all_model_training] signal_quality_auto_promotion skipped; signal_quality_window_runner owns stable-gated promotion reviews")
    else:
        promotion_started_at = time.monotonic()
        _emit("[all_model_training] signal_quality_auto_promotion start")
        promotion_review = _run_json_command(_build_signal_quality_auto_promotion_command(args))
        _emit(
            "[all_model_training] "
            f"signal_quality_auto_promotion done elapsed={time.monotonic() - promotion_started_at:.2f}s "
            f"status={promotion_review.get('status')} review_count={promotion_review.get('review_count')}"
        )
    print(
        json.dumps(
            _build_research_evidence_payload(
                args=args,
                signal_summary=signal_summary,
                family_report=family_report,
                split_queue=split_queue,
                split_evaluator=split_evaluator,
                context_overlay_signal_backfill=context_overlay_signal_backfill,
                negative_pressure=negative_pressure,
                context_watch=context_watch,
                adversarial_review=adversarial_review_evidence,
                action_transition=action_transition_evidence,
                action_evidence_provenance=action_evidence_provenance,
                event_evidence_store=event_evidence_store,
                causal_event_memory=causal_event_memory,
                causal_event_memory_evaluator=causal_event_memory_evaluator,
                causal_event_provenance=causal_event_provenance,
                context_overlay_reliability=context_overlay_reliability,
                window_preflight=window_preflight,
                window_runner=window_runner,
                promotion_review=promotion_review,
            ),
            indent=2,
            ensure_ascii=False,
            default=str,
        ),
        flush=True,
    )
    _emit(f"[all_model_training] complete total_elapsed={time.monotonic() - started_at:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
