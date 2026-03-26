from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETUP_CONFIG = REPO_ROOT / "config" / "advisory_setups.yaml"


@lru_cache(maxsize=1)
def load_setup_registry(config_path: str | None = None) -> list[dict[str, Any]]:
    path = Path(config_path) if config_path else DEFAULT_SETUP_CONFIG
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    setups = payload.get("setups") or []
    normalized: list[dict[str, Any]] = []
    for setup in setups:
        if not isinstance(setup, dict):
            continue
        screeners = [
            str(value)
            for value in (
                setup.get("screeners")
                or setup.get("screener_slugs")
                or ([setup.get("screener_slug")] if setup.get("screener_slug") else [])
            )
            if value
        ]
        overlay_screeners: dict[str, dict[str, list[str]]] = {}
        for overlay_name, raw in (setup.get("overlay_screeners") or {}).items():
            if isinstance(raw, dict):
                add = [str(value) for value in (raw.get("add") or []) if value]
                remove = [str(value) for value in (raw.get("remove") or []) if value]
            else:
                add = [str(value) for value in (raw or []) if value]
                remove = []
            overlay_screeners[str(overlay_name).upper()] = {"add": add, "remove": remove}
        normalized.append(
            {
                "setup_id": str(setup["setup_id"]),
                "setup_name": str(setup.get("setup_name") or setup["setup_id"]),
                "setup_family": str(setup.get("setup_family") or ""),
                "holding_horizon_note": setup.get("holding_horizon_note"),
                "screener_slug": screeners[0] if screeners else None,
                "screener_slugs": screeners,
                "screeners": screeners,
                "screener_mode": str(setup.get("screener_mode") or "union").lower(),
                "allowed_regimes": [str(value) for value in setup.get("allowed_regimes", [])],
                "blocked_regimes": [str(value) for value in setup.get("blocked_regimes", [])],
                "allowed_overlays": [str(value).upper() for value in setup.get("allowed_overlays", [])],
                "blocked_overlays": [str(value).upper() for value in setup.get("blocked_overlays", [])],
                "overlay_screeners": overlay_screeners,
                "market_cap_min": setup.get("market_cap_min"),
                "market_cap_max": setup.get("market_cap_max"),
                "min_avg_traded_value_20d": setup.get("min_avg_traded_value_20d"),
                "max_breakout_extension_pct": setup.get("max_breakout_extension_pct"),
                "watch_pullback_extension_pct": setup.get("watch_pullback_extension_pct"),
                "min_dist_52w_high": setup.get("min_dist_52w_high"),
                "scoring_weights": dict(setup.get("scoring_weights") or {}),
                "score_thresholds": dict(setup.get("score_thresholds") or {}),
                "regime_policy": dict(setup.get("regime_policy") or {}),
                "technical_rules": list(setup.get("technical_rules", [])),
                "fundamental_rules": list(setup.get("fundamental_rules", [])),
                "watch_reasons": list(setup.get("watch_reasons", [])),
                "entry_styles": list(setup.get("entry_styles", [])),
            }
        )
    return normalized
