package store

import (
	"context"
	"testing"
	"time"
)

// Integration tests exercise every reader against the live systrade db.
// They skip (not fail) when the db is unreachable so `go test ./...` works
// on machines without the data layer.
func openOrSkip(t *testing.T) (*Store, context.Context) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	t.Cleanup(cancel)
	s, err := Open(ctx)
	if err != nil {
		t.Skipf("systrade db unreachable, skipping integration test: %v", err)
	}
	t.Cleanup(s.Close)
	return s, ctx
}

func TestIntegrationAdjustedOHLC(t *testing.T) {
	s, ctx := openOrSkip(t)
	opens, closes, err := s.AdjustedOHLC(ctx, "RELIANCE")
	if err != nil {
		t.Fatal(err)
	}
	if opens.Len() != closes.Len() || closes.Len() < 2500 {
		t.Fatalf("RELIANCE adjusted OHLC: %d opens / %d closes, want ≥2500 aligned bars (2013+)",
			opens.Len(), closes.Len())
	}
	if closes.Times[0].Year() > 2014 {
		t.Fatalf("adjusted history starts %v, want ≤2014 (gap-fix regression)", closes.Times[0])
	}
	for i := range closes.Values {
		if closes.Values[i] <= 0 || opens.Values[i] <= 0 {
			t.Fatalf("non-positive price at %v", closes.Times[i])
		}
		if !opens.Times[i].Equal(closes.Times[i]) {
			t.Fatalf("open/close date misalignment at index %d", i)
		}
	}
}

func TestIntegrationAdjustedCloses(t *testing.T) {
	s, ctx := openOrSkip(t)
	ser, err := s.AdjustedCloses(ctx, "TCS")
	if err != nil {
		t.Fatal(err)
	}
	if ser.Len() < 2500 {
		t.Fatalf("TCS adjusted closes: %d bars, want ≥2500", ser.Len())
	}
	// Split adjustment sanity: the adjusted series must not contain any
	// single-day move beyond ±35% (raw series DO at bonus/split dates).
	for i := 1; i < ser.Len(); i++ {
		r := ser.Values[i]/ser.Values[i-1] - 1
		if r > 0.35 || r < -0.35 {
			t.Fatalf("suspicious %+.0f%% daily move at %v — corporate action not adjusted?",
				r*100, ser.Times[i])
		}
	}
}

func TestIntegrationAdjustedSymbols(t *testing.T) {
	s, ctx := openOrSkip(t)
	syms, err := s.AdjustedSymbols(ctx, 2500)
	if err != nil {
		t.Fatal(err)
	}
	if len(syms) < 500 {
		t.Fatalf("only %d symbols with ≥2500 adjusted bars, expected hundreds", len(syms))
	}
}

func TestIntegrationDailyCloses(t *testing.T) {
	s, ctx := openOrSkip(t)
	// dhan_ohlcv_daily is fallback-only since the adjusted table became
	// primary; after the 2026-07 stockey reorg its stock history starts
	// ~2021. It only needs to be present and fresh.
	ser, err := s.DailyCloses(ctx, "RELIANCE")
	if err != nil {
		t.Fatal(err)
	}
	if ser.Len() < 1000 {
		t.Fatalf("RELIANCE dhan closes: %d bars, want ≥1000", ser.Len())
	}
}

func TestIntegrationIndexReaders(t *testing.T) {
	s, ctx := openOrSkip(t)
	// Canonical NSE naming in nseindia_indices is mixed-case: "Nifty 50",
	// "Nifty Bank", … (exact match — it's a data key).
	closes, err := s.IndexCloses(ctx, "Nifty 50")
	if err != nil {
		t.Fatal(err)
	}
	if closes.Len() < 2000 {
		t.Fatalf("Nifty 50 index closes: %d bars, want ≥2000", closes.Len())
	}
	dy, err := s.IndexDivYield(ctx, "Nifty 50")
	if err != nil {
		t.Fatal(err)
	}
	last, ok := dy.Last()
	if !ok || last <= 0 || last > 10 {
		t.Fatalf("Nifty 50 div yield last=%v, want (0,10]%%", last)
	}
}

func TestIntegrationLotSize(t *testing.T) {
	s, ctx := openOrSkip(t)
	lot, err := s.LotSize(ctx, "NIFTY")
	if err != nil {
		t.Fatal(err)
	}
	if lot <= 0 || lot > 200 {
		t.Fatalf("NIFTY lot size %v, want a plausible value (25–75 era-dependent)", lot)
	}
}

func TestIntegrationBackfillReaders(t *testing.T) {
	s, ctx := openOrSkip(t)
	ser, err := s.BackfillCloses(ctx, "GOLDBEES")
	if err != nil {
		t.Fatal(err)
	}
	if ser.Len() < 2000 {
		t.Fatalf("GOLDBEES backfill: %d bars, want ≥2000", ser.Len())
	}
	max, err := s.BackfillMaxDate(ctx, 14428) // GOLDBEES security id
	if err != nil {
		t.Fatal(err)
	}
	if max.Before(time.Now().AddDate(0, -1, 0)) {
		t.Fatalf("GOLDBEES backfill stale: max date %v", max)
	}
	contracts, err := s.ListFuturesContracts(ctx, []string{"NIFTY", "GOLDM"})
	if err != nil {
		t.Fatal(err)
	}
	if len(contracts) < 5 {
		t.Fatalf("only %d futures contracts for NIFTY+GOLDM", len(contracts))
	}
	for _, c := range contracts {
		if c.Segment != "NSE_FNO" && c.Segment != "MCX_COMM" {
			t.Fatalf("contract %s: bad segment %q", c.Symbol, c.Segment)
		}
	}
}
