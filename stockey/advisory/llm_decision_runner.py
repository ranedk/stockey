"""Daily review-only LLM decision runner.

The orchestration that was missing: loop a universe of symbols on an as-of date, build each one's
point-in-time evidence packet (`advisory/llm_evidence_packet`), ask the decision policy
(`advisory/llm_decision_policy.decide`) for a graded + sized proposal, and (optionally) persist them
to `advisory_llm_decisions`. This is what produces the REAL, review-only decisions that the outcome
labeler + monitor + graduation need -- decisions never move capital here (both master flags stay off;
`broker_execution_allowed` is always False), they are recorded so their outcomes can mature.

Pure-ish + injectable: the universe, packet, and price loaders and the LLM caller are all parameters,
so the loop is testable without a DB or a model.
"""

from __future__ import annotations

import math
import os
from collections import Counter
from typing import Any, Callable

from advisory.llm_decision_policy import decide
from advisory.llm_evidence_packet import load_evidence_packet

DEFAULT_CAPITAL_INR = float(os.getenv("LLM_DECISION_RUNNER_CAPITAL_INR", "1000000"))
DEFAULT_UNIVERSE_LIMIT = int(os.getenv("LLM_DECISION_RUNNER_UNIVERSE_LIMIT", "200"))


def _num(value: Any) -> float | None:
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _first_row(query: str, params: dict[str, Any]) -> dict[str, Any] | None:
    try:
        from utils.db import sql_to_df

        df = sql_to_df(query, params=params)
        return None if df is None or df.empty else df.iloc[0].to_dict()
    except Exception:
        return None


def _all_rows(query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        from utils.db import sql_to_df

        df = sql_to_df(query, params=params)
        return [] if df is None or df.empty else [row.to_dict() for _, row in df.iterrows()]
    except Exception:
        return []


def load_default_universe(asof_date: Any, *, limit: int = DEFAULT_UNIVERSE_LIMIT,
                          rows_loader: Callable[[str, dict[str, Any]], list[dict[str, Any]]] = _all_rows) -> list[str]:
    """The symbols tracked as of the latest technical date on/before asof_date (the active universe)."""
    rows = rows_loader(
        "SELECT DISTINCT symbol FROM advisory_technical_daily WHERE asof_date = "
        "(SELECT MAX(asof_date) FROM advisory_technical_daily WHERE asof_date<=%(asof)s) "
        "ORDER BY symbol LIMIT %(limit)s",
        {"asof": str(asof_date)[:10], "limit": int(limit)},
    )
    return [str(r.get("symbol")).upper() for r in rows if r.get("symbol")]


def resolve_default_asof_date(*, row_loader: Callable[[str, dict[str, Any]], dict[str, Any] | None] = _first_row) -> str | None:
    """The latest technical date in the DB -- the natural 'as of' for a daily review-only run.

    Defaulting to this (rather than the wall-clock day) keeps the runner point-in-time and robust on
    market holidays: it decides on the most recent advisory data, not on an empty future date.
    """
    row = row_loader("SELECT MAX(asof_date) AS asof FROM advisory_technical_daily", {})
    if not isinstance(row, dict):
        return None
    value = row.get("asof")
    return str(value)[:10] if value else None


def _default_price_atr(symbol: str, asof_date: Any, *, row_loader: Callable[[str, dict[str, Any]], dict[str, Any] | None] = _first_row) -> tuple[float | None, float | None]:
    row = row_loader(
        "SELECT adj_close, atr_20 FROM advisory_technical_daily WHERE symbol=%(symbol)s AND asof_date<=%(asof)s "
        "ORDER BY asof_date DESC LIMIT 1",
        {"symbol": str(symbol).upper(), "asof": str(asof_date)[:10]},
    )
    if not isinstance(row, dict):
        return None, None
    return _num(row.get("adj_close")), _num(row.get("atr_20"))


def run_daily_decisions(
    asof_date: Any,
    *,
    symbols: list[str] | None = None,
    capital: float = DEFAULT_CAPITAL_INR,
    use_llm: bool = True,
    persist: bool = False,
    model: str | None = None,
    llm_caller: Callable[..., Any] | None = None,
    packet_loader: Callable[[str, Any], dict[str, Any]] = load_evidence_packet,
    price_atr_loader: Callable[[str, Any], tuple[float | None, float | None]] = _default_price_atr,
) -> dict[str, Any]:
    """Run the review-only decision policy over a universe and return a summary (optionally persist).

    Never moves capital: every decision is review-only and `broker_execution_allowed` is False.
    """
    symbols = symbols if symbols is not None else load_default_universe(asof_date)
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for symbol in symbols:
        try:
            packet = packet_loader(symbol, asof_date)
            price, atr = price_atr_loader(symbol, asof_date)
            result = decide(packet, capital=capital, price=price, atr=atr, use_llm=use_llm,
                            model=model, llm_caller=llm_caller)
            results.append(result)
        except Exception as exc:  # noqa: BLE001 - one symbol must not abort the batch
            errors.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})

    persisted = 0
    if persist and results:
        from advisory.llm_decision_store import persist_decisions

        persisted = persist_decisions(results, decided_at=asof_date)

    by_action = Counter((r.get("proposal") or {}).get("action") for r in results)
    by_mode = Counter((r.get("contract") or {}).get("decision_mode") or "none" for r in results)
    return {
        "asof_date": str(asof_date)[:10],
        "universe": len(symbols),
        "decisions": len(results),
        "grounded_for_live": sum(1 for r in results if r.get("meets_data_grounding_for_live")),
        "by_action": dict(by_action),
        "by_decision_mode": dict(by_mode),
        "persisted": persisted,
        "error_count": len(errors),
        "errors": errors[:20],
        "broker_execution_allowed": False,
        "authority_scope": "llm_decision_runner_review_only",
    }


def main() -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Daily review-only LLM decision runner.")
    parser.add_argument("--date", default=None, help="As-of trading date (YYYY-MM-DD). Defaults to the latest advisory technical date.")
    parser.add_argument("--symbols", nargs="*", help="Symbols (default: the active universe).")
    parser.add_argument("--limit", type=int, default=DEFAULT_UNIVERSE_LIMIT)
    parser.add_argument("--capital", type=float, default=DEFAULT_CAPITAL_INR)
    parser.add_argument("--persist", action="store_true", help="Persist decisions to advisory_llm_decisions.")
    parser.add_argument("--no-llm", action="store_true", help="Deterministic WATCH fallback instead of the LLM.")
    parser.add_argument("--label-outcomes", action="store_true",
                        help="After persisting, mature decisions whose horizon elapsed into advisory_llm_decision_outcomes (feeds the .3 monitor).")
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    asof = args.date or resolve_default_asof_date()
    if not asof:
        print(json.dumps({"status": "no_asof_date",
                          "error": "no advisory_technical_daily rows; run the advisory pipeline first"}, indent=2))
        return 1

    symbols = args.symbols if args.symbols else load_default_universe(asof, limit=args.limit)
    summary = run_daily_decisions(asof, symbols=symbols, capital=args.capital,
                                  use_llm=not args.no_llm, persist=args.persist, model=args.model)

    if args.label_outcomes:
        from advisory.llm_decision_outcome_labeler import label_decisions, persist_outcomes

        labeled = label_decisions(asof)
        outcome_rows = labeled.get("outcomes") or []
        summary["outcomes_labeled"] = len(outcome_rows)
        summary["outcomes_matured"] = sum(1 for row in outcome_rows if row.get("matured"))
        summary["outcomes_persisted"] = persist_outcomes(outcome_rows, labeled_at=asof) if args.persist else 0

    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
