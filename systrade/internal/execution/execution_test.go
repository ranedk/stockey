package execution

import (
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/ranedk/systrader/internal/store"
)

func f(v float64) *float64 { return &v }

var day = time.Date(2026, 9, 11, 0, 0, 0, 0, time.UTC)

func sheet(rows ...store.PaperOrderRow) store.PaperPending {
	return store.PaperPending{BasedOn: day, Due: true, Orders: rows}
}

func row(sym, side string, from, to, price, value float64) store.PaperOrderRow {
	r := store.PaperOrderRow{Symbol: sym, Side: side, FillPrice: f(price), FromShares: f(from), ToShares: f(to)}
	if value > 0 {
		r.ValueRs = f(value)
	}
	return r
}

var ids = map[string][]store.Instrument{
	"ABB":  {{Symbol: "ABB", SecurityID: 13}},
	"M&M":  {{Symbol: "M&M", SecurityID: 2031}},
	"TCS":  {{Symbol: "TCS", SecurityID: 11536}},
	"TWIN": {{Symbol: "TWIN", SecurityID: 1}, {Symbol: "TWIN", SecurityID: 2}},
	"TINY": {{Symbol: "TINY", SecurityID: 7, FreezeQty: 100}},
}

func TestBuildReproducesTheSheet(t *testing.T) {
	b := Build("trend-quintile", sheet(
		row("TCS", "BUY", 0, 10, 3000, 30000),
		row("ABB", "EXIT", 9, 0, 7400, 66600),
		row("M&M", "BUY", 20, 20, 3200, 0), // inside the 10% band: nothing to send
	), ids)
	if !b.OK() {
		t.Fatalf("problems: %v", b.Problems)
	}
	if len(b.Orders) != 2 || b.SheetTrades != 2 {
		t.Fatalf("orders %d, sheet trades %d", len(b.Orders), b.SheetTrades)
	}
	sell, buy := b.Orders[0], b.Orders[1]
	if sell.Symbol != "ABB" || sell.Side != "SELL" || sell.Quantity != 9 || sell.SecurityID != "13" {
		t.Fatalf("first order %+v, want the ABB exit of 9", sell)
	}
	if buy.Symbol != "TCS" || buy.Side != "BUY" || buy.Quantity != 10 || buy.SecurityID != "11536" {
		t.Fatalf("second order %+v", buy)
	}
	if b.BuyRs != 30000 || b.SellRs != 66600 {
		t.Fatalf("buy %v sell %v", b.BuyRs, b.SellRs)
	}
}

func TestBuildRefusesWhatItCannotSendExactly(t *testing.T) {
	cases := map[string]store.PaperOrderRow{
		"no current Dhan security id": row("NOPE", "BUY", 0, 5, 100, 500),
		"refusing to guess":           row("TWIN", "BUY", 0, 5, 100, 500),
		"freeze quantity":             row("TINY", "BUY", 0, 500, 10, 5000),
		"unit error":                  row("TCS", "BUY", 0, 10, 3000, 3000),
		"no whole-share change":       row("TCS", "BUY", 10, 10.2, 3000, 600),
	}
	for want, r := range cases {
		b := Build("trend-quintile", sheet(r), ids)
		if b.OK() || !strings.Contains(strings.Join(b.Problems, "; "), want) {
			t.Errorf("%s: problems %v, want %q", r.Symbol, b.Problems, want)
		}
	}
}

func TestCorrelationIDsAreValidStableAndDistinct(t *testing.T) {
	ok := regexp.MustCompile(`^[A-Za-z0-9 _-]{1,30}$`)
	names := []string{"trend-quintile", "trend-speed-blend", "momentum-lookback-blend",
		"low-volatility-blend", "momentum-lowvol-combination"}
	seen := map[string]string{}
	for _, n := range names {
		for _, s := range []string{"M&M", "MM", "BAJAJ-AUTO", "ABCDEFGHIJKLMNOPQRST", "ABB"} {
			id := CorrelationID(n, day, s)
			if !ok.MatchString(id) {
				t.Fatalf("%q is not a valid Dhan correlation id", id)
			}
			if id != CorrelationID(n, day, s) {
				t.Fatal("not deterministic")
			}
			if prev, dup := seen[id]; dup {
				t.Fatalf("%s/%s collides with %s", n, s, prev)
			}
			seen[id] = n + "/" + s
		}
	}
	if CorrelationID("trend-quintile", day, "ABB") == CorrelationID("trend-quintile", day.AddDate(0, 0, 1), "ABB") {
		t.Fatal("a new sheet date must give a new id")
	}
}

func TestLastClosedSession(t *testing.T) {
	at := func(y int, m time.Month, d, h, min int) time.Time { return time.Date(y, m, d, h, min, 0, 0, IST) }
	date := func(m time.Month, d int) time.Time { return time.Date(2026, m, d, 0, 0, 0, 0, time.UTC) }
	hol := map[time.Time]bool{date(10, 2): true} // Gandhi Jayanti, a Friday
	cases := []struct {
		now  time.Time
		want time.Time
	}{
		{at(2026, 9, 14, 21, 15), date(9, 14)}, // Monday evening: today's close
		{at(2026, 9, 14, 15, 29), date(9, 11)}, // Monday before the close: Friday
		{at(2026, 9, 14, 15, 30), date(9, 14)},
		{at(2026, 9, 13, 21, 0), date(9, 11)}, // Sunday: Friday
		{at(2026, 10, 3, 10, 0), date(10, 1)}, // Saturday after a Friday holiday: Thursday
		{at(2026, 10, 2, 21, 0), date(10, 1)}, // the holiday itself
		// 21:00 IST is 15:30 UTC: the IST clock, not the host's, decides the date.
		{time.Date(2026, 9, 14, 19, 0, 0, 0, time.UTC), date(9, 14)},
		{time.Date(2026, 9, 14, 20, 0, 0, 0, time.UTC), date(9, 15).AddDate(0, 0, -1)},
	}
	for _, c := range cases {
		if got := LastClosedSession(c.now, hol); !got.Equal(c.want) {
			t.Errorf("%s: got %s, want %s", c.now, got.Format("2006-01-02"), c.want.Format("2006-01-02"))
		}
	}
}
