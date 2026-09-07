-- Repair: every bar in systrader_ohlcv_daily is stamped one calendar day early.
--
-- Cause (fixed in cmd/dhan + internal/broker/dhan the same day): the backfill
-- derived a bar's date with time.Unix(...).In(time.Local). Dhan stamps a daily
-- bar at the start of the IST trading day; on a UTC host that timestamp
-- (2026-08-07 00:00 IST = 2026-08-06 18:30 UTC) files the bar under the
-- previous calendar day. The shift is uniform over the whole table and the
-- whole history: ~50 Sunday rows per year (Monday's bars) and almost no Friday
-- rows, in every year from 2015 to 2026.
--
-- Evidence that +1 day is exactly right, not approximately right: of the 6,165
-- rows that overlap stockey's authoritative dhan_ohlcv_daily (NIFTYBEES,
-- GOLDBEES, BANKBEES, JUNIORBEES, MON100 from 2021-09), 6,165 match the
-- authoritative close to within half a paisa when shifted +1, and 18 match as
-- stored. NIFTY's COVID low of 7610.25 sits on 2020-03-22, a Sunday; the real
-- date is Monday 2020-03-23.
--
-- The primary key is (security_id, date), so a bulk +1 collides with rows not
-- yet updated. Drop it, shift, rebuild. Wrapped in a transaction: either the
-- whole table is correct or nothing changed.
--
-- Reversal, should it ever be needed: the same statement with -1.

BEGIN;

ALTER TABLE systrader_ohlcv_daily DROP CONSTRAINT systrader_ohlcv_daily_pkey;

UPDATE systrader_ohlcv_daily SET date = date + INTERVAL '1 day';

ALTER TABLE systrader_ohlcv_daily ADD PRIMARY KEY (security_id, date);

-- Guards. Any failure here rolls the whole thing back.
--
-- The obvious guard ("no bar may sit on a weekend") is WRONG and was caught by
-- this transaction on the first attempt: India trades 11 weekend sessions in
-- this history — Budget Saturdays (2015-02-28, 2020-02-01, 2025-02-01),
-- Diwali Muhurat Sundays (2016-10-30, 2019-10-27, 2023-11-12) and NSE's
-- special DR-site sessions (2024-01-20, 2024-03-02, 2024-05-18). Every one of
-- them is a genuine trading day with 1,456-2,507 symbols in
-- advisory_adjusted_ohlcv_daily. So the guard asks the exchange calendar
-- instead of the day of the week.
DO $$
DECLARE
  off_calendar bigint;
  mismatches   bigint;
BEGIN
  -- 1. Every NSE-segment bar must fall on a day the NSE actually traded.
  --    (MCX keeps its own calendar and trades some NSE holidays, so it is
  --    checked only for weekends, below.)
  SELECT count(*) INTO off_calendar
  FROM systrader_ohlcv_daily o
  WHERE o.segment IN ('NSE_EQ', 'IDX_I', 'NSE_FNO')
    AND NOT EXISTS (
      SELECT 1 FROM dim_trading_days d
      WHERE (d.date AT TIME ZONE 'UTC')::date = o.date
    );
  IF off_calendar > 0 THEN
    RAISE EXCEPTION 'repair left % NSE rows on a non-trading day', off_calendar;
  END IF;

  -- 2. A weekend bar in any segment must be one of the exchange's special
  --    sessions, never an artefact.
  SELECT count(*) INTO off_calendar
  FROM systrader_ohlcv_daily o
  WHERE extract(dow from o.date) IN (0, 6)
    AND NOT EXISTS (
      SELECT 1 FROM dim_trading_days d
      WHERE (d.date AT TIME ZONE 'UTC')::date = o.date
    );
  IF off_calendar > 0 THEN
    RAISE EXCEPTION 'repair left % rows on a weekend the exchange was shut', off_calendar;
  END IF;

  -- 3. Every overlapping row must now agree with stockey's own Dhan table.
  SELECT count(*) INTO mismatches
  FROM systrader_ohlcv_daily o
  JOIN (
    SELECT ticker, (date AT TIME ZONE 'Asia/Kolkata')::date AS d, close
    FROM dhan_ohlcv_daily
  ) s ON s.ticker = o.ticker AND s.d = o.date
  WHERE abs(o.close - s.close) >= 0.005;
  IF mismatches > 0 THEN
    RAISE EXCEPTION 'repair left % rows disagreeing with dhan_ohlcv_daily', mismatches;
  END IF;
END $$;

COMMIT;
