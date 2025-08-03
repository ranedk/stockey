
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

public.macro_india_gdp
  - date: timestamp with time zone
  - india_gdp: double precision
  # Indexes
    idx_macro_india_gdp_date: UNIQUE (date)
    macro_india_gdp_date_key: UNIQUE (date)

public.macro_usa
  - date: timestamp with time zone
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
  - date: text
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

public.master_sharpely_funds
  - plan_id: bigint
  - isin_code: text
  - amfi_code: text
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
