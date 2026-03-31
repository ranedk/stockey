from __future__ import annotations

import argparse
import json

import pandas as pd

from advisory.setup_registry import load_setup_registry
from utils.db import db_session, sql_to_df, upsert_to_db
from utils.sync import parse_datetime_arg


TABLE_NAME = "advisory_watchlist"
DEFAULT_SCORE_THRESHOLDS = {
    "pass_now": 0.68,
    "watch_breakout": 0.58,
    "watch_event": 0.48,
}
_TRANSITION_CUTOFF = 0.12


def normalize_timestamp(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce").dt.normalize()


def ensure_watchlist_table() -> None:
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                asof_date TIMESTAMPTZ NOT NULL,
                setup_id TEXT NOT NULL,
                setup_name TEXT,
                regime_name TEXT,
                base_regime TEXT,
                news_overlay TEXT,
                theme_ids TEXT,
                symbol TEXT NOT NULL,
                company_master_id TEXT,
                screener_slug TEXT,
                source_screener_slug TEXT,
                source_screener_list TEXT,
                rank BIGINT,
                candidate_state TEXT,
                current_state TEXT,
                watch_reason_detail TEXT,
                entry_style TEXT,
                attractive_price_low DOUBLE PRECISION,
                attractive_price_high DOUBLE PRECISION,
                invalidation_price DOUBLE PRECISION,
                entry_note TEXT,
                near_miss_flag BOOLEAN,
                last_event_class TEXT,
                last_state_transition_hint TEXT,
                last_event_score_impact DOUBLE PRECISION,
                watch_enabled BOOLEAN,
                watch_reasons_json TEXT,
                watch_status TEXT,
                state_updated_at TIMESTAMPTZ,
                watch_started_at TIMESTAMPTZ,
                last_checked_at TIMESTAMPTZ,
                last_document_published_on TIMESTAMPTZ,
                load_ts TIMESTAMPTZ,
                UNIQUE (asof_date, setup_id, symbol)
            )
            """
        )
        column_defs = {
            "setup_name": "TEXT",
            "regime_name": "TEXT",
            "base_regime": "TEXT",
            "news_overlay": "TEXT",
            "theme_ids": "TEXT",
            "company_master_id": "TEXT",
            "screener_slug": "TEXT",
            "source_screener_slug": "TEXT",
            "source_screener_list": "TEXT",
            "rank": "BIGINT",
            "candidate_state": "TEXT",
            "current_state": "TEXT",
            "watch_reason_detail": "TEXT",
            "entry_style": "TEXT",
            "attractive_price_low": "DOUBLE PRECISION",
            "attractive_price_high": "DOUBLE PRECISION",
            "invalidation_price": "DOUBLE PRECISION",
            "entry_note": "TEXT",
            "near_miss_flag": "BOOLEAN",
            "last_event_class": "TEXT",
            "last_state_transition_hint": "TEXT",
            "last_event_score_impact": "DOUBLE PRECISION",
            "watch_enabled": "BOOLEAN",
            "watch_reasons_json": "TEXT",
            "watch_status": "TEXT",
            "state_updated_at": "TIMESTAMPTZ",
            "watch_started_at": "TIMESTAMPTZ",
            "last_checked_at": "TIMESTAMPTZ",
            "last_document_published_on": "TIMESTAMPTZ",
            "load_ts": "TIMESTAMPTZ",
        }
        for column, sql_type in column_defs.items():
            cur.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS {column} {sql_type}")


def load_candidate_rows(
    *,
    asof_date: pd.Timestamp | None = None,
    setup_ids: list[str] | None = None,
    symbols: list[str] | None = None,
) -> pd.DataFrame:
    clauses = []
    params: list[object] = []
    if asof_date is not None:
        clauses.append("asof_date = %s")
        params.append(asof_date)
    else:
        clauses.append("asof_date = (SELECT MAX(asof_date) FROM advisory_candidates)")
    if setup_ids:
        clauses.append("setup_id = ANY(%s)")
        params.append([value.upper() for value in setup_ids])
    if symbols:
        clauses.append("symbol = ANY(%s)")
        params.append([value.upper() for value in symbols])
    df = sql_to_df(
        f"""
        SELECT *
        FROM advisory_candidates
        WHERE {' AND '.join(clauses)}
        ORDER BY setup_id, symbol
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return df
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    if "watch_enabled" not in df.columns:
        df["watch_enabled"] = True
    else:
        df["watch_enabled"] = df["watch_enabled"].fillna(True)
    if "watch_reasons" not in df.columns:
        df["watch_reasons"] = "[]"
    if "candidate_state" not in df.columns:
        df["candidate_state"] = "PASS_NOW"
    df = df[df["watch_enabled"] == True].reset_index(drop=True)
    return df


