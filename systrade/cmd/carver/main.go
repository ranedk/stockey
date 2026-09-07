// Command carver runs the pre-registered verification of the two inherited
// rules — EWMAC (16/32/64) and carry — on the Indian two-sleeve universe.
//
//	carver -period construction     develop and read here
//	carver -period holdout -burn-the-holdout   one look, then it is spent
//
// Design, universe, costs, controls and decision rule are fixed in
// research/preregistrations/2026-09-07_ewmac_carry_two_sleeve.md. This file
// implements that document and nothing else; a flag that would change the
// experiment is deliberately absent.
package main

import (
	"context"
	"flag"
	"fmt"
	"math"
	"os"
	"time"

	"github.com/ranedk/systrader/internal/backtest"
	"github.com/ranedk/systrader/internal/core"
	"github.com/ranedk/systrader/internal/data"
	"github.com/ranedk/systrader/internal/research"
	"github.com/ranedk/systrader/internal/rules"
	"github.com/ranedk/systrader/internal/sleeve"
	"github.com/ranedk/systrader/internal/store"
)

var references = map[string]string{
	"NIFTY":     "NIFTY",
	"BANKNIFTY": "BANKNIFTY",
	"GOLDM":     "GOLDBEES",
	"SILVERM":   "SILVERBEES",
}

// Fixed by the pre-registration.
var (
	tradedRules = []rules.Rule{
		rules.EWMAC{Fast: 16}, rules.EWMAC{Fast: 32}, rules.EWMAC{Fast: 64}, rules.Carry{},
	}
	shiftOffsets = []int{251, 379, 503, 631, 757, 883, 1009, 1131, 1259, 1381}

	constructionFrom = mustDate("2015-01-01")
	constructionTo   = mustDate("2021-12-31")
	holdoutFrom      = mustDate("2022-01-01")
	holdoutTo        = mustDate("2026-08-21")

	holdoutName = "carver-core-2022-01-to-2026-08"
)

const (
	fdm          = 1.35
	volTargetPct = 0.20
)

// diagnostics enables the post-hoc decomposition; never consulted by any
// decision rule.
var diagnostics bool

func main() {
	period := flag.String("period", "construction", "construction | holdout")
	capital := flag.Float64("capital", 2e7, "trading capital in INR (pre-registered: 2 crore)")
	costMult := flag.Float64("cost-mult", 1, "multiply every cost by this (the 2x sensitivity)")
	burn := flag.Bool("burn-the-holdout", false, "required for -period=holdout: this look is the only one")
	burns := flag.String("burns", "research/holdout_burns.json", "holdout burn registry")
	diag := flag.Bool("diagnostics", true, "print the post-hoc rule decomposition (decides nothing)")
	flag.Parse()
	diagnostics = *diag

	from, to := constructionFrom, constructionTo
	if *period == "holdout" {
		from, to = holdoutFrom, holdoutTo
		if !*burn {
			fatal(fmt.Errorf("refusing to read the holdout without -burn-the-holdout: %s..%s is spent the moment it is looked at",
				from.Format("2006-01-02"), to.Format("2006-01-02")))
		}
		reg, err := research.OpenRegistry(*burns)
		fatalIf(err)
		if reg.Burned(holdoutName) {
			fatal(fmt.Errorf("holdout %q is already burned — there is no second look", holdoutName))
		}
		fatalIf(reg.Burn(holdoutName, "ewmac16,32,64+carry|two-sleeve|20pct-vol|fdm1.35|shift-control",
			"Pre-registered in research/preregistrations/2026-09-07_ewmac_carry_two_sleeve.md."))
		fmt.Printf("holdout %s BURNED and recorded — this is the only look.\n\n", holdoutName)
	} else if *period != "construction" {
		fatal(fmt.Errorf("unknown period %q", *period))
	}

	for _, r := range tradedRules {
		fatalIf(rules.Validate(r))
	}

	ctx := context.Background()
	st, err := store.Open(ctx)
	fatalIf(err)
	defer st.Close()

	m, err := research.CountM("research/LEDGER.md")
	fatalIf(err)
	fmt.Printf("%s window %s..%s | capital Rs %.2f cr | vol target %.0f%% | costs x%.0f\n",
		*period, from.Format("2006-01-02"), to.Format("2006-01-02"), *capital/1e7, 100*volTargetPct, *costMult)
	fmt.Printf("rules: ewmac16/32/64 + carry, equal forecast weights, FDM %.2f, IDM derived point-in-time\n", fdm)
	fmt.Printf("decision bar (pre-registered): monthly paired t > 2.0 vs BOTH controls on the holdout;\n")
	fmt.Printf("workspace Bonferroni bar for context: t = %.2f at M = %d\n\n", backtest.BonferroniBar(m), m)

	runSleeve(ctx, st, "FUTURES", futuresSleeve, from, to, *capital, *costMult, 0)
	runSleeve(ctx, st, "ETF / CASH", etfSleeve, from, to, *capital, *costMult, 1.0)
}

