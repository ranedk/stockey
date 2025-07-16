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

# Dhan

`python -m data.dhanlive.scrip_master`

# Sharpely master setup

`python -m data.sharpelydata.scrip_master`

## Sharpely data

- Financial statements: `get_financial_statement(ticker)`
- Corporate Actions: `get_corporate_actions(ticker)`
- Shareholding: `get_shareholding(ticker)`
- Bulk Insider trades: `get_bulk_insider_trades(ticker)`
- Historical MCap: `get_historical_mcap(ticker)`

`python -m data.sharpelydata.sharpely_data`

>Note: TODO: Add loop to get all ticker data for other scrips

## US Macro data, ISM Manufacturing and India GDP numbers

`python -m data.fred.us_macro`
