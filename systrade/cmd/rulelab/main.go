// Command rulelab characterises the indicator library WITHOUT looking at
// returns.
//
//	rulelab scalars   average |raw forecast| per rule -> the forecast scalar
//	                  that puts it on Law 8's average-absolute-10 convention
//	rulelab corr      pairwise correlation between the library's forecasts,
//	                  plus correlation groups for handcrafted weighting
//
//	rulelab trial     the pre-registered backtest of the surviving rules
//	                  against two matched controls — this one DOES score
//	                  against returns, and is a trial (see trial.go)
//
// scalars and corr compute no P&L, no hit rate and no forward return, and
// that is the point: forecast-distribution fitting and correlation structure
// are the two things Carver calibrates on the whole sample precisely because
// they consult no performance number. Nothing here is a trial, nothing here
// consumes a holdout — see research/LEDGER.md's note on rows 1-2 and 10. The
// moment this command grows a subcommand that scores a rule against returns,
// that subcommand IS a trial and needs its own ledger row.
//
// Data comes from the bar cache built by `coi cache` (internal/bars), never
// from postgres directly: the universe is read once, date-bounded.
package main

import (
	"flag"
	"fmt"
	"math"
	"os"
	"runtime"
	"sync"
	"time"

	"github.com/ranedk/systrader/internal/bars"
	"github.com/ranedk/systrader/internal/core"
	"github.com/ranedk/systrader/internal/data"
	"github.com/ranedk/systrader/internal/rules"
)

const defaultCache = "data/cache/bars_daily.bin"

// volSpan/volMin match internal/core.PriceUnitVol's defaults and the engine's
// sizing vol, so a forecast measured here is the forecast the engine sizes on.
const (
	volSpan = 36
	volMin  = 10
)

type config struct {
	cache          string
	from, to       time.Time
	minTurnover    float64
	turnoverWindow int
	workers        int
}

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	switch os.Args[1] {
	case "scalars":
		runScalars(os.Args[2:])
	case "corr":
		runCorr(os.Args[2:])
	case "trial":
		runTrial(os.Args[2:])
	default:
		usage()
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: rulelab scalars|corr|trial [flags]")
	os.Exit(2)
}

func bindCommon(fs *flag.FlagSet) func() config {
	cache := fs.String("cache", defaultCache, "bar cache file")
	from := fs.String("from", "2013-07-01", "first observation date (inclusive)")
	to := fs.String("to", "2026-06-30", "last observation date (inclusive)")
	minTurnover := fs.Float64("min-turnover", 1e7, "minimum rolling median traded value in INR at the observation")
	turnWin := fs.Int("turnover-window", 60, "bars in the rolling median turnover")
	workers := fs.Int("workers", runtime.NumCPU(), "parallel symbol workers")
	return func() config {
		return config{
			cache:          *cache,
			from:           mustDate(*from),
			to:             mustDate(*to),
			minTurnover:    *minTurnover,
			turnoverWindow: *turnWin,
			workers:        *workers,
		}
	}
}

// ---------------------------------------------------------------------------
// Per-symbol evaluation shared by both subcommands.
// ---------------------------------------------------------------------------

// evaluate returns, for one symbol, each rule's series and the mask of
// observations that count: inside the date window and liquid enough to trade
// at that moment. Indicators are always computed on the FULL history so a
// rule's warm-up is not silently shortened by the date window.
func evaluate(ser bars.Series, lib []rules.Rule, cfg config, scaled bool) (vals [][]float64, mask []bool) {
	n := len(ser.Bars)
	times := make([]time.Time, n)
	closes := make([]float64, n)
	for i, b := range ser.Bars {
		times[i], closes[i] = b.Date, b.Close
	}
	prices := core.New(times, closes)
	inst := &data.Instrument{
		// LongOnly deliberately false even though NSE cash equity is: the
		// clip to [0, +20] would truncate half of every distribution measured
		// here and make correlations between rules look higher than they are.
		// Long-only clipping belongs at the sizing stage, not in the
		// characterisation of the signal.
		Meta:   data.Meta{Symbol: ser.Symbol, PointValue: 1, Block: 1},
		Prices: prices,
	}
	vol := core.PriceUnitVol(prices, volSpan, volMin)

	turnover := bars.MedianTurnover(ser.Bars, cfg.turnoverWindow)
	mask = make([]bool, n)
	for i := range mask {
		d := times[i]
		mask[i] = !d.Before(cfg.from) && !d.After(cfg.to) &&
			!math.IsNaN(turnover[i]) && turnover[i] >= cfg.minTurnover
	}

	vals = make([][]float64, len(lib))
	for k, r := range lib {
		if scaled {
			vals[k] = rules.Forecast(r, inst, vol).Values
		} else {
			vals[k] = r.Raw(inst, vol).Values
		}
	}
	return vals, mask
}

