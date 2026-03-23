#!/usr/bin/env bash

set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-/home/rane/code/stockey/.xstockey/bin/python}"

# Masters
"${PYTHON_BIN}" -m data.nseindia.holidays
"${PYTHON_BIN}" -m data.dhanlive.scrip_master
"${PYTHON_BIN}" -m data.sharpelydata.scrip_master
"${PYTHON_BIN}" -m data.company_master

# Macro and policy data
"${PYTHON_BIN}" -m data.fred.us_macro
"${PYTHON_BIN}" -m data.eaindustry.wpi
"${PYTHON_BIN}" -m data.rbi.download_fbil_gsec
"${PYTHON_BIN}" -m data.rbi.download_bank_rates
"${PYTHON_BIN}" -m data.mospi.cpi
"${PYTHON_BIN}" -m data.nsdl.fpi

# Symbol-scoped fundamentals and event feeds use tracked symbols by default.
"${PYTHON_BIN}" -m data.dhanlive.ohlcv
"${PYTHON_BIN}" -m data.dhanlive.screener
"${PYTHON_BIN}" -m data.sharpelydata.sharpely_data
"${PYTHON_BIN}" -m data.nseindia.corporate_actions
"${PYTHON_BIN}" -m data.nseindia.earnings_events
"${PYTHON_BIN}" -m data.nseindia.insider_deals

# NSE market-wide downloads and parsers
"${PYTHON_BIN}" -m data.nseindia.offmarket
"${PYTHON_BIN}" -m data.nseindia.offmarket_parser
"${PYTHON_BIN}" -m data.nseindia.bhavcopy_downloader
"${PYTHON_BIN}" -m data.nseindia.bhavcopy_parser
"${PYTHON_BIN}" -m data.nseindia.indices_downloader
"${PYTHON_BIN}" -m data.nseindia.indices_parser
"${PYTHON_BIN}" -m data.nseindia.recent_events