def load_latest_event_transitions(
    *,
    asof_date: pd.Timestamp | None = None,
    setup_ids: list[str] | None = None,
    symbols: list[str] | None = None,
) -> pd.DataFrame:
    try:
        clauses = ["1 = 1"]
        params: list[object] = []
        if asof_date is not None:
            clauses.append("asof_date = %s")
            params.append(asof_date)
        else:
            clauses.append("asof_date = (SELECT MAX(asof_date) FROM advisory_event_evaluations)")
        if setup_ids:
            clauses.append("setup_id = ANY(%s)")
            params.append([value.upper() for value in setup_ids])
        if symbols:
            clauses.append("symbol = ANY(%s)")
            params.append([value.upper() for value in symbols])
        df = sql_to_df(
            f"""
            SELECT
                asof_date,
                setup_id,
                symbol,
                published_on,
                event_class,
                state_transition_hint,
                score_impact
            FROM advisory_event_evaluations
            WHERE {' AND '.join(clauses)}
            ORDER BY setup_id, symbol, published_on, load_ts NULLS LAST
            """,
            params=tuple(params) if params else None,
        )
    except Exception:
        return pd.DataFrame()
    if df.empty:
        return df
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    df["published_on"] = pd.to_datetime(df["published_on"], utc=True, errors="coerce")
    df["symbol"] = df["symbol"].astype("string").str.upper()
    df["score_impact"] = pd.to_numeric(df["score_impact"], errors="coerce").fillna(0.0)

    rows: list[dict[str, object]] = []
    for (asof_key, setup_id, symbol), group in df.groupby(["asof_date", "setup_id", "symbol"], dropna=False, sort=False):
        group = group.sort_values(["published_on"], ascending=[True], kind="stable")
        latest = group.iloc[-1]
        hints = {str(value).upper() for value in group["state_transition_hint"].dropna().astype(str)}
        total_score_impact = round(max(-0.35, min(0.35, float(group["score_impact"].sum()))), 4)
        if "DOWNGRADE_TO_REJECT" in hints:
            transition_hint = "DOWNGRADE_TO_REJECT"
        elif "UPGRADE_TO_PASS_NOW" in hints:
            transition_hint = "UPGRADE_TO_PASS_NOW"
        elif total_score_impact >= _TRANSITION_CUTOFF:
            transition_hint = "RAISE_SCORE_ONLY"
        elif total_score_impact <= -_TRANSITION_CUTOFF:
            transition_hint = "CUT_SCORE_ONLY"
        elif "REVIEW_MANUAL" in hints:
            transition_hint = "REVIEW_MANUAL"
        else:
            transition_hint = "NO_CHANGE"
        rows.append(
            {
                "asof_date": asof_key,
                "setup_id": setup_id,
                "symbol": symbol,
                "published_on": latest["published_on"],
                "event_class": latest.get("event_class"),
                "state_transition_hint": transition_hint,
                "score_impact": total_score_impact,
                "has_review_manual": "REVIEW_MANUAL" in hints,
            }
        )
    return pd.DataFrame(rows)


def load_setup_thresholds() -> dict[str, dict[str, float]]:
    thresholds: dict[str, dict[str, float]] = {}
    for setup in load_setup_registry():
        setup_id = str(setup.get("setup_id") or "").upper()
        if not setup_id:
            continue
        raw = {**DEFAULT_SCORE_THRESHOLDS, **(setup.get("score_thresholds") or {})}
        thresholds[setup_id] = {key: float(value) for key, value in raw.items() if value is not None}
    return thresholds


