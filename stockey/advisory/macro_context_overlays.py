from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event
from advisory.macro_features import TABLE_NAME as MACRO_FEATURES_TABLE
from advisory.macro_features import table_exists
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


MACRO_CONTEXT_OVERLAYS_TABLE = "advisory_macro_context_overlays"
MACRO_CONTEXT_OVERLAY_SCHEMA_MIGRATION_ID = "20260620_advisory_macro_context_overlays_base"

MACRO_CONTEXT_OVERLAY_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {MACRO_CONTEXT_OVERLAYS_TABLE} (
        asof_date TIMESTAMPTZ NOT NULL,
        overlay_id TEXT NOT NULL,
        macro_signal_id TEXT NOT NULL,
        macro_signal_name TEXT,
        sector_name TEXT,
        sector_code TEXT,
        direction TEXT NOT NULL,
        pressure_score DOUBLE PRECISION,
        macro_stress_score DOUBLE PRECISION,
        macro_risk_state TEXT,
        trigger_reason TEXT,
        matched_sources_json TEXT,
        authority_scope TEXT NOT NULL DEFAULT 'watchlist_pressure_only',
        production_status TEXT NOT NULL DEFAULT 'active',
        load_ts TIMESTAMPTZ NOT NULL,
        UNIQUE (asof_date, overlay_id)
    )
    """,
]

# Reviewed mapping from semantic macro overlay sectors to the current Sharpely/LSEG-style
# sector codes used by advisory_market_context_universe_daily. This is deliberately
# conservative: it only creates review-only watch/de-risk pressure and never grants
# portfolio or broker authority.
MACRO_SECTOR_CODE_ALIASES: dict[str, tuple[str, ...]] = {
    "AUTOS": ("IN0201",),
    "AVIATION": ("IN0901",),
    "CAPITALMARKETS": ("IN0501",),
    "CHEMICALS": ("IN0101",),
    "CONSUMER": ("IN0401", "IN0202", "IN0206"),
    "CONSUMERDURABLES": ("IN0202",),
    "FMCG": ("IN0401",),
    "INFORMATIONTECHNOLOGY": ("IN0801",),
    "LOGISTICS": ("IN0901",),
    "NBFC": ("IN0501",),
    "OILGAS": ("IN0301",),
    "PAINTS": ("IN0202",),
    "PHARMACEUTICALS": ("IN0601",),
    "REALESTATE": ("IN0205",),
}
MACRO_SECTOR_INTENTIONALLY_BROAD_KEYS = {"HIGHBETA", "SMALLCAP"}


def macro_sector_key(value: Any) -> str:
    return "".join(char for char in str(value or "").upper() if char.isalnum())


def macro_sector_alias_values_sql() -> str:
    values: list[str] = []
    for key, sector_codes in sorted(MACRO_SECTOR_CODE_ALIASES.items()):
        for sector_code in sector_codes:
            values.append(f"('{key}', '{sector_code}')")
    return ",\n                ".join(values) or "('NO_MACRO_ALIAS', 'NO_SECTOR')"


def summarize_macro_sector_alias_coverage(overlays: pd.DataFrame) -> dict[str, Any]:
    if not isinstance(overlays, pd.DataFrame) or overlays.empty or "sector_name" not in overlays.columns:
        return {
            "status": "no_macro_overlay_sectors",
            "sector_count": 0,
            "mapped_sector_count": 0,
            "intentionally_broad_sector_count": 0,
            "unmapped_sector_count": 0,
            "unmapped_sectors": [],
            "authority_scope": "diagnostic_only",
            "broker_execution_allowed": False,
        }
    sectors = sorted({str(value).strip() for value in overlays["sector_name"].dropna().tolist() if str(value).strip()})
    mapped: list[dict[str, Any]] = []
    broad: list[dict[str, Any]] = []
    unmapped: list[str] = []
    for sector in sectors:
        key = macro_sector_key(sector)
        if key in MACRO_SECTOR_CODE_ALIASES:
            mapped.append({"sector_name": sector, "sector_key": key, "sector_codes": list(MACRO_SECTOR_CODE_ALIASES[key])})
        elif key in MACRO_SECTOR_INTENTIONALLY_BROAD_KEYS:
            broad.append(
                {
                    "sector_name": sector,
                    "sector_key": key,
                    "reason": "Broad macro bucket is intentionally not mapped to sector-code targets until a narrower symbol/universe policy is reviewed.",
                }
            )
        else:
            unmapped.append(sector)
    status = "ok" if not unmapped else "unmapped_macro_overlay_sectors"
    return {
        "status": status,
        "sector_count": int(len(sectors)),
        "mapped_sector_count": int(len(mapped)),
        "intentionally_broad_sector_count": int(len(broad)),
        "unmapped_sector_count": int(len(unmapped)),
        "mapped_sectors": mapped,
        "intentionally_broad_sectors": broad,
        "unmapped_sectors": unmapped,
        "authority_scope": "diagnostic_only",
        "broker_execution_allowed": False,
    }


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False, sort_keys=True, default=str)


def _number(value: Any) -> float | None:
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return None
    return float(numeric)


def _slug(value: Any) -> str:
    text = str(value or "").strip().lower()
    out: list[str] = []
    previous_dash = False
    for char in text:
        if char.isalnum():
            out.append(char)
            previous_dash = False
        elif not previous_dash:
            out.append("-")
            previous_dash = True
    return "".join(out).strip("-") or "market"


def ensure_macro_context_overlay_table() -> None:
    apply_schema_migration(
        migration_id=MACRO_CONTEXT_OVERLAY_SCHEMA_MIGRATION_ID,
        statements=MACRO_CONTEXT_OVERLAY_SCHEMA_STATEMENTS,
        owner="advisory.macro_context_overlays",
        description="Create review-only macro sector/context overlay table.",
        metadata={"tables": [MACRO_CONTEXT_OVERLAYS_TABLE], "workflow": "macro_context_overlays"},
    )


def _macro_signal_rows(row: dict[str, Any]) -> list[dict[str, Any]]:
    stress = _number(row.get("macro_stress_score")) or 0.0
    risk_state = str(row.get("macro_risk_state") or "UNKNOWN").strip().upper()
    wti_ret = _number(row.get("wti_ret_20d"))
    crude_wpi = _number(row.get("wpi_crude_petroleum_gas_change_60d"))
    inr_usd = _number(row.get("inr_usd_ret_20d"))
    usd_ret = _number(row.get("broad_usd_ret_20d"))
    gsec_10y = _number(row.get("gsec_10y_change_20d_bps"))
    ust10y = _number(row.get("ust10y_change_20d_bps"))
    cfpi = _number(row.get("india_cfpi_change_60d"))
    vix = _number(row.get("vix_close"))

    signals: list[dict[str, Any]] = []

    if risk_state in {"ELEVATED", "STRESS"} or stress >= 0.40 or (vix is not None and vix >= 24.0):
        pressure = min(1.0, max(stress, 0.45 if risk_state == "ELEVATED" else 0.65 if risk_state == "STRESS" else 0.40))
        signals.append(
            {
                "macro_signal_id": "BROAD_RISK_STRESS",
                "macro_signal_name": "Broad market risk stress",
                "direction": "negative",
                "sectors": ["High beta", "Real Estate", "Smallcap", "Capital Markets"],
                "pressure_score": pressure,
                "trigger_reason": "Macro stress or volatility is elevated; treat affected high-beta sectors as review-only de-risk/watch pressure.",
                "sources": {"macro_stress_score": stress, "macro_risk_state": risk_state, "vix_close": vix},
            }
        )

    if (wti_ret is not None and wti_ret >= 0.10) or (crude_wpi is not None and crude_wpi >= 5.0):
        pressure = min(1.0, max(abs(wti_ret or 0.0) * 3.0, abs(crude_wpi or 0.0) / 10.0, 0.45))
        signals.extend(
            [
                {
                    "macro_signal_id": "CRUDE_UPSTREAM_SUPPORT",
                    "macro_signal_name": "Crude price support",
                    "direction": "positive",
                    "sectors": ["Oil & Gas"],
                    "pressure_score": pressure,
                    "trigger_reason": "Crude-linked macro inputs are rising; upstream and energy-linked names get review-only positive watch pressure.",
                    "sources": {"wti_ret_20d": wti_ret, "wpi_crude_petroleum_gas_change_60d": crude_wpi},
                },
                {
                    "macro_signal_id": "CRUDE_INPUT_COST_PRESSURE",
                    "macro_signal_name": "Crude input-cost pressure",
                    "direction": "negative",
                    "sectors": ["Chemicals", "Paints", "Aviation", "Logistics"],
                    "pressure_score": pressure,
                    "trigger_reason": "Crude-linked macro inputs are rising; oil-input-sensitive sectors get review-only margin-pressure watch.",
                    "sources": {"wti_ret_20d": wti_ret, "wpi_crude_petroleum_gas_change_60d": crude_wpi},
                },
            ]
        )

    if (inr_usd is not None and inr_usd >= 0.02) or (usd_ret is not None and usd_ret >= 0.03):
        pressure = min(1.0, max(abs(inr_usd or 0.0) * 10.0, abs(usd_ret or 0.0) * 8.0, 0.40))
        signals.extend(
            [
                {
                    "macro_signal_id": "RUPEE_WEAKNESS_EXPORT_SUPPORT",
                    "macro_signal_name": "Rupee weakness export support",
                    "direction": "positive",
                    "sectors": ["Information Technology", "Pharmaceuticals"],
                    "pressure_score": pressure,
                    "trigger_reason": "Rupee or dollar strength can support exporters; confirm stock-specific execution before action.",
                    "sources": {"inr_usd_ret_20d": inr_usd, "broad_usd_ret_20d": usd_ret},
                },
                {
                    "macro_signal_id": "RUPEE_WEAKNESS_IMPORT_PRESSURE",
                    "macro_signal_name": "Rupee weakness import pressure",
                    "direction": "negative",
                    "sectors": ["Consumer Durables", "Aviation", "Chemicals"],
                    "pressure_score": pressure,
                    "trigger_reason": "Rupee weakness can raise imported input costs; monitor margin-sensitive importers.",
                    "sources": {"inr_usd_ret_20d": inr_usd, "broad_usd_ret_20d": usd_ret},
                },
            ]
        )

    if (gsec_10y is not None and gsec_10y >= 25.0) or (ust10y is not None and ust10y >= 25.0):
        pressure = min(1.0, max(abs(gsec_10y or 0.0), abs(ust10y or 0.0)) / 75.0)
        signals.append(
            {
                "macro_signal_id": "YIELD_SHOCK_RATE_SENSITIVE_PRESSURE",
                "macro_signal_name": "Yield shock pressure",
                "direction": "negative",
                "sectors": ["Real Estate", "NBFC", "Autos", "Consumer Durables"],
                "pressure_score": max(0.45, pressure),
                "trigger_reason": "Bond yields have moved sharply higher; rate-sensitive sectors get review-only pressure.",
                "sources": {"gsec_10y_change_20d_bps": gsec_10y, "ust10y_change_20d_bps": ust10y},
            }
        )

    if cfpi is not None and cfpi >= 2.0:
        pressure = min(1.0, max(0.35, cfpi / 5.0))
        signals.append(
            {
                "macro_signal_id": "FOOD_INFLATION_CONSUMER_MARGIN_PRESSURE",
                "macro_signal_name": "Food inflation margin pressure",
                "direction": "negative",
                "sectors": ["FMCG", "Consumer"],
                "pressure_score": pressure,
                "trigger_reason": "Food inflation is rising; consumer-margin names get review-only pressure until pricing power is confirmed.",
                "sources": {"india_cfpi_change_60d": cfpi},
            }
        )

    return signals


def build_macro_context_overlays_from_features(features: pd.DataFrame, *, asof_date: pd.Timestamp | None = None) -> pd.DataFrame:
    if features.empty:
        return pd.DataFrame()
    frame = features.copy()
    frame["asof_date"] = pd.to_datetime(frame["asof_date"], utc=True, errors="coerce")
    effective_asof = pd.to_datetime(asof_date or frame["asof_date"].max(), utc=True, errors="coerce").normalize()
    frame = frame[frame["asof_date"] <= effective_asof].sort_values("asof_date")
    if frame.empty:
        return pd.DataFrame()
    latest = frame.iloc[-1].to_dict()
    source_asof = pd.to_datetime(latest.get("asof_date"), utc=True, errors="coerce").normalize()
    load_ts = pd.Timestamp.utcnow()
    rows: list[dict[str, Any]] = []
    for signal in _macro_signal_rows(latest):
        for sector_name in signal["sectors"]:
            sector_code = _slug(sector_name).upper()
            overlay_id = f"{source_asof.date()}:{signal['macro_signal_id']}:{signal['direction']}:{sector_code}"
            rows.append(
                {
                    "asof_date": source_asof,
                    "overlay_id": overlay_id,
                    "macro_signal_id": signal["macro_signal_id"],
                    "macro_signal_name": signal["macro_signal_name"],
                    "sector_name": sector_name,
                    "sector_code": sector_code,
                    "direction": signal["direction"],
                    "pressure_score": round(float(signal["pressure_score"]), 6),
                    "macro_stress_score": _number(latest.get("macro_stress_score")),
                    "macro_risk_state": str(latest.get("macro_risk_state") or "UNKNOWN").strip().upper(),
                    "trigger_reason": str(signal["trigger_reason"])[:1000],
                    "matched_sources_json": _json_dumps(signal["sources"]),
                    "authority_scope": "watchlist_pressure_only",
                    "production_status": "active",
                    "load_ts": load_ts,
                }
            )
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    for column in ["asof_date", "load_ts"]:
        out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")
    for column in ["pressure_score", "macro_stress_score"]:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    return out.drop_duplicates(subset=["asof_date", "overlay_id"], keep="last")


def load_latest_macro_features(*, asof_date: pd.Timestamp | None = None) -> pd.DataFrame:
    if not table_exists(MACRO_FEATURES_TABLE):
        return pd.DataFrame()
    effective_asof = pd.to_datetime(asof_date or pd.Timestamp.utcnow(), utc=True, errors="coerce").normalize()
    try:
        return sql_to_df(
            f"""
            SELECT *
            FROM {MACRO_FEATURES_TABLE}
            WHERE asof_date = (
                SELECT MAX(asof_date)
                FROM {MACRO_FEATURES_TABLE}
                WHERE asof_date <= %(asof_date)s
            )
            """,
            params={"asof_date": effective_asof},
        )
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.macro_context_overlays",
            fallback_type="macro_context_features_load_failed",
            source=MACRO_FEATURES_TABLE,
            severity="warn",
            reason="Macro context overlay builder could not load latest macro feature row.",
            error=exc,
            metadata={"asof_date": str(effective_asof)},
        )
        return pd.DataFrame()


def build_macro_context_overlays(
    *,
    asof_date: pd.Timestamp | None = None,
    features: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    source_mode = "provided_features" if features is not None else "latest_persisted"
    effective_features = features if features is not None else load_latest_macro_features(asof_date=asof_date)
    overlays = build_macro_context_overlays_from_features(effective_features, asof_date=asof_date)
    alias_coverage = summarize_macro_sector_alias_coverage(overlays)
    return overlays, {
        "asof_date": pd.to_datetime(asof_date or pd.Timestamp.utcnow(), utc=True, errors="coerce").normalize(),
        "source_feature_count": int(len(effective_features)),
        "overlay_count": int(len(overlays)),
        "authority_scope": "watchlist_pressure_only",
        "source_mode": source_mode,
        "sector_alias_coverage": alias_coverage,
    }


def persist_macro_context_overlays(df: pd.DataFrame) -> None:
    ensure_macro_context_overlay_table()
    if df.empty:
        return
    upsert_to_db(df, MACRO_CONTEXT_OVERLAYS_TABLE, unique_keys=["asof_date", "overlay_id"], timescaledb_column="asof_date")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build review-only macro sector/context overlays.")
    parser.add_argument("--date", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=["json", "text"], default="text")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    asof_date = pd.to_datetime(parse_datetime_arg(args.date) if args.date else pd.Timestamp.utcnow(), utc=True, errors="coerce").normalize()
    overlays, meta = build_macro_context_overlays(asof_date=asof_date)
    if not args.dry_run:
        persist_macro_context_overlays(overlays)
    payload = {
        "status": "ok",
        "table": MACRO_CONTEXT_OVERLAYS_TABLE,
        "rows": int(len(overlays)),
        "dry_run": bool(args.dry_run),
        "meta": meta,
    }
    if args.format == "json":
        print(json.dumps(payload, default=str, ensure_ascii=False, sort_keys=True))
    else:
        print(f"macro_context_overlays rows={len(overlays)} dry_run={args.dry_run} table={MACRO_CONTEXT_OVERLAYS_TABLE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
