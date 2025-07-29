
public.eaindustry_wpi
  - date: timestamp with time zone IDX:eaindustry_wpi_date_cname_key,idx_eaindustry_wpi_date_cname
  - value: double precision 
  - cname: text IDX:eaindustry_wpi_date_cname_key,idx_eaindustry_wpi_date_cname
  - name: text 
  # Indexes
    eaindustry_wpi_date_cname_key: UNIQUE (date, cname)
    idx_eaindustry_wpi_date_cname: UNIQUE (date, cname)

public.events_capital_change
  - event_type: text IDX:events_capital_change_ticker_event_type_announcement_date_key,idx_events_capital_change_ticker_event_type_announcement_date
  - announcement_date: timestamp with time zone IDX:events_capital_change_ticker_event_type_announcement_date_key,idx_events_capital_change_ticker_event_type_announcement_date
  - ex_date: timestamp with time zone 
  - notes: text 
  - corp_action_id: double precision 
  - new_share_terms: bigint 
  - old_share_terms: bigint 
  - offer_price: text 
  - ticker: text IDX:events_capital_change_ticker_event_type_announcement_date_key,idx_events_capital_change_ticker_event_type_announcement_date
  # Indexes
    events_capital_change_ticker_event_type_announcement_date_key: UNIQUE (ticker, event_type, announcement_date)
    idx_events_capital_change_ticker_event_type_announcement_date: UNIQUE (ticker, event_type, announcement_date)

public.events_dividend
  - announcement_date: timestamp with time zone IDX:events_dividend_ticker_announcement_date_pay_date_key,idx_events_dividend_ticker_announcement_date_pay_date
  - ex_date: timestamp with time zone 
  - pay_date: timestamp with time zone IDX:events_dividend_ticker_announcement_date_pay_date_key,idx_events_dividend_ticker_announcement_date_pay_date
  - record_date: timestamp with time zone 
  - dividend_amount: double precision 
  - corp_action_id: double precision 
  - ticker: text IDX:events_dividend_ticker_announcement_date_pay_date_key,idx_events_dividend_ticker_announcement_date_pay_date
  # Indexes
    events_dividend_ticker_announcement_date_pay_date_key: UNIQUE (ticker, announcement_date, pay_date)
    idx_events_dividend_ticker_announcement_date_pay_date: UNIQUE (ticker, announcement_date, pay_date)

public.events_earnings
  - period_end_date: timestamp with time zone IDX:events_earnings_ticker_report_date_period_end_date_period_l_key,idx_events_earnings_ticker_report_date_period_end_date_period_l
  - eps: double precision 
  - period_length: bigint IDX:events_earnings_ticker_report_date_period_end_date_period_l_key,idx_events_earnings_ticker_report_date_period_end_date_period_l
  - eps_marker: text IDX:events_earnings_ticker_report_date_period_end_date_period_l_key,idx_events_earnings_ticker_report_date_period_end_date_period_l
  - report_date: timestamp with time zone IDX:events_earnings_ticker_report_date_period_end_date_period_l_key,idx_events_earnings_ticker_report_date_period_end_date_period_l
  - ticker: text IDX:events_earnings_ticker_report_date_period_end_date_period_l_key,idx_events_earnings_ticker_report_date_period_end_date_period_l
  # Indexes
    events_earnings_ticker_report_date_period_end_date_period_l_key: UNIQUE (ticker, report_date, period_end_date, period_length, eps_marker)
    idx_events_earnings_ticker_report_date_period_end_date_period_l: UNIQUE (ticker, report_date, period_end_date, period_length, eps_marker)

public.fbil_gsec_par
  - trade_date: text IDX:fbil_gsec_par_trade_date_tenor_years_key,idx_fbil_gsec_par_trade_date_tenor_years
  - tenor_years: double precision IDX:fbil_gsec_par_trade_date_tenor_years_key,idx_fbil_gsec_par_trade_date_tenor_years
  - par_yield_sa: double precision 
  - par_yield_ann: double precision 
  # Indexes
    fbil_gsec_par_trade_date_tenor_years_key: UNIQUE (trade_date, tenor_years)
    idx_fbil_gsec_par_trade_date_tenor_years: UNIQUE (trade_date, tenor_years)

