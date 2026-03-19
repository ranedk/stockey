-- Minimal admin SQL for the stockey database.
-- This file intentionally replaces the old full pg_dump artifact.
-- For human-readable schema docs, use:
--   python -m utils.db_schema_dump --schemas public

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Legacy NSDL tables were previously inferred as TEXT because the loader
-- did not coerce numeric values before the first insert. Run these once
-- on an existing database if needed.

ALTER TABLE IF EXISTS public.fii_investments
    ALTER COLUMN gross_purchases_inr_crore TYPE double precision USING NULLIF(REPLACE(gross_purchases_inr_crore, ',', ''), '')::double precision,
    ALTER COLUMN gross_sales_inr_crore TYPE double precision USING NULLIF(REPLACE(gross_sales_inr_crore, ',', ''), '')::double precision,
    ALTER COLUMN net_investment_inr_crore TYPE double precision USING NULLIF(REPLACE(net_investment_inr_crore, ',', ''), '')::double precision,
    ALTER COLUMN net_investment_usd_million TYPE double precision USING NULLIF(REPLACE(net_investment_usd_million, ',', ''), '')::double precision;

ALTER TABLE IF EXISTS public.fii_derivatives
    ALTER COLUMN buy_number_of_contracts TYPE double precision USING NULLIF(REPLACE(buy_number_of_contracts, ',', ''), '')::double precision,
    ALTER COLUMN buy_amount TYPE double precision USING NULLIF(REPLACE(buy_amount, ',', ''), '')::double precision,
    ALTER COLUMN sell_number_of_contracts TYPE double precision USING NULLIF(REPLACE(sell_number_of_contracts, ',', ''), '')::double precision,
    ALTER COLUMN sell_amount TYPE double precision USING NULLIF(REPLACE(sell_amount, ',', ''), '')::double precision,
    ALTER COLUMN open_interest_eod_number_of_contracts TYPE double precision USING NULLIF(REPLACE(open_interest_eod_number_of_contracts, ',', ''), '')::double precision,
    ALTER COLUMN open_interest_eod_amount TYPE double precision USING NULLIF(REPLACE(open_interest_eod_amount, ',', ''), '')::double precision;
