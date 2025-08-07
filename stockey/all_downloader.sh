#!/bin/bash

python -m data.dhanlive.scrip_master
python -m data.sharpelydata.scrip_master

python -m data.fred.us_macro
python -m data.eaindustry.wpi
python -m data.rbi.download_fbil_gsec
python -m data.rbi.download_bank_rates
python -m data.sharpelydata.sharpely_data
python -m data.nseindia.offmarket
python -m data.nseindia.bhavcopy_downloader
python -m data.nseindia.bhavcopy_parser
python -m data.mospi.cpi
python -m data.nsdl.fpi
python -m data/nseindia/corporate_actions.py
python -m data/nseindia/earnings_events.py
python -m data/nseindia/insider_deals.py
python -m data/nseindia/offmarket_parser.py
python -m data/nseindia/recent_events.py