func runSleeve(ctx context.Context, st *store.Store, name string, specs []spec, from, to time.Time, capital, costMult, maxGross float64) {
	fmt.Printf("========== %s SLEEVE ==========\n", name)
	insts, notes, err := loadSleeve(ctx, st, specs, from, to, costMult)
	fatalIf(err)
	for _, n := range notes {
		fmt.Printf("  %s\n", n)
	}
	if len(insts) < 2 {
		fmt.Printf("  -> not enough instruments to run a sleeve\n\n")
		return
	}
	var symbols []string
	for _, i := range insts {
		symbols = append(symbols, i.Meta.Symbol)
	}
	fmt.Printf("  %d instruments: %v\n", len(insts), symbols)

	base := backtest.Config{
		Capital: capital, VolTargetPct: volTargetPct, Compounding: true,
		FDM: fdm, MaxGrossLeverage: maxGross, ExecuteAtOpen: true,
	}

	real, err := run(base, tradedRules, insts)
	fatalIf(err)

	longOnly, err := run(base, []rules.Rule{alwaysLong{}}, insts)
	fatalIf(err)

	// The shifted control is averaged over its ten pre-registered offsets.
	var shiftBooks []sleeve.Book
	for _, off := range shiftOffsets {
		var shiftedRules []rules.Rule
		for _, r := range tradedRules {
			shiftedRules = append(shiftedRules, shifted{inner: r, offset: off})
		}
		res, err := run(base, shiftedRules, insts)
		fatalIf(err)
		shiftBooks = append(shiftBooks, book(res, capital))
	}

	realBook := book(real, capital)
	longBook := book(longOnly, capital)
	shiftBook := averageBooks(shiftBooks)

	fmt.Printf("\n  %-24s %9s %8s %7s %8s %9s %10s\n", "book", "ann ret", "ann vol", "SR", "skew", "maxDD", "cost SR")
	line := func(label string, r *backtest.Result) {
		mt := r.Metrics
		fmt.Printf("  %-24s %8.2f%% %7.2f%% %7.2f %8.2f %8.1f%% %10.2f\n",
			label, mt.AnnReturnPct, mt.AnnVolPct, mt.Sharpe, mt.Skew, mt.MaxDDPct, mt.CostDragSR)
	}
	line("ewmac+carry", real)
	line("always-long control", longOnly)
	fmt.Printf("  %-24s %8.2f%% %7.2f%% %7.2f %8s %8s %10s\n", "time-shift control",
		100*annualized(shiftBook), 100*annVol(shiftBook), sharpe(shiftBook), "-", "-", "-")

	fmt.Printf("\n  %-34s %9s %8s %11s\n", "paired monthly, rule minus control", "mean", "t", "months won")
	for _, p := range []struct {
		label string
		p     sleeve.Paired
	}{
		{"vs always-long (beta control)", sleeve.PairedMonthly(realBook, longBook)},
		{"vs time-shifted forecasts", sleeve.PairedMonthly(realBook, shiftBook)},
	} {
		fmt.Printf("  %-34s %8.3f%% %8.2f %10.1f%%\n", p.label, 100*p.p.MeanDiff, p.p.T, 100*p.p.MonthsWon)
	}

	// Post-hoc decomposition. The pre-registered decision above is already
	// made and this cannot change it; it exists to answer the obvious next
	// question — which of the two rules did what — before anyone guesses.
	if diagnostics {
		fmt.Printf("\n  post-hoc decomposition (decides nothing):\n")
		decomp := []struct {
			label string
			rs    []rules.Rule
		}{
			{"ewmac only (16/32/64)", []rules.Rule{rules.EWMAC{Fast: 16}, rules.EWMAC{Fast: 32}, rules.EWMAC{Fast: 64}}},
		}
		// A carry-only run on a sleeve where nothing HAS carry is not a
		// result, it is a flat line being compared to a rising market. Only
		// ask the question where it can be answered.
		withCarry := 0
		for _, i := range insts {
			if i.AnnCarry != nil {
				withCarry++
			}
		}
		if withCarry > 0 {
			decomp = append(decomp, struct {
				label string
				rs    []rules.Rule
			}{fmt.Sprintf("carry only (%d/%d instruments have a curve)", withCarry, len(insts)),
				[]rules.Rule{rules.Carry{}}})
		}
		for _, d := range decomp {
			res, err := run(base, d.rs, insts)
			if err != nil {
				fmt.Printf("  %-24s %v\n", d.label, err)
				continue
			}
			b := book(res, capital)
			vsLong := sleeve.PairedMonthly(b, longBook)
			fmt.Printf("  %-24s ann %7.2f%%  SR %5.2f  maxDD %5.1f%%  vs always-long %+7.3f%%/mo t=%5.2f\n",
				d.label, res.Metrics.AnnReturnPct, res.Metrics.Sharpe, res.Metrics.MaxDDPct,
				100*vsLong.MeanDiff, vsLong.T)
		}
	}

	fmt.Printf("\n  per instrument:\n")
	fmt.Printf("  %-12s %10s %10s %12s %14s\n", "symbol", "avg |pos|", "max |pos|", "turnover/yr", "four-block test")
	for _, s := range symbols {
		ir := real.Instruments[s]
		if ir == nil {
			continue
		}
		verdict := "PASS"
		if ir.MaxAbsPos < 4 {
			verdict = fmt.Sprintf("FAIL (max %.1f blocks)", ir.MaxAbsPos)
		}
		fmt.Printf("  %-12s %10.2f %10.2f %12.2f %14s\n", s, ir.AvgAbsPos, ir.MaxAbsPos, ir.Turnover, verdict)
	}
	fmt.Println()
}

