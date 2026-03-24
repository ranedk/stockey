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
        normalized.append(
            {
                "setup_id": str(setup["setup_id"]),
                "setup_name": str(setup.get("setup_name") or setup["setup_id"]),
                "screener_slug": setup.get("screener_slug"),
                "allowed_regimes": [str(value) for value in setup.get("allowed_regimes", [])],
                "market_cap_min": setup.get("market_cap_min"),
                "market_cap_max": setup.get("market_cap_max"),
                "min_avg_traded_value_20d": setup.get("min_avg_traded_value_20d"),
                "max_breakout_extension_pct": setup.get("max_breakout_extension_pct"),
                "min_dist_52w_high": setup.get("min_dist_52w_high"),
                "technical_rules": list(setup.get("technical_rules", [])),
                "fundamental_rules": list(setup.get("fundamental_rules", [])),
                "watch_reasons": list(setup.get("watch_reasons", [])),
            }
        )
    return normalized
