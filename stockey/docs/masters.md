# Master data

There are multiple master data pulled from various sources:

## Dhan Scrip master

This is the main master which has a list of all assets that being traded. This is from Dhan which will be our primary trading account.

`python -m data.dhanlive.scrip_master`

## Sharpely master

This is the master from the Sharpely website, from where we will scrap fundamental data for all the scrips.
The links between all scrips will be via their BSE Ticker or NSE Ticker.

`python -m data.sharpelydata.scrip_master`
