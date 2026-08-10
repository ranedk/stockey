"""Cross-process rate limiter + serializer for all nseindia.com traffic.

NSE's Akamai WAF blocks aggressively on request rate and now effectively requires a real
browser (curl/requests-style scraping of the main site gets rejected outright) -- this session
got blocked repeatedly in practice (2026-08-09/10), including from a burst of debugging
requests spread across multiple independent processes. Small per-call sleeps inside a single
script's own loop are not enough: every process that talks to nseindia.com needs to share ONE
budget.

Every Playwright page.goto() (or plain requests.get()) against nseindia.com/nsearchives.nseindia.com,
across every process on this machine, MUST go through nse_request_gate() (or the nse_goto()
convenience wrapper) so that:
  - at most one process has an NSE request in flight at any time (no parallel requests)
  - consecutive NSE requests are spaced at least NSE_MIN_REQUEST_INTERVAL_SECONDS apart,
    enforced globally across processes -- not just within one script's own loop
"""

from __future__ import annotations

import fcntl
import time
from contextlib import contextmanager
from pathlib import Path

from environs import Env

env = Env()
env.read_env()

BASE_DIR = env("BASE_DIR")
# Deliberately conservative default: NSE now blocks fast on request rate. Do not lower this
# without confirming NSE's current tolerance first -- see the CLAUDE.md "What Not To Do" note
# on widening scope/frequency against a source that has already shown it blocks aggressively.
NSE_MIN_REQUEST_INTERVAL_SECONDS = max(env.float("NSE_MIN_REQUEST_INTERVAL_SECONDS", 10.0), 0.0)
DEFAULT_LOCK_PATH = Path(BASE_DIR) / ".cache" / "nse_rate_limiter.lock"
DEFAULT_TIMESTAMP_PATH = Path(BASE_DIR) / ".cache" / "nse_rate_limiter_last_request_at"


def _read_last_request_time(timestamp_path: Path) -> float | None:
    try:
        return float(timestamp_path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError, OSError):
        return None


def _write_last_request_time(timestamp_path: Path) -> None:
    timestamp_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp_path.write_text(repr(time.time()), encoding="utf-8")


@contextmanager
def nse_request_gate(
    *,
    lock_path: Path = DEFAULT_LOCK_PATH,
    timestamp_path: Path = DEFAULT_TIMESTAMP_PATH,
    min_interval_seconds: float = NSE_MIN_REQUEST_INTERVAL_SECONDS,
    timeout_seconds: float = 900.0,
):
    """Block until it is this process's turn to make ONE request to nseindia.com, then hold
    the cross-process lock for the duration of that single request (caller's `with` body) --
    release it immediately after, so the enforced gap is measured between actual hits to NSE,
    not between logical download attempts (which may include retries, DB writes, parsing).

    Usage:
        with nse_request_gate():
            page.goto("https://www.nseindia.com/...")

    Raises TimeoutError if another process holds the gate for longer than timeout_seconds
    (a stuck/crashed holder) rather than blocking forever.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+")
    acquired = False
    try:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Timed out after {timeout_seconds:.0f}s waiting for another process's "
                        "NSE request to finish (nse_request_gate)"
                    )
                time.sleep(0.5)

        last_request_at = _read_last_request_time(timestamp_path)
        if last_request_at is not None:
            wait_seconds = min_interval_seconds - (time.time() - last_request_at)
            if wait_seconds > 0:
                time.sleep(wait_seconds)

        yield
    finally:
        _write_last_request_time(timestamp_path)
        if acquired:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def nse_goto(page, url: str, **kwargs):
    """Navigate a Playwright page to an nseindia.com URL through the shared cross-process gate."""
    with nse_request_gate():
        return page.goto(url, **kwargs)
