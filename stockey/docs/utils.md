# Utility Commands

## Dump one table locally

```sh
export PGPASSWORD=stockey
pg_dump --host=localhost --port=5432 --username=stockey --table=public.<table_name> --data-only stockey > <table_name>.sql
```

## Restore one table to a server

```sh
export PGPASSWORD=stockey
psql --host=<server_ip> --port=5432 --username=stockey --dbname=stockey --file=<table_name>.sql
```

## Refresh schema summary

```sh
python -m utils.db_schema_dump --schemas public
```

## Safe SQL type migrations for legacy NSDL tables

If `fii_investments` or `fii_derivatives` were created before numeric coercion was added in code, convert them once on the database:

```sql
ALTER TABLE public.fii_investments
    ALTER COLUMN gross_purchases_inr_crore TYPE double precision USING NULLIF(REPLACE(gross_purchases_inr_crore, ',', ''), '')::double precision,
    ALTER COLUMN gross_sales_inr_crore TYPE double precision USING NULLIF(REPLACE(gross_sales_inr_crore, ',', ''), '')::double precision,
    ALTER COLUMN net_investment_inr_crore TYPE double precision USING NULLIF(REPLACE(net_investment_inr_crore, ',', ''), '')::double precision,
    ALTER COLUMN net_investment_usd_million TYPE double precision USING NULLIF(REPLACE(net_investment_usd_million, ',', ''), '')::double precision;

ALTER TABLE public.fii_derivatives
    ALTER COLUMN buy_number_of_contracts TYPE double precision USING NULLIF(REPLACE(buy_number_of_contracts, ',', ''), '')::double precision,
    ALTER COLUMN buy_amount TYPE double precision USING NULLIF(REPLACE(buy_amount, ',', ''), '')::double precision,
    ALTER COLUMN sell_number_of_contracts TYPE double precision USING NULLIF(REPLACE(sell_number_of_contracts, ',', ''), '')::double precision,
    ALTER COLUMN sell_amount TYPE double precision USING NULLIF(REPLACE(sell_amount, ',', ''), '')::double precision,
    ALTER COLUMN open_interest_eod_number_of_contracts TYPE double precision USING NULLIF(REPLACE(open_interest_eod_number_of_contracts, ',', ''), '')::double precision,
    ALTER COLUMN open_interest_eod_amount TYPE double precision USING NULLIF(REPLACE(open_interest_eod_amount, ',', ''), '')::double precision;
```