// ---------------------------------------------------------------------------
// scalars
// ---------------------------------------------------------------------------

func runScalars(args []string) {
	fs := flag.NewFlagSet("scalars", flag.ExitOnError)
	common := bindCommon(fs)
	check(fs.Parse(args))
	cfg := common()
	lib := rules.Library()

	type acc struct {
		n         int64
		sumAbs    float64
		sumSigned float64
		hist      []float64 // reservoir-free: |raw| bucket counts, see absHist
	}
	accs := make([]acc, len(lib))
	for k := range accs {
		accs[k].hist = make([]float64, absHistBuckets)
	}
	var mu sync.Mutex
	var symbols, kept int64

	check(bars.ScanParallel(cfg.cache, cfg.workers, func(ser bars.Series) {
		vals, mask := evaluate(ser, lib, cfg, false)
		local := make([]acc, len(lib))
		for k := range local {
			local[k].hist = make([]float64, absHistBuckets)
		}
		var localKept int64
		for i, ok := range mask {
			if !ok {
				continue
			}
			localKept++
			for k := range lib {
				v := vals[k][i]
				if math.IsNaN(v) {
					continue
				}
				local[k].n++
				local[k].sumAbs += math.Abs(v)
				local[k].sumSigned += v
				local[k].hist[absBucket(math.Abs(v))]++
			}
		}
		mu.Lock()
		symbols++
		kept += localKept
		for k := range lib {
			accs[k].n += local[k].n
			accs[k].sumAbs += local[k].sumAbs
			accs[k].sumSigned += local[k].sumSigned
			for b := range accs[k].hist {
				accs[k].hist[b] += local[k].hist[b]
			}
		}
		mu.Unlock()
	}))

	fmt.Printf("universe %d symbols | %d liquid symbol-days in %s..%s | min turnover Rs %.0f/day\n\n",
		symbols, kept, cfg.from.Format("2006-01-02"), cfg.to.Format("2006-01-02"), cfg.minTurnover)
	fmt.Printf("%-14s %12s %10s %10s %12s %10s\n", "rule", "obs", "mean|raw|", "mean raw", "scalar", "p99|raw|")
	for k, r := range lib {
		a := accs[k]
		if a.n == 0 {
			fmt.Printf("%-14s %12d %10s %10s %12s %10s\n", r.Name(), 0, "-", "-", "-", "-")
			continue
		}
		mean := a.sumAbs / float64(a.n)
		scalar := math.NaN()
		if mean > 0 {
			scalar = 10 / mean
		}
		fmt.Printf("%-14s %12d %10.4f %10.4f %12.3f %10.3f\n",
			r.Name(), a.n, mean, a.sumSigned/float64(a.n), scalar, absQuantile(a.hist, 0.99))
	}
	fmt.Println()
	fmt.Println("scalar = 10 / mean|raw| (Law 8's average-absolute-10 convention).")
	fmt.Println("EWMAC rows are the control: our measured scalar should land near")
	fmt.Println("Carver's published 3.75 / 2.65 / 1.87 for fast 16 / 32 / 64. If it")
	fmt.Println("does not, the procedure is wrong, not the book.")
}

// |raw| histogram: fixed log-ish buckets, wide enough for anything a sane
// rule produces and cheap enough to merge across workers. A quantile off
// bucket counts is accurate to the bucket width, which is all a scalar
// sanity-check needs.
const (
	absHistBuckets = 2000
	absHistMax     = 20.0
)

func absBucket(x float64) int {
	if math.IsNaN(x) {
		return 0
	}
	b := int(x / absHistMax * float64(absHistBuckets))
	if b < 0 {
		b = 0
	}
	if b >= absHistBuckets {
		b = absHistBuckets - 1
	}
	return b
}

func absQuantile(hist []float64, q float64) float64 {
	var total float64
	for _, c := range hist {
		total += c
	}
	if total == 0 {
		return math.NaN()
	}
	target := q * total
	var cum float64
	for b, c := range hist {
		cum += c
		if cum >= target {
			return (float64(b) + 0.5) * absHistMax / float64(absHistBuckets)
		}
	}
	return absHistMax
}

