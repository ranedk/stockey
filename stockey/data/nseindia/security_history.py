import hashlib

import pandas as pd

from utils.fallback_telemetry import record_local_fallback_event
from utils.company_master import map_company_master_ids
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration


MERGER_KEYWORDS = (
    "MERGER",
    "AMALGAMATION",
    "SCHEME OF ARRANGEMENT",
    "DEMERGER",
    "REDUCTION OF CAPITAL",
    "SPIN OFF",
    "SPINOFF",
)
SECURITY_HISTORY_SCHEMA_MIGRATION_ID = "20260611_nse_security_history_base"
SYNC_SOURCE_NAME = "data.nseindia.security_history"
SECURITY_HISTORY_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS dim_security_history (
        symbol TEXT,
        series TEXT,
        isin TEXT,
        effective_from TIMESTAMPTZ,
        effective_to TIMESTAMPTZ,
        price_row_count BIGINT,
        raw_security_key TEXT,
        security_id TEXT NOT NULL,
        predecessor_security_id TEXT,
        successor_security_id TEXT,
        relation_type TEXT,
        mapping_source TEXT,
        confidence DOUBLE PRECISION,
        company_master_id TEXT,
        UNIQUE (security_id, symbol, series, isin, effective_from)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dim_security_overrides (
        override_key TEXT PRIMARY KEY,
        symbol TEXT,
        series TEXT,
        isin TEXT,
        effective_from TIMESTAMPTZ,
        effective_to TIMESTAMPTZ,
        security_id TEXT NOT NULL,
        predecessor_security_id TEXT,
        successor_security_id TEXT,
        relation_type TEXT,
        confidence DOUBLE PRECISION,
        notes TEXT,
        is_active BOOLEAN NOT NULL DEFAULT TRUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dim_security_review_events (
        event_key TEXT PRIMARY KEY,
        event_type TEXT NOT NULL,
        symbol TEXT,
        series TEXT,
        isin TEXT,
        related_symbol TEXT,
        related_series TEXT,
        related_isin TEXT,
        first_seen TIMESTAMPTZ,
        last_seen TIMESTAMPTZ,
        security_id TEXT,
        related_security_id TEXT,
        confidence DOUBLE PRECISION,
        reason TEXT,
        needs_review BOOLEAN NOT NULL DEFAULT TRUE
    )
    """,
]


def values_equal(left: object, right: object) -> bool:
    if pd.isna(left) and pd.isna(right):
        return True
    if pd.isna(left) or pd.isna(right):
        return False
    return bool(left == right)


def ensure_identity_tables() -> None:
    apply_schema_migration(
        migration_id=SECURITY_HISTORY_SCHEMA_MIGRATION_ID,
        statements=SECURITY_HISTORY_SCHEMA_STATEMENTS,
        owner="data.nseindia.security_history",
        description="Create NSE security identity history, override, and review tables.",
        metadata={
            "tables": ["dim_security_history", "dim_security_overrides", "dim_security_review_events"],
            "workflow": "nse_security_identity_history",
        },
    )


def load_price_identity_observations() -> pd.DataFrame:
    df = sql_to_df(
        """
        SELECT
            symbol,
            series,
            isin,
            MIN(date) AS effective_from,
            MAX(date) AS effective_to,
            COUNT(*) AS price_row_count
        FROM nseindia_ohlcv
        GROUP BY symbol, series, isin
        """
    )
    if df.empty:
        return df

    df["effective_from"] = pd.to_datetime(df["effective_from"], utc=True)
    df["effective_to"] = pd.to_datetime(df["effective_to"], utc=True)
    df["symbol"] = df["symbol"].astype("string").str.strip()
    df["series"] = df["series"].astype("string").str.strip()
    df["isin"] = df["isin"].astype("string").str.strip()
    df["raw_security_key"] = df["symbol"].fillna("") + ":" + df["series"].fillna("")
    return df


def load_security_overrides() -> pd.DataFrame:
    ensure_identity_tables()
    overrides = sql_to_df(
        """
        SELECT
            override_key,
            symbol,
            series,
            isin,
            effective_from,
            effective_to,
            security_id,
            predecessor_security_id,
            successor_security_id,
            relation_type,
            confidence,
            notes,
            is_active
        FROM dim_security_overrides
        WHERE is_active = TRUE
        """
    )
    if overrides.empty:
        return overrides

    overrides["effective_from"] = pd.to_datetime(overrides["effective_from"], utc=True, errors="coerce")
    overrides["effective_to"] = pd.to_datetime(overrides["effective_to"], utc=True, errors="coerce")
    return overrides


def default_security_id(symbol: str | None, series: str | None, isin: str | None) -> str:
    if pd.notna(isin) and str(isin).strip():
        return f"isin:{str(isin).strip()}"
    symbol_text = "" if pd.isna(symbol) else str(symbol).strip()
    series_text = "" if pd.isna(series) else str(series).strip()
    return f"raw:{symbol_text}:{series_text}"


def match_override(observation: pd.Series, overrides: pd.DataFrame) -> pd.Series | None:
    if overrides.empty:
        return None

    matches = overrides.copy()
    for column in ["symbol", "series", "isin"]:
        value = observation.get(column)
        if pd.notna(value) and str(value).strip():
            matches = matches[(matches[column].isna()) | (matches[column] == value)]
    if matches.empty:
        return None

    start = observation["effective_from"]
    end = observation["effective_to"]
    matches = matches[
        (matches["effective_from"].isna() | (matches["effective_from"] <= end))
        & (matches["effective_to"].isna() | (matches["effective_to"] >= start))
    ]
    if matches.empty:
        return None

    matches = matches.sort_values(["confidence", "effective_from"], ascending=[False, False], na_position="last")
    return matches.iloc[0]


def assign_security_identity(observations: pd.DataFrame, overrides: pd.DataFrame) -> pd.DataFrame:
    if observations.empty:
        return observations

    df = observations.copy()
    df["security_id"] = df.apply(
        lambda row: default_security_id(row["symbol"], row["series"], row["isin"]),
        axis=1,
    )
    df["predecessor_security_id"] = pd.NA
    df["successor_security_id"] = pd.NA
    df["relation_type"] = df["isin"].notna().map(lambda x: "observed_isin" if x else "raw_symbol_series")
    df["mapping_source"] = "heuristic"
    df["confidence"] = df["isin"].notna().map(lambda x: 0.98 if x else 0.40)

    if not overrides.empty:
        for idx, row in df.iterrows():
            override = match_override(row, overrides)
            if override is None:
                continue
            df.at[idx, "security_id"] = override["security_id"]
            df.at[idx, "predecessor_security_id"] = override["predecessor_security_id"]
            df.at[idx, "successor_security_id"] = override["successor_security_id"]
            df.at[idx, "relation_type"] = override["relation_type"] or "manual_override"
            df.at[idx, "mapping_source"] = "manual_override"
            df.at[idx, "confidence"] = override["confidence"] if pd.notna(override["confidence"]) else 1.0

    return df


def hash_key(*parts: object) -> str:
    text = "||".join("" if part is None else str(part) for part in parts)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def build_review_events(history: pd.DataFrame) -> pd.DataFrame:
    events: list[dict[str, object]] = []
    if history.empty:
        return pd.DataFrame()

    for isin, group in history[history["isin"].notna() & history["isin"].ne("")].groupby("isin", dropna=False):
        combos = group.sort_values(["effective_from", "effective_to", "symbol", "series"])
        unique_pairs = combos[["symbol", "series"]].drop_duplicates()
        if len(unique_pairs) < 2:
            continue
        prior = None
        for row in combos.itertuples():
            if prior is None:
                prior = row
                continue
            if values_equal(prior.symbol, row.symbol) and values_equal(prior.series, row.series):
                continue
            events.append(
                {
                    "event_key": hash_key("rename_candidate", isin, prior.symbol, prior.series, row.symbol, row.series),
                    "event_type": "rename_candidate",
                    "symbol": prior.symbol,
                    "series": prior.series,
                    "isin": isin,
                    "related_symbol": row.symbol,
                    "related_series": row.series,
                    "related_isin": isin,
                    "first_seen": min(prior.effective_from, row.effective_from),
                    "last_seen": max(prior.effective_to, row.effective_to),
                    "security_id": prior.security_id,
                    "related_security_id": row.security_id,
                    "confidence": 0.95,
                    "reason": "Same ISIN observed across multiple symbol/series identifiers",
                    "needs_review": True,
                }
            )
            prior = row

    for keys, group in history.groupby(["symbol", "series"], dropna=False):
        symbol, series = keys
        combos = group[group["isin"].notna() & group["isin"].ne("")].sort_values(["effective_from", "effective_to", "isin"])
        unique_isins = combos["isin"].drop_duplicates()
        if len(unique_isins) < 2:
            continue
        prior = None
        for row in combos.itertuples():
            if prior is None:
                prior = row
                continue
            if values_equal(prior.isin, row.isin):
                continue
            events.append(
                {
                    "event_key": hash_key("identity_break_candidate", symbol, series, prior.isin, row.isin),
                    "event_type": "identity_break_candidate",
                    "symbol": symbol,
                    "series": series,
                    "isin": prior.isin,
                    "related_symbol": symbol,
                    "related_series": series,
                    "related_isin": row.isin,
                    "first_seen": min(prior.effective_from, row.effective_from),
                    "last_seen": max(prior.effective_to, row.effective_to),
                    "security_id": prior.security_id,
                    "related_security_id": row.security_id,
                    "confidence": 0.90,
                    "reason": "Same symbol/series observed with multiple ISIN values",
                    "needs_review": True,
                }
            )
            prior = row

    actions = pd.DataFrame()
    try:
        actions = sql_to_df(
            """
            SELECT date, symbol, series, isin, normalized_subject
            FROM nseindia_corporate_actions_normalized
            """
        )
    except Exception as exc:
        record_local_fallback_event(
            module=SYNC_SOURCE_NAME,
            source="nseindia_corporate_actions_normalized",
            fallback_type="nse_security_history_corporate_action_context_failed",
            severity="warn",
            reason="Could not load corporate-action context while building security identity review events; rename/identity-break candidates still use price identity history.",
            error=exc,
            metadata={"table": "nseindia_corporate_actions_normalized"},
        )
        actions = pd.DataFrame()
    if not actions.empty:
        actions["date"] = pd.to_datetime(actions["date"], utc=True)
        text = actions["normalized_subject"].fillna("").astype(str)
        mask = text.str.contains("|".join(MERGER_KEYWORDS), regex=True)
        for row in actions[mask].itertuples():
            events.append(
                {
                    "event_key": hash_key("corporate_action_identity_event", row.date, row.symbol, row.series, row.isin, row.normalized_subject),
                    "event_type": "corporate_action_identity_event",
                    "symbol": row.symbol,
                    "series": row.series,
                    "isin": row.isin,
                    "related_symbol": None,
                    "related_series": None,
                    "related_isin": None,
                    "first_seen": row.date,
                    "last_seen": row.date,
                    "security_id": None,
                    "related_security_id": None,
                    "confidence": 0.75,
                    "reason": row.normalized_subject,
                    "needs_review": True,
                }
            )

    return pd.DataFrame(events).drop_duplicates(subset=["event_key"]) if events else pd.DataFrame()


def build_security_history() -> tuple[pd.DataFrame, pd.DataFrame]:
    observations = load_price_identity_observations()
    if observations.empty:
        return observations, pd.DataFrame()

    overrides = load_security_overrides()
    history = assign_security_identity(observations, overrides)
    history["company_master_id"] = map_company_master_ids(history["symbol"], exchange="NSE")
    history = history.sort_values(["security_id", "effective_from", "symbol", "series", "isin"])
    review = build_review_events(history)
    return history, review


def sync_security_history() -> tuple[pd.DataFrame, pd.DataFrame]:
    ensure_identity_tables()
    history, review = build_security_history()
    if not history.empty:
        upsert_to_db(history, "dim_security_history", unique_keys=["security_id", "symbol", "series", "isin", "effective_from"])
    if not review.empty:
        upsert_to_db(review, "dim_security_review_events", unique_keys=["event_key"])
    return history, review


def load_security_history() -> pd.DataFrame:
    try:
        history = sql_to_df(
            """
            SELECT
                security_id,
                symbol,
                series,
                isin,
                effective_from,
                effective_to,
                mapping_source,
                confidence
            FROM dim_security_history
            """
        )
    except Exception as exc:
        record_local_fallback_event(
            module=SYNC_SOURCE_NAME,
            source="dim_security_history",
            fallback_type="nse_security_history_load_failed",
            severity="warn",
            reason="Could not load persisted security identity history; downstream attachment will fall back to raw symbol/series/ISIN identity.",
            error=exc,
            metadata={"table": "dim_security_history"},
        )
        return pd.DataFrame()

    if history.empty:
        return history

    history["effective_from"] = pd.to_datetime(history["effective_from"], utc=True)
    history["effective_to"] = pd.to_datetime(history["effective_to"], utc=True)
    return history


def attach_security_identity(
    df: pd.DataFrame,
    *,
    date_col: str = "date",
    symbol_col: str = "symbol",
    series_col: str = "series",
    isin_col: str = "isin",
) -> pd.DataFrame:
    if df.empty:
        return df

    history = load_security_history()
    if history.empty:
        fallback = df.copy()
        fallback["security_id"] = fallback.apply(
            lambda row: default_security_id(row.get(symbol_col), row.get(series_col), row.get(isin_col)),
            axis=1,
        )
        fallback["identity_mapping_source"] = "fallback"
        fallback["identity_confidence"] = fallback[isin_col].notna().map(lambda x: 0.98 if x else 0.40)
        return fallback

    out = df.copy()
    out[date_col] = pd.to_datetime(out[date_col], utc=True)
    out["_series_norm"] = out[series_col].astype("string").fillna("")
    out["_isin_norm"] = out[isin_col].astype("string").fillna("") if isin_col in out.columns else ""
    out["security_id"] = out.apply(
        lambda row: default_security_id(row.get(symbol_col), row.get(series_col), row.get(isin_col)),
        axis=1,
    )
    out["identity_mapping_source"] = "fallback"
    out["identity_confidence"] = out["_isin_norm"].ne("").map(lambda x: 0.98 if x else 0.40)

    history = history.copy()
    history["_series_norm"] = history["series"].astype("string").fillna("")
    history["_isin_norm"] = history["isin"].astype("string").fillna("")

    for keys, idx in out.groupby([symbol_col, "_series_norm"], dropna=False).groups.items():
        symbol, series_norm = keys
        hist_group = history[(history["symbol"] == symbol) & (history["_series_norm"] == series_norm)].copy()
        if hist_group.empty:
            continue

        group = out.loc[idx].copy()
        assigned = pd.Series(False, index=group.index)
        ordered_history = hist_group.sort_values(["confidence", "effective_from"], ascending=[False, False])

        for _, match in ordered_history.iterrows():
            in_window = (group[date_col] >= match["effective_from"]) & (group[date_col] <= match["effective_to"])
            same_isin = group["_isin_norm"] == match["_isin_norm"]
            exact_mask = (~assigned) & in_window & same_isin & group["_isin_norm"].ne("")
            if exact_mask.any():
                matched_index = group.index[exact_mask]
                out.loc[matched_index, "security_id"] = match["security_id"]
                out.loc[matched_index, "identity_mapping_source"] = match["mapping_source"]
                out.loc[matched_index, "identity_confidence"] = match["confidence"]
                assigned.loc[matched_index] = True

        for _, match in ordered_history.iterrows():
            in_window = (group[date_col] >= match["effective_from"]) & (group[date_col] <= match["effective_to"])
            fallback_mask = (~assigned) & in_window
            if fallback_mask.any():
                matched_index = group.index[fallback_mask]
                out.loc[matched_index, "security_id"] = match["security_id"]
                out.loc[matched_index, "identity_mapping_source"] = match["mapping_source"]
                out.loc[matched_index, "identity_confidence"] = match["confidence"]
                assigned.loc[matched_index] = True

    if isin_col in out.columns:
        remaining = out["identity_mapping_source"].eq("fallback") & out["_isin_norm"].ne("")
        if remaining.any():
            for isin, idx in out.loc[remaining].groupby("_isin_norm").groups.items():
                hist_group = history[history["_isin_norm"] == isin].sort_values(
                    ["confidence", "effective_from"],
                    ascending=[False, False],
                )
                if hist_group.empty:
                    continue
                group = out.loc[idx]
                for _, match in hist_group.iterrows():
                    in_window = (group[date_col] >= match["effective_from"]) & (group[date_col] <= match["effective_to"])
                    if in_window.any():
                        matched_index = group.index[in_window]
                        out.loc[matched_index, "security_id"] = match["security_id"]
                        out.loc[matched_index, "identity_mapping_source"] = match["mapping_source"]
                        out.loc[matched_index, "identity_confidence"] = match["confidence"]

    return out.drop(columns=["_series_norm", "_isin_norm"], errors="ignore")


def run() -> None:
    sync_security_history()


if __name__ == "__main__":
    run()
