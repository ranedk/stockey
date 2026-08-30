from __future__ import annotations

from datetime import datetime
import sys
import threading
import time
from typing import Any
from urllib.parse import urlparse

import pandas as pd
import requests

from environs import Env

from utils.fallback_telemetry import record_local_fallback_event
from data.dhanlive.auth import force_refresh_access_token, get_access_token

env = Env()

# BUG FOUND LIVE 2026-08-22: _request had ZERO pacing between calls -- a tight
# sequential loop (scripts/backfill_intraday_5yr.py) hit Dhan's real 5 req/sec
# historical-data rate limit (https://dhan.freshdesk.com/support/solutions/
# articles/82000891163) within the first handful of symbols, and 429s were
# treated as a hard failure with no retry at all (only 401/expired-token
# responses were ever retried). Confirmed live: symbols 4 and 5 of a
# 2,876-symbol backfill run failed outright on the very first burst.
# Per-process pacing only (module-level, in-process) -- NOT a cross-process
# rate limiter like utils/nse_rate_limiter.py's nse_goto/nse_request_gate.
# Sufficient for today's single-process usage; if multiple stockey processes
# ever call Dhan concurrently, this needs the same cross-process treatment
# NSE already has.
_DHAN_RATE_LIMIT_LOCK = threading.Lock()
_dhan_last_request_at = 0.0
DHAN_MIN_REQUEST_INTERVAL_SECONDS = 0.22  # ~4.5 req/sec, safely under the 5/sec cap
DHAN_RATE_LIMIT_MAX_RETRIES = 5
DHAN_RATE_LIMIT_BACKOFF_SECONDS = 3.0  # multiplied by attempt number


def _pace_dhan_request() -> None:
    global _dhan_last_request_at
    with _DHAN_RATE_LIMIT_LOCK:
        now = time.monotonic()
        wait = DHAN_MIN_REQUEST_INTERVAL_SECONDS - (now - _dhan_last_request_at)
        if wait > 0:
            time.sleep(wait)
        _dhan_last_request_at = time.monotonic()
env.read_env()


class DhanAPIError(RuntimeError):
    pass


