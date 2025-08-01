#!/bin/bash

python -m data.dhanlive.scrip_master
python -m data.sharpelydata.scrip_master

python -m data.fred.us_macro
python -m data.eaindustry.wpi
python -m data.rbi.download_fbil_gsec
python -m data.rbi.download_bank_rates
python -m data.ininvesting.in10y
python -m data.sharpelydata.sharpely_data
python -m data.nseindia.offmarket
python -m data.nseindia.bhavcopy_downloader
python -m data.nseindia.events_downloader
python -m data.nseindia.bhavcopy_parser
python -m data.mospi.cpi
python -m data.nsdl.fpi