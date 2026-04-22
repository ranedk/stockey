from __future__ import annotations

import os
import json
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd


DISPLAY_TZ = ZoneInfo(os.getenv("STOCKEY_DISPLAY_TZ", "Asia/Kolkata"))
DISPLAY_TS_FORMAT = "%Y-%m-%d %H:%M:%S %Z"


def to_display_timestamp(value: Any) -> str | None:
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.tz_convert(DISPLAY_TZ).strftime(DISPLAY_TS_FORMAT)


def to_display_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                parsed = json.loads(value)
            except Exception:
                pass
            else:
                return json.dumps(to_display_value(parsed), ensure_ascii=False, default=str, sort_keys=True)
        if (
            stripped.endswith("Z")
            or "+00:00" in stripped
            or stripped.endswith("+0000")
            or stripped.endswith("UTC")
        ):
            display_ts = to_display_timestamp(stripped)
            if display_ts is not None:
                return display_ts
        return value
    if isinstance(value, pd.Timestamp):
        return to_display_timestamp(value)
    if isinstance(value, pd.DataFrame):
        return [to_display_value(row) for row in value.to_dict(orient="records")]
    if isinstance(value, dict):
        return {str(k): to_display_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_display_value(v) for v in value]
    if isinstance(value, tuple):
        return [to_display_value(v) for v in value]
    if pd.isna(value):
        return None
    return value
