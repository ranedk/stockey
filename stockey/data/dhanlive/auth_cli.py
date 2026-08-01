from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta

from environs import Env

from utils.fallback_telemetry import record_local_fallback_event
from data.dhanlive.auth import (
    DEFAULT_TOKEN_CACHE,
    DhanAuthError,
    begin_browser_consent,
    clear_cached_access_token,
    consume_consent_token,
    get_token_id_from_auto_login,
    is_auto_login_configured,
    load_cached_access_token_payload,
    normalize_token_id,
    prompt_for_token_id,
)
from data.dhanlive.client import DhanAPIError, DhanHistoricalClient


env = Env()
env.read_env()


def parse_expiry(raw_expiry: str | None) -> datetime | None:
    if not raw_expiry:
        return None
    text = raw_expiry.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        record_local_fallback_event(
            module="data.dhanlive.auth_cli",
            source="dhan_auth_cache",
            fallback_type="dhan_auth_cli_cached_expiry_parse_failed",
            severity="warn",
            reason="Dhan auth CLI could not parse cached token expiry; cache freshness will be treated as unknown/stale.",
            error=exc,
            metadata={"raw_expiry": raw_expiry[:120]},
        )
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone().astimezone(tz=None).replace(tzinfo=None)
    return parsed


def mask_token(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 10:
        return value
    return f"{value[:6]}...{value[-4:]}"


def cache_status() -> dict[str, object]:
    payload = load_cached_access_token_payload()
    expiry_time = payload.get("expiryTime") if payload else None
    expires_at = parse_expiry(expiry_time)
    now = datetime.now()
    is_fresh = expires_at is not None and now + timedelta(minutes=5) < expires_at
    seconds_to_expiry = (expires_at - now).total_seconds() if expires_at is not None else None
    return {
        "cache_path": str(DEFAULT_TOKEN_CACHE),
        "cache_exists": DEFAULT_TOKEN_CACHE.exists(),
        "has_direct_env_token": bool(env("DHAN_ACCESS_TOKEN", default=None)),
        "has_env_token_id": bool(env("DHAN_TOKEN_ID", default=None)),
        "cached_access_token": mask_token(payload.get("accessToken") if payload else None),
        "cached_expiry_time": expiry_time,
        "cached_seconds_to_expiry": seconds_to_expiry,
        "cached_is_fresh_by_expiry": bool(is_fresh),
    }


def validate_token(access_token: str) -> dict[str, object]:
    try:
        payload = DhanHistoricalClient(access_token=access_token).validate_access_token()
        return {
            "status": "ok",
            "profile_keys": sorted(payload.keys())[:20] if isinstance(payload, dict) else [],
        }
    except (DhanAPIError, DhanAuthError) as exc:
        record_local_fallback_event(
            module="data.dhanlive.auth_cli",
            source="dhan_auth",
            fallback_type="dhan_token_validation_failed",
            severity="error",
            reason="Dhan access-token validation failed; refresh the broker token before running Dhan-backed downloads, watchers, or advisory stages.",
            error=exc,
            metadata={"access_token_present": bool(access_token)},
        )
        return {
            "status": "error",
            "error": str(exc),
        }


def refresh_token(token_input: str | None = None, *, auto_login: bool = False) -> dict[str, object]:
    normalized = normalize_token_id(token_input)
    consent_url = None
    if not normalized:
        if auto_login or is_auto_login_configured():
            normalized = get_token_id_from_auto_login()
        else:
            consent_url = begin_browser_consent()
            normalized = prompt_for_token_id(consent_url)
    payload = consume_consent_token(normalized)
    validation = validate_token(str(payload["accessToken"]))
    return {
        "status": "ok",
        "cache_path": str(DEFAULT_TOKEN_CACHE),
        "consent_url": consent_url,
        "token_id_used": mask_token(normalized),
        "cached_access_token": mask_token(str(payload.get("accessToken"))),
        "expiry_time": payload.get("expiryTime"),
        "validation": validation,
    }


def ensure_token(
    *,
    min_fresh_minutes: int = 10,
    auto_login: bool = False,
    validate: bool = True,
) -> dict[str, object]:
    direct = env("DHAN_ACCESS_TOKEN", default=None)
    if direct:
        validation = validate_token(direct) if validate else {"status": "skipped"}
        return {
            "status": "ok" if validation.get("status") == "ok" or not validate else "error",
            "source": "direct_env_token",
            "refreshed": False,
            "validation": validation,
            "operator_action": "Unset DHAN_ACCESS_TOKEN or provide a valid token if validation fails.",
        }

    payload = load_cached_access_token_payload()
    access_token = str(payload.get("accessToken")) if payload and payload.get("accessToken") else None
    expires_at = parse_expiry(str(payload.get("expiryTime"))) if payload and payload.get("expiryTime") else None
    now = datetime.now()
    min_fresh = max(int(min_fresh_minutes), 0)
    cache_fresh = bool(access_token and expires_at is not None and now + timedelta(minutes=min_fresh) < expires_at)
    if cache_fresh and access_token:
        validation = validate_token(access_token) if validate else {"status": "skipped"}
        if validation.get("status") == "ok" or not validate:
            return {
                "status": "ok",
                "source": "cached_access_token",
                "refreshed": False,
                "cache_path": str(DEFAULT_TOKEN_CACHE),
                "expiry_time": payload.get("expiryTime") if payload else None,
                "seconds_to_expiry": (expires_at - now).total_seconds() if expires_at is not None else None,
                "validation": validation,
            }

    env_token_id = env("DHAN_TOKEN_ID", default=None)
    can_refresh = bool(env_token_id or (auto_login and is_auto_login_configured()))
    if not can_refresh:
        return {
            "status": "error",
            "source": "dhan_auth_preflight",
            "refreshed": False,
            "cache_path": str(DEFAULT_TOKEN_CACHE),
            "cache_exists": DEFAULT_TOKEN_CACHE.exists(),
            "cached_is_fresh_by_expiry": bool(cache_fresh),
            "operator_action": (
                "Refresh Dhan auth before running Dhan-backed stages: "
                "python -m data.dhanlive.auth_cli refresh --clear-cache-first --auto-login"
            ),
            "reason": "No fresh cached token, direct token, DHAN_TOKEN_ID, or automated Dhan login configuration is available.",
        }

    if DEFAULT_TOKEN_CACHE.exists():
        clear_cached_access_token()
    try:
        refreshed = refresh_token(env_token_id, auto_login=auto_login)
    except Exception as exc:
        record_local_fallback_event(
            module="data.dhanlive.auth_cli",
            source="dhan_auth_preflight",
            fallback_type="dhan_auth_cli_ensure_refresh_failed",
            severity="error",
            reason="Dhan auth preflight could not refresh a usable token before a Dhan-backed run.",
            error=exc,
            metadata={
                "auto_login": bool(auto_login),
                "env_token_id_present": bool(env_token_id),
                "cache_path": str(DEFAULT_TOKEN_CACHE),
            },
        )
        return {
            "status": "error",
            "source": "dhan_auth_preflight",
            "refreshed": False,
            "cache_path": str(DEFAULT_TOKEN_CACHE),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "operator_action": (
                "Refresh Dhan auth before running Dhan-backed stages: "
                "python -m data.dhanlive.auth_cli refresh --clear-cache-first --auto-login"
            ),
        }
    return {
        **refreshed,
        "source": "refreshed_access_token",
        "refreshed": True,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect, refresh, and validate the cached Dhan access token.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="Show current Dhan token/cache status")

    validate_parser = subparsers.add_parser("validate", help="Validate the currently configured or cached Dhan access token")
    validate_parser.add_argument("--access-token", help="Optional explicit access token to validate instead of the cached token")

    ensure_parser = subparsers.add_parser("ensure", help="Ensure a valid cached Dhan token exists; refresh non-interactively when possible")
    ensure_parser.add_argument("--min-fresh-minutes", type=int, default=10, help="Refresh if the cached token expires sooner than this many minutes")
    ensure_parser.add_argument("--auto-login", action="store_true", help="Use Playwright/CDP automated login when refresh is required")
    ensure_parser.add_argument("--skip-validate", action="store_true", help="Only check token freshness; do not call the Dhan profile endpoint")

    refresh_parser = subparsers.add_parser("refresh", help="Refresh the cached Dhan token using browser consent")
    refresh_parser.add_argument("--token-id", help="Optional raw tokenId or redirected URL; skips opening the browser if provided")
    refresh_parser.add_argument("--clear-cache-first", action="store_true", help="Delete the existing cached token before refreshing")
    refresh_parser.add_argument("--auto-login", action="store_true", help="Use Playwright/CDP plus DHAN_LOGIN_MOBILE, DHAN_TOTP_SECRET, and DHAN_LOGIN_PIN to complete consent login")

    subparsers.add_parser("clear-cache", help="Delete the cached Dhan access token file")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.command == "status":
        print(json.dumps(cache_status(), indent=2, ensure_ascii=False, default=str))
        return 0

    if args.command == "clear-cache":
        removed = clear_cached_access_token()
        print(
            json.dumps(
                {
                    "status": "ok",
                    "cache_path": str(DEFAULT_TOKEN_CACHE),
                    "removed": bool(removed),
                },
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )
        return 0

    if args.command == "validate":
        access_token = args.access_token
        if not access_token:
            payload = load_cached_access_token_payload()
            access_token = str(payload.get("accessToken")) if payload and payload.get("accessToken") else None
        if not access_token:
            raise SystemExit("No Dhan access token found. Refresh first or pass --access-token.")
        print(json.dumps(validate_token(access_token), indent=2, ensure_ascii=False, default=str))
        return 0

    if args.command == "ensure":
        payload = ensure_token(
            min_fresh_minutes=int(args.min_fresh_minutes),
            auto_login=bool(args.auto_login),
            validate=not bool(args.skip_validate),
        )
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        return 0 if payload.get("status") == "ok" else 1

    if args.command == "refresh":
        if args.clear_cache_first:
            clear_cached_access_token()
        print(json.dumps(refresh_token(args.token_id, auto_login=bool(args.auto_login)), indent=2, ensure_ascii=False, default=str))
        return 0

    raise SystemExit(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