// ---------------------------------------------------------------------------
// corr
// ---------------------------------------------------------------------------

func runCorr(args []string) {
	fs := flag.NewFlagSet("corr", flag.ExitOnError)
	common := bindCommon(fs)
	group := fs.Float64("group-at", 0.7, "correlation at which two rules are called the same family")
	check(fs.Parse(args))
	cfg := common()
	lib := rules.Library()
	k := len(lib)

	// Pooled pairwise sums (every liquid symbol-day where both rules are
	// defined) plus per-rule forecast diagnostics.
	sums := newPairSums(k)
	// Within-symbol correlations averaged over symbols, alongside the pooled
	// number. They answer different questions: pooled includes the
	// cross-sectional dimension (do the rules agree about WHICH stock to hold
	// today), within-symbol is purely time-series (do they agree about WHEN to
	// hold this stock). A pair can look independent pooled and be a duplicate
	// inside every name, or the reverse.
	withinSum := make([]float64, k*k)
	withinN := make([]float64, k*k)
	capHits := make([]int64, k)
	obs := make([]int64, k)
	sumAbs := make([]float64, k)
	var mu sync.Mutex
	var symbols int64

	check(bars.ScanParallel(cfg.cache, cfg.workers, func(ser bars.Series) {
		vals, mask := evaluate(ser, lib, cfg, true)
		local := newPairSums(k)
		localCap := make([]int64, k)
		localObs := make([]int64, k)
		localAbs := make([]float64, k)
		for i, ok := range mask {
			if !ok {
				continue
			}
			for a := 0; a < k; a++ {
				va := vals[a][i]
				if math.IsNaN(va) {
					continue
				}
				localObs[a]++
				localAbs[a] += math.Abs(va)
				if math.Abs(va) >= rules.ForecastCap-1e-9 {
					localCap[a]++
				}
				for b := a; b < k; b++ {
					vb := vals[b][i]
					if math.IsNaN(vb) {
						continue
					}
					local.add(a, b, va, vb)
				}
			}
		}
		mu.Lock()
		symbols++
		sums.merge(local)
		for a := 0; a < k; a++ {
			for b := a; b < k; b++ {
				if local.n[local.idx(a, b)] < minWithinObs {
					continue
				}
				if r := local.corr(a, b); !math.IsNaN(r) {
					withinSum[a*k+b] += r
					withinN[a*k+b]++
				}
			}
		}
		for a := 0; a < k; a++ {
			capHits[a] += localCap[a]
			obs[a] += localObs[a]
			sumAbs[a] += localAbs[a]
		}
		mu.Unlock()
	}))

	fmt.Printf("universe %d symbols | %s..%s | min turnover Rs %.0f/day | forecasts scaled + capped at +/-%.0f, long-only clipping OFF\n\n",
		symbols, cfg.from.Format("2006-01-02"), cfg.to.Format("2006-01-02"), cfg.minTurnover, rules.ForecastCap)

	fmt.Printf("%-14s %12s %12s %10s\n", "rule", "obs", "mean|fcst|", "at cap")
	for a, r := range lib {
		if obs[a] == 0 {
			fmt.Printf("%-14s %12d %12s %10s\n", r.Name(), 0, "-", "-")
			continue
		}
		fmt.Printf("%-14s %12d %12.2f %9.1f%%\n", r.Name(), obs[a], sumAbs[a]/float64(obs[a]),
			100*float64(capHits[a])/float64(obs[a]))
	}
	fmt.Println("\n(mean|fcst| should sit near 10 for every rule; a rule far from it has a")
	fmt.Println("stale scalar. 'at cap' far above ~10% means the rule is a step function")
	fmt.Println("wearing a forecast's clothes.)")

	fmt.Println("\npairwise forecast correlation (pooled over liquid symbol-days):")
	printMatrix(lib, func(a, b int) float64 { return sums.corr(a, b) })

	fmt.Printf("\nsame correlation computed WITHIN each symbol, then averaged over symbols\n(only symbols with >= %d overlapping observations for the pair):\n", minWithinObs)
	printMatrix(lib, func(a, b int) float64 {
		if a > b {
			a, b = b, a
		}
		if withinN[a*k+b] == 0 {
			return math.NaN()
		}
		return withinSum[a*k+b] / withinN[a*k+b]
	})

	fmt.Printf("\ncorrelation groups at rho >= %.2f (complete linkage: every pair inside\n", *group)
	fmt.Println("a group clears the threshold). Handcrafted weights are assigned BETWEEN")
	fmt.Println("groups first, then within, per Carver's App C:")
	for i, g := range groupRules(lib, func(a, b int) float64 { return sums.corr(a, b) }, *group) {
		fmt.Printf("  group %d: %s\n", i+1, g)
	}
}

