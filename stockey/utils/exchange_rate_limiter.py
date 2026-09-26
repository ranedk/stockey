"""Cross-process rate limiter + serializer, generalized per source domain.

Generalized from utils/nse_rate_limiter.py (built 2026-08-09/10 after NSE's Akamai
WAF blocked this session repeatedly on request rate) so BSE -- and any future
exchange/agency source -- gets the same discipline without a copy-pasted second
implementation. utils/nse_rate_limiter.py is now a thin backward-compatible wrapper
around exchange_gate(domain="nse", ...); existing NSE callers are unchanged.

Every Playwright page.goto() (or plain requests.get()) against a rate-sensitive
source, across every process on this machine, MUST go through exchange_request_gate()
so that, per domain:
  - at most one process has a request in flight at any time (no parallel requests)
  - consecutive requests are spaced at least <DOMAIN>_MIN_REQUEST_INTERVAL_SECONDS
    apart, enforced globally across processes -- not just within one script's own loop
"""

from __future__ import annotations

import fcntl
import random
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


_WAIT_JITTER_SECONDS = 0.5
_POLL_SECONDS = 0.05


def _acquire(handle, *, deadline: float, domain: str, timeout_seconds: float) -> None:
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out after {timeout_seconds:.0f}s waiting for another process's "
                    f"{domain} request to finish (exchange_request_gate)"
                )
            time.sleep(_POLL_SECONDS)


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
        waited_for = None
        while True:
            _acquire(handle, deadline=deadline, domain=domain, timeout_seconds=timeout_seconds)
            acquired = True
            last_request_at = _read_last_request_time(timestamp_path)
            wait_seconds = 0.0 if last_request_at is None else min_interval_seconds - (time.time() - last_request_at)
            # Proceed if the gap has passed, or if we already waited out exactly this last
            # request and nobody else went in the meantime.
            if wait_seconds <= 0 or last_request_at == waited_for:
                break
            # FAIRNESS FIX 2026-09-25: the gap used to be slept out WHILE HOLDING the lock.
            # A process that loops on the gate (a backfill) re-took the lock the instant it
            # released it, and a second process polling every 0.5s almost never got a turn:
            # the OCR job fetched ~1 document in 25 minutes beside the BSE announcements
            # backfill. Now the wait happens outside the lock, so every waiting process
            # competes when the gap ends; the jitter keeps the same one from always winning.
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            acquired = False
            time.sleep(wait_seconds + random.uniform(0, _WAIT_JITTER_SECONDS))
            waited_for = last_request_at

        yield
    finally:
        # BUG FOUND LIVE 2026-08-19 (re-audit): this write was unconditional -- unlike the unlock
        # call right below it, which correctly checks `if acquired:`. A process that times out
        # waiting for the lock (never acquired it, never made a request) still overwrote the
        # shared last-request timestamp file. Reproduced live: a timed-out non-holder wrote
        # time.time() at the moment of ITS OWN timeout into the file the real lock holder may be
        # concurrently reading/writing -- an unsynchronized file access this gate's own contract
        # ("hold the cross-process lock for the duration of that single request") says shouldn't
        # happen. By construction the bogus value can't be OLDER than the real last request (it's
        # always time.time() at this process's own timeout, later than when the holder actually
        # started), so this couldn't shrink the enforced gap below the floor on its own -- but it's
        # still a real unsynchronized write this gate exists specifically to prevent.
        if acquired:
            _write_last_request_time(timestamp_path)
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()
