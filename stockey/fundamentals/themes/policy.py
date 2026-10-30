"""The hand-kept policy table and capacity series per theme (docs/THEMATIC_WATCHLIST_PRD.md
section 4.3, build step 3), loaded from versioned YAML beside the theme map:

  policy_vN.yaml    schemes, duties, mandates, budgets, targets -- support and headwinds --
                    each dated and with its source
  capacity_vN.yaml  dated points of national capacity, pipeline and demand per theme, for
                    the capital-cycle ("late") flag

Edits go in a new version file with the date, never in place, like the theme map. Every
entry needs a source URL; `validate` refuses the file otherwise, so a figure without a
source cannot reach the page.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pandas as pd
import yaml

from fundamentals.themes import catalog
from utils.db import db_session, execute_db_operation, upsert_to_db

HERE = Path(__file__).parent
POLICY_TABLE = "fundamentals_theme_policy"
CAPACITY_TABLE = "fundamentals_theme_capacity"

POLICY_KINDS = {"pli", "duty", "mandate", "subsidy", "budget", "target", "tender", "regulation", "trade", "other"}
DIRECTIONS = {"support", "headwind"}
STATUSES = {"active", "ended", "proposed"}
SERIES_KINDS = {"capacity", "demand", "pipeline"}
_DATE_RX = re.compile(r"^\d{4}(-\d{2}(-\d{2})?|-Q[1-4]|-H[12])?$|^FY\d{4}-\d{2}( H[12]| Q[1-4])?$")

_DDL = [
    f"""CREATE TABLE IF NOT EXISTS {POLICY_TABLE} (
        version INT NOT NULL, theme_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
        direction TEXT NOT NULL, authority TEXT, announced TEXT, outlay_cr DOUBLE PRECISION,
        status TEXT NOT NULL, latest_figure TEXT, latest_as_of TEXT, source_url TEXT NOT NULL,
        source_title TEXT, source_date TEXT, note TEXT,
        load_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (version, theme_id, name))""",
    f"""CREATE TABLE IF NOT EXISTS {CAPACITY_TABLE} (
        version INT NOT NULL, theme_id TEXT NOT NULL, series TEXT NOT NULL, kind TEXT NOT NULL,
        period TEXT NOT NULL, value DOUBLE PRECISION NOT NULL, unit TEXT NOT NULL, cumulative BOOLEAN NOT NULL,
        source_url TEXT NOT NULL, source_title TEXT, source_date TEXT, note TEXT,
        load_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (version, theme_id, series, period))""",
]


def ensure_tables() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            for ddl in _DDL:
                cur.execute(ddl)

    execute_db_operation(_op, operation_name="theme_policy:ensure_tables")


def _date_ok(v) -> bool:
    return v is None or bool(_DATE_RX.match(str(v)))


def validate_policy(items: list[dict], theme_ids: set[str]) -> list[str]:
    errs, seen = [], set()
    for n, it in enumerate(items):
        where = f"policy[{n}] {it.get('theme_id')}/{it.get('name')}"
        if it.get("theme_id") not in theme_ids:
            errs.append(f"{where}: unknown theme")
        if not it.get("name"):
            errs.append(f"{where}: no name")
        if it.get("kind") not in POLICY_KINDS:
            errs.append(f"{where}: kind {it.get('kind')!r}")
        if it.get("direction") not in DIRECTIONS:
            errs.append(f"{where}: direction {it.get('direction')!r}")
        if it.get("status") not in STATUSES:
            errs.append(f"{where}: status {it.get('status')!r}")
        if not str(it.get("source_url") or "").startswith("http"):
            errs.append(f"{where}: no source URL")
        for k in ("announced", "latest_as_of", "source_date"):
            if not _date_ok(it.get(k)):
                errs.append(f"{where}: {k} {it.get(k)!r} is not YYYY[-MM[-DD]]")
        if it.get("outlay_cr") is not None and not isinstance(it["outlay_cr"], (int, float)):
            errs.append(f"{where}: outlay_cr is not a number")
        key = (it.get("theme_id"), it.get("name"))
        if key in seen:
            errs.append(f"{where}: duplicate")
        seen.add(key)
    return errs


def validate_capacity(points: list[dict], theme_ids: set[str]) -> list[str]:
    errs, seen, units = [], set(), {}
    for n, p in enumerate(points):
        where = f"capacity[{n}] {p.get('theme_id')}/{p.get('series')}/{p.get('period')}"
        if p.get("theme_id") not in theme_ids:
            errs.append(f"{where}: unknown theme")
        if p.get("kind") not in SERIES_KINDS:
            errs.append(f"{where}: kind {p.get('kind')!r}")
        if not p.get("series") or not p.get("unit"):
            errs.append(f"{where}: no series or unit")
        if not isinstance(p.get("value"), (int, float)) or isinstance(p.get("value"), bool):
            errs.append(f"{where}: value is not a number")
        if not isinstance(p.get("cumulative"), bool):
            errs.append(f"{where}: cumulative is not true/false")
        if not p.get("period") or not _date_ok(p.get("period")) or not _date_ok(p.get("source_date")):
            errs.append(f"{where}: bad period or source_date")
        if not str(p.get("source_url") or "").startswith("http"):
            errs.append(f"{where}: no source URL")
        key = (p.get("theme_id"), p.get("series"))
        if units.setdefault(key, p.get("unit")) != p.get("unit"):
            errs.append(f"{where}: unit {p.get('unit')!r} differs from the series' {units[key]!r}")
        if (*key, p.get("period")) in seen:
            errs.append(f"{where}: duplicate period")
        seen.add((*key, p.get("period")))
    return errs


def load_files(version: int) -> tuple[list[dict], list[dict]]:
    pol = yaml.safe_load((HERE / f"policy_v{version}.yaml").read_text())
    cap = yaml.safe_load((HERE / f"capacity_v{version}.yaml").read_text())
    return pol["items"], cap["points"]


def run(version: int = 1, theme_version: int = 1, dry_run: bool = False) -> dict:
    theme_ids = {t.id for t in catalog.load(theme_version)}
    policy, capacity = load_files(version)
    errs = validate_policy(policy, theme_ids) + validate_capacity(capacity, theme_ids)
    if errs:
        raise ValueError(f"{len(errs)} problems in the v{version} files:\n" + "\n".join(errs[:50]))
    if not dry_run:
        ensure_tables()
        cols = ["note", "source_title", "source_date"]
        pol = pd.DataFrame(policy).assign(version=version)
        cap = pd.DataFrame(capacity).assign(version=version)
        for df in (pol, cap):
            for c in cols:
                if c not in df:
                    df[c] = None
        upsert_to_db(pol, POLICY_TABLE, ["version", "theme_id", "name"])
        upsert_to_db(cap, CAPACITY_TABLE, ["version", "theme_id", "series", "period"])
    return {"policy": len(policy), "capacity_points": len(capacity),
            "themes_with_policy": len({p["theme_id"] for p in policy}),
            "themes_with_capacity": len({p["theme_id"] for p in capacity})}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check and load the theme policy table and capacity series.")
    ap.add_argument("--version", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true", help="check the files only")
    a = ap.parse_args(argv)
    print(json.dumps(run(version=a.version, dry_run=a.dry_run)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