public.fbil_gsec_quote
  - trade_date: text IDX:fbil_gsec_quote_trade_date_isin_key,idx_fbil_gsec_quote_trade_date_isin
  - isin: text IDX:fbil_gsec_quote_trade_date_isin_key,idx_fbil_gsec_quote_trade_date_isin
  - coupon_pct: double precision 
  - maturity_date: timestamp with time zone 
  - clean_price: double precision 
  - ytm_sa: double precision 
  - remark1: text 
  - remark2: text 
  - liquidity_signal: text 
  # Indexes
    fbil_gsec_quote_trade_date_isin_key: UNIQUE (trade_date, isin)
    idx_fbil_gsec_quote_trade_date_isin: UNIQUE (trade_date, isin)

public.fii_derivatives
  - reporting_date: timestamp with time zone IDX:fii_derivatives_reporting_date_instrument_key,idx_fii_derivatives_reporting_date_instrument
  - instrument: text IDX:fii_derivatives_reporting_date_instrument_key,idx_fii_derivatives_reporting_date_instrument
  - buy_number_of_contracts: text 
  - buy_amount: text 
  - sell_number_of_contracts: text 
  - sell_amount: text 
  - open_interest_eod_number_of_contracts: text 
  - open_interest_eod_amount: text 
  # Indexes
    fii_derivatives_reporting_date_instrument_key: UNIQUE (reporting_date, instrument)
    idx_fii_derivatives_reporting_date_instrument: UNIQUE (reporting_date, instrument)

public.fii_investments
  - reporting_date: timestamp with time zone IDX:fii_investments_reporting_date_instrument_key,idx_fii_investments_reporting_date_instrument
  - gross_purchases_inr_crore: text 
  - gross_sales_inr_crore: text 
  - net_investment_inr_crore: text 
  - net_investment_usd_million: text 
  - instrument: text IDX:fii_investments_reporting_date_instrument_key,idx_fii_investments_reporting_date_instrument
  # Indexes
    fii_investments_reporting_date_instrument_key: UNIQUE (reporting_date, instrument)
    idx_fii_investments_reporting_date_instrument: UNIQUE (reporting_date, instrument)

public.historical_mcap
  - timestamp: timestamp with time zone IDX:historical_mcap_ticker_timestamp_key,idx_historical_mcap_ticker_timestamp
  - mcap: double precision 
  - ticker: text IDX:historical_mcap_ticker_timestamp_key,idx_historical_mcap_ticker_timestamp
  # Indexes
    historical_mcap_ticker_timestamp_key: UNIQUE (ticker, timestamp)
    idx_historical_mcap_ticker_timestamp: UNIQUE (ticker, timestamp)

public.ininvesting_gsec
  - last_close: double precision 
  - last_open: double precision 
  - last_max: double precision 
  - last_min: double precision 
  - change_precent: double precision 
  - date: timestamp with time zone IDX:idx_ininvesting_gsec_date,ininvesting_gsec_date_key
  # Indexes
    idx_ininvesting_gsec_date: UNIQUE (date)
    ininvesting_gsec_date_key: UNIQUE (date)

public.macro_india_gdp
  - date: timestamp with time zone IDX:idx_macro_india_gdp_date,macro_india_gdp_date_key
  - india_gdp: double precision 
  # Indexes
    idx_macro_india_gdp_date: UNIQUE (date)
    macro_india_gdp_date_key: UNIQUE (date)

public.macro_usa
  - date: timestamp with time zone IDX:idx_macro_usa_date,macro_usa_date_key
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
  - date: text IDX:idx_macro_usa_ism_date,macro_usa_ism_date_key
  - periodDateUtc: text 
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
  - segment: character varying 
  - security_id: bigint IDX:idx_active_instruments,idx_master_dhan_active
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
  - valid_from: timestamp without time zone 
  - valid_to: timestamp without time zone IDX:idx_active_instruments,idx_master_dhan_active
  - load_ts: timestamp without time zone 
  # Indexes
    idx_active_instruments: (security_id, valid_to)
    idx_master_dhan_active: (security_id, valid_to)

