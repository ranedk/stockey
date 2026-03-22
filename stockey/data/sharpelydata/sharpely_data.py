import argparse
import json
from datetime import datetime

import pandas as pd
from environs import Env

from utils.company_master import attach_company_master_id
from utils.db import upsert_to_db
from utils.http import get_with_retries
from utils.date import last_of_month
from utils.sync import choose_from_date, get_db_max_date, load_tracked_symbols, normalize_date_window, parse_datetime_arg

from . import sharpely_utils as su

env = Env()
env.read_env()
HEADERS = su.get_sharpely_headers()


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


def get_financial_statement(symbol: str, from_date: datetime | None = None, to_date: datetime | None = None):
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

    for table_name, df in table_map.items():
        if df.empty:
            continue
        df["date"] = pd.to_datetime(df["date"])
        df = filter_by_date_range(df, from_date, to_date)
        if df.empty:
            continue
        df = attach_company_master_id(df, ticker_column="symbol", exchange="NSE")
        upsert_to_db(
            df,
            table_name,
            unique_keys=["symbol", "date", "period_length"],
            timescaledb_column="date",
        )


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


def get_shareholding(symbol: str, from_date: datetime | None = None, to_date: datetime | None = None):
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
        return

    df["symbol"] = symbol
    df = df.drop_duplicates(subset=["symbol", "date", "name", "stype"], keep="last")
    df = filter_by_date_range(df, from_date, to_date)
    if df.empty:
        return
    df = attach_company_master_id(df, ticker_column="symbol", exchange="NSE")
    upsert_to_db(
        df,
        "shareholding_top_holders",
        unique_keys=["symbol", "date", "name", "stype"],
        timescaledb_column="date",
    )


def get_historical_mcap(symbol: str, from_date: datetime | None = None, to_date: datetime | None = None):
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
        return
    df["date"] = pd.to_datetime(df["date"])
    df["mcap"] = pd.to_numeric(df["mcap"], errors="coerce")
    df["symbol"] = symbol
    df = filter_by_date_range(df, from_date, to_date)
    if df.empty:
        return
    df = attach_company_master_id(df, ticker_column="symbol", exchange="NSE")
    upsert_to_db(
        df,
        "historical_mcap",
        unique_keys=["symbol", "date"],
        timescaledb_column="date",
    )


def sync_sharpely_data(symbols: list[str], from_date: datetime | None = None, to_date: datetime | None = None):
    _, to_date = normalize_date_window(from_date, to_date)

    for symbol in symbols:
        stmt_from_date = choose_from_date(
            from_date,
            [
                get_db_max_date("stmt_income", filters={"symbol": symbol}),
                get_db_max_date("stmt_balancesheet", filters={"symbol": symbol}),
                get_db_max_date("stmt_cashflow", filters={"symbol": symbol}),
            ],
        )
        if stmt_from_date <= to_date:
            get_financial_statement(symbol, stmt_from_date, to_date)

        shareholding_from_date = choose_from_date(
            from_date,
            [
                get_db_max_date("shareholding_category", filters={"symbol": symbol}),
                get_db_max_date("shareholding_top_holders", filters={"symbol": symbol}),
            ],
        )
        if shareholding_from_date <= to_date:
            get_shareholding(symbol, shareholding_from_date, to_date)

        mcap_from_date = choose_from_date(
            from_date,
            [get_db_max_date("historical_mcap", filters={"symbol": symbol})],
        )
        if mcap_from_date <= to_date:
            get_historical_mcap(symbol, mcap_from_date, to_date)


def main():
    parser = argparse.ArgumentParser(description="Sync Sharpely fundamentals for tracked symbols")
    parser.add_argument("--symbols", nargs="*", help="Symbols, comma-separated or repeated")
    parser.add_argument("--from-date", dest="from_date", help="Start date in YYYY-MM-DD")
    parser.add_argument("--to-date", dest="to_date", help="End date in YYYY-MM-DD")
    args = parser.parse_args()

    symbols = load_tracked_symbols(args.symbols)
    if not symbols:
        raise SystemExit("No symbols provided. Use --symbols, STOCKEY_SYMBOLS, or config/tracked_symbols.txt")

    sync_sharpely_data(
        symbols=symbols,
        from_date=parse_datetime_arg(args.from_date),
        to_date=parse_datetime_arg(args.to_date),
    )


if __name__ == "__main__":
    main()
