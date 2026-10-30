package store

import (
	"context"
	"encoding/json"
	"time"
)

// ThemeMemberRow is one stock in one theme from stockey's fundamentals_theme_member (today's
// members only, synced in full; stockey docs/THEMATIC_WATCHLIST_PRD.md build step 5).
type ThemeMemberRow struct {
	ThemeID  string
	StageKey string
	Weight   float64
}

func (s *Store) ThemeMembers(ctx context.Context) ([]ThemeMemberRow, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT theme_id, stage_key, weight FROM fundamentals_theme_member
		 WHERE stage_key IS NOT NULL ORDER BY theme_id, stage_key`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []ThemeMemberRow
	for rows.Next() {
		var r ThemeMemberRow
		if err := rows.Scan(&r.ThemeID, &r.StageKey, &r.Weight); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

func (s *Store) EnsureThemeTables(ctx context.Context) error {
	_, err := s.pool.Exec(ctx, `
		CREATE TABLE IF NOT EXISTS systrader_theme_snapshot (
			as_of DATE PRIMARY KEY,
			computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
			payload JSONB NOT NULL
		)`)
	return err
}

// SaveThemeSnapshot stores one week's theme views as one JSON document, like the rotation one.
func (s *Store) SaveThemeSnapshot(ctx context.Context, asOf time.Time, payload any) error {
	b, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	_, err = s.pool.Exec(ctx, `
		INSERT INTO systrader_theme_snapshot (as_of, computed_at, payload) VALUES ($1, now(), $2)
		ON CONFLICT (as_of) DO UPDATE SET computed_at = now(), payload = EXCLUDED.payload`, asOf, b)
	return err
}

// LatestThemeSnapshot returns the newest theme snapshot's JSON, its week and when it was computed.
func (s *Store) LatestThemeSnapshot(ctx context.Context) ([]byte, time.Time, time.Time, error) {
	var b []byte
	var asOf, computed time.Time
	err := s.pool.QueryRow(ctx, `
		SELECT payload, as_of, computed_at FROM systrader_theme_snapshot ORDER BY as_of DESC LIMIT 1`).Scan(&b, &asOf, &computed)
	return b, asOf, computed, err
}
