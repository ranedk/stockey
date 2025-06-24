"""HTTP utilities for the project with retries and caching"""

import os
import re
import json
import glob
import datetime as dt
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

from environs import Env
import ua_generator
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


env = Env()
env.read_env()
_CACHE_DIR = Path(env("HTTP_CACHE"))
_CACHE_DIR.mkdir(parents=True, exist_ok=True)


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
    except ValueError:
        cached_date = dt.datetime.fromtimestamp(newest.stat().st_mtime)

    if (dt.datetime.now() - cached_date).days <= max_age_days:
        return newest.read_text(encoding="utf-8")

    return None


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
    timeout: int = 10,
    retries: int = 5,
    backoff_factor: float = 0.3,
    from_cache: bool = True,
    max_age_days: int = 10,
) -> requests.Response:
    """
    GET with retry & 10-day JSON cache.

    See original docstring for parameter meanings.
    """
    # ---------- cache lookup -------------------------------------------------
    base_name, pattern = _build_cache_key(url)
    cached_text = _load_from_cache(pattern, max_age_days)
    if cached_text is not None:
        resp = requests.Response()
        resp._content = cached_text.encode()  # type: ignore[attr-defined]
        resp.status_code = 200
        resp.url = url
        resp.headers["X-Cache"] = "HIT"
        return resp

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
        response = session.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        _save_to_cache(base_name, response.text)
        response.headers["X-Cache"] = "MISS"
        return response
    except requests.RequestException as e:
        # Optional: fall back to *stale* cache if network fails completely
        if cached_text:
            resp = requests.Response()
            resp._content = cached_text.encode()  # type: ignore[attr-defined]
            resp.status_code = 200
            resp.url = url
            resp.headers["X-Cache"] = "STALE"
            return resp
        print(f"Request failed: {e}")
        raise
