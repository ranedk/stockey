from __future__ import annotations

import argparse
import json
from typing import Any

import numpy as np
import pandas as pd

from advisory.data_sync import ensure_advisory_symbol_inputs
from advisory.fundamental_snapshot import build_fundamental_snapshot, persist_fundamental_snapshot
from advisory.peer_sync import sync_peer_data
from advisory.technical_features import build_technical_features, persist_technical_features
from advisory.setup_registry import load_setup_registry
from utils.db import db_session, sql_to_df, upsert_to_db
from utils.sync import parse_datetime_arg


CANDIDATES_TABLE = "advisory_candidates"
REJECTIONS_TABLE = "advisory_candidate_rejections"


def normalize_timestamp(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce").dt.normalize()


def get_effective_dates(asof_date: pd.Timestamp | None = None) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    screener_cutoff = asof_date
    if screener_cutoff is None:
        screener_df = sql_to_df("SELECT MAX(date) AS screener_date FROM advisory_screener_constituents")
        if screener_df.empty or pd.isna(pd.to_datetime(screener_df.iloc[0]["screener_date"], utc=True, errors="coerce")):
            return None, None
        screener_cutoff = pd.to_datetime(
            screener_df.iloc[0]["screener_date"],
            utc=True,
            errors="coerce",
        ).normalize()

    df = sql_to_df(
        """
        SELECT
            (SELECT MAX(asof_date) FROM advisory_market_regime WHERE asof_date <= %(asof_date)s) AS regime_date,
            (SELECT MAX(asof_date) FROM advisory_technical_daily WHERE asof_date <= %(asof_date)s) AS technical_date,
            (SELECT MAX(asof_date) FROM advisory_fundamentals_daily WHERE asof_date <= %(asof_date)s) AS fundamentals_date,
            (SELECT MAX(date) FROM advisory_screener_constituents WHERE date <= %(asof_date)s) AS screener_date
        """,
        params={"asof_date": screener_cutoff},
    )
    if df.empty:
        return None, None
    row = df.iloc[0]
    screener_date = pd.to_datetime(row.get("screener_date"), utc=True, errors="coerce")
    regime_date = pd.to_datetime(row.get("regime_date"), utc=True, errors="coerce")
    technical_date = pd.to_datetime(row.get("technical_date"), utc=True, errors="coerce")
    fundamentals_date = pd.to_datetime(row.get("fundamentals_date"), utc=True, errors="coerce")
    if any(pd.isna(value) for value in [screener_date, regime_date, technical_date, fundamentals_date]):
        return None, None
    evaluation_date = min(regime_date.normalize(), technical_date.normalize(), fundamentals_date.normalize())
    return screener_date.normalize(), evaluation_date


def load_regime(asof_date: pd.Timestamp) -> dict[str, Any] | None:
    df = sql_to_df(
        """
        SELECT *
        FROM advisory_market_regime
        WHERE asof_date = %s
        LIMIT 1
        """,
        params=(asof_date,),
    )
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def load_screener_universe(asof_date: pd.Timestamp, screener_slug: str | None) -> pd.DataFrame:
    clauses = ["date = %s"]
    params: list[object] = [asof_date]
    if screener_slug:
        clauses.append("screener_slug = %s")
        params.append(screener_slug)
    df = sql_to_df(
        f"""
        SELECT
            date AS asof_date,
            screener_slug,
            screener_name,
            ticker AS symbol,
            exchange,
            company_master_id,
            rank,
            last_price,
            volume,
            market_cap,
            pe_ratio
        FROM advisory_screener_constituents
        WHERE {' AND '.join(clauses)}
        ORDER BY rank, symbol
        """,
        params=tuple(params),
    )
    if df.empty:
        return df
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    return df


def load_technical(asof_date: pd.Timestamp) -> pd.DataFrame:
    df = sql_to_df(
        """
        SELECT *
        FROM advisory_technical_daily
        WHERE asof_date = %s
        """,
        params=(asof_date,),
    )
    if df.empty:
        return df
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    return df


def load_fundamentals(asof_date: pd.Timestamp) -> pd.DataFrame:
    df = sql_to_df(
        """
        SELECT *
        FROM advisory_fundamentals_daily
        WHERE asof_date = %s
        """,
        params=(asof_date,),
    )
    if df.empty:
        return df
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    return df


def refresh_missing_snapshots(symbols: list[str], effective_date: pd.Timestamp) -> dict[str, object]:
    sync_result = ensure_advisory_symbol_inputs(symbols, to_date=effective_date)
    peer_sync_result = sync_peer_data(symbols=symbols, to_date=effective_date)

    technical_df = build_technical_features(
        symbols=symbols,
        from_date=effective_date,
        to_date=effective_date,
        rebuild=False,
    )
    persist_technical_features(technical_df, rebuild=False, symbols=symbols)

    fundamentals_df = build_fundamental_snapshot(
        symbols=symbols,
        from_date=effective_date,
        to_date=effective_date,
        rebuild=False,
    )
    persist_fundamental_snapshot(fundamentals_df)

    return {
        "data_sync": sync_result,
        "peer_sync": peer_sync_result,
        "technical_rows": int(len(technical_df)),
        "fundamental_rows": int(len(fundamentals_df)),
    }


def compare(value: Any, operator: str, threshold: Any) -> bool:
    if pd.isna(value):
        return False
    if operator == "eq":
        return value == threshold
    if operator == "gte":
        return value >= threshold
    if operator == "gt":
        return value > threshold
    if operator == "lte":
        return value <= threshold
    if operator == "lt":
        return value < threshold
    raise ValueError(f"Unsupported operator: {operator}")


def evaluate_setup_row(
    row: pd.Series,
    *,
    regime_name: str,
    setup: dict[str, Any],
) -> tuple[bool, list[dict[str, Any]]]:
    rejections: list[dict[str, Any]] = []

    if regime_name not in setup["allowed_regimes"]:
        rejections.append(
            {
                "reason_code": "regime_not_allowed",
                "reason_detail": f"regime={regime_name}",
            }
        )

    if pd.isna(row.get("company_master_id")):
        rejections.append(
            {
                "reason_code": "missing_company_master_id",
                "reason_detail": "company_master_id is null",
            }
        )

    if pd.isna(row.get("adj_close")):
        rejections.append(
            {
                "reason_code": "missing_technical_snapshot",
                "reason_detail": "technical snapshot missing for date",
            }
        )

    if pd.isna(row.get("fundamentals_freshness_status")):
        rejections.append(
            {
                "reason_code": "missing_fundamental_snapshot",
                "reason_detail": "fundamental snapshot missing for date",
            }
        )

    market_cap = row.get("market_cap")
    if setup.get("market_cap_min") is not None and (pd.isna(market_cap) or market_cap < setup["market_cap_min"]):
        rejections.append(
            {
                "reason_code": "market_cap_below_min",
                "reason_detail": f"market_cap={market_cap}",
            }
        )
    if setup.get("market_cap_max") is not None and (pd.isna(market_cap) or market_cap > setup["market_cap_max"]):
        rejections.append(
            {
                "reason_code": "market_cap_above_max",
                "reason_detail": f"market_cap={market_cap}",
            }
        )

    traded_value = row.get("avg_traded_value_20d")
    if setup.get("min_avg_traded_value_20d") is not None and (
        pd.isna(traded_value) or traded_value < setup["min_avg_traded_value_20d"]
    ):
        rejections.append(
            {
                "reason_code": "liquidity_below_min",
                "reason_detail": f"avg_traded_value_20d={traded_value}",
            }
        )

    dist_52w_high = row.get("dist_52w_high")
    if setup.get("min_dist_52w_high") is not None and (
        pd.isna(dist_52w_high) or dist_52w_high < setup["min_dist_52w_high"]
    ):
        rejections.append(
            {
                "reason_code": "too_far_from_high",
                "reason_detail": f"dist_52w_high={dist_52w_high}",
            }
        )

    extension = row.get("breakout_extension_pct")
    if setup.get("max_breakout_extension_pct") is not None and (
        pd.isna(extension) or extension > setup["max_breakout_extension_pct"]
    ):
        rejections.append(
            {
                "reason_code": "overextended_breakout",
                "reason_detail": f"breakout_extension_pct={extension}",
            }
        )

    for rule in setup.get("technical_rules", []):
        column = rule["column"]
        if not compare(row.get(column), rule["operator"], rule["value"]):
            rejections.append(
                {
                    "reason_code": f"technical_{column}",
                    "reason_detail": f"{column}={row.get(column)} expected {rule['operator']} {rule['value']}",
                }
            )

    for rule in setup.get("fundamental_rules", []):
        column = rule["column"]
        if not compare(row.get(column), rule["operator"], rule["value"]):
            rejections.append(
                {
                    "reason_code": f"fundamental_{column}",
                    "reason_detail": f"{column}={row.get(column)} expected {rule['operator']} {rule['value']}",
                }
            )

    return (len(rejections) == 0), rejections


def run_rule_engine(
    *,
    asof_date: pd.Timestamp | None = None,
    setup_ids: list[str] | None = None,
    config_path: str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    screener_date, effective_date = get_effective_dates(asof_date)
    if effective_date is None or screener_date is None:
        return pd.DataFrame(), pd.DataFrame(), {"effective_date": None, "screener_date": None, "regime_name": None}

    regime = load_regime(effective_date)
    if regime is None:
        return pd.DataFrame(), pd.DataFrame(), {"effective_date": str(effective_date), "screener_date": str(screener_date), "regime_name": None}

    setups = load_setup_registry(config_path)
    if setup_ids:
        selected = {value.upper() for value in setup_ids}
        setups = [setup for setup in setups if setup["setup_id"].upper() in selected]

    technical = load_technical(effective_date)
    fundamentals = load_fundamentals(effective_date)

    candidate_rows: list[dict[str, Any]] = []
    rejection_rows: list[dict[str, Any]] = []
    meta = {
        "effective_date": str(effective_date),
        "screener_date": str(screener_date),
        "regime_name": regime.get("regime_name"),
    }

    screener_frames = [
        load_screener_universe(screener_date, setup.get("screener_slug"))
        for setup in setups
    ]
    screener_frames = [frame for frame in screener_frames if not frame.empty]
    if screener_frames:
        symbols_to_refresh = sorted(
            pd.concat(screener_frames, ignore_index=True)["symbol"]
            .dropna()
            .astype("string")
            .str.upper()
            .drop_duplicates()
            .tolist()
        )
        meta["preflight"] = refresh_missing_snapshots(symbols_to_refresh, effective_date)
        technical = load_technical(effective_date)
        fundamentals = load_fundamentals(effective_date)

    for setup in setups:
        universe = load_screener_universe(screener_date, setup.get("screener_slug"))
        if universe.empty:
            rejection_rows.append(
                {
                    "asof_date": effective_date,
                    "screener_date": screener_date,
                    "setup_id": setup["setup_id"],
                    "setup_name": setup["setup_name"],
                    "symbol": None,
                    "company_master_id": None,
                    "reason_code": "missing_screener_universe",
                    "reason_detail": f"screener_slug={setup.get('screener_slug')}",
                    "load_ts": pd.Timestamp.utcnow(),
                }
            )
            continue

        merged = universe.merge(
            technical.drop_duplicates(subset=["asof_date", "symbol"], keep="last"),
            on=["asof_date", "symbol", "company_master_id"],
            how="left",
            suffixes=("", "_tech"),
        )
        merged = merged.merge(
            fundamentals.drop_duplicates(subset=["asof_date", "symbol"], keep="last"),
            on=["asof_date", "symbol", "company_master_id"],
            how="left",
            suffixes=("", "_fund"),
        )
        for _, row in merged.iterrows():
            passed, rejections = evaluate_setup_row(
                row,
                regime_name=str(regime["regime_name"]),
                setup=setup,
            )
            if passed:
                candidate_rows.append(
                    {
                        "asof_date": effective_date,
                        "screener_date": screener_date,
                        "setup_id": setup["setup_id"],
                        "setup_name": setup["setup_name"],
                        "regime_name": regime["regime_name"],
                        "symbol": row["symbol"],
                        "company_master_id": row["company_master_id"],
                        "screener_slug": row.get("screener_slug"),
                        "rank": row.get("rank"),
                        "avg_traded_value_20d": row.get("avg_traded_value_20d"),
                        "rs_vs_benchmark": row.get("rs_vs_benchmark"),
                        "rs_vs_sector": row.get("rs_vs_sector"),
                        "total_revenue_qoq_growth_vs_sector": row.get("total_revenue_qoq_growth_vs_sector"),
                        "profit_after_tax_qoq_growth_vs_sector": row.get("profit_after_tax_qoq_growth_vs_sector"),
                        "debt_to_equity_vs_sector": row.get("debt_to_equity_vs_sector"),
                        "watch_enabled": True,
                        "watch_reasons": json.dumps(setup.get("watch_reasons", [])),
                        "rule_pass": True,
                        "load_ts": pd.Timestamp.utcnow(),
                    }
                )
            else:
                for rejection in rejections:
                    rejection_rows.append(
                        {
                            "asof_date": effective_date,
                            "screener_date": screener_date,
                            "setup_id": setup["setup_id"],
                            "setup_name": setup["setup_name"],
                            "symbol": row["symbol"],
                            "company_master_id": row["company_master_id"],
                            "reason_code": rejection["reason_code"],
                            "reason_detail": rejection["reason_detail"],
                            "load_ts": pd.Timestamp.utcnow(),
                        }
                    )

    return pd.DataFrame(candidate_rows), pd.DataFrame(rejection_rows), meta


def persist_rule_outputs(
    candidates: pd.DataFrame,
    rejections: pd.DataFrame,
    *,
    asof_date: pd.Timestamp | None,
    rebuild: bool = False,
) -> None:
    if rebuild and asof_date is not None:
        with db_session() as (_, cur):
            cur.execute(f"CREATE TABLE IF NOT EXISTS {CANDIDATES_TABLE} (asof_date TIMESTAMPTZ, setup_id TEXT, symbol TEXT)")
            cur.execute(f"CREATE TABLE IF NOT EXISTS {REJECTIONS_TABLE} (asof_date TIMESTAMPTZ, setup_id TEXT, symbol TEXT, reason_code TEXT)")
            cur.execute(f"DELETE FROM {CANDIDATES_TABLE} WHERE asof_date = %s", (asof_date,))
            cur.execute(f"DELETE FROM {REJECTIONS_TABLE} WHERE asof_date = %s", (asof_date,))
    if not candidates.empty:
        upsert_to_db(
            candidates,
            CANDIDATES_TABLE,
            unique_keys=["asof_date", "setup_id", "symbol"],
            timescaledb_column="asof_date",
        )
    if not rejections.empty:
        upsert_to_db(
            rejections,
            REJECTIONS_TABLE,
            unique_keys=["asof_date", "setup_id", "symbol", "reason_code"],
            timescaledb_column="asof_date",
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply setup rules to advisory snapshots.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Asof date in YYYY-MM-DD")
    parser.add_argument("--setup", dest="setup_ids", nargs="*", help="Setup ids to evaluate")
    parser.add_argument("--config", help="Override setup registry YAML path")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def summarize(candidates: pd.DataFrame, rejections: pd.DataFrame, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "ok",
        "candidates_table": CANDIDATES_TABLE,
        "rejections_table": REJECTIONS_TABLE,
        "effective_date": meta.get("effective_date"),
        "screener_date": meta.get("screener_date"),
        "regime_name": meta.get("regime_name"),
        "candidate_count": int(len(candidates)),
        "rejection_count": int(len(rejections)),
        "candidate_sample": candidates.head(10).to_dict(orient="records") if not candidates.empty else [],
        "top_rejections": (
            rejections["reason_code"].value_counts().head(10).to_dict()
            if not rejections.empty
            else {}
        ),
    }


def main() -> int:
    args = parse_args()
    asof_date = pd.Timestamp(args.date, tz="UTC") if args.date else None
    candidates, rejections, meta = run_rule_engine(
        asof_date=asof_date,
        setup_ids=args.setup_ids,
        config_path=args.config,
    )
    effective_date = pd.to_datetime(meta.get("effective_date"), utc=True, errors="coerce")
    if not args.dry_run:
        persist_rule_outputs(
            candidates,
            rejections,
            asof_date=None if pd.isna(effective_date) else effective_date,
            rebuild=args.rebuild,
        )
    result = summarize(candidates, rejections, meta)
    result["dry_run"] = bool(args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
