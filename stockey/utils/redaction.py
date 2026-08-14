from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


REDACTED = "[REDACTED]"
MASKED_MOBILE = "[MOBILE_REDACTED]"

SENSITIVE_KEY_PARTS = (
    "access_token",
    "authtoken",
    "auth_token",
    "authorization",
    "bearer",
    "client_secret",
    "cookie",
    "dhan_login_mobile",
    "dhan_login_pin",
    "dhan_totp_secret",
    "gemini_key",
    "otp",
    "password",
    "pin",
    "refresh_token",
    "screener_in_login",
    "screener_in_password",
    "secret",
    "session",
    "token",
    "tokenid",
    "totp",
    "x-api-key",
)
SENSITIVE_QUERY_KEYS = {
    "access_token",
    "accessToken",
    "auth",
    "authCode",
    "code",
    "key",
    "password",
    "pin",
    "refresh_token",
    "secret",
    "session",
    "token",
    "tokenId",
    "totp",
}

AUTH_HEADER_RE = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
KEY_VALUE_RE = re.compile(
    r"(?P<key>access[-_ ]?token|refresh[-_ ]?token|access[-_ ]?key(?:[-_ ]?id)?|token(?:[-_ ]?id)?"
    r"|authorization|api[-_ ]?key|password|secret|totp|pin|cookie)"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<value>[^\s,;&\"']+)",
    re.IGNORECASE,
)
ENV_ASSIGNMENT_RE = re.compile(
    r"\b(?P<key>[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PIN|TOTP|API_KEY|ACCESS_KEY|MOBILE)[A-Z0-9_]*)=(?P<value>[^\s]+)"
)
INDIAN_MOBILE_RE = re.compile(r"(?<!\d)(?:\+?91[-\s]?)?[6-9]\d{9}(?!\d)")
# any scheme, not just http(s) -- a DB DSN (postgresql://, redis://, ...) never matched the old http(s)-only
# pattern, so a connection string with a real embedded password (utils/db.py builds exactly this) sailed
# through untouched if it ever surfaced in an exception message.
URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+", re.IGNORECASE)


def is_sensitive_key(key: Any) -> bool:
    normalized = str(key or "").strip().lower().replace("-", "_")
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def _redact_url(match: re.Match[str]) -> str:
    url = match.group(0)
    try:
        parts = urlsplit(url)
    except Exception as exc:
        from utils.fallback_telemetry import record_local_fallback_event

        record_local_fallback_event(
            module="utils.redaction",
            source="url_redaction",
            fallback_type="redaction_url_parse_failed",
            severity="warn",
            reason="URL redaction could not parse a URL and replaced the full URL with a redacted marker.",
            error=exc,
            metadata={"url_excerpt": url[:240]},
        )
        return REDACTED
    query_pairs = []
    changed = False
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key in SENSITIVE_QUERY_KEYS or is_sensitive_key(key):
            query_pairs.append((key, REDACTED))
            changed = True
        else:
            query_pairs.append((key, value))

    netloc = parts.netloc
    # userinfo in the authority (scheme://user:pass@host) -- a DB DSN embeds real credentials exactly
    # here, and the query-string redaction above never looks at this part of the URL at all.
    if parts.username or parts.password:
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        netloc = f"{REDACTED}@{host}" if host else REDACTED
        changed = True

    if not changed:
        return url
    return urlunsplit((parts.scheme, netloc, parts.path, urlencode(query_pairs, doseq=True), parts.fragment))


def redact_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    text = URL_RE.sub(_redact_url, text)
    text = AUTH_HEADER_RE.sub(lambda match: f"{match.group(1)} {REDACTED}", text)
    text = KEY_VALUE_RE.sub(lambda match: f"{match.group('key')}{match.group('sep')}{REDACTED}", text)
    text = ENV_ASSIGNMENT_RE.sub(lambda match: f"{match.group('key')}={REDACTED}", text)
    text = INDIAN_MOBILE_RE.sub(MASKED_MOBILE, text)
    return text


def redact_value(value: Any, *, key: Any = None) -> Any:
    if is_sensitive_key(key):
        return REDACTED
    if isinstance(value, dict):
        return redact_mapping(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_mapping(value: dict[str, Any] | None) -> dict[str, Any]:
    if not value:
        return {}
    return {str(key): redact_value(item, key=key) for key, item in value.items()}


def redact_json_text(value: Any) -> str:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception as exc:
            from utils.fallback_telemetry import record_local_fallback_event

            record_local_fallback_event(
                module="utils.redaction",
                source="json_redaction",
                fallback_type="redaction_json_parse_failed",
                severity="warn",
                reason="JSON redaction could not parse text as JSON and used plain text redaction fallback.",
                error=exc,
                metadata={"value_excerpt": value[:240]},
            )
            return redact_text(value) or ""
        return json.dumps(redact_value(parsed), ensure_ascii=False, sort_keys=True, default=str)
    return json.dumps(redact_value(value), ensure_ascii=False, sort_keys=True, default=str)
