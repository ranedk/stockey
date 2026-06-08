from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import yaml

from advisory.signal_quality_promotion import DECISIONS_TABLE as SIGNAL_QUALITY_DECISIONS_TABLE
from advisory.technical_threshold_promotion import DECISIONS_TABLE as TECHNICAL_DECISIONS_TABLE
from utils.db import db_session, sql_to_df, upsert_to_db


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "advisory_setups.yaml"
PREVIEWS_TABLE = "advisory_config_change_previews"
ChangeSource = Literal["technical_threshold", "signal_quality_overlay"]


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def parse_jsonish(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        if pd.isna(value):
            return default
    except Exception:
        pass
    try:
        return json.loads(str(value))
    except Exception:
        return default


def ensure_tables() -> None:
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {PREVIEWS_TABLE} (
                preview_id TEXT NOT NULL,
                generated_at TIMESTAMPTZ NOT NULL,
                source_type TEXT NOT NULL,
                source_key TEXT NOT NULL,
                config_path TEXT NOT NULL,
                review_status TEXT,
                decision_status TEXT,
                patch_payload_json TEXT,
                unified_diff TEXT,
                rollback_note TEXT,
                safety_checks_json TEXT,
                load_ts TIMESTAMPTZ,
                UNIQUE (preview_id)
            )
            """
        )


def _yaml_block(key: str, value: Any, *, indent: int = 4) -> list[str]:
    text = yaml.safe_dump({key: value}, sort_keys=True, default_flow_style=False, allow_unicode=True).rstrip()
    return [(" " * indent) + line for line in text.splitlines()]


def _find_setup_block(lines: list[str], setup_id: str) -> tuple[int, int]:
    marker = f"  - setup_id: {str(setup_id).strip()}"
    start = -1
    for idx, line in enumerate(lines):
        if line.rstrip() == marker:
            start = idx
            break
    if start < 0:
        raise ValueError(f"setup_id not found in config: {setup_id}")
    end = len(lines)
    for idx in range(start + 1, len(lines)):
        if lines[idx].startswith("  - setup_id: "):
            end = idx
            break
    return start, end


def _child_block_end(lines: list[str], start: int, block_end: int, *, child_indent: int = 4) -> int:
    prefix = " " * child_indent
    for idx in range(start + 1, block_end):
        line = lines[idx]
        if line.strip() and not line.startswith(prefix + " "):
            return idx
    return block_end


def render_technical_threshold_config(config_text: str, *, setup_id: str, technical_thresholds: dict[str, Any]) -> str:
    if not technical_thresholds:
        raise ValueError("technical_thresholds cannot be empty")
    lines = config_text.splitlines()
    start, end = _find_setup_block(lines, setup_id)
    new_block = _yaml_block("technical_thresholds", technical_thresholds, indent=4)
    existing_start = -1
    for idx in range(start + 1, end):
        if lines[idx].startswith("    technical_thresholds:"):
            existing_start = idx
            break
    if existing_start >= 0:
        existing_end = _child_block_end(lines, existing_start, end, child_indent=4)
        updated = lines[:existing_start] + new_block + lines[existing_end:]
        return "\n".join(updated) + "\n"

    insert_after = -1
    for preferred_key in ["score_thresholds", "technical_rules"]:
        for idx in range(start + 1, end):
            if lines[idx].startswith(f"    {preferred_key}:"):
                insert_after = _child_block_end(lines, idx, end, child_indent=4)
                break
        if insert_after >= 0:
            break
    if insert_after < 0:
        insert_after = start + 1
    updated = lines[:insert_after] + new_block + lines[insert_after:]
    return "\n".join(updated) + "\n"


def render_signal_quality_overlay_config(config_text: str, *, rule_suggestion: dict[str, Any]) -> str:
    overlay = str(rule_suggestion.get("signal_quality_overlay") or rule_suggestion.get("overlay") or "").strip()
    if not overlay:
        raise ValueError("signal_quality_overlay is required")
    rule = {
        "overlay": overlay,
        "minimum_horizon_days": rule_suggestion.get("minimum_horizon_days"),
        "minimum_matured_rows": rule_suggestion.get("minimum_matured_rows"),
        "minimum_lift_vs_technical_only": rule_suggestion.get("minimum_lift_vs_technical_only"),
        "authority": rule_suggestion.get("authority") or "review_input_only",
        "broker_execution_allowed": False,
        "status": "disabled_review_candidate",
    }
    rule = {key: value for key, value in rule.items() if value is not None}
    lines = config_text.splitlines()
    block = yaml.safe_dump([rule], sort_keys=True, default_flow_style=False, allow_unicode=True).rstrip().splitlines()
    indented = ["  " + line for line in block]
    for idx, line in enumerate(lines):
        if line.rstrip() == "signal_quality_overlay_rules:":
            insert_at = idx + 1
            while insert_at < len(lines) and (lines[insert_at].startswith("  ") or not lines[insert_at].strip()):
                insert_at += 1
            updated = lines[:insert_at] + indented + lines[insert_at:]
            return "\n".join(updated) + "\n"
    updated = lines + ["", "signal_quality_overlay_rules:"] + indented
    return "\n".join(updated) + "\n"


def build_unified_diff(original_text: str, updated_text: str, *, path: str) -> str:
    return "".join(
        difflib.unified_diff(
            original_text.splitlines(keepends=True),
            updated_text.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def build_preview_from_patch(
    *,
    source_type: ChangeSource,
    source_key: str,
    patch_payload: dict[str, Any],
    config_path: Path = DEFAULT_CONFIG_PATH,
    persist: bool = True,
) -> dict[str, Any]:
    config_text = config_path.read_text(encoding="utf-8")
    relative_path = str(config_path.relative_to(REPO_ROOT)) if config_path.is_relative_to(REPO_ROOT) else str(config_path)
    if source_type == "technical_threshold":
        updated_text = render_technical_threshold_config(
            config_text,
            setup_id=str(patch_payload.get("setup_id") or ""),
            technical_thresholds=patch_payload.get("technical_thresholds") if isinstance(patch_payload.get("technical_thresholds"), dict) else {},
        )
    elif source_type == "signal_quality_overlay":
        suggestion = patch_payload.get("rule_suggestion") if isinstance(patch_payload.get("rule_suggestion"), dict) else {}
        updated_text = render_signal_quality_overlay_config(config_text, rule_suggestion=suggestion)
    else:
        raise ValueError(f"Unsupported source_type: {source_type}")
    diff = build_unified_diff(config_text, updated_text, path=relative_path)
    if not diff.strip():
        raise ValueError("Generated config diff is empty")
    generated_at = pd.Timestamp.utcnow()
    preview_id = f"{source_type}:{source_key}:{generated_at.isoformat()}"
    result = {
        "status": "ok",
        "preview_id": preview_id,
        "generated_at": generated_at,
        "source_type": source_type,
        "source_key": source_key,
        "config_path": relative_path,
        "review_status": "manual_review_required",
        "decision_status": "preview_only_not_applied",
        "patch_payload": patch_payload,
        "unified_diff": diff,
        "rollback_note": f"Rollback by reverting the reviewed change in {relative_path}; this preview did not modify files.",
        "safety_checks": [
            "Preview only; no file write was performed.",
            "Broker execution remains disabled by this preview.",
            "Apply manually only after checking sample size, trust gate status, and production impact.",
        ],
        "applied": False,
    }
    if persist:
        persist_preview(result)
    return result


def persist_preview(result: dict[str, Any]) -> None:
    ensure_tables()
    row = pd.DataFrame(
        [
            {
                "preview_id": result["preview_id"],
                "generated_at": result["generated_at"],
                "source_type": result["source_type"],
                "source_key": result["source_key"],
                "config_path": result["config_path"],
                "review_status": result["review_status"],
                "decision_status": result["decision_status"],
                "patch_payload_json": json_dumps(result["patch_payload"]),
                "unified_diff": result["unified_diff"],
                "rollback_note": result["rollback_note"],
                "safety_checks_json": json_dumps(result["safety_checks"]),
                "load_ts": pd.Timestamp.utcnow(),
            }
        ]
    )
    upsert_to_db(row, PREVIEWS_TABLE, unique_keys=["preview_id"])


def load_latest_approved_technical_decision(*, setup_id: str, config_id: str, reviewed_at: Any | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"setup_id": str(setup_id), "config_id": str(config_id)}
    reviewed_filter = ""
    if reviewed_at:
        parsed = pd.to_datetime(reviewed_at, utc=True, errors="coerce")
        if pd.isna(parsed):
            raise ValueError(f"Invalid reviewed_at: {reviewed_at}")
        params["reviewed_at"] = parsed
        reviewed_filter = "AND reviewed_at = %(reviewed_at)s"
    df = sql_to_df(
        f"""
        SELECT *
        FROM {TECHNICAL_DECISIONS_TABLE}
        WHERE setup_id = %(setup_id)s
          AND config_id = %(config_id)s
          AND decision = 'approved'
          {reviewed_filter}
        ORDER BY decided_at DESC
        LIMIT 1
        """,
        params=params,
        retries=3,
    )
    if df.empty:
        raise ValueError(f"No approved technical threshold decision found for {setup_id}/{config_id}")
    row = df.iloc[0].to_dict()
    row["final_patch"] = parse_jsonish(row.get("final_patch_json"), {})
    return row


def load_latest_approved_signal_quality_decision(
    *,
    evaluated_at: Any,
    horizon_days: int,
    variant: str,
    reviewed_at: Any | None = None,
) -> dict[str, Any]:
    parsed_evaluated = pd.to_datetime(evaluated_at, utc=True, errors="coerce")
    if pd.isna(parsed_evaluated):
        raise ValueError(f"Invalid evaluated_at: {evaluated_at}")
    params: dict[str, Any] = {
        "evaluated_at": parsed_evaluated,
        "horizon_days": int(horizon_days),
        "variant": str(variant),
    }
    reviewed_filter = ""
    if reviewed_at:
        parsed_reviewed = pd.to_datetime(reviewed_at, utc=True, errors="coerce")
        if pd.isna(parsed_reviewed):
            raise ValueError(f"Invalid reviewed_at: {reviewed_at}")
        params["reviewed_at"] = parsed_reviewed
        reviewed_filter = "AND reviewed_at = %(reviewed_at)s"
    df = sql_to_df(
        f"""
        SELECT *
        FROM {SIGNAL_QUALITY_DECISIONS_TABLE}
        WHERE evaluated_at = %(evaluated_at)s
          AND horizon_days = %(horizon_days)s
          AND variant = %(variant)s
          AND decision = 'approved'
          {reviewed_filter}
        ORDER BY decided_at DESC
        LIMIT 1
        """,
        params=params,
        retries=3,
    )
    if df.empty:
        raise ValueError(f"No approved signal-quality decision found for {evaluated_at}/{horizon_days}/{variant}")
    row = df.iloc[0].to_dict()
    row["final_patch"] = parse_jsonish(row.get("final_patch_json"), {})
    return row


def build_technical_threshold_preview(*, setup_id: str, config_id: str, reviewed_at: Any | None = None, persist: bool = True) -> dict[str, Any]:
    decision = load_latest_approved_technical_decision(setup_id=setup_id, config_id=config_id, reviewed_at=reviewed_at)
    source_key = f"{decision.get('reviewed_at')}:{setup_id}:{config_id}"
    result = build_preview_from_patch(
        source_type="technical_threshold",
        source_key=source_key,
        patch_payload=decision.get("final_patch") or {},
        persist=persist,
    )
    result["decision"] = {key: decision.get(key) for key in ["decided_at", "reviewed_at", "setup_id", "config_id", "operator_id", "decision_reason"]}
    return result


def build_signal_quality_overlay_preview(
    *,
    evaluated_at: Any,
    horizon_days: int,
    variant: str,
    reviewed_at: Any | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    decision = load_latest_approved_signal_quality_decision(
        evaluated_at=evaluated_at,
        horizon_days=horizon_days,
        variant=variant,
        reviewed_at=reviewed_at,
    )
    source_key = f"{decision.get('reviewed_at')}:{decision.get('evaluated_at')}:{horizon_days}:{variant}"
    result = build_preview_from_patch(
        source_type="signal_quality_overlay",
        source_key=source_key,
        patch_payload=decision.get("final_patch") or {},
        persist=persist,
    )
    result["decision"] = {
        key: decision.get(key)
        for key in ["decided_at", "reviewed_at", "evaluated_at", "horizon_days", "variant", "operator_id", "decision_reason"]
    }
    return result


def load_previews(limit: int = 25) -> list[dict[str, Any]]:
    ensure_tables()
    df = sql_to_df(
        f"""
        SELECT *
        FROM {PREVIEWS_TABLE}
        ORDER BY generated_at DESC
        LIMIT %(limit)s
        """,
        params={"limit": max(1, int(limit))},
        retries=3,
    )
    if df.empty:
        return []
    out = df.copy()
    for col in ["patch_payload_json", "safety_checks_json"]:
        if col in out.columns:
            out[col.replace("_json", "")] = out[col].map(lambda value: parse_jsonish(value, [] if col == "safety_checks_json" else {}))
    out = out.astype(object).where(pd.notna(out), None)
    return out.to_dict(orient="records")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate reviewed config diffs from approved promotion decisions without applying them.")
    parser.add_argument("--source-type", choices=["technical_threshold", "signal_quality_overlay"], required=True)
    parser.add_argument("--setup-id")
    parser.add_argument("--config-id")
    parser.add_argument("--evaluated-at")
    parser.add_argument("--horizon-days", type=int)
    parser.add_argument("--variant")
    parser.add_argument("--reviewed-at")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.source_type == "technical_threshold":
        if not args.setup_id or not args.config_id:
            raise SystemExit("--setup-id and --config-id are required for technical_threshold")
        result = build_technical_threshold_preview(
            setup_id=args.setup_id,
            config_id=args.config_id,
            reviewed_at=args.reviewed_at,
            persist=not bool(args.dry_run),
        )
    else:
        if not args.evaluated_at or args.horizon_days is None or not args.variant:
            raise SystemExit("--evaluated-at, --horizon-days, and --variant are required for signal_quality_overlay")
        result = build_signal_quality_overlay_preview(
            evaluated_at=args.evaluated_at,
            horizon_days=int(args.horizon_days),
            variant=args.variant,
            reviewed_at=args.reviewed_at,
            persist=not bool(args.dry_run),
        )
    result["dry_run"] = bool(args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
