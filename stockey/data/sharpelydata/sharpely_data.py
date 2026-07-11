import argparse
import json
from datetime import datetime

import pandas as pd
from environs import Env

from advisory.fallback_telemetry import record_local_fallback_event
from utils.company_master import attach_company_master_id
from utils.db import sql_to_df, upsert_to_db
from utils.http import get_with_retries
from utils.date import last_of_month
from utils.sync import choose_from_date, get_db_max_date, load_tracked_symbols, normalize_date_window, parse_datetime_arg

from . import sharpely_utils as su

env = Env()
env.read_env()
HEADERS = su.get_sharpely_headers()
SHARPELY_STOCK_META_TABLE = "sharpely_stock_meta"
SHARPELY_STOCK_PEERS_TABLE = "sharpely_stock_peers"
STOCKEY_RUN_STATE: dict[str, object] = {}


def _record_sharpely_fallback(
    *,
    fallback_type: str,
    reason: str,
    error: Exception | None = None,
    symbol: str | None = None,
    metadata: dict[str, object] | None = None,
) -> None:
    record_local_fallback_event(
        module="data.sharpelydata.sharpely_data",
        source="sharpely",
        fallback_type=fallback_type,
        severity="warn",
        symbol=symbol,
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def classify_sharpely_sync_error(error: object) -> str:
    text = str(error or "").lower()
    if "unauthorized" in text or "forbidden" in text or "status 401" in text or "status 403" in text:
        return "auth_unavailable"
    if "timed out" in text or "timeout" in text or "connection" in text or "status 502" in text or "status 503" in text or "status 504" in text:
        return "source_unavailable"
    if "json" in text or "keyerror" in text or "columns" in text:
        return "parse_failed"
    return "failed"


def classify_sharpely_run_state(*, rows_written: int, failures: list[dict[str, object]], symbol_count: int) -> str:
    if symbol_count <= 0:
        return "no_data"
    if failures:
        classifications = [str(row.get("classification") or classify_sharpely_sync_error(row.get("error"))) for row in failures]
        if rows_written > 0:
            return "partial_failed"
        if classifications and all(item == "auth_unavailable" for item in classifications):
            return "auth_unavailable"
        if classifications and all(item == "source_unavailable" for item in classifications):
            return "source_unavailable"
        if classifications and all(item == "parse_failed" for item in classifications):
            return "parse_failed"
        return "failed"
    return "ok" if rows_written > 0 else "no_data"


def filter_by_date_range(df: pd.DataFrame, from_date: datetime | None, to_date: datetime | None) -> pd.DataFrame:
    if df.empty or "date" not in df.columns:
        return df

    series = pd.to_datetime(df["date"], errors="coerce")
    if from_date is not None:
        df = df[series >= pd.Timestamp(from_date)]
        series = pd.to_datetime(df["date"], errors="coerce")
    if to_date is not None:
        df = df[series <= pd.Timestamp(to_date)]
    return df.reset_index(drop=True)


def get_financial_statement(symbol: str, from_date: datetime | None = None, to_date: datetime | None = None) -> dict[str, object]:
    resp = get_with_retries(
        f"https://pyapiv2.mintbox.ai/api/core/getFinancialStatementsV2/ticker={symbol}",
        headers=HEADERS,
    ).json()
    fin = json.loads(resp["statements"])

    income_fccs = {
        "SREV": "gross_revenue",
        "STLR": "total_revenue",
        "SCOR": "cost_of_operating_revenue",
        "SDCS": "depreciation",
        "SOET": "total_operating_expenses",
        "SOPR": "operating_profit",
        "SNII": "net_interest_expense",
        "SIEN": "interest_expense",
        "SIBT": "profit_before_tax",
        "STAX": "income_tax_expense",
        "SCTX": "income_tax_current",
        "SDTR": "income_tax_deferred",
        "SIAT": "net_income_after_tax",
        "SNIC": "profit_after_tax",
        "SBASC": "shares_used_basic_eps",
        "SBAIC": "eps_basic",
        "SDWSC": "shares_used_diluted_eps",
        "SDAIC": "eps_diluted",
        "SEBIT": "ebit",
        "SEBITDA": "ebitda",
        "SDEA": "depreciation_and_amortization",
        "SVLAR": "employee_and_related_expenses",
        "SINTEX": "interest_expense",
        "SOPEX": "operating_expenses",
    }

    balance_fccs = {
        "SCAE": "cash_and_cash_equivalents",
        "SCASH": "cash_and_cash_equivalents",
        "SSTI": "short_term_investments",
        "SANR": "accounts_receivable",
        "SINY": "total_inventories",
        "STCA": "total_current_assets",
        "SPPE": "property_plant_equipment_net",
        "SINN": "intangible_assets_net",
        "STLA": "total_non_current_assets",
        "ATOT": "total_assets",
        "SAPA": "accounts_payable",
        "SAEC": "accrued_expenses",
        "SSTD": "short_term_debt_and_cpltd",
        "SCLT": "total_current_liabilities",
        "SLTD": "long_term_debt",
        "SLLT": "other_non_current_liabilities_total",
        "STLB": "total_liabilities",
        "QTEP": "total_shareholders_equity",
        "SRED": "retained_earnings",
        "STBL": "total_liabilities_and_equity",
        "SCLR": "short_term_loans_and_receivables",
        "SCLD": "capitalized_leases_current_portion",
        "SLCL": "capitalized_lease_obligations_long_term",
        "SDTA": "deferred_tax_asset_long_term",
        "STCAXIN": "total_current_assets_ex_inventories",
        "STIN": "intangible_assets_ex_goodwill_net",
        "STFL": "finance_and_operating_lease_liabilities",
        "STDL": "lease_debt_including_liabilities",
        "SACRU": "accruals_short_term",
        "SLNS": "loans_short_term",
        "SLNG": "loans_long_term",
        "STIV": "total_investments",
        "SDTX": "deferred_investment_tax_credits_long_term",
        "SINBL": "interest_bearing_liabilities_total",
        "SSND": "net_debt",
    }

    cashflow_fccs = {
        "SPLS": "net_income_starting_line",
        "SNCR": "non_cash_adjustments",
        "SDAI": "depreciation_and_amortization",
        "SCWC": "change_in_working_capital",
        "STLO": "net_cash_from_operating_activities",
        "SCAP": "capital_expenditures_net",
        "SBAS": "acquisition_or_disposal_of_business",
        "STLI": "net_cash_from_investing_activities",
        "SCDP": "dividends_paid_total",
        "SCSBN": "common_stock_buyback_net",
        "SPSS": "stock_issuance_retirement_net",
        "SPRD": "debt_issuance_retirement_total",
        "STLF": "net_cash_from_financing_activities",
        "SFCFO": "free_operating_cash_flow",
        "SFCFE": "free_cash_flow_to_equity",
        "SFCFL": "free_operating_cash_flow_gross",
        "SNCC": "net_change_in_cash",
        "SNCB": "cash_beginning_balance",
        "SNCE": "cash_ending_balance",
    }

    table_map = {
        "stmt_income": parse_consolidated_statement(symbol, fin["inc_consol_interim"], income_fccs),
        "stmt_balancesheet": parse_consolidated_statement(symbol, fin["bal_consol_interim"], balance_fccs),
        "stmt_cashflow": parse_consolidated_statement(symbol, fin["cas_consol_interim"], cashflow_fccs),
    }

    row_counts: dict[str, int] = {}
    for table_name, df in table_map.items():
        if df.empty:
            row_counts[table_name] = 0
            continue
        df["date"] = pd.to_datetime(df["date"])
        df = filter_by_date_range(df, from_date, to_date)
        if df.empty:
            row_counts[table_name] = 0
            continue
        df = attach_company_master_id(df, ticker_column="symbol", exchange="NSE")
        upsert_to_db(
            df,
            table_name,
            unique_keys=["symbol", "date", "period_length"],
            timescaledb_column="date",
        )
        row_counts[table_name] = int(len(df))
    return {
        "symbol": symbol,
        "rows": int(sum(row_counts.values())),
        "income_rows": row_counts.get("stmt_income", 0),
        "balancesheet_rows": row_counts.get("stmt_balancesheet", 0),
        "cashflow_rows": row_counts.get("stmt_cashflow", 0),
    }


def parse_consolidated_statement(symbol, data, fccs):
    headers = {h["period_end_date"]: h["period_length"] for h in data["header"]}
    periods = data["statement"]["columns"]

    rows = []
    for item in data["statement"]["data"]:
        fcc_code = item[0]
        if fcc_code in fccs:
            values = item[1:]
            for i, period in enumerate(periods):
                rows.append(
                    {
                        "date": period,
                        "period_length": headers.get(period),
                        "metric": fccs[fcc_code],
                        "value": values[i] if i < len(values) else None,
                    }
                )

    df_long = pd.DataFrame(rows)
    if df_long.empty:
        return pd.DataFrame(columns=["date", "period_length", "symbol"])

    df_long["value"] = pd.to_numeric(df_long["value"], errors="coerce")
    df = df_long.pivot_table(
        index=["date", "period_length"], columns="metric", values="value"
    ).reset_index()
    df["symbol"] = symbol
    return df


def normalize_meta_rows(symbol: str, meta_data: list[dict]) -> pd.DataFrame:
    rows = []
    for item in meta_data:
        if not isinstance(item, dict):
            continue
        row = {
            "symbol": symbol,
            "proper_name": item.get("proper_name"),
            "isin": item.get("isin"),
            "nse_ticker": item.get("nse_ticker"),
            "bse_ticker": item.get("bse_ticker"),
            "sector_code": item.get("sector_code"),
            "industry_code": item.get("industry_code"),
            "nse_basic_ind_code": item.get("nse_basic_ind_code"),
            "macro_sec_code": item.get("macro_sec_code"),
            "style_box_code": item.get("style_box_code"),
            "risk_box_code": item.get("risk_box_code"),
            "mcap": item.get("mcap"),
            "enterprise_val": item.get("enterprise_val"),
            "price": item.get("price"),
            "price_date": item.get("price_date"),
            "as_on_date": item.get("as_on_date"),
            "raw_json": json.dumps(item, ensure_ascii=False, sort_keys=True),
        }
        rows.append(row)

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    for col in [
        "style_box_code",
        "risk_box_code",
        "mcap",
        "enterprise_val",
        "price",
    ]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ["price_date", "as_on_date"]:
        df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")
    df = attach_company_master_id(df, ticker_column="symbol", exchange="NSE")
    return df.drop_duplicates(subset=["symbol", "as_on_date"], keep="last")


def save_stock_meta(symbol: str, meta_data: list[dict]) -> pd.DataFrame:
    df = normalize_meta_rows(symbol, meta_data)
    if df.empty:
        return df
    upsert_to_db(
        df,
        SHARPELY_STOCK_META_TABLE,
        unique_keys=["symbol", "as_on_date"],
        timescaledb_column="as_on_date",
    )
    return df


def get_stock_meta(symbol: str):
    json_data = {
        'stocks': [
            symbol,
        ],
        'cols': [],
        'all_cols': True,
    }

    response = get_with_retries('https://pyapiv2.mintbox.ai/api/core/getStocksColData', headers=HEADERS, method='POST', json_data=json_data)
    meta_data = json.loads(json.loads(response.content))
    return meta_data


def load_latest_stock_meta(symbol: str) -> dict | None:
    df = sql_to_df(
        f"""
        SELECT *
        FROM {SHARPELY_STOCK_META_TABLE}
        WHERE symbol = %s
        ORDER BY as_on_date DESC
        LIMIT 1
        """,
        params=(symbol.upper(),),
    )
    if df.empty:
        return None
    row = df.iloc[0].to_dict()
    raw_json = row.get("raw_json")
    if isinstance(raw_json, str):
        try:
            return json.loads(raw_json)
        except json.JSONDecodeError as exc:
            _record_sharpely_fallback(
                fallback_type="sharpely_cached_meta_json_parse_failed",
                reason="Cached Sharpely stock metadata raw_json could not be parsed; using row fields as fallback.",
                error=exc,
                symbol=symbol.upper(),
                metadata={"table": SHARPELY_STOCK_META_TABLE},
            )
            return row
    return row


def normalize_peer_rows(symbol: str, meta: dict, peers: list[dict]) -> pd.DataFrame:
    rows = []
    anchor_as_on = meta.get("as_on_date")
    anchor_ts = pd.to_datetime(anchor_as_on, utc=True, errors="coerce")
    for rank, item in enumerate(peers, start=1):
        if not isinstance(item, dict) or not item.get("symbol"):
            continue
        peer_as_on = item.get("as_on_date")
        peer_ts = pd.to_datetime(peer_as_on, unit="ms", utc=True, errors="coerce")
        effective_as_on = anchor_ts
        if pd.isna(effective_as_on):
            effective_as_on = peer_ts
        rows.append(
            {
                "anchor_symbol": symbol,
                "peer_symbol": str(item.get("symbol")).strip().upper(),
                "sector_code": item.get("sector_code") or meta.get("sector_code"),
                "industry_code": item.get("industry_code") or meta.get("industry_code"),
                "nse_basic_ind_code": item.get("nse_basic_ind_code") or meta.get("nse_basic_ind_code"),
                "peer_rank": rank,
                "is_self_peer": str(item.get("symbol")).strip().upper() == symbol,
                "nse_segment": item.get("nse_segment"),
                "bse_segment": item.get("bse_segment"),
                "nse_active": item.get("nse_active"),
                "is_exclusion_list": item.get("is_exclusion_list"),
                "peer_mcap": item.get("mcap"),
                "peer_as_on_date": peer_ts,
                "as_on_date": effective_as_on,
                "raw_json": json.dumps(item, ensure_ascii=False, sort_keys=True),
            }
        )

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["anchor_symbol"] = df["anchor_symbol"].astype("string").str.strip().str.upper()
    df["peer_symbol"] = df["peer_symbol"].astype("string").str.strip().str.upper()
    for col in ["peer_rank", "nse_active", "is_exclusion_list", "peer_mcap"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["is_self_peer"] = df["is_self_peer"].astype("boolean")
    df = attach_company_master_id(df, ticker_column="anchor_symbol", exchange="NSE", target_column="anchor_company_master_id")
    df = attach_company_master_id(df, ticker_column="peer_symbol", exchange="NSE", target_column="peer_company_master_id")
    return df.drop_duplicates(subset=["anchor_symbol", "peer_symbol", "as_on_date"], keep="last")


def save_stock_peers(symbol: str, meta: dict, peers: list[dict]) -> pd.DataFrame:
    df = normalize_peer_rows(symbol, meta, peers)
    if df.empty:
        return df
    upsert_to_db(
        df,
        SHARPELY_STOCK_PEERS_TABLE,
        unique_keys=["anchor_symbol", "peer_symbol", "as_on_date"],
        timescaledb_column="as_on_date",
    )
    return df


def get_stock_peers(symbol: str, meta: dict | None = None):
    meta = meta or load_latest_stock_meta(symbol) or {}
    industry_code = meta.get("nse_basic_ind_code")
    if not industry_code:
        raise ValueError(f"Missing nse_basic_ind_code for {symbol}. Run get_stock_meta first.")

    json_data = {
        'rules': [
            {
                'abs_val_1': industry_code,
                'abs_val_2': None,
                'adj_operator': None,
                'checklist_id': 0,
                'client_id': 0,
                'comp_operator': 'eq',
                'is_active': 1,
                'is_advanced': 0,
                'oper': 'isin_comp',
                'pos': 2,
                'primary_col': 'nse_basic_ind_code',
                'primary_col_table': 'S',
                'rel_comp_name': None,
                'rel_comp_stat': None,
                'rule_id': 2,
                'rule_name': 'nse_basic_ind_code',
                'sec_col': None,
                'sec_col_table': None,
            },
        ],
        'cols': [],
    }
    response = get_with_retries(
        'https://pyapiv2.mintbox.ai/api/core/getAllScreenedStocksNew',
        headers=HEADERS,
        method='POST',
        json_data=json_data,
    )
    peers = json.loads(json.loads(response.content))
    return peers


def get_shareholding(symbol: str, from_date: datetime | None = None, to_date: datetime | None = None) -> dict[str, object]:
    resp = get_with_retries(
        f"https://pyapiv2.mintbox.ai/api/core/getShareHoldingsDataAccord/symbol={symbol}",
        headers=HEADERS,
    ).json()
    shs = json.loads(resp["shareholdings"])

    records = []
    for report_date, holders in shs.items():
        for row in holders["data"]:
            entry = row.copy()
            entry["date"] = pd.to_datetime(last_of_month(datetime.strptime(report_date, "%Y%m")))
            records.append(entry)

    df = pd.DataFrame(records)
    category_rows = 0
    if not df.empty:
        df["symbol"] = symbol
        df = filter_by_date_range(df, from_date, to_date)
        if not df.empty:
            df = attach_company_master_id(df, ticker_column="symbol", exchange="NSE")
            upsert_to_db(
                df,
                "shareholding_category",
                unique_keys=["symbol", "date", "sh_code"],
                timescaledb_column="date",
            )
            category_rows = int(len(df))

    type_map = {
        "1": "indian",
        "2": "institutional",
        "6": "non-institutional",
    }
    records = []
    for report_date, holders in shs.items():
        for k, stype in type_map.items():
            if not holders.get(k):
                continue
            for row in holders[k]["top_holders"]:
                entry = row.copy()
                entry["date"] = pd.to_datetime(last_of_month(datetime.strptime(report_date, "%Y%m")))
                entry["stype"] = stype
                records.append(entry)

    df = pd.DataFrame(records)
    if df.empty:
        return {
            "symbol": symbol,
            "rows": category_rows,
            "category_rows": category_rows,
            "top_holder_rows": 0,
        }

    df["symbol"] = symbol
    df = df.drop_duplicates(subset=["symbol", "date", "name", "stype"], keep="last")
    df = filter_by_date_range(df, from_date, to_date)
    if df.empty:
        return {
            "symbol": symbol,
            "rows": category_rows,
            "category_rows": category_rows,
            "top_holder_rows": 0,
        }
    df = attach_company_master_id(df, ticker_column="symbol", exchange="NSE")
    upsert_to_db(
        df,
        "shareholding_top_holders",
        unique_keys=["symbol", "date", "name", "stype"],
        timescaledb_column="date",
    )
    top_holder_rows = int(len(df))
    return {
        "symbol": symbol,
        "rows": int(category_rows + top_holder_rows),
        "category_rows": category_rows,
        "top_holder_rows": top_holder_rows,
    }


def get_historical_mcap(symbol: str, from_date: datetime | None = None, to_date: datetime | None = None) -> dict[str, object]:
    json_data = {
        "stock": symbol,
        "metric_code": "mcap",
        "frequency": "D",
        "start_date": from_date.strftime("%Y-%m-%d") if from_date else None,
        "end_date": to_date.strftime("%Y-%m-%d") if to_date else None,
    }
    resp = get_with_retries(
        "https://pyapiv2.mintbox.ai/api/core/getHistoricalMetricData",
        headers=HEADERS,
        method="POST",
        json_data=json_data,
    ).json()
    mcap_data = json.loads(resp)

    records = []
    for row in mcap_data:
        records.append({"date": row["timestamp"], "mcap": row["mcap"]})

    df = pd.DataFrame(records)
    if df.empty:
        return {"symbol": symbol, "rows": 0}
    df["date"] = pd.to_datetime(df["date"])
    df["mcap"] = pd.to_numeric(df["mcap"], errors="coerce")
    df["symbol"] = symbol
    df = filter_by_date_range(df, from_date, to_date)
    if df.empty:
        return {"symbol": symbol, "rows": 0}
    df = attach_company_master_id(df, ticker_column="symbol", exchange="NSE")
    upsert_to_db(
        df,
        "historical_mcap",
        unique_keys=["symbol", "date"],
        timescaledb_column="date",
    )
    return {"symbol": symbol, "rows": int(len(df))}


def sync_sharpely_data(symbols: list[str], from_date: datetime | None = None, to_date: datetime | None = None) -> dict[str, object]:
    effective_from_date, to_date = normalize_date_window(from_date, to_date)
    snapshot_target = pd.Timestamp(to_date)
    if snapshot_target.tzinfo is not None:
        snapshot_target = snapshot_target.tz_convert("UTC").tz_localize(None)
    snapshot_target = snapshot_target.normalize()
    to_date = snapshot_target.to_pydatetime()
    normalized_symbols = [str(symbol).strip().upper() for symbol in symbols if str(symbol or "").strip()]
    summary: dict[str, object] = {
        "source": "sharpely_fundamentals",
        "symbols": normalized_symbols[:100],
        "symbol_count": len(normalized_symbols),
        "from_date": pd.Timestamp(effective_from_date).date().isoformat() if effective_from_date else None,
        "to_date": pd.Timestamp(to_date).date().isoformat() if to_date else None,
        "rows": 0,
        "rows_read": len(normalized_symbols),
        "rows_written": 0,
        "meta_rows": 0,
        "peer_rows": 0,
        "statement_rows": 0,
        "shareholding_rows": 0,
        "historical_mcap_rows": 0,
        "meta_refreshed_count": 0,
        "peer_refreshed_count": 0,
        "statement_refreshed_count": 0,
        "shareholding_refreshed_count": 0,
        "mcap_refreshed_count": 0,
        "failed_symbol_count": 0,
        "failed_symbols": [],
        "classification_counts": {},
        "no_data_count": 0,
        "source_unavailable_count": 0,
        "auth_unavailable_count": 0,
        "parse_failed_count": 0,
        "symbols_skipped_no_work": [],
        "fallback_used": False,
        "state_advanced": False,
    }
    failures: list[dict[str, object]] = []

    for symbol in normalized_symbols:
        try:
            symbol_work_count = 0
            meta_max_date = get_db_max_date(
                SHARPELY_STOCK_META_TABLE,
                date_column="as_on_date",
                filters={"symbol": symbol},
            )
            peers_max_date = get_db_max_date(
                SHARPELY_STOCK_PEERS_TABLE,
                date_column="as_on_date",
                filters={"anchor_symbol": symbol},
            )
            meta_max_ts = pd.Timestamp(meta_max_date).normalize() if meta_max_date is not None else None
            peers_max_ts = pd.Timestamp(peers_max_date).normalize() if peers_max_date is not None else None
            # Meta/peers are slow-changing classification+valuation snapshots (sector codes,
            # style boxes, peer groups); price/mcap freshness comes from the bhavcopy. A
            # weekly snapshot is the default -- daily re-fetches were ~all API cost, no signal.
            refresh_days = max(1, int(env.int("SHARPELY_SNAPSHOT_REFRESH_DAYS", 7)))
            snapshot_floor = snapshot_target - pd.Timedelta(days=refresh_days - 1)
            need_meta_snapshot = meta_max_ts is None or meta_max_ts < snapshot_floor
            need_peers_snapshot = peers_max_ts is None or peers_max_ts < snapshot_floor
            cached_meta: dict | None = None
            if need_meta_snapshot or need_peers_snapshot:
                meta_payload = get_stock_meta(symbol)
                meta_df = save_stock_meta(symbol, meta_payload)
                if not meta_df.empty:
                    raw = meta_df.iloc[-1].get("raw_json")
                    cached_meta = json.loads(raw) if isinstance(raw, str) else None
                meta_rows = int(len(meta_df))
                summary["meta_rows"] = int(summary["meta_rows"]) + meta_rows
                summary["meta_refreshed_count"] = int(summary["meta_refreshed_count"]) + 1
                symbol_work_count += meta_rows
            if need_peers_snapshot:
                peer_payload = get_stock_peers(symbol, meta=cached_meta)
                peer_df = save_stock_peers(symbol, cached_meta or {}, peer_payload)
                peer_rows = int(len(peer_df))
                summary["peer_rows"] = int(summary["peer_rows"]) + peer_rows
                summary["peer_refreshed_count"] = int(summary["peer_refreshed_count"]) + 1
                symbol_work_count += peer_rows

            stmt_from_date = choose_from_date(
                from_date,
                [
                    get_db_max_date("stmt_income", filters={"symbol": symbol}),
                    get_db_max_date("stmt_balancesheet", filters={"symbol": symbol}),
                    get_db_max_date("stmt_cashflow", filters={"symbol": symbol}),
                ],
            )
            if stmt_from_date <= to_date:
                statement_result = get_financial_statement(symbol, stmt_from_date, to_date)
                statement_rows = int(statement_result.get("rows") or 0)
                summary["statement_rows"] = int(summary["statement_rows"]) + statement_rows
                summary["statement_refreshed_count"] = int(summary["statement_refreshed_count"]) + 1
                symbol_work_count += statement_rows

            shareholding_from_date = choose_from_date(
                from_date,
                [
                    get_db_max_date("shareholding_category", filters={"symbol": symbol}),
                    get_db_max_date("shareholding_top_holders", filters={"symbol": symbol}),
                ],
            )
            if shareholding_from_date <= to_date:
                shareholding_result = get_shareholding(symbol, shareholding_from_date, to_date)
                shareholding_rows = int(shareholding_result.get("rows") or 0)
                summary["shareholding_rows"] = int(summary["shareholding_rows"]) + shareholding_rows
                summary["shareholding_refreshed_count"] = int(summary["shareholding_refreshed_count"]) + 1
                symbol_work_count += shareholding_rows

            mcap_from_date = choose_from_date(
                from_date,
                [get_db_max_date("historical_mcap", filters={"symbol": symbol})],
            )
            if mcap_from_date <= to_date:
                mcap_result = get_historical_mcap(symbol, mcap_from_date, to_date)
                mcap_rows = int(mcap_result.get("rows") or 0)
                summary["historical_mcap_rows"] = int(summary["historical_mcap_rows"]) + mcap_rows
                summary["mcap_refreshed_count"] = int(summary["mcap_refreshed_count"]) + 1
                symbol_work_count += mcap_rows
            if symbol_work_count <= 0:
                skipped = list(summary["symbols_skipped_no_work"])
                if len(skipped) < 100:
                    skipped.append(symbol)
                summary["symbols_skipped_no_work"] = skipped
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}"
            classification = classify_sharpely_sync_error(error_text)
            failures.append({"symbol": symbol, "classification": classification, "error": error_text})
            _record_sharpely_fallback(
                fallback_type="sharpely_symbol_sync_failed",
                reason="Sharpely fundamentals sync failed for one symbol; the batch continued with classified run-state.",
                error=exc,
                symbol=symbol,
                metadata={"classification": classification},
            )
    rows_written = (
        int(summary["meta_rows"])
        + int(summary["peer_rows"])
        + int(summary["statement_rows"])
        + int(summary["shareholding_rows"])
        + int(summary["historical_mcap_rows"])
    )
    summary["rows"] = rows_written
    summary["rows_written"] = rows_written
    failure_classifications = [str(row.get("classification") or "failed") for row in failures]
    classification_counts = {key: failure_classifications.count(key) for key in sorted(set(failure_classifications))}
    summary["classification"] = classify_sharpely_run_state(
        rows_written=rows_written,
        failures=failures,
        symbol_count=len(normalized_symbols),
    )
    summary["status"] = "ok" if summary["classification"] in {"ok", "no_data"} else "failed"
    summary["failed_symbol_count"] = len(failures)
    summary["failed_symbols"] = [str(row.get("symbol") or "") for row in failures[:100]]
    summary["classification_counts"] = classification_counts
    summary["source_unavailable_count"] = int(classification_counts.get("source_unavailable", 0))
    summary["auth_unavailable_count"] = int(classification_counts.get("auth_unavailable", 0))
    summary["parse_failed_count"] = int(classification_counts.get("parse_failed", 0))
    summary["no_data_count"] = len(summary["symbols_skipped_no_work"]) if rows_written <= 0 else 0
    summary["state_advanced"] = rows_written > 0
    return summary


def main() -> int:
    global STOCKEY_RUN_STATE
    parser = argparse.ArgumentParser(description="Sync Sharpely fundamentals for tracked symbols")
    parser.add_argument("--symbols", nargs="*", help="Symbols, comma-separated or repeated")
    parser.add_argument("--from-date", dest="from_date", help="Start date in YYYY-MM-DD")
    parser.add_argument("--to-date", dest="to_date", help="End date in YYYY-MM-DD")
    args = parser.parse_args()

    symbols = load_tracked_symbols(args.symbols)
    if not symbols:
        raise SystemExit("No symbols provided. Use --symbols, STOCKEY_SYMBOLS, or config/tracked_symbols.txt")

    STOCKEY_RUN_STATE = sync_sharpely_data(
        symbols=symbols,
        from_date=parse_datetime_arg(args.from_date),
        to_date=parse_datetime_arg(args.to_date),
    )
    print(json.dumps({"status": STOCKEY_RUN_STATE.get("status", "ok"), **STOCKEY_RUN_STATE}, ensure_ascii=False, default=str), flush=True)
    return 1 if STOCKEY_RUN_STATE.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