public.master_sharpely_equity
  - symbol: text IDX:idx_master_sharpely_equity_symbol_bse_ticker,master_sharpely_equity_symbol_bse_ticker_key
  - bse_ticker: text IDX:idx_master_sharpely_equity_symbol_bse_ticker,master_sharpely_equity_symbol_bse_ticker_key
  - proper_name: text 
  - lseg_instrument_id: text 
  - nse_active: double precision 
  - nse_segment: text 
  - bse_segment: text 
  - sector_code: text 
  # Indexes
    idx_master_sharpely_equity_symbol_bse_ticker: UNIQUE (symbol, bse_ticker)
    master_sharpely_equity_symbol_bse_ticker_key: UNIQUE (symbol, bse_ticker)

public.master_sharpely_funds
  - plan_id: bigint 
  - isin_code: text 
  - amfi_code: text IDX:idx_master_sharpely_funds_amfi_code,master_sharpely_funds_amfi_code_key
  - sharpely_id: text 
  - regular_plan_id: double precision 
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

public.mospi_cpi
  - state: text IDX:idx_mospi_cpi_cpi_for_month_state_group_sub_group,mospi_cpi_cpi_for_month_state_group_sub_group_key
  - group: double precision IDX:idx_mospi_cpi_cpi_for_month_state_group_sub_group,mospi_cpi_cpi_for_month_state_group_sub_group_key
  - sub_group: text IDX:idx_mospi_cpi_cpi_for_month_state_group_sub_group,mospi_cpi_cpi_for_month_state_group_sub_group_key
  - description: text 
  - rural: double precision 
  - urban: double precision 
  - combined: double precision 
  - status: text 
  - cpi_for_month: timestamp with time zone IDX:idx_mospi_cpi_cpi_for_month_state_group_sub_group,mospi_cpi_cpi_for_month_state_group_sub_group_key
  - reported_on: timestamp with time zone 
  # Indexes
    idx_mospi_cpi_cpi_for_month_state_group_sub_group: UNIQUE (cpi_for_month, state, group, sub_group)
    mospi_cpi_cpi_for_month_state_group_sub_group_key: UNIQUE (cpi_for_month, state, group, sub_group)

public.nseindia_cat_turnover
  - trade_date: timestamp with time zone IDX:idx_nseindia_cat_turnover_trade_date_client_category,nseindia_cat_turnover_trade_date_client_category_key
  - client_category: text IDX:idx_nseindia_cat_turnover_trade_date_client_category,nseindia_cat_turnover_trade_date_client_category_key
  - buy_rs_cr: double precision 
  - sell_rs_cr: double precision 
  # Indexes
    idx_nseindia_cat_turnover_trade_date_client_category: UNIQUE (trade_date, client_category)
    nseindia_cat_turnover_trade_date_client_category_key: UNIQUE (trade_date, client_category)

public.nseindia_catg
  - symbol: text IDX:idx_nseindia_catg_for_month_series_symbol_isin,nseindia_catg_for_month_series_symbol_isin_key
  - series: text IDX:idx_nseindia_catg_for_month_series_symbol_isin,nseindia_catg_for_month_series_symbol_isin_key
  - isin: text IDX:idx_nseindia_catg_for_month_series_symbol_isin,nseindia_catg_for_month_series_symbol_isin_key
  - category: text 
  - impact_cost: double precision 
  - for_month: timestamp with time zone IDX:idx_nseindia_catg_for_month_series_symbol_isin,nseindia_catg_for_month_series_symbol_isin_key
  # Indexes
    idx_nseindia_catg_for_month_series_symbol_isin: UNIQUE (for_month, series, symbol, isin)
    nseindia_catg_for_month_series_symbol_isin_key: UNIQUE (for_month, series, symbol, isin)

