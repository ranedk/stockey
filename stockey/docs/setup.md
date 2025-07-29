# Dev postgres setup:

```sh
$ sudo -u postgres psql
```

```sh
CREATE DATABASE stockey;
CREATE USER stockey WITH ENCRYPTED PASSWORD 'stockey';
GRANT ALL PRIVILEGES ON DATABASE stockey TO stockey;
GRANT ALL ON SCHEMA public TO stockey;
ALTER DATABASE stockey OWNER TO stockey;
GRANT USAGE ON SCHEMA public TO stockey;
```

# `pg_dump` & `pg_restore`

```sh
# PG DUMP with a format
PGPASSWORD=stockey \
pg_dump \
  -h localhost \
  -p 5432 \
  -U stockey \
  --format=custom \
  -f /tmp/full_db_dump.custom \
  stockey

# PG RESTORE from a format
PGPASSWORD=stockey \
pg_restore \
  --disable-triggers \
  -h localhost \
  -p 5432 \
  -U stockey \
  -d stockey \
  /tmp/full_db_dump.custom
```

# Redis

The state of downloads is stored in redis, which can be updated regularly:

**Backup**

`python utils/redis_bkp_restore.py --host localhost --port 6379 --db 0 --file backups/redis_global_backup.json backup`

# S3 backups

`aws s3 ls s3://stockeydata/`

```
    PRE bhavcopy/           # bhavcopy dump files
    PRE nsedeals/           # nse deals dump files
    PRE pgdump/             # postgres dumps with date marks
    PRE rdbdump/            # redis dumps with date marks
```

# Dhan

`python -m data.dhanlive.scrip_master`

This is the main master which has a list of all assets that being traded. This is from Dhan which will be our primary trading account.

# Sharpely master setup

`python -m data.sharpelydata.scrip_master`

This is the master from the Sharpely website, from where we will scrap fundamental data for all the scrips.
The links between all scrips will be via their BSE Ticker or NSE Ticker.

## Sharpely data

- Financial statements: `get_financial_statement(ticker)`
- Corporate Actions: `get_corporate_actions(ticker)`
- Shareholding: `get_shareholding(ticker)`
- Bulk Insider trades: `get_bulk_insider_trades(ticker)`
- Historical MCap: `get_historical_mcap(ticker)`

`python -m data.sharpelydata.sharpely_data`

> TODO: Add loop to get all ticker data for other scrips

## US Macro data, ISM Manufacturing and India GDP numbers

In table `macro_usa`

- 10-year Treasury constant-maturity
- Effective Fed-funds rate (daily)
- CBOE VIX close
- Growth & inflation
- NFP (level, ‘000)
- CPI SA
- CPI core SA
- USD & commodities
- Trade-weighted dollar (goods only)
- WTI crude spot $/bbl
- India GDP numbers
- INR USD Spot price

In table `macro_usa_ism` - ISM Manufacturing data

`python -m data.fred.us_macro`

This is backed by redis to figure if downloads have been done or not.


# Bhavcopy

The daily bhavcopy has daily market data about price, volumes and trades.
The script downloads all bhavcopy zip from NSE.

You will have to stop all running instances of chrome to be able to run the new instance in debugging mode. You can use `pkill chrome` to kill all instances.

OSX: `/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir=./chromesetup`

OR

Ubuntu: `/opt/google/chrome/chrome --remote-debugging-port=9222 --user-data-dir=./chromesetup`

## Run the bhavcopy downloader

```shell
python data/bhavcopy/downloader.py
```

This is backed by redis to figure if downloads have been done or not.

# Bulk/Block/Short Deals downloader

This has the bulk, block and short deals from the market

You will have to stop all running instances of chrome to be able to run the new instance in debugging mode. You can use `pkill chrome` to kill all instances.
OSX: `/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir=./chromesetup`

OR

Ubuntu: `/opt/google/chrome/chrome --remote-debugging-port=9222 --user-data-dir=./chromesetup`

## Run the bhavcopy downloader

```shell
python data/bhavcopy/bulk_block_short_downloader.py
```

This is backed by redis to figure if downloads have been done or not.

# RBI Bank rates

For kinds of bank rates set by RBI monetary policy

You will have to stop all running instances of chrome to be able to run the new instance in debugging mode. You can use `pkill chrome` to kill all instances.
OSX: `/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir=./chromesetup`

OR

Ubuntu: `/opt/google/chrome/chrome --remote-debugging-port=9222 --user-data-dir=./chromesetup`

`python -m data.rbi.download_bank_rates`

This is run daily, but updates rarely. There is no need to figure out last pulled dated since this returns entire data every time.

# Schema and AI

The schema is generated using `python -m utils.db_schema_dump`
The definitions are added later using AI

# TODO

NSE Indices historical data by day

https://www.niftyindices.com/reports/historical-data

# NOTES

1. ISIN is not unique, there can be multiple Symbols with the same ISIN because the same underlying can be traded in different series (e.g. Nifty 50 and Nifty 50 Future).
2. The same SYMBOL and ISIN can be a part of more than one series e.g. SHAKTIPUMP is traded in series BE and EQ
3. When company changes its name, symbol also changes but ISIN remains the same
