package capacity

import (
	"math"
	"testing"
	"time"

	"github.com/ranedk/systrader/internal/costs"
	"github.com/ranedk/systrader/internal/paper"
)

var t0 = time.Date(2020, 1, 1, 0, 0, 0, 0, time.UTC)

// book builds one day. px is the adjusted price, flat through the day except
// that PrevClose is the previous day's price; fc ranks the names.
func book(i int, px, prev, fc map[string]float64) paper.Day {
	d := paper.Day{Date: t0.AddDate(0, 0, i)}
	for s, p := range px {
		pc := prev[s]
		if pc == 0 {
			pc = p
		}
		d.Obs = append(d.Obs, paper.Obs{Symbol: s, Open: p, Close: p, PrevClose: pc,
			Forecast: fc[s], Turnover: 1e15, Eligible: true})
	}
	return d
}

func spec() paper.Spec {
	s := paper.FrozenSpec()
	s.Quantile = 2
	s.RebalanceEvery = 2
	return s
}

func ones(time.Time) (map[string]float64, error) {
	return map[string]float64{"A": 1, "B": 1, "C": 1, "D": 1}, nil
}

var rank = map[string]float64{"A": 4, "B": 3, "C": 2, "D": 1}

func flat(p float64) map[string]float64 {
	return map[string]float64{"A": p, "B": p, "C": p, "D": p}
}

func TestWholeSharesAndInertia(t *testing.T) {
	up5 := map[string]float64{"A": 105, "B": 100, "C": 100, "D": 100}
	up30 := map[string]float64{"A": 130, "B": 100, "C": 100, "D": 100}
	days := []paper.Day{
		book(0, flat(100), nil, rank),
		book(1, flat(100), nil, rank),
		book(2, up5, flat(100), rank), // 5 shares vs a target of round(500/105)=5: hold
		book(3, up5, nil, rank),
		book(4, up30, up5, rank), // target round(500/130)=4, 20% away: sell one
	}
	r, err := Simulate(spec(), days, ones, Policy{Capital: 1000, Inertia: 0.10})
	if err != nil {
		t.Fatal(err)
	}
	if want := 1000.0 + 130; math.Abs(r.Traded-want) > 1e-9 {
		t.Fatalf("traded %v, want %v (two buys of 5 x 100, one sale of 1 x 130)", r.Traded, want)
	}
	if r.DP != costs.DPCharge {
		t.Fatalf("DP %v, want one sale's %v", r.DP, costs.DPCharge)
	}
	if math.Abs(r.Deployed[4]-(4*130+500)) > 1e-9 {
		t.Fatalf("deployed %v, want 1020", r.Deployed[4])
	}
	// The 5% rise on day 2 is a gross return on the deployed book.
	if want := 25.0 / 1000; math.Abs(r.Gross[2]-want) > 1e-12 {
		t.Fatalf("gross day 2 = %v, want %v", r.Gross[2], want)
	}
	if r.Net[0] >= r.Gross[0] || r.Net[2] != r.Gross[2] {
		t.Fatal("costs belong to trading days only")
	}
}

func TestSplitIsNotATrade(t *testing.T) {
	// Adjusted price flat at 50. Before the 1:2 split the market quoted 100
	// (ratio 2); after, 50. The holding's 5 shares become 10 — no order.
	ratio := 2.0
	raw := func(time.Time) (map[string]float64, error) {
		return map[string]float64{"A": ratio, "B": ratio, "C": ratio, "D": ratio}, nil
	}
	days := []paper.Day{book(0, flat(50), nil, rank), book(1, flat(50), nil, rank), book(2, flat(50), nil, rank)}
	var r *Result
	var err error
	// Switch the ratio between the two rebalances by simulating in two steps'
	// worth of calls: the closure is read on day 0 and day 2.
	calls := 0
	rr := func(d time.Time) (map[string]float64, error) {
		calls++
		if calls > 1 {
			ratio = 1
		}
		return raw(d)
	}
	if r, err = Simulate(spec(), days, rr, Policy{Capital: 1000, Inertia: 0.10}); err != nil {
		t.Fatal(err)
	}
	if r.Traded != 1000 || r.DP != 0 {
		t.Fatalf("traded %v with DP %v; a split must not trade", r.Traded, r.DP)
	}
	if r.SmallPositionDays != 0 {
		t.Fatal("5 and then 10 shares are not small positions")
	}
}

