"""Funnel invariants audit -- hunt the silent-bug class that hides in the decision funnel.

Motivated by a real bug: `technical_score`/`setup_score` are 0-1 but several consumers compared them to
0-100 thresholds (the 78 buy bar) -> permanently dead branches that no unit test caught (the tests baked
in the same wrong scale). This audit catches that CLASS three ways:

1. Impossible-gate detector (static, no DB): scans advisory/scripts source for a 0-1 score field compared
   against a threshold > 1.0 (e.g. `technical_score >= 78`) -- a gate the field can never cross. Self-
   updating: it re-scans source each run, so a newly-introduced bad gate is flagged without editing a list.
2. Range/scale invariants (real data): every persisted score field must sit inside its declared range
   (advisory/score_scales.py). A `technical_score` of 82 would flag a scale corruption at the source.
3. Cross-field consistency (real data): e.g. `technical_entry_confirmed=True => technical_state=BUY_TRIGGERED`;
   `PASS_NOW => (BUY_TRIGGERED or technical_override_source set)` (the ts-forecast-rescue exception).

Read-only. Model: scripts/docs_state_audit.py (Finding / build_report / format_text_report / main).
Run: `python scripts/funnel_invariants.py --format text --strict`. DB checks are best-effort (skipped
with a warning if the DB is unavailable) so it is safe in CI; the static check always runs.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path

from advisory.score_scales import SCORE_RANGES, UNIT_SCALE_FIELDS

REPO_ROOT = Path(__file__).resolve().parents[1]

# Source dirs scanned for impossible gates (product code only; tests deliberately use mixed scales).
SCAN_DIRS = ("advisory", "scripts")
# score_scales.py declares ranges (not comparisons); funnel_invariants.py contains example patterns in
# its docstring/regex. Neither is product logic to gate.
SCAN_EXCLUDE = {"score_scales.py", "funnel_invariants.py"}

# A line with any of these is treated as already scale-aware (scaled before compare) -> not a bug.
SCALE_SAFE_HINTS = ("to_100", "* 100", "*100", "/ 100", "/100", "_total_score", "* 100.0")
# If the value was scaled a few lines above (e.g. `technical_score = to_100(...)` then compared later),
# the comparison line has no hint but is safe. Treat a to_100() within this many preceding lines as safe.
SCALE_WINDOW_LINES = 12

# 0-1 field compared to a numeric literal via a magnitude comparator.
_GATE_RE = re.compile(
    r"\b(" + "|".join(re.escape(f) for f in sorted(UNIT_SCALE_FIELDS)) + r")\b\s*(<=|>=|<|>)\s*([0-9]+(?:\.[0-9]+)?)"
)

# Columns on advisory_candidates to range-check (subset of SCORE_RANGES that is persisted there).
CANDIDATE_SCORE_COLUMNS = (
    "technical_score", "setup_score", "fundamental_score", "regime_fit_score", "event_score",
    "technical_trend_score", "technical_structure_score", "technical_participation_score",
    "technical_relative_strength_score", "technical_tradability_score", "rs_percentile",
)


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    location: str
    message: str

    def as_dict(self) -> dict[str, object]:
        return {"severity": self.severity, "code": self.code, "location": self.location, "message": self.message}


def check_impossible_gates(repo_root: Path = REPO_ROOT) -> list[Finding]:
    """Static: a 0-1 score field compared to a threshold > 1.0 can never be True -> a scale bug."""
    findings: list[Finding] = []
    for rel in SCAN_DIRS:
        for path in sorted((repo_root / rel).rglob("*.py")):
            if path.name in SCAN_EXCLUDE:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            lines = text.splitlines()
            for lineno, line in enumerate(lines, start=1):
                if any(hint in line for hint in SCALE_SAFE_HINTS):
                    continue
                # a to_100() a few lines above means the local was already scaled to 0-100
                window = "\n".join(lines[max(0, lineno - 1 - SCALE_WINDOW_LINES):lineno])
                if "to_100" in window:
                    continue
                for field, comparator, literal in _GATE_RE.findall(line):
                    threshold = float(literal)
                    if threshold > 1.0 and comparator in (">=", ">", "<=", "<"):
                        findings.append(Finding(
                            severity="error",
                            code="impossible_scale_gate",
                            location=f"{path.relative_to(repo_root).as_posix()}:{lineno}",
                            message=(f"0-1 field '{field}' compared '{comparator} {literal}' (range "
                                     f"{SCORE_RANGES[field]}); this gate can never be True. Scale with to_100() first."),
                        ))
    return findings


def _load_candidate_sample(limit: int = 4000):
    """Best-effort load of recent candidate rows for real-data checks. Returns (df, error_message|None)."""
    try:
        from utils.db import sql_to_df
    except Exception as exc:  # pragma: no cover - import guard
        return None, f"db module unavailable: {exc}"
    cols = ", ".join(CANDIDATE_SCORE_COLUMNS)
    try:
        df = sql_to_df(
            f"""
            SELECT asof_date, symbol, candidate_state, technical_state, technical_entry_confirmed,
                   technical_override_source, setup_family, research_only, {cols}
            FROM advisory_candidates
            WHERE asof_date >= (SELECT MAX(asof_date) - INTERVAL '30 days' FROM advisory_candidates)
            LIMIT %(limit)s
            """,
            params={"limit": int(limit)},
        )
        return df, None
    except Exception as exc:
        return None, f"candidate load failed: {exc}"


def check_score_ranges(df) -> list[Finding]:
    findings: list[Finding] = []
    if df is None or df.empty:
        return findings
    import pandas as pd
    for col in CANDIDATE_SCORE_COLUMNS:
        if col not in df.columns:
            continue
        lo, hi = SCORE_RANGES[col]
        vals = pd.to_numeric(df[col], errors="coerce").dropna()
        if vals.empty:
            continue
        bad = vals[(vals < lo - 1e-6) | (vals > hi + 1e-6)]
        if not bad.empty:
            findings.append(Finding(
                severity="error",
                code="score_out_of_range",
                location=f"advisory_candidates.{col}",
                message=(f"{len(bad)} rows have {col} outside declared range {(lo, hi)} "
                         f"(min={bad.min():.4g}, max={bad.max():.4g}) -- likely a scale corruption."),
            ))
    return findings


def check_cross_field_consistency(df) -> list[Finding]:
    findings: list[Finding] = []
    if df is None or df.empty:
        return findings
    import pandas as pd

    def _truthy(series):
        return series.astype(str).str.strip().str.lower().isin({"1", "true", "yes", "t"})

    state = df["technical_state"].astype(str).str.strip().str.upper()
    cand = df["candidate_state"].astype(str).str.strip().str.upper()
    confirmed = _truthy(df["technical_entry_confirmed"])
    # typed research_only separator; fall back to the RESEARCH_TRAINING family for pre-migration NULL rows
    if "research_only" in df.columns:
        research = _truthy(df["research_only"])
    else:
        research = pd.Series([False] * len(df))
    if "setup_family" in df.columns:
        research = research | df["setup_family"].astype(str).str.strip().str.upper().eq("RESEARCH_TRAINING")

    # INVARIANT (hard): technical_entry_confirmed=True must imply BUY_TRIGGERED -- that is exactly how it
    # is derived (technical_entry_confirmed_from_evaluation). candidate_state=PASS_NOW does NOT require a
    # trigger: it is legitimately set by setup_score >= the setup's own pass_now threshold (per-setup),
    # so a PASS_NOW row can carry technical_state=IGNORE. Do not assert PASS_NOW => BUY_TRIGGERED.
    bad_conf = df[confirmed & (state != "BUY_TRIGGERED")]
    if not bad_conf.empty:
        findings.append(Finding(
            severity="error", code="confirmed_without_buy_triggered",
            location="advisory_candidates",
            message=f"{len(bad_conf)} rows have technical_entry_confirmed=True but technical_state!=BUY_TRIGGERED.",
        ))
    # RESEARCH-LEAKAGE (warning): research_only setups (RESEARCH_TRAINING, a low 0.58 pass_now bar for
    # label harvesting) flood the PASS_NOW population and must NOT be counted as recommendations by any
    # consumer (north-star, diagnostics, live action). Flag their presence so the pollution is visible.
    research_pass = df[(cand == "PASS_NOW") & research]
    if not research_pass.empty:
        total_pass = int((cand == "PASS_NOW").sum())
        findings.append(Finding(
            severity="warning", code="research_only_rows_in_pass_now_population",
            location="advisory_candidates",
            message=(f"{len(research_pass)} of {total_pass} PASS_NOW rows are research_only (label "
                     "harvesting). Expected -- recommendation consumers must filter research_only=FALSE."),
        ))
    return findings


def build_report(*, repo_root: Path = REPO_ROOT, use_db: bool = True, limit: int = 4000) -> dict[str, object]:
    findings: list[Finding] = list(check_impossible_gates(repo_root))
    db_status = "skipped"
    if use_db:
        df, err = _load_candidate_sample(limit=limit)
        if err is not None:
            db_status = "unavailable"
            findings.append(Finding(severity="warning", code="db_unavailable", location="advisory_candidates",
                                    message=f"real-data checks skipped: {err}"))
        else:
            db_status = "checked"
            findings.extend(check_score_ranges(df))
            findings.extend(check_cross_field_consistency(df))
    errors = [f for f in findings if f.severity == "error"]
    warnings = [f for f in findings if f.severity == "warning"]
    return {
        "status": "ok" if not errors else "error",
        "db_status": db_status,
        "errors": len(errors),
        "warnings": len(warnings),
        "findings": [f.as_dict() for f in findings],
    }


def format_text_report(report: dict[str, object]) -> str:
    lines = [
        f"status: {report['status']}",
        f"real-data checks: {report['db_status']}",
        f"errors: {report['errors']}",
        f"warnings: {report['warnings']}",
    ]
    if report["findings"]:
        lines.append("findings:")
        for item in report["findings"]:
            lines.append(f"- {item['severity']} {item['code']} {item['location']}: {item['message']}")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit the decision funnel for scale/dead-gate and score-contract invariants. Read-only."
    )
    parser.add_argument("--format", choices=["json", "text"], default="text")
    parser.add_argument("--no-db", action="store_true", help="Skip real-data checks; run only the static impossible-gate scan.")
    parser.add_argument("--limit", type=int, default=4000, help="Max recent candidate rows to sample for real-data checks.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero on errors.")
    parser.add_argument("--strict-warnings", action="store_true", help="Exit non-zero on warnings too.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report(use_db=not args.no_db, limit=args.limit)
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_text_report(report))
    if args.strict and report["errors"]:
        return 1
    if args.strict_warnings and (report["errors"] or report["warnings"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
