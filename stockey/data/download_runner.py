from __future__ import annotations

import argparse
import importlib
import json
import runpy
import sys
import time
import traceback
from typing import Any

import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event
from advisory.sync_state import persist_sync_state
from utils.redis_utils import install_resilient_redis


DOWNLOADER_STEPS = [
    {"module": "data.nseindia.holidays", "args": [], "purpose": "holiday_calendar"},
    {"module": "data.dhanlive.scrip_master", "args": [], "purpose": "dhan_master_precheck"},
    {"module": "data.sharpelydata.scrip_master", "args": [], "purpose": "sharpely_master_precheck"},
    {"module": "data.company_master", "args": [], "purpose": "identity_build"},
    {"module": "data.dhanlive.ohlcv", "args": [], "purpose": "dhan_ohlcv_precheck"},
    {"module": "data.screenerin.screener_parser", "args": [], "purpose": "screener_sync_registered"},
    {"module": "data.fred.us_macro", "args": [], "purpose": "macro"},
    {"module": "data.eaindustry.wpi", "args": [], "purpose": "macro"},
    {"module": "data.rbi.download_fbil_gsec", "args": [], "purpose": "macro"},
    {"module": "data.rbi.download_bank_rates", "args": [], "purpose": "macro"},
    {"module": "data.mospi.cpi", "args": [], "purpose": "macro"},
    {"module": "data.nsdl.fpi", "args": [], "purpose": "macro"},
    {"module": "data.sharpelydata.sharpely_data", "args": [], "purpose": "fundamentals"},
    {"module": "data.nseindia.corporate_actions", "args": [], "purpose": "events"},
    {"module": "data.nseindia.earnings_events", "args": [], "purpose": "events"},
    {"module": "data.nseindia.insider_deals", "args": [], "purpose": "events"},
    {"module": "data.nseindia.offmarket", "args": [], "purpose": "market_wide"},
    {"module": "data.nseindia.bhavcopy_downloader", "args": [], "purpose": "market_wide"},
    {"module": "data.nseindia.indices_downloader", "args": [], "purpose": "market_wide"},
    {"module": "data.nseindia.recent_events", "args": [], "purpose": "events"},
    {"module": "data.economictimes.rss", "args": [], "purpose": "news"},
]

PARSER_STEPS = [
    {"module": "data.nseindia.offmarket_parser", "args": [], "purpose": "market_wide"},
    {"module": "data.nseindia.bhavcopy_parser", "args": [], "purpose": "market_wide"},
    {"module": "data.nseindia.indices_parser", "args": [], "purpose": "market_wide"},
    {"module": "data.benchmark_sync", "args": [], "purpose": "benchmark_sync"},
    {"module": "advisory.event_evidence_store", "args": [], "purpose": "compact_event_evidence"},
]

DOWNLOAD_STEPS = [*DOWNLOADER_STEPS, *PARSER_STEPS]

PARSER_MODULES = {str(step["module"]) for step in PARSER_STEPS}


def _step_phase(step: dict[str, Any]) -> str:
    module_name = str(step.get("module") or "")
    return "parser" if module_name in PARSER_MODULES else "downloader"


def _source_name(module_name: str) -> str:
    return f"download_runner:{module_name}"


def classify_run_status(*, status: str, module_name: str, error: str | None = None, returncode: int = 0) -> str:
    if status in {"skipped_dry_run", "skipped"}:
        return "skipped"
    if status == "ok" and int(returncode or 0) == 0:
        return "ok"
    text = f"{module_name} {error or ''}".lower()
    if any(token in text for token in ["401", "403", "unauthorized", "forbidden", "token", "login", "auth", "credential"]):
        return "auth_unavailable"
    if any(token in text for token in ["no data", "empty", "not available for date", "no rows"]):
        return "no_data"
    if module_name in PARSER_MODULES or any(token in text for token in ["parser", "parse", "read_excel", "bad zip", "corrupt", "schema", "undefinedcolumn", "valueerror"]):
        return "parse_failed"
    if any(token in text for token in ["timeout", "timed out", "connection", "connectionreset", "connection refused", "503", "502", "504", "rate limit", "nseindia"]):
        return "source_unavailable"
    return "failed"


