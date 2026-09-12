package main

import (
	"math"
	"testing"
	"time"

	"github.com/ranedk/systrader/internal/sleeve"
)

func dday(n int) time.Time { return time.Date(2020, 1, 1, 0, 0, 0, 0, time.UTC).AddDate(0, 0, n) }

func book(start int, net ...float64) sleeve.Book {
	b := sleeve.Book{Name: "b"}
	for i, r := range net {
		b.Dates = append(b.Dates, dday(start+i))
		b.Gross = append(b.Gross, r+0.001)
		b.Net = append(b.Net, r)
		b.Turnover = append(b.Turnover, 0.1)
	}
	return b
}

func TestStaggerAveragesOnlyTheDatesEveryBookCovers(t *testing.T) {
	a := book(0, 0.01, 0.02, 0.03, 0.04)
	b := book(2, 0.05, 0.06, 0.07) // starts two days later, runs one day longer
	s := stagger([]sleeve.Book{a, b})
	if len(s.Dates) != 2 || !s.Dates[0].Equal(dday(2)) || !s.Dates[1].Equal(dday(3)) {
		t.Fatalf("common dates = %v, want days 2 and 3", s.Dates)
	}
	if math.Abs(s.Net[0]-(0.03+0.05)/2) > 1e-12 || math.Abs(s.Net[1]-(0.04+0.06)/2) > 1e-12 {
		t.Errorf("staggered net = %v", s.Net)
	}
}

func TestTrimDropsTheCashOnlyWarmUp(t *testing.T) {
	b := book(0, 0, 0, 0.01, 0.02)
	b.Turnover[0], b.Turnover[1] = 0, 0 // no positions until day 2
	start := firstTraded(b)
	if !start.Equal(dday(2)) {
		t.Fatalf("first traded %v, want day 2", start)
	}
	tr := trimFrom(b, start)
	if len(tr.Net) != 2 || tr.Net[0] != 0.01 {
		t.Errorf("trimmed book = %v", tr.Net)
	}
}

func TestDoubleCostChargesTheCostTwice(t *testing.T) {
	b := sleeve.Book{Dates: []time.Time{dday(0)}, Gross: []float64{0.010}, Net: []float64{0.0075}, Turnover: []float64{1}}
	if got := doubleCost(b).Net[0]; math.Abs(got-0.005) > 1e-12 {
		t.Errorf("net at 2x cost = %v, want 0.005 (cost 25bps -> 50bps)", got)
	}
}
