import json
from utils.http import get_with_retries, get_dynamic_headers
from utils.duck import upsert_to_duckdb_auto
from . import sharpely_utils as su
import pandas as pd
from environs import Env


env = Env()
env.read_env()
HEADERS = su.get_sharpely_headers()


def get_financial_statement(ticker):
    resp = get_with_retries(
        f"https://pyapiv2.mintbox.ai/api/core/getFinancialStatementsV2/ticker={ticker}",
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
        ticker, fin["inc_consol_interim"], income_fccs
    )

    balance_fccs = balance_sheet_fcc_map = {
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
        ticker, fin["bal_consol_interim"], balance_fccs
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
        ticker, fin["cas_consol_interim"], cashflow_fccs
    )

    for dbname, df in zip(
        ["income", "balancesheet", "cashflow"], [income_df, balance_df, cashflow_df]
    ):
        upsert_to_duckdb_auto(
            df,
            env("DUCKDB"),
            env("SCHEMA"),
            dbname,
            unique_keys=["ticker", "period_end_date", "period_length"],
        )


def parse_consolidated_statement(ticker, data, fccs):
    headers = {h["period_end_date"]: h["period_length"] for h in data["header"]}
    periods = data["statement"]["columns"]

    # Step 1: Create FCC to metric name map
    fcc_map = {entry["FCC"]: entry["lseg_name"] for entry in data["fcc_map"]}

    # Step 3: Extract relevant data rows
    rows = []
    for item in data["statement"]["data"]:
        fcc_code = item[0]
        if fcc_code in fccs:
            values = item[1:]
            for i, period in enumerate(periods):
                rows.append(
                    {
                        "period_end_date": period,
                        "period_length": headers.get(period),
                        "metric": fccs[fcc_code],
                        "value": values[i] if i < len(values) else None,
                    }
                )

    # Step 4: Convert to DataFrame and pivot
    df_long = pd.DataFrame(rows)
    df = df_long.pivot_table(
        index=["period_end_date", "period_length"], columns="metric", values="value"
    ).reset_index()
    df["ticker"] = ticker
    return df


"""

Use this to get lseg_instrument_id to

https://pyapiv2.mintbox.ai/api/core/getStockProfile/symbol=SHAKTIPUMP
https://pyapiv2.mintbox.ai/api/core/stock_insights_detailed/ticker=SHAKTIPUMP
https://pyapiv2.mintbox.ai/api/core/getCorporateActionsV2/lseg_instrument_id=8590071662

"""


def get_shareholding(ticker):
    resp = get_with_retries(
        f"https://pyapiv2.mintbox.ai/api/core/getShareHoldingsDataV1/symbol={ticker}",
        headers=HEADERS,
    ).json()
    shs = json.loads(resp["shareholdings"])

    records = []
    for report_date, holders in shs.items():
        for row in holders["data"]:
            entry = row.copy()
            entry["report_date"] = pd.to_datetime(report_date)
            records.append(entry)

    df = pd.DataFrame(records)
    df = df.rename(columns={"symbol": "ticker"})
    upsert_to_duckdb_auto(
        df,
        env("DUCKDB"),
        env("SCHEMA"),
        "shareholding_category",
        unique_keys=["ticker", "report_date", "category_code"],
    )

    records = []
    for report_date, holders in shs.items():
        for row in holders["top_holders"]:
            entry = row.copy()
            entry["report_date"] = pd.to_datetime(report_date)
            records.append(entry)

    df = pd.DataFrame(records)
    df = df.rename(columns={"symbol": "ticker"})
    upsert_to_duckdb_auto(
        df,
        env("DUCKDB"),
        env("SCHEMA"),
        "shareholding_top_holders",
        unique_keys=["ticker", "report_date", "holder"],
    )


if __name__ == "__main__":
    get_shareholding("HDFCBANK")
