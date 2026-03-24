from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from advisory.setup_registry import load_setup_registry
from advisory.setup_trace import build_trace
from utils.sync import parse_datetime_arg


def build_dashboard(*, asof_date: pd.Timestamp | None = None, setup_ids: list[str] | None = None) -> pd.DataFrame:
    setups = load_setup_registry()
    if setup_ids:
        selected = {value.upper() for value in setup_ids}
        setups = [setup for setup in setups if str(setup["setup_id"]).upper() in selected]

    rows: list[dict[str, Any]] = []
    for setup in setups:
        trace = build_trace(str(setup["setup_id"]), asof_date=asof_date)
        stage = trace.get("stage_summary", {})
        funnel = trace.get("funnel_summary", {})
        decision = trace.get("decision_summary", {})
        top_rejections = decision.get("top_rejection_reasons") or {}
        top_reason = None
        if isinstance(top_rejections, dict) and top_rejections:
            top_reason = next(iter(top_rejections.items()))
        rows.append(
            {
                "setup_id": setup["setup_id"],
                "setup_name": setup["setup_name"],
                "screener_slug": setup.get("screener_slug"),
                "asof_date": stage.get("asof_date"),
                "regime_name": stage.get("regime_name"),
                "screener_universe_count": stage.get("screener_universe_count"),
                "candidate_count": stage.get("candidate_count"),
                "rejection_count": stage.get("rejection_count"),
                "watchlist_count": stage.get("watchlist_count"),
                "announcement_event_count": stage.get("announcement_event_count"),
                "news_event_count": stage.get("news_event_count"),
                "evaluation_count": stage.get("evaluation_count"),
                "allocation_count": stage.get("allocation_count"),
                "portfolio_count": stage.get("portfolio_count"),
                "execution_count": stage.get("execution_count"),
                "screener_to_candidate": funnel.get("screener_to_candidate"),
                "candidate_to_watchlist": funnel.get("candidate_to_watchlist"),
                "evaluation_to_allocation": funnel.get("evaluation_to_allocation"),
                "latest_event_verdict": decision.get("latest_event_verdict"),
                "latest_event_source": decision.get("latest_event_source"),
                "latest_portfolio_status": decision.get("latest_portfolio_status"),
                "top_rejection_reason": None if top_reason is None else top_reason[0],
                "top_rejection_count": None if top_reason is None else top_reason[1],
            }
        )
    return pd.DataFrame(rows)


def _text_cell(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "-"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    text = str(value)
    return text if text else "-"


def render_text_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "No setups found."
    columns = [
        ("setup_id", 22),
        ("regime_name", 20),
        ("univ", 5),
        ("cand", 5),
        ("rej", 5),
        ("watch", 5),
        ("eval", 5),
        ("alloc", 5),
        ("port", 5),
        ("top_rejection_reason", 28),
    ]
    header = " ".join(label.ljust(width) for label, width in columns)
    separator = " ".join("-" * width for _, width in columns)
    lines = [header, separator]
    for _, row in df.iterrows():
        values = [
            _text_cell(row.get("setup_id"))[:22],
            _text_cell(row.get("regime_name"))[:20],
            _text_cell(row.get("screener_universe_count"))[:5],
            _text_cell(row.get("candidate_count"))[:5],
            _text_cell(row.get("rejection_count"))[:5],
            _text_cell(row.get("watchlist_count"))[:5],
            _text_cell(row.get("evaluation_count"))[:5],
            _text_cell(row.get("allocation_count"))[:5],
            _text_cell(row.get("portfolio_count"))[:5],
            _text_cell(row.get("top_rejection_reason"))[:28],
        ]
        lines.append(
            " ".join(value.ljust(width) for value, (_, width) in zip(values, columns))
        )
    return "\n".join(lines)


def summarize(df: pd.DataFrame) -> dict[str, Any]:
    return {
        "status": "ok",
        "setup_count": int(len(df)),
        "rows": df.to_dict(orient="records") if not df.empty else [],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show the advisory setup dashboard.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Optional asof date in YYYY-MM-DD")
    parser.add_argument("--setup", dest="setup_ids", nargs="*", help="Optional setup ids")
    parser.add_argument("--format", choices=["json", "text"], default="text")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    asof_date = pd.Timestamp(args.date, tz="UTC") if args.date else None
    df = build_dashboard(asof_date=asof_date, setup_ids=args.setup_ids)
    if args.format == "json":
        print(json.dumps(summarize(df), indent=2, ensure_ascii=False, default=str))
    else:
        print(render_text_table(df))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
