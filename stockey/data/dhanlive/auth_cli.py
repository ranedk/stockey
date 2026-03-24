from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta

from environs import Env

from data.dhanlive.auth import (
    DEFAULT_TOKEN_CACHE,
    DhanAuthError,
    begin_browser_consent,
    clear_cached_access_token,
    consume_consent_token,
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
    except ValueError:
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
    now = datetime.utcnow()
    is_fresh = expires_at is not None and now + timedelta(minutes=5) < expires_at
    return {
        "cache_path": str(DEFAULT_TOKEN_CACHE),
        "cache_exists": DEFAULT_TOKEN_CACHE.exists(),
        "has_direct_env_token": bool(env("DHAN_ACCESS_TOKEN", default=None)),
        "has_env_token_id": bool(env("DHAN_TOKEN_ID", default=None)),
        "cached_access_token": mask_token(payload.get("accessToken") if payload else None),
        "cached_expiry_time": expiry_time,
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
        return {
            "status": "error",
            "error": str(exc),
        }


def refresh_token(token_input: str | None = None) -> dict[str, object]:
    normalized = normalize_token_id(token_input)
    consent_url = None
    if not normalized:
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect, refresh, and validate the cached Dhan access token.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="Show current Dhan token/cache status")

    validate_parser = subparsers.add_parser("validate", help="Validate the currently configured or cached Dhan access token")
    validate_parser.add_argument("--access-token", help="Optional explicit access token to validate instead of the cached token")

    refresh_parser = subparsers.add_parser("refresh", help="Refresh the cached Dhan token using browser consent")
    refresh_parser.add_argument("--token-id", help="Optional raw tokenId or redirected URL; skips opening the browser if provided")
    refresh_parser.add_argument("--clear-cache-first", action="store_true", help="Delete the existing cached token before refreshing")

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

    if args.command == "refresh":
        if args.clear_cache_first:
            clear_cached_access_token()
        print(json.dumps(refresh_token(args.token_id), indent=2, ensure_ascii=False, default=str))
        return 0

    raise SystemExit(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
