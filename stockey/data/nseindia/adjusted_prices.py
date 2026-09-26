import argparse
import json
import re
from dataclasses import dataclass
from typing import Iterable

import pandas as pd

from data.nseindia.security_history import attach_security_identity
from utils.company_master import attach_company_master_id
from utils.db import sql_to_df, upsert_to_db


BONUS_RE = re.compile(r"BONUS\s+(\d+)\s*:\s*(\d+)", re.IGNORECASE)
SPLIT_RE = re.compile(
    r"FROM\s+RS\.?\s*(\d+(?:\.\d+)?)\s*.*?\s+TO\s+RS\.?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
DIVIDEND_RE = re.compile(r"RS\.?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:/-)?\s*PER\s+SH", re.IGNORECASE)


@dataclass
class ParsedAction:
    action_type: str
    old_units: float | None = None
    new_units: float | None = None
    old_face_value: float | None = None
    new_face_value: float | None = None
    cash_amount: float | None = None


def canonicalize_subject(subject: str) -> str:
    return re.sub(r"\s+", " ", (subject or "").strip()).upper()


def parse_action(subject: str, face_value: object = None) -> ParsedAction:
    text = canonicalize_subject(subject)

    bonus_match = BONUS_RE.search(text)
    if bonus_match:
        bonus_new = float(bonus_match.group(1))
        bonus_old = float(bonus_match.group(2))
        return ParsedAction(
            action_type="bonus",
            old_units=bonus_old,
            new_units=bonus_old + bonus_new,
        )

    split_match = SPLIT_RE.search(text)
    if split_match:
        old_face = float(split_match.group(1))
        new_face = float(split_match.group(2))
        if old_face > 0 and new_face > 0:
            share_multiple = old_face / new_face
            return ParsedAction(
                action_type="split",
                old_units=1.0,
                new_units=share_multiple,
                old_face_value=old_face,
                new_face_value=new_face,
            )

    dividend_match = DIVIDEND_RE.search(text)
    if dividend_match:
        return ParsedAction(
            action_type="dividend",
            cash_amount=float(dividend_match.group(1)),
        )

    if "DIVIDEND" in text or "DIV " in text or "DIV-" in text:
        return ParsedAction(action_type="dividend")
    if "ANNUAL GENERAL MEETING" in text or text.startswith("AGM"):
        return ParsedAction(action_type="meeting")

    return ParsedAction(action_type="other")


def price_factor_from_action(action: ParsedAction) -> float | None:
    if action.action_type in {"bonus", "split"} and action.old_units and action.new_units:
        return action.old_units / action.new_units
    return None


def volume_factor_from_action(action: ParsedAction) -> float | None:
    if action.action_type in {"bonus", "split"} and action.old_units and action.new_units:
        return action.new_units / action.old_units
    return None


def load_corporate_actions_sources(symbols: Iterable[str] | None = None) -> pd.DataFrame:
    symbol_list = list(symbols) if symbols else []
    where_clause = ""
    params: tuple = ()
    if symbol_list:
        placeholders = ", ".join(["%s"] * len(symbol_list))
        where_clause = f" WHERE symbol IN ({placeholders})"
        params = tuple(symbol_list)

    frames = []
    api_query = f"""
        SELECT 'api' AS source, date, symbol, series, face_value, subject, record_date, isin
        FROM nseindia_corporate_actions
        {where_clause}
    """
    frames.append(sql_to_df(api_query, params=params))

    bc_query = f"""
        SELECT 'bc' AS source, date, symbol, series, NULL::text AS face_value, subject, record_date, NULL::text AS isin
        FROM nseindia_corporate_actions_bc_raw
        {where_clause}
    """
    try:
        frames.append(sql_to_df(bc_query, params=params))
    except Exception as exc:
        message = str(exc).lower()
        if exc.__class__.__name__ in {"UndefinedTable", "UndefinedColumn"} or "does not exist" in message:
            print(f"[adjusted_prices] corporate actions bhavcopy table unavailable: {exc}", flush=True)
        else:
            raise

    non_empty = [frame for frame in frames if not frame.empty]
    return pd.concat(non_empty, ignore_index=True) if non_empty else pd.DataFrame()


def normalize_corporate_actions(symbols: Iterable[str] | None = None) -> pd.DataFrame:
    raw = load_corporate_actions_sources(symbols)
    if raw.empty:
        return raw

    raw["date"] = pd.to_datetime(raw["date"], utc=True)
    raw["record_date"] = pd.to_datetime(raw["record_date"], utc=True, errors="coerce")
    raw["subject"] = raw["subject"].astype("string").str.strip()
    raw["series"] = raw["series"].astype("string").str.strip()
    raw["symbol"] = raw["symbol"].astype("string").str.strip()
    raw["isin"] = raw["isin"].astype("string").str.strip()
    raw["normalized_subject"] = raw["subject"].map(canonicalize_subject)

    parsed = raw.apply(
        lambda row: parse_action(row["subject"], row.get("face_value")),
        axis=1,
    )
    raw["action_type"] = parsed.map(lambda x: x.action_type)
    raw["old_units"] = parsed.map(lambda x: x.old_units)
    raw["new_units"] = parsed.map(lambda x: x.new_units)
    raw["old_face_value"] = parsed.map(lambda x: x.old_face_value)
    raw["new_face_value"] = parsed.map(lambda x: x.new_face_value)
    raw["cash_amount_per_share"] = parsed.map(lambda x: x.cash_amount)
    raw["price_adjustment_factor"] = parsed.map(price_factor_from_action)
    raw["volume_adjustment_factor"] = parsed.map(volume_factor_from_action)

    dedupe_keys = ["date", "symbol", "series", "normalized_subject"]
    raw = raw.sort_values(["date", "symbol", "series", "source"]).drop_duplicates(
        subset=dedupe_keys,
        keep="last",
    )
    return attach_security_identity(raw)


def sync_normalized_actions(symbols: Iterable[str] | None = None) -> pd.DataFrame:
    normalized = normalize_corporate_actions(symbols)
    if normalized.empty:
        return normalized

    normalized = attach_company_master_id(normalized, ticker_column="symbol", exchange="NSE")
    upsert_to_db(
        normalized,
        "nseindia_corporate_actions_normalized",
        unique_keys=["date", "symbol", "series", "normalized_subject"],
        timescaledb_column="date",
    )
    return normalized


STOCKEY_RUN_STATE: dict[str, object] = {}


def main():
    """Normalize NSE corporate actions into nseindia_corporate_actions_normalized.

    2026-08-14: the adjusted-price build (`build_adjusted_prices`/`sync_adjusted_prices`,
    the `--only adjust`/`all` modes, `nseindia_ohlcv_adjusted`) was removed -- superseded by
    `data/nseindia/price_adjustment.py`'s factor table + `advisory_adjusted_ohlcv_daily` view,
    which covers both split/bonus and total-return adjustment from one source. This module's
    only remaining job is corporate-action normalization, which `has_recent_adjustment()`
    (`data/dhanlive/ohlcv.py`) depends on to detect recent splits/bonuses.
    """
    global STOCKEY_RUN_STATE
    parser = argparse.ArgumentParser(description="Normalize NSE corporate actions")
    parser.add_argument("--symbols", nargs="*", help="Optional symbols, comma-separated or repeated")
    args = parser.parse_args()

    symbols = None
    if args.symbols:
        symbols = []
        for item in args.symbols:
            symbols.extend(part.strip().upper() for part in item.split(",") if part.strip())

    normalized = sync_normalized_actions(symbols)
    state: dict[str, object] = {
        "status": "ok",
        "normalized_rows": int(len(normalized)),
        "normalized_symbols": int(normalized["symbol"].nunique()) if not normalized.empty else 0,
    }
    STOCKEY_RUN_STATE = state
    print(json.dumps(state, default=str), flush=True)


if __name__ == "__main__":
    main()
