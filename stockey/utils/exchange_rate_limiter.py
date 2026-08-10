"""Cross-process rate limiter + serializer, generalized per source domain.

Generalized from utils/nse_rate_limiter.py (built 2026-08-09/10 after NSE's Akamai
WAF blocked this session repeatedly on request rate) so BSE -- and any future
exchange/agency source -- gets the same discipline without a copy-pasted second
implementation. utils/nse_rate_limiter.py is now a thin backward-compatible wrapper
around exchange_gate(domain="nse", ...); existing NSE callers are unchanged.

Every Playwright page.goto() (or plain requests.get()) against a rate-sensitive
source, across every process on this machine, MUST go through exchange_request_gate()
(or the exchange_goto() convenience wrapper) so that, per domain:
  - at most one process has a request in flight at any time (no parallel requests)
  - consecutive requests are spaced at least <DOMAIN>_MIN_REQUEST_INTERVAL_SECONDS
    apart, enforced globally across processes -- not just within one script's own loop
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


def default_lock_path(domain: str) -> Path:
    return Path(BASE_DIR) / ".cache" / f"{domain}_rate_limiter.lock"


def default_timestamp_path(domain: str) -> Path:
    return Path(BASE_DIR) / ".cache" / f"{domain}_rate_limiter_last_request_at"


def default_min_interval_seconds(domain: str, *, fallback: float = 10.0) -> float:
    # Deliberately conservative default: rate-sensitive sources (NSE already proved
    # this) block fast. Do not lower without confirming the specific source's current
    # tolerance first -- see CLAUDE.md's "What Not To Do" note on widening scope/
    # frequency against a source that has already shown it blocks aggressively.
    #
    # NOTE for scripts/env_example_audit.py: the env var name below is built
    # dynamically (f"{domain.upper()}_..."), so NSE_MIN_REQUEST_INTERVAL_SECONDS and
    # BSE_MIN_REQUEST_INTERVAL_SECONDS will show up as false-positive "unused" in that
    # audit's static regex scan -- they are used, just not as a literal string a
    # regex can match. Both are genuinely read here for every domain this module gates.
    return max(env.float(f"{domain.upper()}_MIN_REQUEST_INTERVAL_SECONDS", fallback), 0.0)


def _read_last_request_time(timestamp_path: Path) -> float | None:
    try:
        return float(timestamp_path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError, OSError):
        return None


def _write_last_request_time(timestamp_path: Path) -> None:
    timestamp_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp_path.write_text(repr(time.time()), encoding="utf-8")


@contextmanager
def exchange_request_gate(
    *,
    domain: str,
    lock_path: Path | None = None,
    timestamp_path: Path | None = None,
    min_interval_seconds: float | None = None,
    timeout_seconds: float = 900.0,
):
    """Block until it is this process's turn to make ONE request against `domain`,
    then hold the cross-process lock for the duration of that single request
    (caller's `with` body) -- release it immediately after, so the enforced gap is
    measured between actual hits to the source, not between logical fetch attempts
    (which may include retries, DB writes, parsing).

    Usage:
        with exchange_request_gate(domain="bse"):
            page.goto("https://www.bseindia.com/...")

    Raises TimeoutError if another process holds this domain's gate for longer than
    timeout_seconds (a stuck/crashed holder) rather than blocking forever. Separate
    domains never block each other -- each gets its own lock file.
    """
    lock_path = lock_path or default_lock_path(domain)
    timestamp_path = timestamp_path or default_timestamp_path(domain)
    if min_interval_seconds is None:
        min_interval_seconds = default_min_interval_seconds(domain)

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
                        f"{domain} request to finish (exchange_request_gate)"
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


def exchange_goto(page, url: str, *, domain: str, **kwargs):
    """Navigate a Playwright page to a `domain` URL through the shared cross-process gate."""
    with exchange_request_gate(domain=domain):
        return page.goto(url, **kwargs)