public.nseindia_circuit_hit
  - symbol: text IDX:idx_nseindia_circuit_hit_date_symbol_series_circuit_hit,nseindia_circuit_hit_date_symbol_series_circuit_hit_key
  - series: text IDX:idx_nseindia_circuit_hit_date_symbol_series_circuit_hit,nseindia_circuit_hit_date_symbol_series_circuit_hit_key
  - circuit_hit: text IDX:idx_nseindia_circuit_hit_date_symbol_series_circuit_hit,nseindia_circuit_hit_date_symbol_series_circuit_hit_key
  - date: timestamp with time zone IDX:idx_nseindia_circuit_hit_date_symbol_series_circuit_hit,nseindia_circuit_hit_date_symbol_series_circuit_hit_key
  # Indexes
    idx_nseindia_circuit_hit_date_symbol_series_circuit_hit: UNIQUE (date, symbol, series, circuit_hit)
    nseindia_circuit_hit_date_symbol_series_circuit_hit_key: UNIQUE (date, symbol, series, circuit_hit)

public.nseindia_cmvolt
  - date: timestamp with time zone IDX:idx_nseindia_cmvolt_date_symbol,nseindia_cmvolt_date_symbol_key
  - symbol: text IDX:idx_nseindia_cmvolt_date_symbol,nseindia_cmvolt_date_symbol_key
  - close: double precision 
  - previous_close: double precision 
  - log_return: double precision 
  - previous_day_daily_volatility: double precision 
  - current_day_daily_volatility: double precision 
  - annualized_volatility: double precision 
  # Indexes
    idx_nseindia_cmvolt_date_symbol: UNIQUE (date, symbol)
    nseindia_cmvolt_date_symbol_key: UNIQUE (date, symbol)

public.nseindia_mcap
  - symbol: text IDX:idx_nseindia_mcap_date_symbol_series,nseindia_mcap_date_symbol_series_key
  - series: text IDX:idx_nseindia_mcap_date_symbol_series,nseindia_mcap_date_symbol_series_key
  - category: text 
  - last_trade_date: text 
  - face_value_rs: double precision 
  - issue_size: text 
  - close_price_paid_up_value_rs: double precision 
  - market_cap_rs: double precision 
  - date: timestamp with time zone IDX:idx_nseindia_mcap_date_symbol_series,nseindia_mcap_date_symbol_series_key
  # Indexes
    idx_nseindia_mcap_date_symbol_series: UNIQUE (date, symbol, series)
    nseindia_mcap_date_symbol_series_key: UNIQUE (date, symbol, series)

public.nseindia_var1
  - symbol: text IDX:idx_nseindia_var1_for_date_entry_number_series_symbol_isin,nseindia_var1_for_date_entry_number_series_symbol_isin_key
  - series: text IDX:idx_nseindia_var1_for_date_entry_number_series_symbol_isin,nseindia_var1_for_date_entry_number_series_symbol_isin_key
  - isin: text IDX:idx_nseindia_var1_for_date_entry_number_series_symbol_isin,nseindia_var1_for_date_entry_number_series_symbol_isin_key
  - security_var: double precision 
  - index_var: double precision 
  - var_margin: double precision 
  - extreme_loss_rate: double precision 
  - adhoc_margin: double precision 
  - applicable_margin: double precision 
  - for_date: timestamp with time zone IDX:idx_nseindia_var1_for_date_entry_number_series_symbol_isin,nseindia_var1_for_date_entry_number_series_symbol_isin_key
  - entry_number: bigint IDX:idx_nseindia_var1_for_date_entry_number_series_symbol_isin,nseindia_var1_for_date_entry_number_series_symbol_isin_key
  # Indexes
    idx_nseindia_var1_for_date_entry_number_series_symbol_isin: UNIQUE (for_date, entry_number, series, symbol, isin)
    nseindia_var1_for_date_entry_number_series_symbol_isin_key: UNIQUE (for_date, entry_number, series, symbol, isin)

public.rbi_bank_rates
  - effective_date: timestamp with time zone IDX:idx_rbi_bank_rates_effective_date,rbi_bank_rates_effective_date_key
  - bank_rate: double precision 
  - repo_rate: double precision 
  - reverse_repo_rate: double precision 
  - sdf_rate: double precision 
  - msf_rate: double precision 
  - crr: text 
  - slr: double precision 
  # Indexes
    idx_rbi_bank_rates_effective_date: UNIQUE (effective_date)
    rbi_bank_rates_effective_date_key: UNIQUE (effective_date)

