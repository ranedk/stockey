"""Backward-compatible NSE-specific wrapper around utils.exchange_rate_limiter.

Generalized 2026-08-10 into utils/exchange_rate_limiter.py so BSE (and any future
rate-sensitive source) gets the same cross-process discipline without a copy-pasted
second implementation -- see that module's docstring for the full rationale (NSE's
Akamai WAF blocked this session repeatedly on request rate, 2026-08-09/10).

This module keeps its original public names/defaults/behavior unchanged so the
existing NSE call sites (data/nseindia/*.py) don't need to change their imports.
"""

from __future__ import annotations

import time  # noqa: F401 -- `time` module is a process-wide singleton; tests patch
# nse_rate_limiter.time.sleep/.time, which also affects exchange_rate_limiter.py's
# calls since both modules' `import time` reference the same object in sys.modules.
from contextlib import contextmanager
from pathlib import Path

from utils.exchange_rate_limiter import (
    BASE_DIR,
    default_min_interval_seconds,
    exchange_request_gate,
)

NSE_MIN_REQUEST_INTERVAL_SECONDS = default_min_interval_seconds("nse")
DEFAULT_LOCK_PATH = Path(BASE_DIR) / ".cache" / "nse_rate_limiter.lock"
DEFAULT_TIMESTAMP_PATH = Path(BASE_DIR) / ".cache" / "nse_rate_limiter_last_request_at"


@contextmanager
def nse_request_gate(
    *,
    lock_path: Path | None = None,
    timestamp_path: Path | None = None,
    min_interval_seconds: float | None = None,
    timeout_seconds: float = 900.0,
):
    """See utils.exchange_rate_limiter.exchange_request_gate (domain="nse"). Reads
    DEFAULT_LOCK_PATH/DEFAULT_TIMESTAMP_PATH/NSE_MIN_REQUEST_INTERVAL_SECONDS from
    this module at call time (not baked into parameter defaults), so tests that
    monkeypatch those module attributes take effect."""
    with exchange_request_gate(
        domain="nse",
        lock_path=lock_path if lock_path is not None else DEFAULT_LOCK_PATH,
        timestamp_path=timestamp_path if timestamp_path is not None else DEFAULT_TIMESTAMP_PATH,
        min_interval_seconds=(
            min_interval_seconds if min_interval_seconds is not None else NSE_MIN_REQUEST_INTERVAL_SECONDS
        ),
        timeout_seconds=timeout_seconds,
    ):
        yield


def nse_goto(page, url: str, **kwargs):
    """Navigate a Playwright page to an nseindia.com URL through the shared cross-process gate."""
    with nse_request_gate():
        return page.goto(url, **kwargs)
