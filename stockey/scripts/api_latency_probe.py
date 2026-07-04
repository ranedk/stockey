from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from environs import Env

from advisory.fallback_telemetry import record_local_fallback_event
from advisory.performance_slowlog import record_slow_operation


env = Env()
env.read_env()

DEFAULT_BASE_URL = env.str("OPERATOR_API_BASE_URL", "http://127.0.0.1:8085")
DEFAULT_ENDPOINTS = [
    "/api/health",
    "/api/home",
    "/api/summary",
    "/api/actions?limit=25&compact=true",
    "/api/portfolio?limit=25&compact=true",
    "/api/watchlist?limit=25&compact=true",
    "/api/workbench",
    "/api/data-health",
]
DEFAULT_THRESHOLD_MS = env.float("API_LATENCY_PROBE_SLOW_MS", 750.0)
DEFAULT_TIMEOUT_SECONDS = env.float("API_LATENCY_PROBE_TIMEOUT_SECONDS", 30.0)
DEFAULT_OUTPUT_PATH = Path(env.str("API_LATENCY_PROBE_OUTPUT_FILE", "logs/performance/latest_api_latency_probe.json"))


def _record_probe_fallback(
    *,
    fallback_type: str,
    reason: str,
    endpoint: str,
    error: Exception,
    status_code: int | None = None,
) -> None:
    record_local_fallback_event(
        module="scripts.api_latency_probe",
        source="operator_api",
        fallback_type=fallback_type,
        severity="warn",
        reason=reason,
        error=error,
        metadata={
            "endpoint": endpoint,
            "route": endpoint.split("?", 1)[0],
            "status_code": status_code,
        },
    )


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def write_probe_summary(path: str | Path | None, payload: dict[str, Any]) -> str | None:
    if not path:
        return None
    output_path = Path(path).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, default=_json_default), encoding="utf-8")
    return str(output_path)


def probe_endpoint(base_url: str, endpoint: str, *, timeout_seconds: float, threshold_ms: float) -> dict[str, Any]:
    url = base_url.rstrip("/") + endpoint
    started = time.perf_counter()
    status_code = None
    error = None
    body_bytes = 0
    try:
        with urllib.request.urlopen(url, timeout=float(timeout_seconds)) as response:
            status_code = int(response.status)
            body = response.read()
            body_bytes = len(body)
    except urllib.error.HTTPError as exc:
        status_code = int(exc.code)
        error = f"HTTPError: {exc}"
        body_bytes = len(exc.read() or b"")
        _record_probe_fallback(
            fallback_type="api_latency_probe_http_error",
            reason="Operator API latency probe received an HTTP error response.",
            endpoint=endpoint,
            error=exc,
            status_code=status_code,
        )
    except Exception as exc:
        error = f"{exc.__class__.__name__}: {exc}"
        _record_probe_fallback(
            fallback_type="api_latency_probe_request_failed",
            reason="Operator API latency probe could not reach or read the endpoint.",
            endpoint=endpoint,
            error=exc,
        )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    event = record_slow_operation(
        kind="api_latency_probe",
        operation=f"GET {endpoint.split('?', 1)[0]}",
        elapsed_ms=elapsed_ms,
        threshold_ms=threshold_ms,
        details={
            "method": "GET",
            "route": endpoint.split("?", 1)[0],
            "endpoint": endpoint,
            "status_code": status_code,
            "body_bytes": body_bytes,
            "error": error,
        },
    )
    return {
        "endpoint": endpoint,
        "status_code": status_code,
        "elapsed_ms": round(elapsed_ms, 2),
        "body_bytes": body_bytes,
        "error": error,
        "slow_logged": event is not None,
        "fingerprint": None if event is None else event.get("fingerprint"),
        "is_new_slow_issue": None if event is None else event.get("is_new"),
    }


def run_probe(
    *,
    base_url: str = DEFAULT_BASE_URL,
    endpoints: list[str] | None = None,
    threshold_ms: float = DEFAULT_THRESHOLD_MS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    rows = [
        probe_endpoint(base_url, endpoint, timeout_seconds=timeout_seconds, threshold_ms=threshold_ms)
        for endpoint in (endpoints or DEFAULT_ENDPOINTS)
    ]
    slow_rows = [row for row in rows if row.get("slow_logged")]
    errors = [row for row in rows if row.get("error") or (row.get("status_code") and int(row["status_code"]) >= 400)]
    return {
        "status": "ok" if not errors else "error",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "threshold_ms": threshold_ms,
        "timeout_seconds": timeout_seconds,
        "endpoint_count": len(rows),
        "slow_count": len(slow_rows),
        "error_count": len(errors),
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe operator API endpoint latency and write slow-operation logs.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--endpoint", action="append", dest="endpoints", help="Endpoint path to probe. Can be repeated.")
    parser.add_argument("--threshold-ms", type=float, default=DEFAULT_THRESHOLD_MS)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--output-path", "--output", dest="output_path", default=str(DEFAULT_OUTPUT_PATH), help="Where to write the latest probe summary JSON. Use empty string to disable.")
    args = parser.parse_args(argv)
    result = run_probe(
        base_url=args.base_url,
        endpoints=args.endpoints,
        threshold_ms=float(args.threshold_ms),
        timeout_seconds=float(args.timeout_seconds),
    )
    output_path = str(Path(args.output_path).expanduser()) if args.output_path else None
    if output_path:
        result["output_path"] = output_path
    write_probe_summary(output_path, result)
    print(json.dumps(result, indent=2, default=_json_default))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