def _record_dhan_client_fallback(
    *,
    fallback_type: str,
    reason: str,
    error: Exception | None = None,
    response: requests.Response | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    response_url = getattr(response, "url", None)
    host = urlparse(str(response_url)).netloc if response_url else None
    path = urlparse(str(response_url)).path if response_url else None
    record_local_fallback_event(
        module="data.dhanlive.client",
        source="dhan_api",
        fallback_type=fallback_type,
        severity="warn",
        reason=reason,
        error=error,
        metadata={
            "status_code": getattr(response, "status_code", None),
            "url_host": host,
            "url_path": path,
            **(metadata or {}),
        },
    )


class DhanHistoricalClient:
    BASE_URL = "https://api.dhan.co/v2"

    def __init__(self, access_token: str | None = None, timeout: int = 60, auth_attempts: int | None = None):
        self.access_token = access_token or get_access_token()
        self.timeout = timeout
        self.auth_attempts = max(int(auth_attempts or env.int("DHAN_API_AUTH_ATTEMPTS", default=3)), 1)
        self.session = requests.Session()
        self._set_access_token(self.access_token)

    def _set_access_token(self, access_token: str) -> None:
        self.access_token = access_token
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "access-token": self.access_token,
            }
        )

    def validate_access_token(self) -> dict[str, Any]:
        return self._request("GET", f"{self.BASE_URL}/profile")

    def fetch_daily(
        self,
        *,
        security_id: int | str,
        exchange_segment: str,
        instrument: str,
        from_date: datetime,
        to_date: datetime,
        oi: bool = False,
        expiry_code: int = 0,
    ) -> dict[str, Any]:
        payload = {
            "securityId": str(security_id),
            "exchangeSegment": exchange_segment,
            "instrument": instrument,
            "expiryCode": expiry_code,
            "oi": oi,
            "fromDate": from_date.strftime("%Y-%m-%d"),
            "toDate": to_date.strftime("%Y-%m-%d"),
        }
        return self._request("POST", f"{self.BASE_URL}/charts/historical", json=payload)

    def fetch_intraday(
        self,
        *,
        security_id: int | str,
        exchange_segment: str,
        instrument: str,
        interval_minutes: int,
        from_datetime: datetime,
        to_datetime: datetime,
        oi: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "securityId": str(security_id),
            "exchangeSegment": exchange_segment,
            "instrument": instrument,
            "interval": str(interval_minutes),
            "oi": oi,
            "fromDate": from_datetime.strftime("%Y-%m-%d %H:%M:%S"),
            "toDate": to_datetime.strftime("%Y-%m-%d %H:%M:%S"),
        }
        return self._request("POST", f"{self.BASE_URL}/charts/intraday", json=payload)

    def _request_with_rate_limit_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        response: requests.Response | None = None
        for rate_attempt in range(1, DHAN_RATE_LIMIT_MAX_RETRIES + 1):
            _pace_dhan_request()
            response = self.session.request(method, url, timeout=self.timeout, **kwargs)
            if response.status_code != 429:
                return response
            if rate_attempt >= DHAN_RATE_LIMIT_MAX_RETRIES:
                break
            backoff = DHAN_RATE_LIMIT_BACKOFF_SECONDS * rate_attempt
            print(
                f"[data.dhanlive.client] Dhan rate limit (429); backing off {backoff:.0f}s "
                f"(attempt {rate_attempt}/{DHAN_RATE_LIMIT_MAX_RETRIES})",
                file=sys.stderr,
                flush=True,
            )
            _record_dhan_client_fallback(
                fallback_type="dhan_rate_limited",
                reason="Dhan API returned 429 despite in-process pacing; backing off and retrying.",
                response=response,
                metadata={"rate_attempt": rate_attempt, "backoff_seconds": backoff},
            )
            time.sleep(backoff)
        assert response is not None
        return response

    def _request(self, method: str, url: str, **kwargs) -> dict[str, Any] | list[dict[str, Any]]:
        last_response: requests.Response | None = None
        for attempt in range(1, self.auth_attempts + 1):
            response = self._request_with_rate_limit_retry(method, url, **kwargs)
            last_response = response
            if not self._is_auth_failure(response):
                return self._parse_response(response)
            if attempt >= self.auth_attempts:
                break
            print(
                f"[data.dhanlive.client] Dhan auth failed; refreshing token attempt={attempt + 1}/{self.auth_attempts}",
                file=sys.stderr,
                flush=True,
            )
            try:
                self._set_access_token(force_refresh_access_token(current_token=self.access_token))
            except Exception as exc:
                if attempt >= self.auth_attempts - 1:
                    raise DhanAPIError(f"Dhan auth refresh failed after {self.auth_attempts} attempts: {exc}") from exc
                print(
                    f"[data.dhanlive.client] Dhan auth refresh failed; retrying login error={exc.__class__.__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
        return self._parse_response(last_response)

    @staticmethod
    def _is_auth_failure(response: requests.Response) -> bool:
        if response.status_code == 401:
            return True
        text = response.text.lower()
        return response.status_code in {400, 403} and "access token" in text and ("invalid" in text or "expired" in text)

    def _parse_response(self, response: requests.Response | None) -> dict[str, Any] | list[dict[str, Any]]:
        if response is None:
            raise DhanAPIError("Dhan API request failed before receiving a response")
        try:
            payload = response.json()
        except ValueError as exc:
            _record_dhan_client_fallback(
                fallback_type="dhan_response_json_parse_failed",
                reason="Dhan API response could not be parsed as JSON; falling back to raw response text.",
                error=exc,
                response=response,
                metadata={"response_ok": bool(response.ok)},
            )
            payload = {"raw_text": response.text}

        if response.ok:
            return payload

        error_message = None
        if isinstance(payload, dict):
            error_message = payload.get("errorMessage") or payload.get("message")
        raise DhanAPIError(
            f"Dhan API request failed with status {response.status_code}: "
            f"{error_message or response.text.strip()}"
        )


def candles_to_df(payload: dict[str, Any]) -> pd.DataFrame:
    if not payload:
        return pd.DataFrame()

    columns = ["timestamp", "open", "high", "low", "close", "volume"]
    if "open_interest" in payload:
        columns.append("open_interest")

    lengths = {column: len(payload.get(column, [])) for column in columns if column in payload}
    if not lengths:
        return pd.DataFrame()
    expected = max(lengths.values())
    for column, size in lengths.items():
        if size != expected:
            raise ValueError(f"Unexpected Dhan candle payload: {column} has {size} rows, expected {expected}")

    df = pd.DataFrame({column: payload.get(column, []) for column in columns if column in payload})
    if df.empty:
        return df

    numeric_columns = ["open", "high", "low", "close", "volume", "open_interest"]
    for column in numeric_columns:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")

    df["source_timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True, errors="coerce")
    return df
