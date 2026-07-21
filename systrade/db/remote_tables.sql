CREATE TABLE public.nseindia_ohlcv (
    symbol text,
    series text,
    open double precision,
    high double precision,
    low double precision,
    close double precision,
    last double precision,
    previous_close double precision,
    volume bigint,
    total_value double precision,
    date timestamp with time zone,
    number_of_trades bigint,
    isin text
);

CREATE TABLE public.nseindia_indices (
    index_name text,
    date timestamp with time zone NOT NULL,
    open double precision,
    high double precision,
    low double precision,
    close double precision,
    points_change double precision,
    percent_change double precision,
    volume double precision,
    turnover_cr double precision,
    pe double precision,
    pb double precision,
    div_yield double precision
);

CREATE TABLE public.nseindia_corporate_actions (
    symbol text,
    series text,
    face_value text,
    subject text,
    date timestamp with time zone NOT NULL,
    record_date timestamp with time zone,
    start_date timestamp with time zone,
    end_date timestamp with time zone,
    nd_start_date timestamp with time zone,
    nd_end_date timestamp with time zone,
    company text,
    isin text,
    ca_broadcast_date timestamp with time zone
);

CREATE TABLE public.dim_trading_days (
    date timestamp with time zone NOT NULL,
    is_next_day_working boolean,
    is_previous_day_working boolean,
    is_month_end boolean,
    is_quarter_end boolean,
    is_year_end boolean,
    week_number bigint
);

CREATE TABLE public.nseindia_holidays (
    date timestamp with time zone NOT NULL,
    holiday text,
    morning_session text,
    evening_session text,
    type text,
    type_name text
);

-- Indexes for the access patterns systrader uses
CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol_date ON public.nseindia_ohlcv (symbol, date);
CREATE INDEX IF NOT EXISTS idx_ohlcv_date ON public.nseindia_ohlcv (date);
CREATE INDEX IF NOT EXISTS idx_indices_name_date ON public.nseindia_indices (index_name, date);
CREATE INDEX IF NOT EXISTS idx_corpact_symbol ON public.nseindia_corporate_actions (symbol, date);