public.rbi_currency_rates
  - Date: text 
  - USD: double precision 
  - GBP: double precision 
  - EURO: double precision 
  - YEN: double precision 
  - date: timestamp with time zone IDX:idx_rbi_currency_rates_date,rbi_currency_rates_date_key
  # Indexes
    idx_rbi_currency_rates_date: UNIQUE (date)
    rbi_currency_rates_date_key: UNIQUE (date)

public.shareholding_category
  - ticker: text IDX:idx_shareholding_category_ticker_report_date_category_code,shareholding_category_ticker_report_date_category_code_key
  - category_name: text 
  - category_code: text IDX:idx_shareholding_category_ticker_report_date_category_code,shareholding_category_ticker_report_date_category_code_key
  - sh_per: double precision 
  - qoq_change: double precision 
  - pledge_per: double precision 
  - num_shldrs: double precision 
  - quarter_end: text 
  - report_date: timestamp with time zone IDX:idx_shareholding_category_ticker_report_date_category_code,shareholding_category_ticker_report_date_category_code_key
  # Indexes
    idx_shareholding_category_ticker_report_date_category_code: UNIQUE (ticker, report_date, category_code)
    shareholding_category_ticker_report_date_category_code_key: UNIQUE (ticker, report_date, category_code)

public.shareholding_top_holders
  - holder: text IDX:idx_shareholding_top_holders_ticker_report_date_holder,shareholding_top_holders_ticker_report_date_holder_key
  - ticker: text IDX:idx_shareholding_top_holders_ticker_report_date_holder,shareholding_top_holders_ticker_report_date_holder_key
  - sh_per: double precision 
  - pledge_per: text 
  - code: text 
  - quarter_end: text 
  - report_date: timestamp with time zone IDX:idx_shareholding_top_holders_ticker_report_date_holder,shareholding_top_holders_ticker_report_date_holder_key
  # Indexes
    idx_shareholding_top_holders_ticker_report_date_holder: UNIQUE (ticker, report_date, holder)
    shareholding_top_holders_ticker_report_date_holder_key: UNIQUE (ticker, report_date, holder)

public.stmt_balancesheet
  - period_end_date: text IDX:idx_stmt_balancesheet_ticker_period_end_date_period_length,stmt_balancesheet_ticker_period_end_date_period_length_key
  - period_length: text IDX:idx_stmt_balancesheet_ticker_period_end_date_period_length,stmt_balancesheet_ticker_period_end_date_period_length_key
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
  - ticker: text IDX:idx_stmt_balancesheet_ticker_period_end_date_period_length,stmt_balancesheet_ticker_period_end_date_period_length_key
  # Indexes
    idx_stmt_balancesheet_ticker_period_end_date_period_length: UNIQUE (ticker, period_end_date, period_length)
    stmt_balancesheet_ticker_period_end_date_period_length_key: UNIQUE (ticker, period_end_date, period_length)

public.stmt_cashflow
  - period_end_date: text IDX:idx_stmt_cashflow_ticker_period_end_date_period_length,stmt_cashflow_ticker_period_end_date_period_length_key
  - period_length: text IDX:idx_stmt_cashflow_ticker_period_end_date_period_length,stmt_cashflow_ticker_period_end_date_period_length_key
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
  - ticker: text IDX:idx_stmt_cashflow_ticker_period_end_date_period_length,stmt_cashflow_ticker_period_end_date_period_length_key
  # Indexes
    idx_stmt_cashflow_ticker_period_end_date_period_length: UNIQUE (ticker, period_end_date, period_length)
    stmt_cashflow_ticker_period_end_date_period_length_key: UNIQUE (ticker, period_end_date, period_length)

public.stmt_income
  - period_end_date: text IDX:idx_stmt_income_ticker_period_end_date_period_length,stmt_income_ticker_period_end_date_period_length_key
  - period_length: text IDX:idx_stmt_income_ticker_period_end_date_period_length,stmt_income_ticker_period_end_date_period_length_key
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
  - ticker: text IDX:idx_stmt_income_ticker_period_end_date_period_length,stmt_income_ticker_period_end_date_period_length_key
  # Indexes
    idx_stmt_income_ticker_period_end_date_period_length: UNIQUE (ticker, period_end_date, period_length)
    stmt_income_ticker_period_end_date_period_length_key: UNIQUE (ticker, period_end_date, period_length)
