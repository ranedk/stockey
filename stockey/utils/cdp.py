"""Shared Chrome/CDP preflight for every Playwright consumer in stockey.

Why this exists (2026-08-31): Chrome-with-remote-debugging is host state -- it does
NOT survive a reboot and nothing restarts it (deliberately: the operator starts it by
hand, see HANDOFF.md). When it is down, `connect_over_cdp` does not fail fast. It
opens the websocket, then blocks for the full 30s Playwright timeout and dies with a
stack trace that names `_browser_type.py`, not Chrome. That trace is what buried the
2026-08-26..28 outage: `data_readiness` -- the one job whose whole purpose is to shout
when data goes stale -- was itself crashing on it, silently, for three days, while
bhavcopy landed after the adjustment window and `advisory_adjusted_ohlcv_daily` sat
three trading days behind with every cron job still exiting 0.

So: preflight the endpoint over plain HTTP with a 2s budget and, when it is down, exit
on a banner nobody can scroll past instead of a 30s hang and a misleading traceback.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

# Distinct from 1 (a real error) so a caller -- or a cron wrapper reading exit codes --
# can tell "the operator has not started Chrome" apart from "this collector is broken".
CDP_UNAVAILABLE_EXIT_CODE = 3

_PREFLIGHT_TIMEOUT_SECONDS = 2.0


class CDPUnavailable(RuntimeError):
    """Chrome is not reachable at the configured CDP endpoint."""


def cdp_status(endpoint: str) -> tuple[bool, str]:
    """Return (reachable, detail). Never raises, never blocks longer than ~2s.

    Hits /json/version, which is a cheap static handler -- it does not open a tab or
    touch the browser's page state the way connect_over_cdp does.
    """
    if not endpoint:
        return False, "CDP_ENDPOINT is not set"
    url = f"{endpoint.rstrip('/')}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=_PREFLIGHT_TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.URLError as exc:
        return False, f"cannot reach {url}: {exc.reason}"
    except Exception as exc:  # noqa: BLE001 -- a preflight must never be the thing that crashes
        return False, f"cannot reach {url}: {type(exc).__name__}: {exc}"
    return True, str(payload.get("Browser", "unknown build"))


def _banner(endpoint: str, detail: str, caller: str) -> str:
    endpoint_shown = endpoint or "(unset)"
    # Colour only when stderr is a terminal -- a cron log full of raw escape codes is
    # harder to read, not more visible, which defeats the entire point of this banner.
    tty = sys.stderr.isatty()
    red = "\033[1;41;97m" if tty else ""
    hot = "\033[1;31m" if tty else ""
    warn = "\033[1;33m" if tty else ""
    go = "\033[1;32m" if tty else ""
    off = "\033[0m" if tty else ""
    bar = red + "#" * 78 + off

    def band(text: str = "") -> str:
        return red + ("  " + text).ljust(78) + off

    lines = [
        "",
        bar,
        band(),
        band("CHROME IS NOT RUNNING -- THIS JOB CANNOT COLLECT ANYTHING"),
        band(),
        bar,
        "",
        f"{hot}  caller       :{off} {caller}",
        f"{hot}  CDP endpoint :{off} {endpoint_shown}",
        f"{hot}  detail       :{off} {detail}",
        "",
        f"{warn}  Chrome with remote debugging is host state. It does not survive a reboot",
        f"  and nothing restarts it automatically -- that is deliberate.{off}",
        "",
        f"{go}  START IT:{off}",
        "      cd ~/code/trading/stockey && scripts/start_chrome_cdp.sh",
        "",
        f"{go}  VERIFY:{off}",
        "      curl -s http://localhost:9222/json/version",
        "",
        f"{hot}  exiting {CDP_UNAVAILABLE_EXIT_CODE} without touching the database.{off}",
        bar,
        "",
    ]
    return "\n".join(lines)


def require_cdp(endpoint: str, *, caller: str) -> None:
    """Exit loudly (code 3) if Chrome is not reachable. Call before any Playwright work."""
    reachable, detail = cdp_status(endpoint)
    if reachable:
        return
    print(_banner(endpoint, detail, caller), file=sys.stderr, flush=True)
    raise SystemExit(CDP_UNAVAILABLE_EXIT_CODE)


def connect_over_cdp(playwright, endpoint: str, *, caller: str):
    """Drop-in for `playwright.chromium.connect_over_cdp(endpoint)` with the preflight.

    Same return value, so call sites change by one argument and keep their existing
    browser/context/page handling untouched.
    """
    require_cdp(endpoint, caller=caller)
    return playwright.chromium.connect_over_cdp(endpoint)
