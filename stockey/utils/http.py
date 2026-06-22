"""HTTP utilities for the project with retries and caching"""

import datetime as dt
import glob
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import requests
import ua_generator
from bs4 import BeautifulSoup
from environs import Env
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from advisory.fallback_telemetry import record_local_fallback_event

env = Env()
env.read_env()
_CACHE_DIR = Path(env("HTTP_CACHE"))
_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def hidden_inputs_to_dict(html: str) -> dict[str, str | None]:
    """
    Parse HTML and return a dict of all <input type="hidden"> elements,
    keyed by their 'name' attribute with values from their 'value' attribute.
    If either attribute is missing, that element is skipped.
    """
    soup = BeautifulSoup(html, "html.parser")

    hidden_fields = {}
    for tag in soup.find_all("input", {"type": "hidden"}):
        name = tag.get("name")
        value = tag.get("value")  # returns None if attribute missing
        if name is not None:  # ignore unnamed inputs
            hidden_fields[name] = value
    return hidden_fields


def get_dynamic_headers():
    """Dynamically generate headers for requests."""
    ua = ua_generator.generate(
        browser=("chrome", "firefox"),
        device=("desktop",),
        platform=("windows", "macos"),
    )
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9,uz;q=0.8",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "connection": "keep-alive",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
    }
    headers.update(ua.headers.get())
    return headers


def _slugify(text: str) -> str:
    """Lower-case, URL-decode and keep only 0-9 / a-z, replacing runs with '_'."""
    text = unquote(text).lower()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_") or "none"


def _build_cache_key(url: str) -> tuple[str, str]:
    """
    Returns (base_name, file_pattern) where
      base_name  = endpoint & parameter values joined by '__'
      file_pattern = glob-ready pattern that matches any dated file for this base
    """
    p = urlparse(url)
    segments = [s for s in p.path.split("/") if s]

    # ---- endpoint -----------------------------------------------------------
    endpoint = segments[-1]
    param_vals: list[str] = []

    # If the endpoint segment itself contains '=', treat previous segment
    # as the endpoint and keep the value part
    if "=" in endpoint:
        name, val = endpoint.split("=", 1)
        endpoint = segments[-2] if len(segments) >= 2 else name
        param_vals.append(val)

    # Extra "name=value" segments after the endpoint (rare but handled)
    for seg in segments[segments.index(endpoint) + 1 :]:
        if "=" in seg:
            param_vals.append(seg.split("=", 1)[1])

    # ---- query-string params ------------------------------------------------
    for values in parse_qs(p.query, keep_blank_values=True).values():
        param_vals.extend(values)

    # Reduce to slug-safe, lower-case tokens
    endpoint_slug = _slugify(endpoint)
    param_slugs = [_slugify(v) for v in param_vals] or ["noparam"]

    base_name = "__".join([endpoint_slug] + param_slugs)
    file_pattern = str(_CACHE_DIR / f"{base_name}__*.json")
    return base_name, file_pattern


def _record_http_cache_fallback(
    *,
    fallback_type: str,
    reason: str,
    error: Exception | None = None,
    cache_file: Path | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="utils.http",
        source="http_cache",
        fallback_type=fallback_type,
        severity="warn",
        reason=reason,
        error=error,
        metadata={
            "cache_file": cache_file.name if cache_file else None,
            **(metadata or {}),
        },
    )


def _load_from_cache(file_pattern: str, max_age_days: int = 10) -> str | None:
    """Return cached text if the newest copy is recent enough, else None."""
    files = sorted(glob.glob(file_pattern), key=os.path.getmtime, reverse=True)
    if not files:
        return None

    newest = Path(files[0])
    # Parse date chunk:  ...__YYYY_DD_MM.json
    try:
        date_part = newest.stem.rsplit("__", 1)[-1]
        cached_date = dt.datetime.strptime(date_part, "%Y_%d_%m")
    except ValueError as exc:
        _record_http_cache_fallback(
            fallback_type="http_cache_date_parse_failed",
            reason="HTTP cache filename date could not be parsed; falling back to file modified time.",
            error=exc,
            cache_file=newest,
            metadata={"date_part": date_part},
        )
        cached_date = dt.datetime.fromtimestamp(newest.stat().st_mtime)

    if (dt.datetime.now() - cached_date).days <= max_age_days:
        try:
            return newest.read_text(encoding="utf-8")
        except Exception as exc:
            _record_http_cache_fallback(
                fallback_type="http_cache_read_failed",
                reason="HTTP cache file could not be read; falling back to network request.",
                error=exc,
                cache_file=newest,
            )
            return None

    return None


