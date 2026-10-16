package stageapi

import (
	"testing"
	"time"

	"github.com/ranedk/systrader/internal/core"
)

func TestTruncateThroughKeepsBarsOnOrBeforeTheDay(t *testing.T) {
	d := func(s string) time.Time { x, _ := time.Parse("2006-01-02", s); return x }
	s := core.New([]time.Time{d("2026-10-01"), d("2026-10-02"), d("2026-10-05")}, []float64{1, 2, 3})
	got := truncateThrough(s, d("2026-10-02"))
	if got.Len() != 2 || got.Values[1] != 2 {
		t.Fatalf("want 2 bars through 10-02, got %v", got.Values)
	}
	if truncateThrough(s, d("2026-09-30")).Len() != 0 {
		t.Fatal("a day before the first bar keeps nothing")
	}
}
