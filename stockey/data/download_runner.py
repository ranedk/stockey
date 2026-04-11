from __future__ import annotations

import argparse
import json
import runpy
import sys
import time
import traceback
from typing import Any

from utils.redis_utils import install_resilient_redis


DOWNLOAD_STEPS = [
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
    {"module": "data.nseindia.offmarket_parser", "args": [], "purpose": "market_wide"},
    {"module": "data.nseindia.bhavcopy_downloader", "args": [], "purpose": "market_wide"},
    {"module": "data.nseindia.bhavcopy_parser", "args": [], "purpose": "market_wide"},
    {"module": "data.nseindia.indices_downloader", "args": [], "purpose": "market_wide"},
    {"module": "data.nseindia.indices_parser", "args": [], "purpose": "market_wide"},
    {"module": "data.nseindia.recent_events", "args": [], "purpose": "events"},
    {"module": "data.economictimes.rss", "args": [], "purpose": "news"},
]


def _emit_progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _normalize_exit_code(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    return 1


def run_download_module(step: dict[str, Any]) -> dict[str, Any]:
    started_at = time.monotonic()
    original_argv = sys.argv[:]
    module_name = str(step.get("module") or "")
    module_args = [str(value) for value in (step.get("args") or [])]
    purpose = str(step.get("purpose") or "")
    _emit_progress(f"[data.download_runner] module={module_name} start purpose={purpose or '-'} args={module_args}")
    try:
        sys.argv = [module_name, *module_args]
        runpy.run_module(module_name, run_name="__main__")
        result = {
            "module": module_name,
            "args": module_args,
            "purpose": purpose,
            "status": "ok",
            "returncode": 0,
            "elapsed_seconds": round(time.monotonic() - started_at, 4),
        }
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
        _emit_progress(
            f"[data.download_runner] module={module_name} failed error={exc.__class__.__name__} elapsed={result['elapsed_seconds']:.2f}s"
        )
        _emit_progress(traceback_text.rstrip())
        return result
    finally:
        sys.argv = original_argv


def run_all_downloads(*, continue_on_error: bool = False, dry_run: bool = False) -> dict[str, Any]:
    install_resilient_redis()
    if dry_run:
        return {
            "status": "skipped_dry_run",
            "modules": [step["module"] for step in DOWNLOAD_STEPS],
            "steps": DOWNLOAD_STEPS,
            "results": [],
        }

    results: list[dict[str, Any]] = []
    status = "ok"
    for step in DOWNLOAD_STEPS:
        result = run_download_module(step)
        results.append(result)
        if result["status"] != "ok":
            status = "warning" if continue_on_error else "failed"
            if not continue_on_error:
                break
    return {
        "status": status,
        "modules": [step["module"] for step in DOWNLOAD_STEPS],
        "steps": DOWNLOAD_STEPS,
        "results": results,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run all raw download modules sequentially in-process.")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = run_all_downloads(
        continue_on_error=bool(args.continue_on_error),
        dry_run=bool(args.dry_run),
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return 0 if payload.get("status") != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
