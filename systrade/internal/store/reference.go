package store

import (
	"context"
	"time"
)

// Reference data used to slice research results: what a symbol IS, rather than
// what its price did. Both tables are stockey-owned mirrors; we only read.

// SectorCodes maps symbol to NSE's macro-economic sector code. Coverage is
// partial (about 3,700 of 6,100 symbols carry one), and the local mirror has
// the codes but no lookup table of their names — callers that need a readable
// label identify a sector by its largest members instead.
func (s *Store) SectorCodes(ctx context.Context) (map[string]string, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT symbol, sector_code FROM dim_security
		WHERE sector_code IS NOT NULL AND symbol IS NOT NULL`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]string{}
	for rows.Next() {
		var sym, code string
		if err := rows.Scan(&sym, &code); err != nil {
			return nil, err
		}
		out[sym] = code
	}
	return out, rows.Err()
}

// MCapPoint is one reported market capitalisation.
type MCapPoint struct {
	Date time.Time
	MCap float64
}

// MarketCaps returns each symbol's reported market caps, ascending by date, so
// a caller can read the latest value known ON a given date rather than one
// published afterwards. Coverage is the large/mid-cap end only (~970 symbols).
func (s *Store) MarketCaps(ctx context.Context) (map[string][]MCapPoint, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT symbol, date, mcap FROM historical_mcap
		WHERE mcap IS NOT NULL AND mcap > 0
		ORDER BY symbol, date`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string][]MCapPoint{}
	for rows.Next() {
		var sym string
		var d time.Time
		var m float64
		if err := rows.Scan(&sym, &d, &m); err != nil {
			return nil, err
		}
		out[sym] = append(out[sym], MCapPoint{
			Date: time.Date(d.Year(), d.Month(), d.Day(), 0, 0, 0, 0, time.UTC),
			MCap: m,
		})
	}
	return out, rows.Err()
}
