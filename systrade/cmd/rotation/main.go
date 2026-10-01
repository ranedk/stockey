// Command rotation computes the industry-rotation panel (docs/SECTOR_ROTATION_PRD.md).
//
//	go run ./cmd/rotation snapshot     # write this week's snapshot for the API / screener
//
// Reporting only: the snapshot is a view of facts; nothing here trades.
package main

import (
	"context"
	"fmt"
	"log"
	"os"
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
		fmt.Fprintln(os.Stderr, "usage: rotation snapshot")
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
