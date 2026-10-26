"""The theme map (docs/THEMATIC_WATCHLIST_PRD.md section 4.1): our own list of real end
markets, versioned as themes_v<N>.yaml beside this file. Loading validates the file, so a
malformed edit fails here rather than as a silent empty theme downstream."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

HERE = Path(__file__).parent
LAYERS = {"upstream", "midstream", "end_product", "service"}
REQUIRED = ("id", "name", "area", "layer", "definition", "include", "min_exposure", "capacity", "policy")


@dataclass(frozen=True)
class Theme:
    id: str
    name: str
    area: str
    layer: str
    definition: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    min_exposure: float
    capacity: tuple[str, ...]
    policy: tuple[str, ...]


def load(version: int = 1) -> list[Theme]:
    raw = yaml.safe_load((HERE / f"themes_v{version}.yaml").read_text())
    if raw.get("version") != version:
        raise ValueError(f"themes_v{version}.yaml says version {raw.get('version')}")
    out, seen = [], set()
    for t in raw["themes"]:
        missing = [k for k in REQUIRED if not t.get(k)]
        if missing:
            raise ValueError(f"theme {t.get('id')!r} is missing {missing}")
        if t["id"] in seen:
            raise ValueError(f"duplicate theme id {t['id']!r}")
        if t["layer"] not in LAYERS:
            raise ValueError(f"theme {t['id']!r} has unknown layer {t['layer']!r}")
        if not 0 < float(t["min_exposure"]) <= 100:
            raise ValueError(f"theme {t['id']!r} min_exposure out of range")
        seen.add(t["id"])
        out.append(Theme(
            id=t["id"], name=t["name"], area=t["area"], layer=t["layer"], definition=t["definition"],
            include=tuple(t["include"]), exclude=tuple(t.get("exclude") or ()),
            min_exposure=float(t["min_exposure"]), capacity=tuple(t["capacity"]), policy=tuple(t["policy"]),
        ))
    return out