def _first_present(mapping: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def build_run_state_result(result: dict[str, Any], *, step: dict[str, Any]) -> dict[str, Any]:
    module_name = str(result.get("module") or step.get("module") or "")
    status = str(result.get("status") or "failed")
    error = str(result.get("error") or "") or None
    returncode = int(result.get("returncode") or 0)
    classification = classify_run_status(status=status, module_name=module_name, error=error, returncode=returncode)
    now = pd.Timestamp.utcnow()
    explicit_state_advanced = result.get("state_advanced")
    state = {
        "source": module_name,
        "module": module_name,
        "args": [str(value) for value in (result.get("args") or step.get("args") or [])],
        "purpose": str(result.get("purpose") or step.get("purpose") or ""),
        "phase": _step_phase(step),
        "status": status,
        "classification": classification,
        "elapsed_seconds": result.get("elapsed_seconds"),
        "returncode": returncode,
        "from": result.get("from_date") or result.get("from_datetime"),
        "to": result.get("to_date") or result.get("to_datetime"),
        "rows": result.get("rows") or result.get("rows_written") or result.get("row_count"),
        "rows_written": result.get("rows_written"),
        "rows_read": result.get("rows_read"),
        "skipped": bool(status in {"skipped", "skipped_dry_run"}),
        "retries": _first_present(result, ["retries", "retry_count", "retry_attempts"]),
        "retry_count": _first_present(result, ["retry_count", "retries", "retry_attempts"]),
        "attempt_count": _first_present(result, ["attempt_count", "attempts", "download_attempts"]),
        "failed_attempt_count": _first_present(result, ["failed_attempt_count", "failed_attempts", "failure_count"]),
        "fallback_count": _first_present(result, ["fallback_count", "fallbacks"]),
        "source_unavailable_count": _first_present(result, ["source_unavailable_count", "source_unavailable"]),
        "no_data_count": _first_present(result, ["no_data_count", "empty_count", "empty_processed_count"]),
        "fallback_used": bool(result.get("fallback_used", False)),
        "error_class": (error.split(":", 1)[0] if error and ":" in error else result.get("error_class")),
        "error": error,
        "state_advanced": bool(explicit_state_advanced) if explicit_state_advanced is not None else bool(classification in {"ok", "no_data"}),
        "updated_at": now.isoformat(),
    }
    return {key: value for key, value in state.items() if value not in (None, "", [], {})}


def extract_module_run_state(module_globals: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(module_globals, dict):
        return {}
    for key in ["STOCKEY_RUN_STATE", "RUN_STATE", "RUN_RESULT"]:
        value = module_globals.get(key)
        if isinstance(value, dict):
            return dict(value)
    return {}


def merge_module_run_state(result: dict[str, Any], module_state: dict[str, Any]) -> dict[str, Any]:
    if not module_state:
        return result
    out = dict(result)
    out["module_run_state"] = module_state
    for key in [
        "from_date",
        "from_datetime",
        "to_date",
        "to_datetime",
        "rows",
        "rows_written",
        "rows_read",
        "row_count",
        "retries",
        "retry_count",
        "retry_attempts",
        "attempts",
        "attempt_count",
        "download_attempts",
        "failed_attempt_count",
        "failed_attempts",
        "failure_count",
        "fallback_count",
        "fallbacks",
        "source_unavailable_count",
        "source_unavailable",
        "no_data_count",
        "empty_count",
        "fallback_used",
        "state_advanced",
    ]:
        if key in module_state and out.get(key) in (None, "", [], {}):
            out[key] = module_state[key]
    return out


def persist_download_run_state(result: dict[str, Any], *, step: dict[str, Any]) -> dict[str, Any]:
    run_state = build_run_state_result(result, step=step)
    module_name = str(run_state.get("module") or step.get("module") or "")
    classification = str(run_state.get("classification") or "failed")
    sync_status = "ok" if classification in {"ok", "no_data", "skipped"} else "error"
    error_text = str(run_state.get("error") or classification) if sync_status == "error" else None
    try:
        persist_sync_state(
            source_name=_source_name(module_name),
            scope_key=str(run_state.get("purpose") or "default"),
            last_success_at=pd.Timestamp.utcnow() if sync_status == "ok" else None,
            state=run_state,
            status=sync_status,
            error_text=error_text,
        )
        return {"status": "ok", "source_name": _source_name(module_name), "classification": classification}
    except Exception as exc:
        record_local_fallback_event(
            module="data.download_runner",
            source=_source_name(module_name),
            fallback_type="download_runner_sync_state_persist_failed",
            severity="error",
            reason="Download runner could not persist standardized run state to advisory_sync_state.",
            error=exc,
            metadata={
                "module": module_name,
                "purpose": str(run_state.get("purpose") or ""),
                "classification": classification,
                "sync_status": sync_status,
            },
        )
        return {
            "status": "error",
            "source_name": _source_name(module_name),
            "classification": classification,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _attach_and_persist_run_state(result: dict[str, Any], *, step: dict[str, Any]) -> dict[str, Any]:
    result["run_state"] = build_run_state_result(result, step=step)
    result["sync_state_persist"] = persist_download_run_state(result, step=step)
    if result["sync_state_persist"].get("status") != "ok":
        _emit_progress(
            f"[data.download_runner] sync_state_persist failed module={result.get('module')} error={result['sync_state_persist'].get('error')}"
        )
    return result


def _emit_progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _normalize_exit_code(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    return 1


def _execute_module_entrypoint(module_name: str) -> tuple[int, dict[str, Any]]:
    """Run a Stockey module while preserving its exported run state.

    Most project modules end with ``raise SystemExit(main())``. Running those
    through ``runpy.run_module(..., run_name="__main__")`` loses module globals
    when ``SystemExit`` is raised, so downloader health cannot see
    ``STOCKEY_RUN_STATE``. Importing the module and calling ``main`` directly
    keeps the module namespace available. Keep a runpy fallback for legacy
    modules that do not expose a callable ``main``.
    """
    sys.modules.pop(module_name, None)
    module = importlib.import_module(module_name)
    main_fn = getattr(module, "main", None)
    if callable(main_fn):
        try:
            return _normalize_exit_code(main_fn()), extract_module_run_state(vars(module))
        except SystemExit as exc:
            code = _normalize_exit_code(exc.code)
            if code != 0:
                record_local_fallback_event(
                    module="data.download_runner",
                    source=module_name,
                    fallback_type="download_runner_entrypoint_system_exit_nonzero",
                    severity="error",
                    reason="Download runner module entrypoint raised SystemExit with a non-zero status.",
                    error=f"SystemExit: {code}",
                    metadata={"module": module_name, "returncode": code},
                )
            return code, extract_module_run_state(vars(module))

    sys.modules.pop(module_name, None)
    module_globals = runpy.run_module(module_name, run_name="__main__")
    return 0, extract_module_run_state(module_globals)


def run_download_module(step: dict[str, Any]) -> dict[str, Any]:
    started_at = time.monotonic()
    original_argv = sys.argv[:]
    module_name = str(step.get("module") or "")
    module_args = [str(value) for value in (step.get("args") or [])]
    purpose = str(step.get("purpose") or "")
    _emit_progress(f"[data.download_runner] module={module_name} start purpose={purpose or '-'} args={module_args}")
    try:
        sys.argv = [module_name, *module_args]
        code, module_state = _execute_module_entrypoint(module_name)
        if code != 0:
            result = {
                "module": module_name,
                "args": module_args,
                "purpose": purpose,
                "status": "failed",
                "returncode": code,
                "elapsed_seconds": round(time.monotonic() - started_at, 4),
            }
            result = merge_module_run_state(result, module_state)
            result = _attach_and_persist_run_state(result, step=step)
            record_local_fallback_event(
                module="data.download_runner",
                source=module_name,
                fallback_type="download_runner_module_nonzero_exit",
                severity="error",
                reason="Download runner module returned a non-zero status.",
                error=f"returncode={code}",
                metadata={"module": module_name, "purpose": purpose, "returncode": code},
            )
            _emit_progress(
                f"[data.download_runner] module={module_name} failed returncode={code} elapsed={result['elapsed_seconds']:.2f}s"
            )
            return result
        result = {
            "module": module_name,
            "args": module_args,
            "purpose": purpose,
            "status": "ok",
            "returncode": 0,
            "elapsed_seconds": round(time.monotonic() - started_at, 4),
        }
        result = merge_module_run_state(result, module_state)
        result = _attach_and_persist_run_state(result, step=step)
        _emit_progress(
            f"[data.download_runner] module={module_name} done elapsed={result['elapsed_seconds']:.2f}s"
        )
        return result
    except SystemExit as exc:
        code = _normalize_exit_code(exc.code)
        if code == 0:
            result = {
                "module": module_name,
                "args": module_args,
                "purpose": purpose,
                "status": "ok",
                "returncode": 0,
                "elapsed_seconds": round(time.monotonic() - started_at, 4),
            }
            result = _attach_and_persist_run_state(result, step=step)
            _emit_progress(
                f"[data.download_runner] module={module_name} done elapsed={result['elapsed_seconds']:.2f}s"
            )
            return result
        result = {
            "module": module_name,
            "args": module_args,
            "purpose": purpose,
            "status": "failed",
            "returncode": code,
            "elapsed_seconds": round(time.monotonic() - started_at, 4),
        }
        result = _attach_and_persist_run_state(result, step=step)
        record_local_fallback_event(
            module="data.download_runner",
            source=module_name,
            fallback_type="download_runner_module_nonzero_exit",
            severity="error",
            reason="Download runner module exited with a non-zero status.",
            error=f"SystemExit: {code}",
            metadata={"module": module_name, "purpose": purpose, "returncode": code},
        )
        _emit_progress(
            f"[data.download_runner] module={module_name} failed returncode={code} elapsed={result['elapsed_seconds']:.2f}s"
        )
        return result
    except Exception as exc:
        traceback_text = traceback.format_exc()
        result = {
            "module": module_name,
            "args": module_args,
            "purpose": purpose,
            "status": "failed",
            "returncode": 1,
            "elapsed_seconds": round(time.monotonic() - started_at, 4),
            "error": f"{exc.__class__.__name__}: {exc}",
        }
        result = _attach_and_persist_run_state(result, step=step)
        record_local_fallback_event(
            module="data.download_runner",
            source=module_name,
            fallback_type="download_runner_module_exception",
            severity="error",
            reason="Download runner module raised an exception.",
            error=exc,
            metadata={"module": module_name, "purpose": purpose},
        )
        _emit_progress(
            f"[data.download_runner] module={module_name} failed error={exc.__class__.__name__} elapsed={result['elapsed_seconds']:.2f}s"
        )
        _emit_progress(traceback_text.rstrip())
        return result
    finally:
        sys.argv = original_argv


def _steps_for_phase(phase: str) -> list[dict[str, Any]]:
    normalized = str(phase or "all").strip().lower()
    if normalized == "downloaders":
        return DOWNLOADER_STEPS
    if normalized == "parsers":
        return PARSER_STEPS
    if normalized == "all":
        return DOWNLOAD_STEPS
    raise ValueError(f"Unsupported phase: {phase}")


def run_all_downloads(*, continue_on_error: bool = False, dry_run: bool = False, phase: str = "all") -> dict[str, Any]:
    install_resilient_redis()
    steps = _steps_for_phase(phase)
    if dry_run:
        return {
            "status": "skipped_dry_run",
            "phase": phase,
            "modules": [step["module"] for step in steps],
            "steps": steps,
            "results": [],
        }

    results: list[dict[str, Any]] = []
    status = "ok"
    for step in steps:
        result = run_download_module(step)
        results.append(result)
        if result["status"] != "ok":
            status = "warning" if continue_on_error else "failed"
            if not continue_on_error:
                break
    return {
        "status": status,
        "phase": phase,
        "modules": [step["module"] for step in steps],
        "steps": steps,
        "results": results,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run downloader/parser modules sequentially in-process.")
    parser.add_argument("--phase", choices=["downloaders", "parsers", "all"], default="all")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = run_all_downloads(
        continue_on_error=bool(args.continue_on_error),
        dry_run=bool(args.dry_run),
        phase=str(args.phase),
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return 0 if payload.get("status") != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
