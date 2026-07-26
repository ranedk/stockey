// Backfill storage: series fetched by us from the Dhan API live in
// systrader_ohlcv_daily, a table WE own. They must never be written into
// dhan_ohlcv_daily — that table is mirrored from stockey and its incremental
// sync keys on local max(date); foreign rows would silently break it.
package store

import (
	"context"
	"time"

	"github.com/ranedk/systrader/internal/core"
)

// BackfillRow is one daily bar destined for systrader_ohlcv_daily.
type BackfillRow struct {
	Ticker     string // stable friendly name; futures: UNDERLYING-YYYY-MM-DD (expiry)
	SecurityID int64
	Segment    string // Dhan exchangeSegment used for the fetch (NSE_EQ, MCX_COMM…)
	Instrument string // EQUITY | INDEX | FUTIDX | FUTCOM
	Expiry     *time.Time
	Date       time.Time
	Open       float64
	High       float64
	Low        float64
	Close      float64
	Volume     float64
}

// EnsureBackfillTable creates systrader_ohlcv_daily if absent (idempotent).
func (s *Store) EnsureBackfillTable(ctx context.Context) error {
	_, err := s.pool.Exec(ctx, `
		CREATE TABLE IF NOT EXISTS systrader_ohlcv_daily (
			ticker      varchar NOT NULL,
			security_id bigint  NOT NULL,
			segment     varchar NOT NULL,
			instrument  varchar NOT NULL,
			expiry      date,
			date        date    NOT NULL,
			open   double precision,
			high   double precision,
			low    double precision,
			close  double precision,
			volume double precision,
			fetched_at timestamptz NOT NULL DEFAULT now(),
			PRIMARY KEY (security_id, date)
		);
		CREATE INDEX IF NOT EXISTS idx_systrader_ohlcv_ticker_date
			ON systrader_ohlcv_daily (ticker, date);`)
	return err
}

// UpsertBackfill writes rows, overwriting any prior fetch for the same
// (security_id, date) — re-running a backfill is always safe.
func (s *Store) UpsertBackfill(ctx context.Context, rows []BackfillRow) (int, error) {
	n := 0
	for _, r := range rows {
		_, err := s.pool.Exec(ctx, `
			INSERT INTO systrader_ohlcv_daily
				(ticker, security_id, segment, instrument, expiry, date,
				 open, high, low, close, volume, fetched_at)
			VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11, now())
			ON CONFLICT (security_id, date) DO UPDATE SET
				ticker=excluded.ticker, segment=excluded.segment,
				instrument=excluded.instrument, expiry=excluded.expiry,
				open=excluded.open, high=excluded.high, low=excluded.low,
				close=excluded.close, volume=excluded.volume,
				fetched_at=now()`,
			r.Ticker, r.SecurityID, r.Segment, r.Instrument, r.Expiry, r.Date,
			r.Open, r.High, r.Low, r.Close, r.Volume)
		if err != nil {
			return n, err
		}
		n++
	}
	return n, nil
}

// BackfillMaxDate returns the latest stored date for a security (zero time if
// none) so fetches can resume incrementally.
func (s *Store) BackfillMaxDate(ctx context.Context, securityID int64) (time.Time, error) {
	var d *time.Time
	err := s.pool.QueryRow(ctx, `
		SELECT max(date) FROM systrader_ohlcv_daily WHERE security_id=$1`,
		securityID).Scan(&d)
	if err != nil || d == nil {
		return time.Time{}, err
	}
	return *d, nil
}

// BackfillCloses loads a backfilled close series by ticker.
func (s *Store) BackfillCloses(ctx context.Context, ticker string) (core.Series, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT date, close FROM systrader_ohlcv_daily
		WHERE ticker = $1 AND close IS NOT NULL AND close > 0
		ORDER BY date ASC`, ticker)
	if err != nil {
		return core.Series{}, err
	}
	defer rows.Close()
	return scanSeries(rows.Next, rows.Scan, rows.Err)
}

// FuturesContract identifies one listed contract from master_dhan_instruments.
type FuturesContract struct {
	SecurityID int64
	Symbol     string
	Underlying string
	Expiry     time.Time
	Segment    string // Dhan exchangeSegment (NSE_FNO or MCX_COMM)
	Instrument string // FUTIDX | FUTCOM
	LotSize    float64
}

// ListFuturesContracts returns all contracts in the master for the given
// underlying symbols (NSE index futures + MCX commodity futures).
func (s *Store) ListFuturesContracts(ctx context.Context, underlyings []string) ([]FuturesContract, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT DISTINCT security_id, symbol_name, underlying_symbol,
		       sm_expiry_date, instrument, COALESCE(lot_size,0)
		FROM master_dhan_instruments
		WHERE instrument IN ('FUTIDX','FUTCOM')
		  AND underlying_symbol = ANY($1)
		  AND sm_expiry_date IS NOT NULL
		  AND ((instrument='FUTIDX' AND exch_id='NSE') OR (instrument='FUTCOM' AND exch_id='MCX'))
		ORDER BY underlying_symbol, sm_expiry_date`, underlyings)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []FuturesContract
	for rows.Next() {
		var c FuturesContract
		if err := rows.Scan(&c.SecurityID, &c.Symbol, &c.Underlying,
			&c.Expiry, &c.Instrument, &c.LotSize); err != nil {
			return nil, err
		}
		if c.Instrument == "FUTIDX" {
			c.Segment = "NSE_FNO"
		} else {
			c.Segment = "MCX_COMM"
		}
		out = append(out, c)
	}
	return out, rows.Err()
}