func TestMinimumPositionLiftsSmallTargets(t *testing.T) {
	days := []paper.Day{book(0, flat(100), nil, rank)}
	r, err := Simulate(spec(), days, ones, Policy{Capital: 1000, MinPosition: 600, Inertia: 0.10})
	if err != nil {
		t.Fatal(err)
	}
	if r.Deployed[0] != 1200 {
		t.Fatalf("deployed %v, want 2 x 6 shares x 100", r.Deployed[0])
	}
}

func TestLumpyNamesAreCountedNotHidden(t *testing.T) {
	px := map[string]float64{"A": 1200, "B": 400, "C": 100, "D": 100}
	days := []paper.Day{book(0, px, nil, rank), book(1, px, nil, rank)}
	r, err := Simulate(spec(), days, ones, Policy{Capital: 1000, Inertia: 0.10})
	if err != nil {
		t.Fatal(err)
	}
	if r.Targeted != 2 || r.RoundedToZero != 1 {
		t.Fatalf("targeted %d, rounded to zero %d; want 2 and 1 (A at 1200 against 500)", r.Targeted, r.RoundedToZero)
	}
	if r.PositionDays != 2 || r.SmallPositionDays != 2 {
		t.Fatalf("position-days %d, small %d; want B's 1 share on both days", r.PositionDays, r.SmallPositionDays)
	}
}

func TestExitsSellEverything(t *testing.T) {
	swap := map[string]float64{"A": 1, "B": 2, "C": 3, "D": 4}
	days := []paper.Day{book(0, flat(100), nil, rank), book(1, flat(100), nil, rank), book(2, flat(100), nil, swap)}
	r, err := Simulate(spec(), days, ones, Policy{Capital: 1000, Inertia: 0.10})
	if err != nil {
		t.Fatal(err)
	}
	if r.DP != 2*costs.DPCharge || r.Traded != 3000 {
		t.Fatalf("DP %v traded %v; want two full exits and two entries", r.DP, r.Traded)
	}
	rb := r.Rebalances[len(r.Rebalances)-1]
	if len(rb.Positions) != 2 {
		t.Fatalf("held %d after the swap, want 2", len(rb.Positions))
	}
}

func TestSummaryAgainstAnIdenticalBook(t *testing.T) {
	days := []paper.Day{book(0, flat(100), nil, rank), book(1, flat(110), flat(100), rank)}
	r, err := Simulate(spec(), days, ones, Policy{Capital: 1000, Inertia: 0.10})
	if err != nil {
		t.Fatal(err)
	}
	ideal := []paper.NavPoint{{Date: days[0].Date, Return: r.Gross[0]}, {Date: days[1].Date, Return: r.Gross[1]}}
	s := Summarize(r, ideal, ideal)
	if s.TE != 0 || s.MeanDiff != 0 {
		t.Fatalf("TE %v, mean diff %v against itself", s.TE, s.MeanDiff)
	}
	if s.Names != 2 || s.MedianPos != 500 {
		t.Fatalf("names %v median %v", s.Names, s.MedianPos)
	}
}

func TestTodaySizedScalesCapitalByBookSize(t *testing.T) {
	// Today's book is 4 names; this day's is 2, so Rs 1,000 sizes as Rs 500:
	// 250 a name, the position a 4-name book at Rs 1,000 would hold.
	days := []paper.Day{book(0, flat(50), nil, rank)}
	r, err := Simulate(spec(), days, ones, Policy{Capital: 1000, Inertia: 0.10, RefNames: 4})
	if err != nil {
		t.Fatal(err)
	}
	if r.Deployed[0] != 500 || r.Capital[0] != 500 {
		t.Fatalf("deployed %v on capital %v, want 500 on 500", r.Deployed[0], r.Capital[0])
	}
	if got := Summarize(r, nil, nil).Deployed; got != 1 {
		t.Fatalf("deployed share %v, want 1", got)
	}
}

