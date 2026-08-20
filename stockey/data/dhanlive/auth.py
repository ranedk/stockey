from __future__ import annotations

import asyncio
import fcntl
import json
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from environs import Env

from utils.fallback_telemetry import record_local_fallback_event


env = Env()
env.read_env()


AUTH_BASE_URL = "https://auth.dhan.co"
DEFAULT_TOKEN_CACHE = Path(env("BASE_DIR")) / ".cache" / "dhan_access_token.json"
DEFAULT_LOGIN_LOCK = Path(env("BASE_DIR")) / ".cache" / "dhan_login.lock"


class DhanAuthError(RuntimeError):
    pass


def _record_dhan_auth_fallback(
    *,
    fallback_type: str,
    reason: str,
    error: Exception | str,
    metadata: dict | None = None,
) -> None:
    record_local_fallback_event(
        module="data.dhanlive.auth",
        source="dhan_auth",
        fallback_type=fallback_type,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def load_cached_access_token_payload(cache_path: Path = DEFAULT_TOKEN_CACHE) -> dict | None:
    if not cache_path.exists():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _record_dhan_auth_fallback(
            fallback_type="dhan_cached_access_token_read_failed",
            reason="Dhan cached access-token payload could not be read or parsed; cached broker auth was ignored.",
            error=exc,
            metadata={"cache_path": str(cache_path)},
        )
        return None
    return payload if isinstance(payload, dict) else None


def load_cached_access_token(cache_path: Path = DEFAULT_TOKEN_CACHE) -> str | None:
    payload = load_cached_access_token_payload(cache_path)
    if payload is None:
        return None
    token = payload.get("accessToken")
    expiry_time = payload.get("expiryTime")
    if not token or not expiry_time:
        return None

    expires_at = _parse_expiry(expiry_time)
    if expires_at is None:
        return None
    if datetime.now(timezone.utc) + timedelta(minutes=5) >= expires_at:
        return None
    return token


def cache_access_token(payload: dict, cache_path: Path = DEFAULT_TOKEN_CACHE) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def clear_cached_access_token(cache_path: Path = DEFAULT_TOKEN_CACHE) -> bool:
    try:
        cache_path.unlink()
        return True
    except FileNotFoundError as exc:
        _record_dhan_auth_fallback(
            fallback_type="dhan_cached_access_token_clear_missing",
            reason="Requested Dhan cached access-token clear, but no cache file was present.",
            error=exc,
            metadata={"cache_path": str(cache_path)},
        )
        return False


def generate_consent_app_id(
    *,
    client_id: str | None = None,
    api_key: str | None = None,
    api_secret: str | None = None,
    timeout: int = 60,
) -> str:
    client_id = client_id or env("DHAN_CLIENT_ID")
    api_key = api_key or env("DHAN_API_KEY")
    api_secret = api_secret or env("DHAN_API_SECRET")

    response = requests.post(
        f"{AUTH_BASE_URL}/app/generate-consent",
        params={"client_id": client_id},
        headers={
            "Accept": "application/json",
            "app_id": api_key,
            "app_secret": api_secret,
        },
        timeout=timeout,
    )
    payload = _parse_json_response(response)
    if not response.ok:
        raise DhanAuthError(f"Generate consent failed: {payload}")

    consent_app_id = payload.get("consentAppId")
    if not consent_app_id:
        raise DhanAuthError(f"Generate consent returned no consentAppId: {payload}")
    return str(consent_app_id)


def consume_consent_token(
    token_id: str,
    *,
    api_key: str | None = None,
    api_secret: str | None = None,
    timeout: int = 60,
) -> dict:
    api_key = api_key or env("DHAN_API_KEY")
    api_secret = api_secret or env("DHAN_API_SECRET")
    response = requests.get(
        f"{AUTH_BASE_URL}/app/consumeApp-consent",
        params={"tokenId": token_id},
        headers={
            "Accept": "application/json",
            "app_id": api_key,
            "app_secret": api_secret,
        },
        timeout=timeout,
    )
    payload = _parse_json_response(response)
    if not response.ok:
        raise DhanAuthError(f"Consume consent failed: {payload}")
    if not payload.get("accessToken"):
        raise DhanAuthError(f"Consume consent returned no accessToken: {payload}")
    cache_access_token(payload)
    return payload


def build_consent_login_url(consent_app_id: str) -> str:
    return f"{AUTH_BASE_URL}/login/consentApp-login?consentAppId={consent_app_id}"


def build_new_consent_url() -> str:
    consent_app_id = generate_consent_app_id()
    return build_consent_login_url(consent_app_id)


def begin_browser_consent() -> str:
    consent_url = build_new_consent_url()
    open_browser_url(consent_url)
    return consent_url


def is_auto_login_configured() -> bool:
    if env.bool("DHAN_AUTO_LOGIN_ENABLED", default=False):
        return True
    return all(
        bool(env(name, default=None))
        for name in ("CDP_ENDPOINT", "DHAN_LOGIN_MOBILE", "DHAN_LOGIN_PIN", "DHAN_TOTP_SECRET")
    )


def _is_inside_running_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _extract_token_id_from_auto_login_stdout(stdout: str) -> str:
    text = str(stdout or "").strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        _record_dhan_auth_fallback(
            fallback_type="dhan_auto_login_subprocess_json_parse_failed",
            reason="Dhan automated-login subprocess did not return parseable JSON.",
            error=exc,
            metadata={"stdout_tail": text[-500:]},
        )
        raise DhanAuthError("Dhan auto-login subprocess returned non-JSON output") from exc
    token_id = normalize_token_id(str(payload.get("token_id") or ""))
    if not token_id:
        raise DhanAuthError(f"Dhan auto-login subprocess returned no token_id: {payload}")
    return token_id


def _get_token_id_via_auto_login_subprocess(consent_url: str) -> str:
    command = [
        sys.executable,
        "-m",
        "data.dhanlive.web_login",
        "--consent-url",
        consent_url,
        "--print-token-id",
    ]
    timeout_seconds = max(int(env.int("DHAN_AUTO_LOGIN_SUBPROCESS_TIMEOUT_SECONDS", default=180)), 1)
    print(
        "[data.dhanlive.auth] running Dhan automated login in subprocess to avoid Playwright sync API inside an asyncio loop",
        file=sys.stderr,
        flush=True,
    )
    try:
        proc = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _record_dhan_auth_fallback(
            fallback_type="dhan_auto_login_subprocess_failed",
            reason="Dhan automated-login subprocess could not complete.",
            error=exc,
            metadata={"timeout_seconds": timeout_seconds},
        )
        raise DhanAuthError(f"Dhan auto-login subprocess failed: {exc}") from exc
    if proc.returncode != 0:
        _record_dhan_auth_fallback(
            fallback_type="dhan_auto_login_subprocess_nonzero",
            reason="Dhan automated-login subprocess exited with a non-zero status.",
            error=f"returncode={proc.returncode}",
            metadata={"stderr_tail": str(proc.stderr or "")[-1000:], "stdout_tail": str(proc.stdout or "")[-500:]},
        )
        raise DhanAuthError(f"Dhan auto-login subprocess failed with exit code {proc.returncode}: {str(proc.stderr or '').strip()}")
    return _extract_token_id_from_auto_login_stdout(proc.stdout)


def _is_playwright_sync_inside_loop_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return "playwright sync api" in text and "asyncio loop" in text


def get_token_id_from_auto_login() -> str:
    from data.dhanlive.web_login import get_token_id_via_automated_login

    consent_url = build_new_consent_url()
    if _is_inside_running_event_loop():
        return _get_token_id_via_auto_login_subprocess(consent_url)
    try:
        return get_token_id_via_automated_login(consent_url)
    except Exception as exc:
        if not _is_playwright_sync_inside_loop_error(exc):
            raise
        _record_dhan_auth_fallback(
            fallback_type="dhan_auto_login_sync_playwright_inside_async_loop",
            reason="Dhan automated login hit Playwright sync API inside an asyncio loop; retrying in subprocess.",
            error=exc,
            metadata={"consent_url_host": "auth.dhan.co"},
        )
        return _get_token_id_via_auto_login_subprocess(consent_url)


@contextmanager
def _dhan_login_lock(lock_path: Path = DEFAULT_LOGIN_LOCK, *, timeout_seconds: float = 300.0):
    """Serialize the CDP-driven Dhan login across processes/cron jobs. Only one process may
    drive the shared Chrome CDP session's login form at a time -- concurrent callers (e.g. a
    manual run overlapping a scheduled downloader) would otherwise both submit mobile/TOTP/PIN
    into the same browser tab, and Dhan's login rate-limiter rejects the second submission as
    'too many attempts'. Blocks (polling) up to timeout_seconds, then raises rather than hanging
    forever if a holder is stuck; the OS releases the lock automatically if a holder dies."""
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
                    raise DhanAuthError(
                        f"Timed out after {timeout_seconds:.0f}s waiting for another process's Dhan login to finish"
                    )
                time.sleep(1.0)
        yield
    finally:
        if acquired:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def get_access_token() -> str:
    direct = env("DHAN_ACCESS_TOKEN", default=None)
    if direct:
        return direct

    cached = load_cached_access_token()
    if cached:
        return cached

    token_id = normalize_token_id(env("DHAN_TOKEN_ID", default=None))
    if token_id:
        return str(consume_consent_token(token_id)["accessToken"])

    # Only one process should drive the login UI at a time. Re-check the cache after
    # acquiring the lock in case a concurrent caller just finished logging in.
    with _dhan_login_lock():
        cached = load_cached_access_token()
        if cached:
            return cached
        if is_auto_login_configured():
            return str(consume_consent_token(get_token_id_from_auto_login())["accessToken"])
        consent_url = begin_browser_consent()
        pasted_token_id = prompt_for_token_id(consent_url)
        return str(consume_consent_token(pasted_token_id)["accessToken"])


def force_refresh_access_token(current_token: str | None = None) -> str:
    """Force a real re-login, unless a concurrent caller already refreshed while we waited
    for the lock. ``current_token`` (the caller's just-failed token) makes that distinction:
    a same-as-before cached token means nothing changed and this really is a dead token that
    needs a fresh login, not a cache to reuse -- comparing only "is something cached and
    timestamp-valid" (without current_token) would keep returning that same dead token forever
    whenever Dhan invalidates a token server-side before its stated expiry."""
    with _dhan_login_lock():
        cached = load_cached_access_token()
        if cached and cached != current_token:
            return cached
        clear_cached_access_token()
        if is_auto_login_configured():
            token_id = get_token_id_from_auto_login()
        else:
            consent_url = begin_browser_consent()
            token_id = prompt_for_token_id(consent_url)
        return str(consume_consent_token(token_id)["accessToken"])


def extract_token_id(url: str) -> str | None:
    parsed = urlparse(url)
    token_values = parse_qs(parsed.query).get("tokenId")
    if token_values:
        return token_values[0]
    return None


def normalize_token_id(raw_value: str | None) -> str | None:
    if not raw_value:
        return None
    value = raw_value.strip()
    if not value:
        return None
    if "tokenId=" in value:
        return extract_token_id(value)
    return value


def prompt_for_token_id(consent_url: str) -> str:
    if not sys.stdin.isatty():
        raise DhanAuthError(
            "Dhan login requires browser consent, but stdin is not interactive. "
            "Open the consent URL in a browser, then set DHAN_TOKEN_ID to the redirected URL or tokenId and rerun. "
            f"Consent URL: {consent_url}"
        )

    print(
        "Complete the Dhan login in the opened browser tab, then paste the full redirected URL here.",
        flush=True,
    )
    print(
        "Expected format: https://trade.singularity45.ai/dhan/?tokenId=...",
        flush=True,
    )
    raw_value = input("Redirect URL: ").strip()
    token_id = normalize_token_id(raw_value)
    if not token_id:
        raise DhanAuthError(
            "Could not extract tokenId from the pasted value. "
            "Paste the full redirected URL or the raw tokenId."
        )
    return token_id


def open_browser_url(url: str) -> None:
    commands: list[list[str]] = []
    chrome_binary = env("CHROME_BINARY", default=None)
    if chrome_binary:
        commands.append([chrome_binary, url])
    commands.extend(
        [
            ["google-chrome", url],
            ["xdg-open", url],
            ["open", url],
        ]
    )

    for command in commands:
        try:
            subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except FileNotFoundError as exc:
            _record_dhan_auth_fallback(
                fallback_type="dhan_browser_launcher_missing",
                reason="Configured Dhan browser launcher command was not found; trying the next launcher.",
                error=exc,
                metadata={"command": command[0], "url_host": urlparse(str(url)).netloc},
            )
            continue
        except OSError as exc:
            raise DhanAuthError(f"Failed to open browser for Dhan consent URL: {exc}") from exc
    raise DhanAuthError(f"Could not find a browser launcher for Dhan consent URL: {url}")


def _parse_json_response(response: requests.Response) -> dict:
    try:
        return response.json()
    except ValueError as exc:
        raise DhanAuthError(f"Expected JSON from Dhan auth endpoint, got: {response.text[:500]}") from exc


def parse_iso_expiry_to_utc(raw_expiry: str) -> datetime | None:
    """Pure parse, no telemetry -- shared by _parse_expiry below and auth_cli.py's own
    parse_expiry(), which used to carry a second, independent implementation of exactly this
    logic (BUG FOUND LIVE 2026-08-19, re-audit: not currently wrong -- both sides of its own
    comparison happened to stay in the same, naive-local timezone -- but a real duplicate of the
    one bug class already fixed here once (see the comment below), one careless edit on either
    side away from reintroducing it). Callers own their own telemetry on a parse failure."""
    text = raw_expiry.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    # Dhan's expiryTime is UTC in practice ('...Z'); treat a bare timestamp as UTC too
    # rather than guessing the local zone. Always return timezone-aware UTC so callers
    # compare like-for-like against datetime.now(timezone.utc) -- a prior version
    # returned a naive LOCAL-wall-clock value that got compared against naive
    # datetime.utcnow() in load_cached_access_token(), silently treating already-expired
    # tokens as valid for ~5.5h (the IST offset) past their real expiry.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_expiry(raw_expiry: str) -> datetime | None:
    text = raw_expiry.strip()
    if not text:
        return None
    try:
        return parse_iso_expiry_to_utc(text)
    except ValueError as exc:
        _record_dhan_auth_fallback(
            fallback_type="dhan_cached_access_token_expiry_invalid",
            reason="Dhan cached access-token expiry could not be parsed; cached broker auth was ignored.",
            error=exc,
            metadata={"raw_expiry": raw_expiry},
        )
        return None
