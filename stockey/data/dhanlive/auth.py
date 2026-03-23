from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from environs import Env


env = Env()
env.read_env()


AUTH_BASE_URL = "https://auth.dhan.co"
DEFAULT_TOKEN_CACHE = Path(env("BASE_DIR")) / ".cache" / "dhan_access_token.json"


class DhanAuthError(RuntimeError):
    pass


def load_cached_access_token(cache_path: Path = DEFAULT_TOKEN_CACHE) -> str | None:
    if not cache_path.exists():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    token = payload.get("accessToken")
    expiry_time = payload.get("expiryTime")
    if not token or not expiry_time:
        return None

    expires_at = _parse_expiry(expiry_time)
    if expires_at is None:
        return None
    if datetime.utcnow() + timedelta(minutes=5) >= expires_at:
        return None
    return token


def cache_access_token(payload: dict, cache_path: Path = DEFAULT_TOKEN_CACHE) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


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


def begin_browser_consent() -> str:
    consent_app_id = generate_consent_app_id()
    consent_url = build_consent_login_url(consent_app_id)
    open_browser_url(consent_url)
    return consent_url


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

    consent_url = begin_browser_consent()
    pasted_token_id = prompt_for_token_id(consent_url)
    return str(consume_consent_token(pasted_token_id)["accessToken"])


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
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise DhanAuthError(f"Failed to open browser for Dhan consent URL: {exc}") from exc
    raise DhanAuthError(f"Could not find a browser launcher for Dhan consent URL: {url}")


def _parse_json_response(response: requests.Response) -> dict:
    try:
        return response.json()
    except ValueError as exc:
        raise DhanAuthError(f"Expected JSON from Dhan auth endpoint, got: {response.text[:500]}") from exc


def _parse_expiry(raw_expiry: str) -> datetime | None:
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