func TestPlanOrdersIsTheNextRebalance(t *testing.T) {
	days := []paper.Day{book(0, flat(100), nil, rank)}
	r, err := Simulate(spec(), days, ones, Policy{Capital: 1000, Inertia: 0.10})
	if err != nil {
		t.Fatal(err)
	}
	swap := map[string]float64{"A": 1, "B": 2, "C": 3, "D": 4}
	next := book(1, flat(100), nil, swap)
	ratios, _ := ones(next.Date)
	trades, held, err := r.PlanOrders(spec(), next, ratios)
	if err != nil {
		t.Fatal(err)
	}
	if held["A"] != 5 || held["B"] != 5 || len(held) != 2 {
		t.Fatalf("held going in: %v, want A and B at 5 shares", held)
	}
	want := []struct {
		sym, side string
		to        float64
	}{{"A", "EXIT", 0}, {"B", "EXIT", 0}, {"C", "BUY", 5}, {"D", "BUY", 5}}
	if len(trades) != len(want) {
		t.Fatalf("%d trades, want %d: %+v", len(trades), len(want), trades)
	}
	for i, w := range want {
		tr := trades[i]
		if tr.Symbol != w.sym || tr.Side != w.side || tr.ToShares != w.to || tr.Value != 500 {
			t.Fatalf("trade %d = %+v, want %s %s to %v worth 500", i, tr, w.side, w.sym, w.to)
		}
	}
	if trades[0].Cost < costs.DPCharge || trades[2].Cost >= costs.DPCharge {
		t.Fatal("a sale pays the DP charge and a purchase does not")
	}
}

func TestBookRendersTheAccount(t *testing.T) {
	days := []paper.Day{book(0, flat(100), nil, rank), book(1, flat(110), flat(100), rank)}
	r, err := Simulate(spec(), days, ones, Policy{Capital: 1000, Inertia: 0.10})
	if err != nil {
		t.Fatal(err)
	}
	b := r.Book("account")
	if want := 100 * (1 + r.Net[0]) * (1 + r.Net[1]); math.Abs(b.NAV[1].NAV-want) > 1e-9 {
		t.Fatalf("NAV %v, want %v", b.NAV[1].NAV, want)
	}
	if !b.NAV[0].Rebalanced || b.NAV[1].Rebalanced || b.NAV[0].Cost <= 0 || b.NAV[1].Cost != 0 {
		t.Fatalf("rebalance and cost marks wrong: %+v", b.NAV)
	}
	if b.Shares["A"] != 5 || b.Shares["B"] != 5 || math.Abs(b.Holdings["A"]+b.Holdings["B"]-1) > 1e-12 {
		t.Fatalf("shares %v weights %v", b.Shares, b.Holdings)
	}
	if e := b.Entries["A"]; e.Price != 100 || e.LastPrice != 110 {
		t.Fatalf("entry %+v, want bought at 100, last 110", e)
	}
	if len(b.Orders) != 2 || !b.Orders[0].InShares || b.Orders[0].ToShares != 5 || b.Orders[0].CostRs <= 0 ||
		math.Abs(b.Orders[0].ToWeight-0.5) > 1e-12 {
		t.Fatalf("orders %+v", b.Orders)
	}
}

func TestPlanOrdersPricesAHeldNameThatLeftEQFromItsFallbackSeries(t *testing.T) {
	r := &Result{Policy: AccountPolicy(), hold: map[string]*holding{"GONEBE": {shares: 100, value: 80000}}}
	latest := paper.Day{Date: time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)}
	trades, held, err := r.PlanOrders(spec(), latest, map[string]float64{}, map[string]float64{"GONEBE": 800})
	if err != nil || len(trades) != 1 {
		t.Fatalf("one exit expected, got %v %v", trades, err)
	}
	if tr := trades[0]; tr.Side != "EXIT" || tr.FromShares != 100 || tr.ToShares != 0 || held["GONEBE"] != 100 {
		t.Fatalf("exit 100 shares at the BE price, got %+v held %v", tr, held)
	}
}
