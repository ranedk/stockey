// Command rotation computes the industry-rotation panel (docs/SECTOR_ROTATION_PRD.md).
//
//	go run ./cmd/rotation snapshot     # write this week's snapshot for the API / screener
//	go run ./cmd/rotation themes       # theme indices, breadth and trend scores for stockey's theme map
//
// Reporting only: the snapshot is a view of facts; nothing here trades.
package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"sort"
	"time"

	"github.com/joho/godotenv"

	"github.com/ranedk/systrader/internal/bars"
	"github.com/ranedk/systrader/internal/rotation"
	"github.com/ranedk/systrader/internal/store"
)

// HistoryFrom gives the 2013-07 stats window a year of warm-up (30-week MA + 26-week RS).
var HistoryFrom = time.Date(2012, 1, 1, 0, 0, 0, 0, time.UTC)

func main() {
	_ = godotenv.Load()
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: rotation snapshot|themes")
		os.Exit(2)
	}
	ctx := context.Background()
	st, err := store.Open(ctx)
	if err != nil {
		log.Fatal(err)
	}
	defer st.Close()
	switch os.Args[1] {
	case "snapshot":
		if err := runSnapshot(ctx, st); err != nil {
			log.Fatal(err)
		}
	case "themes":
		if err := runThemes(ctx, st); err != nil {
			log.Fatal(err)
		}
	default:
		log.Fatalf("unknown command %q", os.Args[1])
	}
}

// LoadUniverse reads every EQ symbol's adjusted bars once and builds the weekly universe.
func LoadUniverse(ctx context.Context, st *store.Store, from, to time.Time) (*rotation.Universe, map[string]string, error) {
	b := rotation.NewBuilder()
	if err := st.StreamAdjustedBars(ctx, from, to, func(ser bars.Series) error {
		b.Add(ser)
		return nil
	}); err != nil {
		return nil, nil, err
	}
	raw, err := st.RotationMembership(ctx)
	if err != nil {
		return nil, nil, err
	}
	membership := make(map[string]rotation.Membership, len(raw))
	for sym, m := range raw {
		membership[sym] = rotation.Membership{Sector: m[0], Industry: m[1]}
	}
	names, err := st.SectorNames(ctx)
	if err != nil {
		return nil, nil, err
	}
	return rotation.Build(b.Panel(), membership), names, nil
}

func runSnapshot(ctx context.Context, st *store.Store) error {
	started := time.Now()
	u, names, err := LoadUniverse(ctx, st, HistoryFrom, time.Now())
	if err != nil {
		return err
	}
	last := len(u.Panel.Weeks) - 1
	if last < 0 {
		return fmt.Errorf("no weekly data")
	}
	last = lastCompletedWeek(u.Panel)
	snap := u.Snapshot(last, names)
	if err := st.EnsureRotationTables(ctx); err != nil {
		return err
	}
	if err := st.SaveRotationSnapshot(ctx, u.Panel.Weeks[last], snap); err != nil {
		return err
	}
	lead := 0
	for _, ind := range snap.Industries {
		if ind.Leading {
			lead++
		}
	}
	cand := 0
	for _, s := range snap.Stocks {
		if s.Candidate {
			cand++
		}
	}
	fmt.Printf("rotation snapshot %s: %d industries (%d leading), %d stocks (%d candidates), market stage %d, breadth %.1f%%, %s\n",
		snap.AsOf, len(snap.Industries), lead, len(snap.Stocks), cand, snap.Market.Stage, snap.Market.BreadthPct,
		time.Since(started).Round(time.Second))
	return nil
}

// lastCompletedWeek: the current week is unfinished until its Friday, so report the last
// COMPLETED week, the one the paper tracks act on, rather than a mid-week reading (review 2026-10-02).
func lastCompletedWeek(p *rotation.Panel) int {
	last := len(p.Weeks) - 1
	ist := time.Now().In(time.FixedZone("IST", 5*3600+1800))
	if w := p.Weeks[last]; w.Weekday() != time.Friday && rotation.WeekKey(w) == rotation.WeekKey(ist) && last > 0 {
		last--
	}
	return last
}

// runThemes builds the theme views for stockey's theme map (fundamentals_theme_member, synced).
// NSE EQ names come from the same stream as the rotation view; members outside it (BSE-only
// "BSE:<scrip>" keys, BE/SME series) are read through the stage API's series. The market index
// is built from the EQ stream alone, so RS matches the rotation view exactly.
func runThemes(ctx context.Context, st *store.Store) error {
	started := time.Now()
	rows, err := st.ThemeMembers(ctx)
	if err != nil {
		return fmt.Errorf("theme members (is fundamentals_theme_member synced?): %w", err)
	}
	if len(rows) == 0 {
		return fmt.Errorf("no theme members in fundamentals_theme_member")
	}
	b := rotation.NewBuilder()
	streamed := map[string]bool{}
	if err := st.StreamAdjustedBars(ctx, HistoryFrom, time.Now(), func(ser bars.Series) error {
		b.Add(ser)
		streamed[ser.Symbol] = true
		return nil
	}); err != nil {
		return err
	}
	themes := map[string][]rotation.ThemeMember{}
	extra := map[string]bool{}
	for _, r := range rows {
		themes[r.ThemeID] = append(themes[r.ThemeID], rotation.ThemeMember{Key: r.StageKey, Weight: r.Weight})
		if !streamed[r.StageKey] && !extra[r.StageKey] {
			extra[r.StageKey] = true
			closes, vols, err := st.StageSeries(ctx, r.StageKey)
			if err != nil {
				return fmt.Errorf("series for %s: %w", r.StageKey, err)
			}
			ser := bars.Series{Symbol: r.StageKey}
			for k, t := range closes.Times {
				if t.Before(HistoryFrom) {
					continue
				}
				c := closes.Values[k]
				ser.Bars = append(ser.Bars, bars.Bar{Date: t, Open: c, High: c, Low: c, Close: c, Vol: vols.Values[k]})
			}
			b.Add(ser)
		}
	}
	p := b.Panel()
	if len(p.Weeks) == 0 {
		return fmt.Errorf("no weekly data")
	}
	var nse []*rotation.Stock
	for sym, s := range p.Stocks {
		if streamed[sym] {
			nse = append(nse, s)
		}
	}
	sort.Slice(nse, func(i, j int) bool { return nse[i].Symbol < nse[j].Symbol })
	market := rotation.BuildIndex(p, nse, 1)
	last := lastCompletedWeek(p)
	snap := rotation.BuildThemes(p, market, themes, last)
	if err := st.EnsureThemeTables(ctx); err != nil {
		return err
	}
	if err := st.SaveThemeSnapshot(ctx, p.Weeks[last], snap); err != nil {
		return err
	}
	fmt.Printf("theme snapshot %s: %d themes, %d stocks (%d outside the EQ stream, %d with no series), %s\n",
		snap.AsOf, len(snap.Themes), len(snap.Stocks), len(extra), len(snap.Missing), time.Since(started).Round(time.Second))
	return nil
}
