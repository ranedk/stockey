"""Strict JSON for values that came out of pandas.

A row pulled from a multi-row DataFrame turns SQL NULL into float NaN, and json.dumps
writes that as a bare `NaN` -- not JSON. It round-trips through Python and then breaks
any strict consumer (Starlette renders with allow_nan=False). 130 stored L3 alerts held
one (2026-09-23 data audit). Write through dumps_strict; read old rows with loads_lenient.
"""
from __future__ import annotations

import json
import math
from typing import Any


def _clean(value: Any) -> Any:
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def dumps_strict(value: Any, **kwargs: Any) -> str:
    kwargs.setdefault("ensure_ascii", False)
    kwargs.setdefault("default", str)
    return json.dumps(_clean(value), allow_nan=False, **kwargs)


def loads_lenient(text: str | None) -> Any:
    if not text:
        return None
    return _clean(json.loads(text, parse_constant=lambda _: None))
