import json
from datetime import datetime

import pandas as pd
from environs import Env

from utils.db import upsert_to_db
from utils.http import get_with_retries
from utils.date import last_of_month

from . import sharpely_db as sdb
from . import sharpely_utils as su

env = Env()
env.read_env()
HEADERS = su.get_sharpely_headers()


def get_financial_statement(symbol):
    resp = get_with_retries(
        f"https://pyapiv2.mintbox.ai/api/core/getFinancialStatementsV2/ticker={symbol}",
        headers=HEADERS,
    ).json()
    fin = json.loads(resp["statements"])

    # Step 2: Filter FCC codes you care about (add/remove as needed)
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
        "SINTEX": "interest_expense",  # same meaning as SIEN
        "SOPEX": "operating_expenses",  # generic form, same as SOET in some cases
    }

    income_df = parse_consolidated_statement(
        symbol, fin["inc_consol_interim"], income_fccs
    )

    balance_fccs = {
        # --- Universal drivers (all sectors) ---
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
        # --- Product / Manufacturing-heavy ---
        "SCLR": "short_term_loans_and_receivables",
        "SCLD": "capitalized_leases_current_portion",
        "SLCL": "capitalized_lease_obligations_long_term",
        "SDTA": "deferred_tax_asset_long_term",
        "STCAXIN": "total_current_assets_ex_inventories",
        # --- Services / SaaS / IP-driven ---
        "STIN": "intangible_assets_ex_goodwill_net",
        "STFL": "finance_and_operating_lease_liabilities",
        "STDL": "lease_debt_including_liabilities",
        "SACRU": "accruals_short_term",
        # --- Financial Services / FinTech / Banks ---
        "SLNS": "loans_short_term",
        "SLNG": "loans_long_term",
        "STIV": "total_investments",
        "SDTX": "deferred_investment_tax_credits_long_term",
        "SINBL": "interest_bearing_liabilities_total",
        "SSND": "net_debt",
    }

    balance_df = parse_consolidated_statement(
        symbol, fin["bal_consol_interim"], balance_fccs
    )

    cashflow_fccs = {
        # ── Operating activities ─────────────────────────────────────────
        "SPLS": "net_income_starting_line",  # Profit / (loss) starting point
        "SNCR": "non_cash_adjustments",  # All non-cash add-backs excl. D&A
        "SDAI": "depreciation_and_amortization",  # Depreciation, depletion & amort
        "SCWC": "change_in_working_capital",  # ∆ working capital (±)
        "STLO": "net_cash_from_operating_activities",  # Operating cash flow (OCF)
        # ── Investing activities ────────────────────────────────────────
        "SCAP": "capital_expenditures_net",  # Net CapEx (PPE & intangibles)
        "SBAS": "acquisition_or_disposal_of_business",  # M&A cash flows
        "STLI": "net_cash_from_investing_activities",  # Total investing CF
        # ── Financing activities ────────────────────────────────────────
        "SCDP": "dividends_paid_total",  # All cash dividends
        "SCSBN": "common_stock_buyback_net",  # Share repurchase / issuance net
        "SPSS": "stock_issuance_retirement_net",  # Stock issued / retired (all classes)
        "SPRD": "debt_issuance_retirement_total",  # Net debt movement (LT + ST)
        "STLF": "net_cash_from_financing_activities",  # Total financing CF
        # ── Free-cash metrics (supplemental) ────────────────────────────
        "SFCFO": "free_operating_cash_flow",  # FOCF after dividends
        "SFCFE": "free_cash_flow_to_equity",  # FCFE (for DDM / levered DCF)
        "SFCFL": "free_operating_cash_flow_gross",  # FOCF before dividends
        # ── Cash reconciliation ─────────────────────────────────────────
        "SNCC": "net_change_in_cash",  # Δ cash
        "SNCB": "cash_beginning_balance",
        "SNCE": "cash_ending_balance",
    }

    cashflow_df = parse_consolidated_statement(
        symbol, fin["cas_consol_interim"], cashflow_fccs
    )

    for dbname, df in zip(
        ["stmt_income", "stmt_balancesheet", "stmt_cashflow"],
        [income_df, balance_df, cashflow_df],
    ):
        df["date"] = pd.to_datetime(df["date"])
        upsert_to_db(
            df,
            dbname,
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
    df = df_long.pivot_table(
        index=["date", "period_length"], columns="metric", values="value"
    ).reset_index()
    df["symbol"] = symbol
    return df


def get_shareholding(symbol):
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
    df['symbol'] = symbol
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
            for row in holders[k]['top_holders']:
                entry = row.copy()
                entry["date"] = pd.to_datetime(last_of_month(datetime.strptime(report_date, "%Y%m")))
                entry['stype'] = stype
                records.append(entry)

    df = pd.DataFrame(records)
    df['symbol'] = symbol
    df = df[df.duplicated(subset=["symbol", "date", "name", "stype"], keep='first')]
    upsert_to_db(
        df,
        "shareholding_top_holders",
        unique_keys=["symbol", "date", "name", "stype"],
        timescaledb_column="date",
    )

def get_historical_mcap(symbol):
    json_data = {
        "stock": symbol,
        "metric_code": "mcap",
        "frequency": "D",
        "start_date": None,
        "end_date": None,
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
        entry = {}
        entry["date"] = row["timestamp"]
        entry["mcap"] = row["mcap"]
        records.append(entry)

    df = pd.DataFrame(records)
    df["date"] = pd.to_datetime(df["date"])
    df["symbol"] = symbol
    upsert_to_db(
        df,
        "historical_mcap",
        unique_keys=["symbol", "date"],
        timescaledb_column="date"
    )


if __name__ == "__main__":
    for symbol in ["SHAKTIPUMP", "HDFCBANK"]:
        get_historical_mcap(symbol)
        get_financial_statement(symbol)
        get_shareholding(symbol)
