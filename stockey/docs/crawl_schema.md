# Schema of crawled data

This file is a point-in-time dump from one database, not a canonical migration source. If live table types drift from code, regenerate it with `python -m utils.db_schema_dump --schemas public`.

```
public.eaindustry_wpi
  - date: timestamp with time zone NOT NULL
  - value: double precision
  - cname: text
  - name: text
  # Indexes
    eaindustry_wpi_date_cname_key: UNIQUE (date, cname)
    eaindustry_wpi_date_idx: (date)
    idx_eaindustry_wpi_date_cname: UNIQUE (date, cname)

public.fbil_gsec_par
  - date: timestamp with time zone NOT NULL
  - tenor_years: double precision
  - par_yield_sa: double precision
  - par_yield_ann: double precision
  # Indexes
    fbil_gsec_par_date_idx: (date)
    fbil_gsec_par_date_tenor_years_key: UNIQUE (date, tenor_years)
    idx_fbil_gsec_par_date_tenor_years: UNIQUE (date, tenor_years)

public.fbil_gsec_quote
  - date: timestamp with time zone NOT NULL
  - isin: text
  - coupon_pct: double precision
  - maturity_date: timestamp with time zone
  - clean_price: double precision
  - ytm_sa: double precision
  - remark1: text
  - remark2: text
  - liquidity_signal: text
  # Indexes
    fbil_gsec_quote_date_idx: (date)
    fbil_gsec_quote_date_isin_key: UNIQUE (date, isin)
    idx_fbil_gsec_quote_date_isin: UNIQUE (date, isin)

public.fii_derivatives
  - date: timestamp with time zone NOT NULL
  - instrument: text
  - buy_number_of_contracts: text
  - buy_amount: text
  - sell_number_of_contracts: text
  - sell_amount: text
  - open_interest_eod_number_of_contracts: text
  - open_interest_eod_amount: text
  # Indexes
    fii_derivatives_date_idx: (date)
    fii_derivatives_date_instrument_key: UNIQUE (date, instrument)
    idx_fii_derivatives_date_instrument: UNIQUE (date, instrument)

public.fii_investments
  - date: timestamp with time zone NOT NULL
  - gross_purchases_inr_crore: text
  - gross_sales_inr_crore: text
  - net_investment_inr_crore: text
  - net_investment_usd_million: text
  - instrument: text
  # Indexes
    fii_investments_date_idx: (date)
    fii_investments_date_instrument_key: UNIQUE (date, instrument)
    idx_fii_investments_date_instrument: UNIQUE (date, instrument)

public.historical_mcap
  - date: timestamp with time zone NOT NULL
  - mcap: double precision
  - symbol: text
  # Indexes
    historical_mcap_date_idx: (date)
    historical_mcap_symbol_date_key: UNIQUE (symbol, date)
    idx_historical_mcap_symbol_date: UNIQUE (symbol, date)

public.macro_india_gdp
  - date: timestamp with time zone NOT NULL
  - india_gdp: double precision
  # Indexes
    idx_macro_india_gdp_date: UNIQUE (date)
    macro_india_gdp_date_key: UNIQUE (date)

public.macro_usa
  - date: timestamp with time zone NOT NULL
  - ust10y_yield: double precision
  - fedfunds_eff: double precision
  - vix_close: double precision
  - nonfarm_payrolls: double precision
  - cpi_headline: double precision
  - cpi_core: double precision
  - broad_usd_index: double precision
  - wti_crude_spot: double precision
  - inr_usd_spot: double precision
  # Indexes
    idx_macro_usa_date: UNIQUE (date)
    macro_usa_date_key: UNIQUE (date)

public.macro_usa_ism
  - date: timestamp with time zone NOT NULL
  - for_month: text
  - actual: double precision
  - revised: double precision
  - consensus: double precision
  - previous: double precision
  - isPreliminary: boolean
  - isBetterThanExpected: text
  - hasHistorical: boolean
  # Indexes
    idx_macro_usa_ism_date: UNIQUE (date)
    macro_usa_ism_date_key: UNIQUE (date)

public.master_dhan_instruments
  - exch_id: character varying
  - segment: character varying PK NOT NULL
  - security_id: bigint PK NOT NULL
  - isin: character varying
  - instrument: character varying
  - underlying_security_id: bigint
  - underlying_symbol: character varying
  - symbol_name: character varying
  - display_name: character varying
  - instrument_type: character varying
  - series: character varying
  - lot_size: double precision
  - sm_expiry_date: date
  - strike_price: double precision
  - option_type: character varying
  - tick_size: double precision
  - expiry_flag: character varying
  - bracket_flag: character varying
  - cover_flag: character varying
  - asm_gsm_flag: character varying
  - asm_gsm_category: character varying
  - buy_sell_indicator: character varying
  - buy_co_min_margin_per: double precision
  - sell_co_min_margin_per: double precision
  - buy_co_sl_range_max_perc: double precision
  - sell_co_sl_range_max_perc: double precision
  - buy_co_sl_range_min_perc: double precision
  - sell_co_sl_range_min_perc: double precision
  - buy_bo_min_margin_per: double precision
  - sell_bo_min_margin_per: double precision
  - buy_bo_sl_range_max_perc: double precision
  - sell_bo_sl_range_max_perc: double precision
  - buy_bo_sl_range_min_perc: double precision
  - sell_bo_sl_min_range: double precision
  - buy_bo_profit_range_max_perc: double precision
  - sell_bo_profit_range_max_perc: double precision
  - buy_bo_profit_range_min_perc: double precision
  - sell_bo_profit_range_min_perc: double precision
  - mtf_leverage: double precision
  - valid_from: timestamp without time zone PK NOT NULL
  - valid_to: timestamp without time zone
  - load_ts: timestamp without time zone NOT NULL
  # Indexes
    idx_master_dhan_active: (security_id, segment, valid_to)
    master_dhan_instruments_pkey: UNIQUE (security_id, segment, valid_from)

public.master_sharpely_equity
  - sharpely_id: text
  - symbol: text
  - bse_ticker: text
  - proper_name: text
  - lseg_instrument_id: text
  - nse_active: double precision
  - nse_segment: text
  - bse_segment: text
  - sector_code: text
  # Indexes
    idx_master_sharpely_equity_symbol_bse_ticker: UNIQUE (symbol, bse_ticker)
    master_sharpely_equity_symbol_bse_ticker_key: UNIQUE (symbol, bse_ticker)

public.company_master
  - company_master_id: text
  - company_name: text
  - sharpely_id: text
  - dhan_bse_id: bigint
  - dhan_nse_id: bigint
  - bse_ticker: text
  - nse_ticker: text
  # Indexes
    idx_company_master_company_master_id: UNIQUE (company_master_id)

public.master_sharpely_funds
  - plan_id: bigint
  - isin_code: text
  - amfi_code: text
  - sharpely_id: text
  - regular_plan_id: text
  - nse_symbol: text
  - bse_symbol: text
  - bse_scheme_code: text
  - basic_name: text
  - is_index_fund: bigint
  - is_etf_fund: bigint
  - is_fof: bigint
  - is_dividend: bigint
  - variant: bigint
  - variant_fund_id: bigint
  - amc_full_name: text
  - category_id: text
  - category_name: text
  - sharpely_bm_id: double precision
  - plan_name: text
  - type_id: bigint
  - is_direct_plan: bigint
  - objective_text: text
  # Indexes
    idx_master_sharpely_funds_amfi_code: UNIQUE (amfi_code)
    master_sharpely_funds_amfi_code_key: UNIQUE (amfi_code)

public.nseindia_corporate_actions
  - company_master_id: text
  - symbol: text
  - series: text
  - face_value: text
  - subject: text
  - date: timestamp with time zone NOT NULL
  - record_date: timestamp with time zone
  - start_date: timestamp with time zone
  - end_date: timestamp with time zone
  - nd_start_date: timestamp with time zone
  - nd_end_date: timestamp with time zone
  - company: text
  - isin: text
  - ca_broadcast_date: timestamp with time zone
  # Indexes
    idx_nseindia_corporate_actions_date_symbol: UNIQUE (date, symbol)
    nseindia_corporate_actions_date_idx: (date)
    nseindia_corporate_actions_date_symbol_key: UNIQUE (date, symbol)

public.nseindia_earnings_events
  - company_master_id: text
  - symbol: text
  - company: text
  - industry: text
  - audited: text
  - cumulative: text
  - period: text
  - financial_year: text
  - seq_number: text
  - bank: text
  - from_date: timestamp with time zone
  - to_date: timestamp with time zone
  - reporting_date: timestamp with time zone
  - consolidated: text
  - isin: text
  - date: timestamp with time zone NOT NULL
  # Indexes
    idx_nseindia_earnings_events_date_symbol_reporting_date_period: UNIQUE (date, symbol, reporting_date, period)
    nseindia_earnings_events_date_idx: (date)
    nseindia_earnings_events_date_symbol_reporting_date_period_key: UNIQUE (date, symbol, reporting_date, period)

public.nseindia_indices
  - index_name: text
  - date: timestamp with time zone NOT NULL
  - open: double precision
  - high: double precision
  - low: double precision
  - close: double precision
  - points_change: double precision
  - percent_change: double precision
  - volume: double precision
  - turnover_cr: double precision
  - pe: double precision
  - pb: double precision
  - div_yield: double precision
  # Indexes
    idx_nseindia_indices_close_date_index_name: UNIQUE (date, index_name)
    nseindia_indices_close_date_idx: (date)
    nseindia_indices_close_date_index_name_key: UNIQUE (date, index_name)

public.nseindia_insider_deals
  - disclosure_id: text
  - person_id: text
  - symbol: text
  - insider_name: text
  - person_category: text
  - transaction_type: text
  - quantity: bigint
  - value_inr: double precision
  - trade_date_from: timestamp with time zone
  - trade_date_to: timestamp with time zone
  - date: timestamp with time zone NOT NULL
  - acq_mode: text
  - security_type: text
  - derivative_type: text
  - exchange: text
  - holding_pct_before: double precision
  - holding_pct_after: double precision
  - holding_shares_before: text
  - holding_shares_after: text
  - reporting_date: timestamp with time zone
  # Indexes
    idx_nseindia_insider_deals_disclosure_id_person_id_date_symbol_: UNIQUE (disclosure_id, person_id, date, symbol, insider_name, transaction_type, holding_shares_after, holding_pct_before)
    nseindia_insider_deals_date_idx: (date)
    nseindia_insider_deals_disclosure_id_person_id_date_symbol__key: UNIQUE (disclosure_id, person_id, date, symbol, insider_name, transaction_type, holding_shares_after, holding_pct_before)

public.rbi_bank_rates
  - bank_rate: double precision
  - repo_rate: double precision
  - reverse_repo_rate: double precision
  - sdf_rate: double precision
  - msf_rate: double precision
  - crr: double precision
  - slr: double precision
  - date: timestamp with time zone NOT NULL
  # Indexes
    idx_rbi_bank_rates_date: UNIQUE (date)
    rbi_bank_rates_date_key: UNIQUE (date)

public.shareholding_category
  - symbol: text
  - sh_code: text
  - sh_per: double precision
  - DispOrder: double precision
  - Description: text
  - shp_cat_id: double precision
  - Bold: double precision
  - date: timestamp with time zone NOT NULL
  # Indexes
    idx_shareholding_category_symbol_date_sh_code: UNIQUE (symbol, date, sh_code)
    shareholding_category_date_idx: (date)
    shareholding_category_symbol_date_sh_code_key: UNIQUE (symbol, date, sh_code)

public.shareholding_top_holders
  - name: text
  - shp_cat_id: bigint
  - percentage: double precision
  - pledge_encumbered_percentage: double precision
  - date: timestamp with time zone NOT NULL
  - stype: text
  - symbol: text
  # Indexes
    idx_shareholding_top_holders_symbol_date_name_stype: UNIQUE (symbol, date, name, stype)
    shareholding_top_holders_date_idx: (date)
    shareholding_top_holders_symbol_date_name_stype_key: UNIQUE (symbol, date, name, stype)

public.stmt_balancesheet
  - date: timestamp with time zone NOT NULL
  - period_length: text
  - accounts_payable: double precision
  - accounts_receivable: double precision
  - accruals_short_term: double precision
  - accrued_expenses: double precision
  - capitalized_lease_obligations_long_term: double precision
  - capitalized_leases_current_portion: double precision
  - cash_and_cash_equivalents: double precision
  - deferred_investment_tax_credits_long_term: double precision
  - deferred_tax_asset_long_term: double precision
  - finance_and_operating_lease_liabilities: double precision
  - intangible_assets_ex_goodwill_net: double precision
  - intangible_assets_net: double precision
  - interest_bearing_liabilities_total: double precision
  - lease_debt_including_liabilities: double precision
  - loans_long_term: double precision
  - loans_short_term: double precision
  - long_term_debt: double precision
  - net_debt: double precision
  - other_non_current_liabilities_total: double precision
  - property_plant_equipment_net: double precision
  - retained_earnings: double precision
  - short_term_debt_and_cpltd: double precision
  - short_term_investments: double precision
  - short_term_loans_and_receivables: double precision
  - total_assets: double precision
  - total_current_assets: double precision
  - total_current_assets_ex_inventories: double precision
  - total_current_liabilities: double precision
  - total_inventories: double precision
  - total_investments: double precision
  - total_liabilities: double precision
  - total_liabilities_and_equity: double precision
  - total_non_current_assets: double precision
  - total_shareholders_equity: double precision
  - symbol: text
  # Indexes
    idx_stmt_balancesheet_symbol_date_period_length: UNIQUE (symbol, date, period_length)
    stmt_balancesheet_date_idx: (date)
    stmt_balancesheet_symbol_date_period_length_key: UNIQUE (symbol, date, period_length)

public.stmt_cashflow
  - date: timestamp with time zone NOT NULL
  - period_length: text
  - acquisition_or_disposal_of_business: double precision
  - capital_expenditures_net: double precision
  - cash_beginning_balance: double precision
  - cash_ending_balance: double precision
  - change_in_working_capital: double precision
  - common_stock_buyback_net: double precision
  - debt_issuance_retirement_total: double precision
  - depreciation_and_amortization: double precision
  - dividends_paid_total: double precision
  - free_cash_flow_to_equity: double precision
  - free_operating_cash_flow: double precision
  - free_operating_cash_flow_gross: double precision
  - net_cash_from_financing_activities: double precision
  - net_cash_from_investing_activities: double precision
  - net_cash_from_operating_activities: double precision
  - net_change_in_cash: double precision
  - net_income_starting_line: double precision
  - non_cash_adjustments: double precision
  - stock_issuance_retirement_net: double precision
  - symbol: text
  # Indexes
    idx_stmt_cashflow_symbol_date_period_length: UNIQUE (symbol, date, period_length)
    stmt_cashflow_date_idx: (date)
    stmt_cashflow_symbol_date_period_length_key: UNIQUE (symbol, date, period_length)

public.stmt_income
  - date: timestamp with time zone NOT NULL
  - period_length: text
  - cost_of_operating_revenue: double precision
  - depreciation: double precision
  - depreciation_and_amortization: double precision
  - ebit: double precision
  - ebitda: double precision
  - employee_and_related_expenses: double precision
  - eps_basic: double precision
  - eps_diluted: double precision
  - gross_revenue: double precision
  - income_tax_current: double precision
  - income_tax_deferred: double precision
  - income_tax_expense: double precision
  - interest_expense: double precision
  - net_income_after_tax: double precision
  - net_interest_expense: double precision
  - operating_expenses: double precision
  - operating_profit: double precision
  - profit_after_tax: double precision
  - profit_before_tax: double precision
  - shares_used_basic_eps: double precision
  - shares_used_diluted_eps: double precision
  - total_operating_expenses: double precision
  - total_revenue: double precision
  - symbol: text
  # Indexes
    idx_stmt_income_symbol_date_period_length: UNIQUE (symbol, date, period_length)
    stmt_income_date_idx: (date)
    stmt_income_symbol_date_period_length_key: UNIQUE (symbol, date, period_length)
```
