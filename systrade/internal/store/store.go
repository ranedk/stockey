// Package store reads market data from the local systrade postgres database
// (tables mirrored 1:1 from stockey — see scripts/sync_from_stockey.sh).
package store

import (
	"context"
	"fmt"
	"os"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/joho/godotenv"

	"github.com/ranedk/systrader/internal/core"
)

type Store struct {
	pool *pgxpool.Pool
}

// Open connects using POSTGRES_* env vars (loading .env if present).
func Open(ctx context.Context) (*Store, error) {
	_ = godotenv.Load() // best-effort; real env wins over file
	dsn := fmt.Sprintf("postgres://%s:%s@%s:%s/%s",
		getenv("POSTGRES_USER", "systrade"),
		getenv("POSTGRES_PASSWORD", "systrade"),
		getenv("POSTGRES_HOST", "localhost"),
		getenv("POSTGRES_PORT", "5432"),
		getenv("POSTGRES_DB", "systrade"),
	)
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		return nil, err
	}
	if err := pool.Ping(ctx); err != nil {
		return nil, fmt.Errorf("store: cannot reach systrade db: %w", err)
	}
	return &Store{pool: pool}, nil
}

func (s *Store) Close() { s.pool.Close() }

// DailyCloses loads a close-price series for one ticker from dhan_ohlcv_daily
// (primary history: 2015+). Ordered by date ascending, deduplicated.
func (s *Store) DailyCloses(ctx context.Context, ticker string) (core.Series, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT date, close FROM dhan_ohlcv_daily
		WHERE ticker = $1 AND close IS NOT NULL AND close > 0
		ORDER BY date ASC`, ticker)
	if err != nil {
		return core.Series{}, err
	}
	defer rows.Close()
	return scanSeries(rows.Next, rows.Scan, rows.Err)
}

// AdjustedCloses loads the corporate-action-adjusted close series for a
// symbol from advisory_adjusted_ohlcv_daily (2013+, includes delisted names).
// EQ series only — BE (trade-to-trade) names can't be traded intraday and
// carry different liquidity; exclude them from backtests by default.
// PREFER THIS over DailyCloses for any return computation: unadjusted closes
// fabricate huge fake returns at split/bonus dates.
func (s *Store) AdjustedCloses(ctx context.Context, symbol string) (core.Series, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT date, adj_close FROM advisory_adjusted_ohlcv_daily
		WHERE symbol = $1 AND series = 'EQ'
		  AND adj_close IS NOT NULL AND adj_close > 0
		ORDER BY date ASC`, symbol)
	if err != nil {
		return core.Series{}, err
	}
	defer rows.Close()
	return scanSeries(rows.Next, rows.Scan, rows.Err)
}

// AdjustedSymbols lists symbols in the adjusted table with at least minBars
// EQ-series bars — the widest point-in-time-honest universe we have
// (delisted symbols included, so no survivorship filter is applied here).
func (s *Store) AdjustedSymbols(ctx context.Context, minBars int) ([]string, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT symbol FROM advisory_adjusted_ohlcv_daily
		WHERE series = 'EQ'
		GROUP BY symbol HAVING count(*) >= $1 ORDER BY symbol`, minBars)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var t string
		if err := rows.Scan(&t); err != nil {
			return nil, err
		}
		out = append(out, t)
	}
	return out, rows.Err()
}

// IndexCloses loads an index series from nseindia_indices.
func (s *Store) IndexCloses(ctx context.Context, indexName string) (core.Series, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT date, close FROM nseindia_indices
		WHERE index_name = $1 AND close IS NOT NULL AND close > 0
		ORDER BY date ASC`, indexName)
	if err != nil {
		return core.Series{}, err
	}
	defer rows.Close()
	return scanSeries(rows.Next, rows.Scan, rows.Err)
}

// IndexDivYield loads the dividend-yield series for an index — the raw
// ingredient for the equity-carry rule (yield − funding).
func (s *Store) IndexDivYield(ctx context.Context, indexName string) (core.Series, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT date, div_yield FROM nseindia_indices
		WHERE index_name = $1 AND div_yield IS NOT NULL
		ORDER BY date ASC`, indexName)
	if err != nil {
		return core.Series{}, err
	}
	defer rows.Close()
	return scanSeries(rows.Next, rows.Scan, rows.Err)
}

// LotSize returns the current F&O lot size for an underlying symbol from
// master_dhan_instruments (0 if not found).
func (s *Store) LotSize(ctx context.Context, underlying string) (float64, error) {
	var lot float64
	err := s.pool.QueryRow(ctx, `
		SELECT COALESCE(lot_size, 0) FROM master_dhan_instruments
		WHERE underlying_symbol = $1 AND instrument = 'FUTIDX' AND valid_to IS NULL
		ORDER BY sm_expiry_date ASC LIMIT 1`, underlying).Scan(&lot)
	if err != nil {
		// fall back to stock futures
		err = s.pool.QueryRow(ctx, `
			SELECT COALESCE(lot_size, 0) FROM master_dhan_instruments
			WHERE underlying_symbol = $1 AND instrument = 'FUTSTK' AND valid_to IS NULL
			ORDER BY sm_expiry_date ASC LIMIT 1`, underlying).Scan(&lot)
	}
	return lot, err
}

// Tickers lists distinct equity tickers with at least minBars daily bars —
// the raw universe before any point-in-time filtering.
func (s *Store) Tickers(ctx context.Context, minBars int) ([]string, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT ticker FROM dhan_ohlcv_daily
		WHERE asset_type = 'stock'
		GROUP BY ticker HAVING count(*) >= $1 ORDER BY ticker`, minBars)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var t string
		if err := rows.Scan(&t); err != nil {
			return nil, err
		}
		out = append(out, t)
	}
	return out, rows.Err()
}

func scanSeries(next func() bool, scan func(...any) error, rowsErr func() error) (core.Series, error) {
	var times []time.Time
	var vals []float64
	for next() {
		var t time.Time
		var v float64
		if err := scan(&t, &v); err != nil {
			return core.Series{}, err
		}
		// Normalize to date (strip tz-time) so all sources align on days.
		d := time.Date(t.Year(), t.Month(), t.Day(), 0, 0, 0, 0, time.UTC)
		if len(times) > 0 && times[len(times)-1].Equal(d) {
			vals[len(vals)-1] = v // dedupe: keep the latest row for a day
			continue
		}
		times = append(times, d)
		vals = append(vals, v)
	}
	if err := rowsErr(); err != nil {
		return core.Series{}, err
	}
	if len(times) == 0 {
		return core.Series{}, fmt.Errorf("store: no rows")
	}
	return core.New(times, vals), nil
}

func getenv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