def derive_current_state(
    candidate_state: str,
    transition_hint: str | None,
    *,
    setup_score: float | None = None,
    score_impact: float | None = None,
    thresholds: dict[str, float] | None = None,
) -> str:
    state = str(candidate_state or "WATCH_EVENT").upper()
    hint = "" if pd.isna(transition_hint) else str(transition_hint).upper()
    if hint == "UPGRADE_TO_PASS_NOW":
        return "PASS_NOW"
    if hint == "DOWNGRADE_TO_REJECT":
        return "REJECT"
    if hint == "RAISE_SCORE_ONLY" and setup_score is not None and score_impact is not None and thresholds:
        adjusted_score = float(setup_score) + float(score_impact)
        if adjusted_score >= float(thresholds.get("pass_now", DEFAULT_SCORE_THRESHOLDS["pass_now"])):
            return "PASS_NOW"
        if adjusted_score >= float(thresholds.get("watch_breakout", DEFAULT_SCORE_THRESHOLDS["watch_breakout"])):
            return "WATCH_BREAKOUT"
    if hint == "CUT_SCORE_ONLY" and setup_score is not None and score_impact is not None and thresholds:
        adjusted_score = float(setup_score) + float(score_impact)
        if adjusted_score < float(thresholds.get("watch_event", DEFAULT_SCORE_THRESHOLDS["watch_event"])):
            return "REJECT"
        if adjusted_score < float(thresholds.get("watch_breakout", DEFAULT_SCORE_THRESHOLDS["watch_breakout"])) and state == "PASS_NOW":
            return "WATCH_EVENT"
    return state


def build_watchlist(
    *,
    asof_date: pd.Timestamp | None = None,
    setup_ids: list[str] | None = None,
    symbols: list[str] | None = None,
) -> pd.DataFrame:
    candidates = load_candidate_rows(asof_date=asof_date, setup_ids=setup_ids, symbols=symbols)
    if candidates.empty:
        return pd.DataFrame()
    thresholds_by_setup = load_setup_thresholds()
    event_transitions = load_latest_event_transitions(asof_date=asof_date, setup_ids=setup_ids, symbols=symbols)

    try:
        existing = sql_to_df(
            """
            SELECT asof_date, setup_id, symbol, last_checked_at, last_document_published_on, watch_status, current_state
            FROM advisory_watchlist
            """
        )
    except Exception:
        existing = pd.DataFrame()
    if not existing.empty:
        existing["asof_date"] = normalize_timestamp(existing["asof_date"])
        existing["symbol"] = existing["symbol"].astype("string").str.upper()
        existing = existing[
            existing.set_index(["asof_date", "setup_id", "symbol"]).index.isin(
                candidates.set_index(["asof_date", "setup_id", "symbol"]).index
            )
        ]

    out = candidates.copy()
    if not event_transitions.empty:
        if "has_review_manual" not in event_transitions.columns:
            event_transitions = event_transitions.copy()
            event_transitions["has_review_manual"] = False
        out = out.merge(
            event_transitions[["setup_id", "symbol", "event_class", "state_transition_hint", "score_impact", "has_review_manual"]],
            on=["setup_id", "symbol"],
            how="left",
        )
    else:
        out["event_class"] = pd.NA
        out["state_transition_hint"] = pd.NA
        out["score_impact"] = pd.NA
        out["has_review_manual"] = False
    out["current_state"] = out.apply(
        lambda row: derive_current_state(
            str(row.get("candidate_state") or ""),
            row.get("state_transition_hint"),
            setup_score=pd.to_numeric(row.get("setup_score"), errors="coerce"),
            score_impact=pd.to_numeric(row.get("score_impact"), errors="coerce"),
            thresholds=thresholds_by_setup.get(str(row.get("setup_id") or "").upper(), DEFAULT_SCORE_THRESHOLDS),
        ),
        axis=1,
    )
    out["watch_reason_detail"] = out.apply(
        lambda row: (
            f"{str(row.get('watch_reason_detail') or '').strip()}; event {row.get('event_class')} -> {row.get('state_transition_hint')}"
            if pd.notna(row.get("event_class")) and pd.notna(row.get("state_transition_hint")) and str(row.get("watch_reason_detail") or "").strip()
            else (
                f"event {row.get('event_class')} -> {row.get('state_transition_hint')}"
                if pd.notna(row.get("event_class")) and pd.notna(row.get("state_transition_hint"))
                else row.get("watch_reason_detail")
            )
        ),
        axis=1,
    )
    out["watch_enabled"] = out["current_state"] != "REJECT"
    out["watch_reasons_json"] = out["watch_reasons"].astype("string")
    out["watch_status"] = out["current_state"].map(lambda value: "rejected" if value == "REJECT" else "active")
    out["has_review_manual"] = out["has_review_manual"].fillna(False).astype(bool)
    out.loc[out["has_review_manual"], "watch_status"] = "review_manual"
    out["last_event_class"] = out["event_class"]
    out["last_state_transition_hint"] = out["state_transition_hint"]
    out["last_event_score_impact"] = out["score_impact"]
    out["state_updated_at"] = pd.Timestamp.utcnow()
    out["watch_started_at"] = pd.Timestamp.utcnow()
    out["last_checked_at"] = pd.NaT
    out["last_document_published_on"] = pd.NaT
    out["load_ts"] = pd.Timestamp.utcnow()
    for column in ["base_regime", "news_overlay", "theme_ids", "source_screener_slug", "source_screener_list"]:
        if column not in out.columns:
            out[column] = pd.NA

    if not existing.empty:
        out = out.merge(
            existing,
            on=["asof_date", "setup_id", "symbol"],
            how="left",
            suffixes=("", "_existing"),
        )
        out["watch_status"] = out["watch_status_existing"].fillna(out["watch_status"])
        out["current_state"] = out["current_state"].fillna(out["current_state_existing"])
        out["last_checked_at"] = pd.to_datetime(out["last_checked_at_existing"], utc=True, errors="coerce")
        out["last_document_published_on"] = pd.to_datetime(
            out["last_document_published_on_existing"], utc=True, errors="coerce"
        )
        out = out.drop(
            columns=[
                "watch_status_existing",
                "current_state_existing",
                "last_checked_at_existing",
                "last_document_published_on_existing",
            ],
            errors="ignore",
        )

    ordered_cols = [
        "asof_date",
        "setup_id",
        "setup_name",
        "regime_name",
        "base_regime",
        "news_overlay",
        "theme_ids",
        "symbol",
        "company_master_id",
        "screener_slug",
        "source_screener_slug",
        "source_screener_list",
        "rank",
        "candidate_state",
        "current_state",
        "watch_reason_detail",
        "entry_style",
        "attractive_price_low",
        "attractive_price_high",
        "invalidation_price",
        "entry_note",
        "near_miss_flag",
        "last_event_class",
        "last_state_transition_hint",
        "last_event_score_impact",
        "watch_enabled",
        "watch_reasons_json",
        "watch_status",
        "state_updated_at",
        "watch_started_at",
        "last_checked_at",
        "last_document_published_on",
        "load_ts",
    ]
    return out[ordered_cols].drop_duplicates(subset=["asof_date", "setup_id", "symbol"], keep="last")