def _load_latest_cache(file_pattern: str) -> tuple[str | None, Path | None]:
    """Return newest cached text regardless of age for explicit stale-cache fallback."""
    files = sorted(glob.glob(file_pattern), key=os.path.getmtime, reverse=True)
    if not files:
        return None, None
    newest = Path(files[0])
    try:
        return newest.read_text(encoding="utf-8"), newest
    except Exception as exc:
        _record_http_cache_fallback(
            fallback_type="http_stale_cache_read_failed",
            reason="HTTP stale-cache fallback was unavailable because the newest cache file could not be read.",
            error=exc,
            cache_file=newest,
        )
        return None, newest


def _save_to_cache(base_name: str, text: str) -> None:
    today = dt.datetime.now().strftime("%Y_%d_%m")
    fname = _CACHE_DIR / f"{base_name}__{today}.json"
    fname.write_text(text, encoding="utf-8")


# -----------------------------------------------------------------------------
# public API
# -----------------------------------------------------------------------------
def get_with_retries(
    url: str,
    headers: dict | None = None,
    *,
    cookies: dict | None = None,
    method: str = "GET",
    data: Any | None = None,
    params: dict | None = None,
    json_data: Any | None = None,
    timeout: int = 10,
    retries: int = 5,
    backoff_factor: float = 0.3,
    from_cache: bool = True,
    stream: bool = False,
    max_age_days: int = 10,
) -> requests.Response:
    """
    GET with retry & 10-day JSON cache.

    See original docstring for parameter meanings.
    """
    method = method.upper()
    if method not in ["GET", "POST"]:
        raise ValueError("Only GET and POST allowed")

    # ---------- cache lookup -------------------------------------------------
    base_name, pattern = _build_cache_key(url)
    cached_text = _load_from_cache(pattern, max_age_days) if from_cache else None
    if cached_text is not None:
        resp = requests.Response()
        resp._content = cached_text.encode()  # type: ignore[attr-defined]
        resp.status_code = 200
        resp.url = url
        resp.headers["X-Cache"] = "HIT"
        return resp
    stale_cached_text, stale_cache_file = _load_latest_cache(pattern) if from_cache else (None, None)

    # ---------- network fetch with retry ------------------------------------
    session = requests.Session()
    retry_strategy = Retry(
        total=retries,
        backoff_factor=backoff_factor,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods={"GET"},
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    try:
        response = session.request(
            method,
            url,
            headers=headers,
            cookies=cookies,
            timeout=timeout,
            data=data,
            params=params,
            json=json_data,
            stream=stream,
        )
        response.raise_for_status()
        _save_to_cache(base_name, response.text)
        response.headers["X-Cache"] = "MISS"
        return response
    except requests.RequestException as e:
        # Optional: fall back to *stale* cache if network fails completely.
        if stale_cached_text is not None:
            _record_http_cache_fallback(
                fallback_type="http_network_failed_stale_cache_used",
                reason="HTTP request failed after retries; returning stale cached response and marking it explicitly.",
                error=e,
                cache_file=stale_cache_file,
                metadata={
                    "method": method,
                    "host": urlparse(url).netloc,
                    "path": urlparse(url).path,
                    "timeout": timeout,
                    "retries": retries,
                },
            )
            resp = requests.Response()
            resp._content = stale_cached_text.encode()  # type: ignore[attr-defined]
            resp.status_code = 200
            resp.url = url
            resp.headers["X-Cache"] = "STALE"
            return resp
        _record_http_cache_fallback(
            fallback_type="http_network_failed_no_cache",
            reason="HTTP request failed after retries and no cached response was available.",
            error=e,
            metadata={
                "method": method,
                "host": urlparse(url).netloc,
                "path": urlparse(url).path,
                "timeout": timeout,
                "retries": retries,
                "from_cache": from_cache,
            },
        )
        print(f"Request failed: {e}")
        raise