// pairSums accumulates the five sums Pearson needs, for every rule pair.
type pairSums struct {
	k                     int
	n                     []float64
	sx, sy, sxx, syy, sxy []float64
}

func newPairSums(k int) *pairSums {
	m := k * k
	return &pairSums{k: k,
		n: make([]float64, m), sx: make([]float64, m), sy: make([]float64, m),
		sxx: make([]float64, m), syy: make([]float64, m), sxy: make([]float64, m)}
}

func (p *pairSums) idx(a, b int) int { return a*p.k + b }

func (p *pairSums) add(a, b int, va, vb float64) {
	i := p.idx(a, b)
	p.n[i]++
	p.sx[i] += va
	p.sy[i] += vb
	p.sxx[i] += va * va
	p.syy[i] += vb * vb
	p.sxy[i] += va * vb
}

func (p *pairSums) merge(o *pairSums) {
	for i := range p.n {
		p.n[i] += o.n[i]
		p.sx[i] += o.sx[i]
		p.sy[i] += o.sy[i]
		p.sxx[i] += o.sxx[i]
		p.syy[i] += o.syy[i]
		p.sxy[i] += o.sxy[i]
	}
}

// corr is the Pearson correlation of rules a and b over the symbol-days where
// BOTH were defined — the pair's own overlap, never a longer window borrowed
// from one side.
func (p *pairSums) corr(a, b int) float64 {
	if a > b {
		a, b = b, a
	}
	i := p.idx(a, b)
	n := p.n[i]
	if n < 2 {
		return math.NaN()
	}
	cov := p.sxy[i]/n - (p.sx[i]/n)*(p.sy[i]/n)
	vx := p.sxx[i]/n - (p.sx[i]/n)*(p.sx[i]/n)
	vy := p.syy[i]/n - (p.sy[i]/n)*(p.sy[i]/n)
	if vx <= 0 || vy <= 0 {
		return math.NaN()
	}
	return cov / math.Sqrt(vx*vy)
}

// minWithinObs is the overlap a single symbol needs before its own
// correlation is allowed into the average — a 30-bar symbol would otherwise
// contribute a number that is mostly noise with the same weight as a 3,000-bar
// one.
const minWithinObs = 250

func printMatrix(lib []rules.Rule, corr func(a, b int) float64) {
	fmt.Printf("%-14s", "")
	for _, r := range lib {
		fmt.Printf("%8.8s", r.Name())
	}
	fmt.Println()
	for a, ra := range lib {
		fmt.Printf("%-14s", ra.Name())
		for b := range lib {
			v := corr(a, b)
			if math.IsNaN(v) {
				fmt.Printf("%8s", "-")
			} else {
				fmt.Printf("%8.2f", v)
			}
		}
		fmt.Println()
	}
}

// groupRules clusters by COMPLETE linkage: a group forms only if EVERY pair
// inside it reaches the threshold. Single linkage was the obvious choice and
// the wrong one — it chains (A~B, B~C, A independent of C all end up in one
// group), which on this library merged trend and mean-reversion into a single
// bucket through a string of intermediate rules.
//
// Absolute value on purpose: a rule that is the NEGATIVE of another carries
// the same information and must not be handed a second, independent weight.
func groupRules(lib []rules.Rule, corr func(a, b int) float64, thresh float64) []string {
	k := len(lib)
	groups := make([][]int, 0, k)
	for i := range lib {
		placed := false
		for g := range groups {
			fits := true
			for _, j := range groups[g] {
				v := corr(i, j)
				if math.IsNaN(v) || math.Abs(v) < thresh {
					fits = false
					break
				}
			}
			if fits {
				groups[g] = append(groups[g], i)
				placed = true
				break
			}
		}
		if !placed {
			groups = append(groups, []int{i})
		}
	}
	var out []string
	for _, g := range groups {
		s := lib[g[0]].Name()
		for _, i := range g[1:] {
			s += ", " + lib[i].Name()
		}
		out = append(out, s)
	}
	return out
}

func mustDate(s string) time.Time {
	t, err := time.Parse("2006-01-02", s)
	check(err)
	return t
}

func check(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, "rulelab:", err)
		os.Exit(1)
	}
}
