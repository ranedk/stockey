import argparse
import re
from dataclasses import dataclass
from typing import Iterable

import pandas as pd

from data.nseindia.security_history import attach_security_identity
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
    except Exception:
        pass

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


def build_adjusted_prices(symbols: Iterable[str] | None = None) -> pd.DataFrame:
    symbol_clause = ""
    params: tuple = ()
    if symbols:
        symbols = list(symbols)
        placeholders = ", ".join(["%s"] * len(symbols))
        symbol_clause = f" WHERE symbol IN ({placeholders})"
        params = tuple(symbols)

    prices = sql_to_df(
        f"""
        SELECT date, isin, symbol, series, open, high, low, close, last, previous_close, volume, total_value, number_of_trades
        FROM nseindia_ohlcv
        {symbol_clause}
        ORDER BY isin, series, symbol, date
        """,
        params=params,
    )
    if prices.empty:
        return prices

    prices["date"] = pd.to_datetime(prices["date"], utc=True)
    actions = normalize_corporate_actions(symbols)

    if actions.empty:
        adjusted = prices.copy()
        adjusted["cum_price_adjustment_factor"] = 1.0
        adjusted["cum_volume_adjustment_factor"] = 1.0
        adjusted["cum_total_return_factor"] = 1.0
        adjusted["adjustment_source"] = "none"
        adjusted = attach_security_identity(adjusted)
        return adjusted

    adjusted_frames = []
    group_cols = ["isin", "series", "symbol"]
    prices["join_isin"] = prices["isin"].fillna("")
    for keys, price_group in prices.groupby(["join_isin", "series", "symbol"], dropna=False):
        _, series, symbol = keys
        action_group = actions[
            (actions["series"] == series)
            & (actions["symbol"] == symbol)
        ].copy()

        price_group = price_group.sort_values("date").copy()
        if not action_group.empty:
            action_group = action_group.merge(
                price_group[["date", "previous_close"]].rename(columns={"previous_close": "action_previous_close"}),
                on="date",
                how="left",
            )
            action_group["effective_total_return_factor"] = action_group["price_adjustment_factor"].fillna(1.0)
            dividend_mask = (
                action_group["action_type"].eq("dividend")
                & action_group["cash_amount_per_share"].notna()
                & action_group["action_previous_close"].notna()
                & (action_group["action_previous_close"] > 0)
            )
            action_group.loc[dividend_mask, "effective_total_return_factor"] = (
                (action_group.loc[dividend_mask, "action_previous_close"] - action_group.loc[dividend_mask, "cash_amount_per_share"])
                / action_group.loc[dividend_mask, "action_previous_close"]
            )
            action_group["effective_total_return_factor"] = action_group["effective_total_return_factor"].clip(lower=0.0)
            daily_factors = action_group.groupby("date", dropna=False).agg(
                daily_price_factor=("price_adjustment_factor", lambda s: s.dropna().prod() if s.notna().any() else 1.0),
                daily_volume_factor=("volume_adjustment_factor", lambda s: s.dropna().prod() if s.notna().any() else 1.0),
                daily_total_return_factor=("effective_total_return_factor", lambda s: s.dropna().prod() if s.notna().any() else 1.0),
            ).reset_index()
        else:
            daily_factors = pd.DataFrame(
                columns=["date", "daily_price_factor", "daily_volume_factor", "daily_total_return_factor"]
            )

        price_group = price_group.merge(daily_factors, on="date", how="left")
        for col in ["daily_price_factor", "daily_volume_factor", "daily_total_return_factor"]:
            price_group[col] = pd.to_numeric(price_group[col], errors="coerce").fillna(1.0)

        reverse_price = price_group["daily_price_factor"].iloc[::-1].cumprod().iloc[::-1]
        reverse_volume = price_group["daily_volume_factor"].iloc[::-1].cumprod().iloc[::-1]
        reverse_total = price_group["daily_total_return_factor"].iloc[::-1].cumprod().iloc[::-1]
        price_group["cum_price_adjustment_factor"] = reverse_price.shift(-1, fill_value=1.0)
        price_group["cum_volume_adjustment_factor"] = reverse_volume.shift(-1, fill_value=1.0)
        price_group["cum_total_return_factor"] = reverse_total.shift(-1, fill_value=1.0)

        for col in ["open", "high", "low", "close", "last", "previous_close"]:
            price_group[f"adj_{col}"] = price_group[col] * price_group["cum_price_adjustment_factor"]
            price_group[f"tr_adj_{col}"] = price_group[col] * price_group["cum_total_return_factor"]
        price_group["adj_volume"] = price_group["volume"] * price_group["cum_volume_adjustment_factor"]
        price_group["adjustment_source"] = "normalized_corporate_actions"
        price_group = price_group.drop(
            columns=["daily_price_factor", "daily_volume_factor", "daily_total_return_factor"],
            errors="ignore",
        )
        adjusted_frames.append(price_group)

    adjusted = pd.concat(adjusted_frames, ignore_index=True)
    adjusted = attach_security_identity(adjusted)
    adjusted = adjusted.drop(columns=["join_isin"])
    return adjusted[[
        "security_id",
        "identity_mapping_source",
        "identity_confidence",
    ] + group_cols + [
        "date",
        "open",
        "high",
        "low",
        "close",
        "last",
        "previous_close",
        "volume",
        "total_value",
        "number_of_trades",
        "cum_price_adjustment_factor",
        "cum_volume_adjustment_factor",
        "cum_total_return_factor",
        "adj_open",
        "adj_high",
        "adj_low",
        "adj_close",
        "adj_last",
        "adj_previous_close",
        "tr_adj_open",
        "tr_adj_high",
        "tr_adj_low",
        "tr_adj_close",
        "tr_adj_last",
        "tr_adj_previous_close",
        "adj_volume",
        "adjustment_source",
    ]]


def sync_normalized_actions(symbols: Iterable[str] | None = None) -> pd.DataFrame:
    normalized = normalize_corporate_actions(symbols)
    if normalized.empty:
        return normalized

    upsert_to_db(
        normalized,
        "nseindia_corporate_actions_normalized",
        unique_keys=["date", "symbol", "series", "normalized_subject"],
        timescaledb_column="date",
    )
    return normalized


def sync_adjusted_prices(symbols: Iterable[str] | None = None) -> pd.DataFrame:
    adjusted = build_adjusted_prices(symbols)
    if adjusted.empty:
        return adjusted

    upsert_to_db(
        adjusted,
        "nseindia_ohlcv_adjusted",
        unique_keys=["date", "symbol", "series"],
        timescaledb_column="date",
    )
    return adjusted


def main():
    parser = argparse.ArgumentParser(description="Normalize NSE corporate actions and build adjusted prices")
    parser.add_argument("--symbols", nargs="*", help="Optional symbols, comma-separated or repeated")
    parser.add_argument(
        "--only",
        choices=["normalize", "adjust", "all"],
        default="all",
        help="Run only one stage or both",
    )
    args = parser.parse_args()

    symbols = None
    if args.symbols:
        symbols = []
        for item in args.symbols:
            symbols.extend(part.strip().upper() for part in item.split(",") if part.strip())

    if args.only in {"normalize", "all"}:
        sync_normalized_actions(symbols)
    if args.only in {"adjust", "all"}:
        sync_adjusted_prices(symbols)


if __name__ == "__main__":
    main()
