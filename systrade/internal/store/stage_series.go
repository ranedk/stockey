package store

import (
	"context"
	"strings"
	"time"

	"github.com/ranedk/systrader/internal/core"
)

// BSEKeyPrefix marks a stage key that is a BSE scrip code, not an NSE symbol.
const BSEKeyPrefix = "BSE:"

// stageSeriesRank orders the NSE series one date can appear in; the lowest wins.
const stageSeriesRank = `CASE series WHEN 'EQ' THEN 0 WHEN 'BE' THEN 1 WHEN 'SM' THEN 2 WHEN 'ST' THEN 3 ELSE 4 END`

// StageKeys lists every key the stage API should try to classify (2026-09-24):
//   - NSE symbols trading in ANY equity series (EQ, BE trade-to-trade, SM/ST SME,
//     BZ), not EQ only -- a stock moved to BE looked "stale" and got no stage, and
//     stockey's portfolio fails closed on a missing stage;
//   - BSE-only companies as "BSE:<scrip_code>" -- stockey's watchlist is ~1/3
//     BSE-only names that systrader never read at all.
//
// This is a REPORTING read for the stage API only. Backtests and paper tracks keep
// AdjustedCloses (EQ only) -- changing their input series would alter frozen tracks.
// Minimum history is enforced on the merged series by the caller, not here, so a new
// NSE listing with years of BSE history still qualifies.
func (s *Store) StageKeys(ctx context.Context) ([]string, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT symbol FROM advisory_adjusted_ohlcv_daily
		 WHERE series IN ('EQ','BE','SM','ST','BZ') AND date >= now() - interval '60 days'
		 GROUP BY symbol
		UNION
		SELECT 'BSE:' || b.scrip_code FROM bse_advisory_adjusted_ohlcv_daily b
		 WHERE b.date >= now() - interval '60 days'
		   AND NOT EXISTS (
		       SELECT 1 FROM company_master cm
		         JOIN advisory_adjusted_ohlcv_daily a ON a.symbol = cm.nse_ticker
		        WHERE cm.bse_scrip_code = b.scrip_code AND a.date >= now() - interval '60 days')
		 GROUP BY b.scrip_code
		ORDER BY 1`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var k string
		if err := rows.Scan(&k); err != nil {
			return nil, err
		}
		out = append(out, k)
	}
	return out, rows.Err()
}

// StageSeries returns adjusted daily closes and volumes for one stage key. An NSE
// symbol uses its adjusted series across equity series (one row per date, EQ first)
// and is PREFIXED with the company's BSE history before its first NSE date -- a stock
// newly listed on NSE (ZFSTEERING, 2026-04) has decades on BSE. A "BSE:" key uses the
// BSE series alone. Both sources are backward-adjusted to today's prices, so the join
// point is continuous.
func (s *Store) StageSeries(ctx context.Context, key string) (closes, volumes core.Series, err error) {
	var query string
	var arg string
	if strings.HasPrefix(key, BSEKeyPrefix) {
		arg = strings.TrimPrefix(key, BSEKeyPrefix)
		query = `
			SELECT date, adj_close, COALESCE(adj_volume, 0) FROM bse_advisory_adjusted_ohlcv_daily
			 WHERE scrip_code = $1 AND adj_close > 0
			 ORDER BY date`
	} else {
		arg = key
		query = `
			WITH nse AS (
			  SELECT DISTINCT ON (date) date, adj_close, adj_volume
			    FROM advisory_adjusted_ohlcv_daily
			   WHERE symbol = $1 AND series IN ('EQ','BE','SM','ST','BZ') AND adj_close > 0
			   ORDER BY date, ` + stageSeriesRank + `
			), bse AS (
			  SELECT DISTINCT ON (b.date) b.date, b.adj_close, b.adj_volume
			    FROM bse_advisory_adjusted_ohlcv_daily b
			    JOIN company_master cm ON cm.bse_scrip_code = b.scrip_code
			   WHERE cm.nse_ticker = $1 AND b.adj_close > 0
			     AND b.date < (SELECT min(date) FROM nse)
			   ORDER BY b.date
			)
			SELECT date, adj_close, COALESCE(adj_volume, 0) FROM bse
			UNION ALL
			SELECT date, adj_close, COALESCE(adj_volume, 0) FROM nse
			ORDER BY date`
	}
	rows, err := s.pool.Query(ctx, query, arg)
	if err != nil {
		return core.Series{}, core.Series{}, err
	}
	defer rows.Close()
	var times []time.Time
	var cvals, vvals []float64
	for rows.Next() {
		var t time.Time
		var c, v float64
		if err := rows.Scan(&t, &c, &v); err != nil {
			return core.Series{}, core.Series{}, err
		}
		d := time.Date(t.Year(), t.Month(), t.Day(), 0, 0, 0, 0, time.UTC)
		if n := len(times); n > 0 && times[n-1].Equal(d) {
			cvals[n-1], vvals[n-1] = c, v
			continue
		}
		times = append(times, d)
		cvals = append(cvals, c)
		vvals = append(vvals, v)
	}
	if err := rows.Err(); err != nil {
		return core.Series{}, core.Series{}, err
	}
	if len(times) == 0 {
		return core.Series{}, core.Series{}, nil
	}
	return core.New(times, cvals), core.New(times, vvals), nil
}
