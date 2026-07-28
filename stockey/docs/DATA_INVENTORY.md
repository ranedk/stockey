# Data Inventory & Pure-TA Scope (decision 2026-07-27)

Operator decision: stockey goes **pure data platform for TA**. All LLM-token
consumers and all fundamental-analysis collection/analysis are removed.
Supersedes the keep-fundamentals variant discussed earlier the same day;
`docs/ADVISORY_SPLIT.md` remains valid for the advisory/ module mechanics,
with its Category scope widened by this document (fundamental/news/event
analysis now also archives).

Rationale: stockey's own FDR-gated research found no robust cross-regime
selection alpha in fundamentals/deal-flow/features; news & announcement
pipelines burn LLM tokens with no backtestable output; systrader (Carver
framework) needs only the price/CA/rates/identity core below.

## KEEP — collectors and tables (the entire go-forward API)

| Source | Modules (data/…) | Tables |
|---|---|---|
| NSE bhavcopy | nseindia/bhavcopy_{downloader,history,parser} | nseindia_ohlcv, nseindia_mcap, nseindia_mto, nseindia_52wk, nseindia_cmvolt, nseindia_circuit_hit, nseindia_cat_turnover, nseindia_catg, nseindia_var1, nseindia_short_selling |
| NSE corporate actions | nseindia/corporate_action{s,_events} | nseindia_corporate_actions{,_bc_raw,_normalized}, events_dividend, events_capital_change |
| Price adjustment | advisory/price_adjustment.py → PROMOTE to data/ | advisory_adjusted_ohlcv_daily (systrader's PRIMARY series) |
| NSE indices | nseindia/indices_{downloader,parser} | nseindia_indices |
| NSE calendar | nseindia/holidays | nseindia_holidays, dim_trading_days |
| Dhan broker | dhanlive/* (incl. auth/web_login) | master_dhan_instruments, dhan_ohlcv_daily, dhan_ohlcv_intraday (future 1-min landing zone) |
| RBI/FBIL | rbi/* | rbi_bank_rates, rbi_currency_rates, fbil_gsec_par, fbil_gsec_quote |
| Identity | company_master, nseindia/security_history | company_master, dim_security* |
| Sharpely (mcap slice ONLY) | sharpelydata/sharpely_data.py | historical_mcap |

Cron keeps only: complete_data.sh, all_downloaders_queue.sh,
all_price_adjustment.sh, all_ohlcv_reconcile.sh, all_data_readiness.sh,
log rotation. Everything else unschedules.

## REMOVE — LLM-token consumers

- data/announcements/* (categorize.py/prompts.py classify with LLM calls) →
  announcement_pipeline_documents, announcement_pipeline_reports
- data/economictimes/rss.py → economictimes_rss_items (fed LLM news themes)
- Advisory LLM stack (llm_*, event policy, adversarial review, news themes)

## REMOVE — fundamental / non-TA

- data/screenerin/* → screenerin_* tables
- Sharpely fundamentals → sharpely_stock_meta, sharpely_stock_peers,
  master_sharpely_{equity,funds}, stmt_{balancesheet,cashflow,income},
  shareholding_*, advisory_fundamentals_daily
- nseindia/insider_deals + deal-flow tables (nseindia_insider_deals,
  nseindia_{block,bulk}_deals) — sweep showed IC ≈ 0
- nseindia/earnings_events → nseindia_earnings_events, nseindia_events
  (BORDERLINE: LLM-free and useful for FnO event-vol later — freeze the
  collector rather than delete if cheap)
- Macro: mospi_cpi, eaindustry_wpi, macro_usa*, macro_india_gdp, fii_*
  (data/mospi, data/eaindustry, data/fred, data/nsdl)
- features_* precomputed tables
- ALL advisory_* tables except advisory_adjusted_ohlcv_daily
  (verify advisory_sync_state isn't used by downloaders before dropping)

## Execution notes

- Archive code (git branch/attic), drop cron entries in the same commit.
- Table drops on the cloud DB: take a final dump first; drops free space on
  the small instance.
- systrader's sync list already matches the KEEP set; nothing to change
  downstream except deleting the never-built universe_screen design.
