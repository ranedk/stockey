package traits

import (
	"context"
	"math"
	"sync"
	"time"

	"github.com/ranedk/systrader/internal/bars"
	"github.com/ranedk/systrader/internal/store"
)

// The inputs every trait computation shares, so `cmd/traits` (which judges
// the traits) and `cmd/slice` (which cuts by them) measure exactly the same
// thing.

// Windows and minimums, one set for every caller.
const (
	TurnoverWindow   = 60
	BetaWindow       = 252 // a year of days, the published convention for beta
	BetaMinValid     = 200
	HighWindow       = 252 // the 52-week high
	DeliveryWindow   = 20
	DeliveryMinValid = 15
	CircuitWindow    = 60 // a quarter of trading
	// MaxDailyMove: a daily move beyond ±50% is a data error or a mis-adjusted
	// corporate action, not a price — NSE's circuit limits cap real daily
	// moves at 20%.
	MaxDailyMove = 0.5
	// marketMinNames: a day with fewer eligible names has no market to speak of.
	marketMinNames = 100
)

// DailyReturn is bar i's close-to-close return, NaN where it cannot be a
// real price move.
func DailyReturn(b []bars.Bar, i int) float64 {
	if i == 0 || b[i-1].Close <= 0 || b[i].Close <= 0 {
		return math.NaN()
	}
	r := b[i].Close/b[i-1].Close - 1
	if math.Abs(r) > MaxDailyMove {
		return math.NaN()
	}
	return r
}

// Day is t's calendar date at midnight UTC — the key every table joins on.
func Day(t time.Time) time.Time {
	u := t.UTC()
	return time.Date(u.Year(), u.Month(), u.Day(), 0, 0, 0, 0, time.UTC)
}

// Align lays dated values onto a bar series' dates, NaN where there is none.
func Align(times []time.Time, pts []store.DatedValue) []float64 {
	byDate := make(map[time.Time]float64, len(pts))
	for _, p := range pts {
		byDate[p.Date] = p.Value
	}
	out := make([]float64, len(times))
	for i, t := range times {
		out[i] = math.NaN()
		if v, ok := byDate[t]; ok {
			out[i] = v
		}
	}
	return out
}

// MarketReturns scans the bar cache for the market beta is measured against —
// the equal-weight mean return of the eligible universe, eligibility decided
// the day BEFORE (60-bar median turnover at or above floor) — and for each
// symbol's last trading day.
func MarketReturns(cache string, floor float64) (map[time.Time]float64, map[string]time.Time, error) {
	type acc struct {
		sum float64
		n   int
	}
	sums := map[time.Time]*acc{}
	last := map[string]time.Time{}
	var mu sync.Mutex
	err := bars.ScanParallel(cache, 0, func(ser bars.Series) {
		b := ser.Bars
		if len(b) < 2 {
			return
		}
		turn := bars.MedianTurnover(b, TurnoverWindow)
		local := map[time.Time]float64{}
		for i := 1; i < len(b); i++ {
			r := DailyReturn(b, i)
			if math.IsNaN(r) || math.IsNaN(turn[i-1]) || turn[i-1] < floor {
				continue
			}
			local[Day(b[i].Date)] = r
		}
		mu.Lock()
		for d, r := range local {
			a := sums[d]
			if a == nil {
				a = &acc{}
				sums[d] = a
			}
			a.sum += r
			a.n++
		}
		last[ser.Symbol] = Day(b[len(b)-1].Date)
		mu.Unlock()
	})
	if err != nil {
		return nil, nil, err
	}
	mkt := map[time.Time]float64{}
	for d, a := range sums {
		if a.n >= marketMinNames {
			mkt[d] = a.sum / float64(a.n)
		}
	}
	return mkt, last, nil
}

// Context holds what a symbol's traits need beyond its own bars.
type Context struct {
	Market   map[time.Time]float64
	Delivery map[string][]store.DatedValue
	Circuits map[string][]store.DatedValue
}

// LoadContext builds the Context for decisions in [from, to] on the universe
// above floor. Delivery and band hits are loaded from 120 calendar days
// before `from`, which covers their 20- and 60-trading-day windows.
func LoadContext(ctx context.Context, st *store.Store, cache string, floor float64, from, to time.Time) (Context, error) {
	mkt, _, err := MarketReturns(cache, floor)
	if err != nil {
		return Context{}, err
	}
	loadFrom := from.AddDate(0, 0, -120)
	deliv, err := st.DeliveryPercents(ctx, loadFrom, to)
	if err != nil {
		return Context{}, err
	}
	circuits, err := st.CircuitHits(ctx, loadFrom, to)
	if err != nil {
		return Context{}, err
	}
	return Context{Market: mkt, Delivery: deliv, Circuits: circuits}, nil
}

// Arrays is one symbol's traits, one value per bar, NaN where undefined.
type Arrays struct {
	Beta, Dist52, Delivery, UpCircuits, LoCircuits []float64
}

// For computes a symbol's traits on every bar.
func (c Context) For(ser bars.Series) Arrays {
	b := ser.Bars
	n := len(b)
	times := make([]time.Time, n)
	closes := make([]float64, n)
	highs := make([]float64, n)
	ret := make([]float64, n)
	mk := make([]float64, n)
	for i, x := range b {
		times[i], closes[i], highs[i] = Day(x.Date), x.Close, x.High
		ret[i] = DailyReturn(b, i)
		mk[i] = math.NaN()
		if v, ok := c.Market[times[i]]; ok {
			mk[i] = v
		}
	}
	beta, _ := RollingBetaIdio(ret, mk, BetaWindow, BetaMinValid)
	ch := Align(times, c.Circuits[ser.Symbol])
	for i := range ch {
		if math.IsNaN(ch[i]) {
			ch[i] = 0 // the band-hit file lists hits only: no row is no hit
		}
	}
	return Arrays{
		Beta:       beta,
		Dist52:     RollingDistFromHigh(highs, closes, HighWindow),
		Delivery:   RollingMedian(Align(times, c.Delivery[ser.Symbol]), DeliveryWindow, DeliveryMinValid),
		UpCircuits: RollingCount(ch, 1, CircuitWindow),
		LoCircuits: RollingCount(ch, -1, CircuitWindow),
	}
}
