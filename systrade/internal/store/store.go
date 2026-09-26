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

// AdjustedVolume loads the corporate-action-adjusted volume series for a
// symbol from advisory_adjusted_ohlcv_daily's adj_volume column (raw volume
// divided by the cumulative price-adjustment factor, so a share that has
// since split shows pre-split-equivalent volume — consistent with adj_close).
// EQ series only, same rationale as AdjustedCloses. Used by internal/stage's
// VolumeRatio field; nil-safe for callers that pass this through as *core.
// Series (Classify treats a missing/short volume series as "no confirmation
// data," never as an error).
func (s *Store) AdjustedVolume(ctx context.Context, symbol string) (core.Series, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT date, adj_volume FROM advisory_adjusted_ohlcv_daily
		WHERE symbol = $1 AND series = 'EQ'
		  AND adj_volume IS NOT NULL AND adj_volume >= 0
		ORDER BY date ASC`, symbol)
	if err != nil {
		return core.Series{}, err
	}
	defer rows.Close()
	return scanSeries(rows.Next, rows.Scan, rows.Err)
}

// AdjustedOHLC loads adjusted open AND close series for a symbol: raw
// opens/closes from nseindia_ohlcv (bhavcopy, 2013+) × cum_adj_factor from
// the advisory table. Opens exist so backtests can fill at open(T+1) after
// deciding at close(T) — filling at the decision close is mild look-ahead.
func (s *Store) AdjustedOHLC(ctx context.Context, symbol string) (opens, closes core.Series, err error) {
	rows, err := s.pool.Query(ctx, `
		SELECT o.date, o.open * a.cum_adj_factor, o.close * a.cum_adj_factor
		FROM nseindia_ohlcv o
		JOIN advisory_adjusted_ohlcv_daily a
		  ON a.symbol = o.symbol AND a.date = o.date AND a.series = o.series
		WHERE o.symbol = $1 AND o.series = 'EQ'
		  AND o.open > 0 AND o.close > 0 AND a.cum_adj_factor > 0
		ORDER BY o.date ASC`, symbol)
	if err != nil {
		return core.Series{}, core.Series{}, err
	}
	defer rows.Close()
	var times []time.Time
	var ovals, cvals []float64
	for rows.Next() {
		var t time.Time
		var o, c float64
		if err := rows.Scan(&t, &o, &c); err != nil {
			return core.Series{}, core.Series{}, err
		}
		d := time.Date(t.Year(), t.Month(), t.Day(), 0, 0, 0, 0, time.UTC)
		if len(times) > 0 && times[len(times)-1].Equal(d) {
			ovals[len(ovals)-1], cvals[len(cvals)-1] = o, c
			continue
		}
		times = append(times, d)
		ovals = append(ovals, o)
		cvals = append(cvals, c)
	}
	if err := rows.Err(); err != nil {
		return core.Series{}, core.Series{}, err
	}
	if len(times) == 0 {
		return core.Series{}, core.Series{}, fmt.Errorf("store: no adjusted OHLC for %s", symbol)
	}
	return core.New(times, ovals), core.New(times, cvals), nil
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

// IntradayCloses loads 1-min-granularity OHLCV close bars for a ticker from
// dhan_ohlcv_intraday (HF_DATA_PLATFORM_PLAN.md Phase 2 — synced via the
// SEPARATE scripts/sync_intraday_from_stockey.sh, not the daily sync_from_
// stockey.sh; see that script's own header for why). intervalMinutes matches
// the source's own bar granularity (1 for raw 1-min bars — no other interval
// has been confirmed present as of this writing).
//
// COVERAGE CAVEAT (confirmed live 2026-08-22): this is a curated ~635-ticker
// universe, NOT full NSE coverage — RELIANCE, for one, is absent. No active
// collector for this table was found anywhere in stockey's current codebase
// (no cron entry, empty STOCKEY_SYMBOLS, no live enqueue call for its queue-
// task handler) despite the data being fresh through the same day as this
// sync — the mechanism keeping it current is not yet understood. Confirm a
// symbol has coverage before relying on it; don't assume universe parity
// with the daily adjusted series.
//
// Deliberately does NOT reuse scanSeries: that helper normalizes to whole
// dates and dedupes same-day rows, which would silently collapse an entire
// day's worth of 1-min bars into one.
func (s *Store) IntradayCloses(ctx context.Context, ticker string, intervalMinutes int, from, to time.Time) (core.Series, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT timestamp, close FROM dhan_ohlcv_intraday
		WHERE ticker = $1 AND interval_minutes = $2
		  AND timestamp >= $3 AND timestamp <= $4
		  AND close IS NOT NULL AND close > 0
		ORDER BY timestamp ASC`, ticker, intervalMinutes, from, to)
	if err != nil {
		return core.Series{}, err
	}
	defer rows.Close()
	var times []time.Time
	var vals []float64
	for rows.Next() {
		var t time.Time
		var v float64
		if err := rows.Scan(&t, &v); err != nil {
			return core.Series{}, err
		}
		times = append(times, t)
		vals = append(vals, v)
	}
	if err := rows.Err(); err != nil {
		return core.Series{}, err
	}
	if len(times) == 0 {
		return core.Series{}, fmt.Errorf("store: no intraday bars for %s (interval=%dm, %s–%s)", ticker, intervalMinutes, from.Format("2006-01-02"), to.Format("2006-01-02"))
	}
	return core.New(times, vals), nil
}

// IntradayTickers lists distinct tickers with at least minBars bars at the
// given interval — the actual (curated, not full-universe) coverage, per
// IntradayCloses's caveat.
func (s *Store) IntradayTickers(ctx context.Context, intervalMinutes int, minBars int) ([]string, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT ticker FROM dhan_ohlcv_intraday
		WHERE interval_minutes = $1
		GROUP BY ticker HAVING count(*) >= $2 ORDER BY ticker`, intervalMinutes, minBars)
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
