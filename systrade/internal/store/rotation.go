package store

import (
	"context"
	"encoding/json"
	"time"
)

// Industry rotation (docs/SECTOR_ROTATION_PRD.md). Membership and names come from two
// stockey-owned mirrors (master_sharpely_equity, fundamentals_sector_reference, synced
// since 2026-10-01); the snapshot tables below are systrader's own output.

// RotationMembership maps NSE symbol -> (sector code, industry code). A symbol listed
// twice prefers its active row.
func (s *Store) RotationMembership(ctx context.Context) (map[string][2]string, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT DISTINCT ON (symbol) symbol, sector_code, industry_code
		  FROM master_sharpely_equity
		 WHERE symbol IS NOT NULL AND industry_code IS NOT NULL AND sector_code IS NOT NULL
		 ORDER BY symbol, nse_active DESC NULLS LAST`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string][2]string{}
	for rows.Next() {
		var sym, sec, ind string
		if err := rows.Scan(&sym, &sec, &ind); err != nil {
			return nil, err
		}
		out[sym] = [2]string{sec, ind}
	}
	return out, rows.Err()
}

// SectorNames maps every sector / industry code to its latest name.
func (s *Store) SectorNames(ctx context.Context) (map[string]string, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT DISTINCT ON (code) code, description FROM fundamentals_sector_reference
		 WHERE description IS NOT NULL ORDER BY code, as_of_date DESC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]string{}
	for rows.Next() {
		var code, name string
		if err := rows.Scan(&code, &name); err != nil {
			return nil, err
		}
		out[code] = name
	}
	return out, rows.Err()
}

// EnsureRotationTables creates the snapshot tables.
func (s *Store) EnsureRotationTables(ctx context.Context) error {
	_, err := s.pool.Exec(ctx, `
		CREATE TABLE IF NOT EXISTS systrader_rotation_snapshot (
			as_of DATE PRIMARY KEY,
			computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
			payload JSONB NOT NULL
		)`)
	return err
}

// SaveRotationSnapshot stores one week's full snapshot (market, industries, stocks, stats)
// as one JSON document: the view reads it whole, and history stays queryable by as_of.
func (s *Store) SaveRotationSnapshot(ctx context.Context, asOf time.Time, payload any) error {
	b, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	_, err = s.pool.Exec(ctx, `
		INSERT INTO systrader_rotation_snapshot (as_of, computed_at, payload) VALUES ($1, now(), $2)
		ON CONFLICT (as_of) DO UPDATE SET computed_at = now(), payload = EXCLUDED.payload`, asOf, b)
	return err
}

// LatestRotationSnapshot returns the newest snapshot's JSON and its date.
func (s *Store) LatestRotationSnapshot(ctx context.Context) ([]byte, time.Time, error) {
	var b []byte
	var asOf time.Time
	err := s.pool.QueryRow(ctx, `
		SELECT payload, as_of FROM systrader_rotation_snapshot ORDER BY as_of DESC LIMIT 1`).Scan(&b, &asOf)
	return b, asOf, err
}

// StoryFilterRow is one company's stockey story score on one date (fundamentals_story_filter_daily,
// a stockey-owned view synced since 2026-10-01).
type StoryFilterRow struct {
	Date         time.Time
	Symbol       string
	Score        float64
	HasFlaw      bool
	ScoreVersion int
}

// StoryFilter returns every synced row with an NSE symbol, ascending by date.
func (s *Store) StoryFilter(ctx context.Context) ([]StoryFilterRow, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT date, symbol, story_score, has_flaw, score_version FROM fundamentals_story_filter_daily
		 WHERE symbol IS NOT NULL AND story_score IS NOT NULL ORDER BY date`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []StoryFilterRow
	for rows.Next() {
		var r StoryFilterRow
		if err := rows.Scan(&r.Date, &r.Symbol, &r.Score, &r.HasFlaw, &r.ScoreVersion); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}