func run(cfg backtest.Config, rs []rules.Rule, insts []*data.Instrument) (*backtest.Result, error) {
	w := 1 / float64(len(rs))
	for _, r := range rs {
		cfg.Rules = append(cfg.Rules, backtest.RuleSpec{Rule: r, Weight: w})
	}
	return backtest.Run(cfg, insts)
}

// book turns an engine result into the return series the paired statistics
// read: daily P&L over the equity that earned it.
func book(r *backtest.Result, capital float64) sleeve.Book {
	b := sleeve.Book{Name: "engine"}
	prev := capital
	for i, d := range r.Daily.Times {
		if prev <= 0 {
			break
		}
		ret := r.Daily.Values[i] / prev
		b.Dates = append(b.Dates, d)
		b.Gross = append(b.Gross, ret)
		b.Net = append(b.Net, ret)
		b.Turnover = append(b.Turnover, 0)
		prev = r.Equity.Values[i]
	}
	return b
}

// averageBooks averages several control runs day by day, so the control is the
// expected behaviour of the shift rather than one lucky draw.
func averageBooks(bs []sleeve.Book) sleeve.Book {
	if len(bs) == 0 {
		return sleeve.Book{}
	}
	out := sleeve.Book{Name: "time-shift control", Dates: bs[0].Dates}
	for i := range bs[0].Dates {
		var sum float64
		for _, b := range bs {
			if i < len(b.Net) {
				sum += b.Net[i]
			}
		}
		r := sum / float64(len(bs))
		out.Gross = append(out.Gross, r)
		out.Net = append(out.Net, r)
		out.Turnover = append(out.Turnover, 0)
	}
	return out
}

func annualized(b sleeve.Book) float64 {
	eq := 1.0
	for _, r := range b.Net {
		eq *= 1 + r
	}
	if len(b.Net) == 0 {
		return math.NaN()
	}
	return math.Pow(eq, 252/float64(len(b.Net))) - 1
}

func annVol(b sleeve.Book) float64 {
	if len(b.Net) < 2 {
		return math.NaN()
	}
	var s, ss float64
	for _, r := range b.Net {
		s += r
		ss += r * r
	}
	n := float64(len(b.Net))
	return math.Sqrt(math.Max(0, ss/n-(s/n)*(s/n))) * math.Sqrt(252)
}

func sharpe(b sleeve.Book) float64 {
	v := annVol(b)
	if v <= 0 {
		return math.NaN()
	}
	var s float64
	for _, r := range b.Net {
		s += r
	}
	return (s / float64(len(b.Net))) * 252 / v
}

// --- control rules ----------------------------------------------------------

// alwaysLong is control C1: own the sleeve, vol-targeted, and never think.
type alwaysLong struct{}

func (alwaysLong) Name() string { return "always-long" }

func (alwaysLong) Story() string {
	return "Control, not a rule: it holds every instrument at full positive forecast " +
		"every single day. It exists to measure how much of a trend system's return " +
		"is just being long a rising universe with volatility targeting on top."
}

func (alwaysLong) Scalar() float64 { return 1 }

func (alwaysLong) Raw(inst *data.Instrument, _ core.Series) core.Series {
	out := make([]float64, inst.Prices.Len())
	for i := range out {
		out[i] = 10
	}
	return core.New(inst.Prices.Times, out)
}

// shifted is control C2: the real forecast, slid along the time axis. Same
// distribution, same autocorrelation, same turnover and cost bill — no
// alignment with what prices actually did.
type shifted struct {
	inner  rules.Rule
	offset int
}

func (s shifted) Name() string { return s.inner.Name() + fmt.Sprintf("-shift%d", s.offset) }

func (s shifted) Story() string {
	return "Control, not a rule: " + s.inner.Name() + "'s own forecast circularly " +
		"shifted in time, so every statistical property of the signal survives except " +
		"the one being tested — that it points at the right day."
}

func (s shifted) Scalar() float64 { return s.inner.Scalar() }

func (s shifted) Raw(inst *data.Instrument, vol core.Series) core.Series {
	raw := s.inner.Raw(inst, vol)
	n := raw.Len()
	if n == 0 {
		return raw
	}
	out := make([]float64, n)
	for i := range out {
		out[i] = raw.Values[(i+s.offset)%n]
	}
	return core.New(raw.Times, out)
}

func mustDate(s string) time.Time {
	t, err := time.Parse("2006-01-02", s)
	fatalIf(err)
	return t
}

func fatalIf(err error) {
	if err != nil {
		fatal(err)
	}
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "carver:", err)
	os.Exit(1)
}