def persist_watchlist(df: pd.DataFrame, *, rebuild: bool = False, asof_date: pd.Timestamp | None = None) -> None:
    ensure_watchlist_table()
    if df.empty:
        return
    if rebuild and asof_date is not None:
        with db_session() as (_, cur):
            cur.execute(f"DELETE FROM {TABLE_NAME} WHERE asof_date = %s", (asof_date,))
    upsert_to_db(
        df,
        TABLE_NAME,
        unique_keys=["asof_date", "setup_id", "symbol"],
        timescaledb_column="asof_date",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build advisory announcement watchlist from candidates.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Asof date in YYYY-MM-DD")
    parser.add_argument("--setup", dest="setup_ids", nargs="*", help="Optional setup ids")
    parser.add_argument("--symbols", nargs="*", help="Optional symbols")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def summarize(df: pd.DataFrame) -> dict[str, object]:
    if df.empty:
        return {
            "status": "ok",
            "table": TABLE_NAME,
            "row_count": 0,
            "setup_count": 0,
            "symbol_count": 0,
            "sample": [],
        }
    return {
        "status": "ok",
        "table": TABLE_NAME,
        "row_count": int(len(df)),
        "setup_count": int(df["setup_id"].nunique()),
        "symbol_count": int(df["symbol"].nunique()),
        "sample": df.head(10).to_dict(orient="records"),
    }


def main() -> int:
    args = parse_args()
    asof_date = pd.Timestamp(args.date, tz="UTC") if args.date else None
    df = build_watchlist(
        asof_date=asof_date,
        setup_ids=args.setup_ids,
        symbols=args.symbols,
    )
    if not args.dry_run:
        persist_watchlist(df, rebuild=args.rebuild, asof_date=asof_date or (df["asof_date"].max() if not df.empty else None))
    result = summarize(df)
    result["dry_run"] = bool(args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
