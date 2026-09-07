// Package sleeve backtests a long-only cross-sectional equity sleeve and, in
// the same pass, the matched controls that say whether the signal did any of
// the work.
//
// Why this exists next to internal/backtest rather than inside it: the engine
// there runs a Carver portfolio of a few dozen instruments with a
// correlation-derived IDM, which is the right model for the futures/ETF sleeve
// and is quadratic in instrument count. An NSE cash-equity sleeve picks from
// ~3,000 names a day. The question this package answers is narrower and does
// not need the IDM: given a rule's forecast, does a book built from it beat a
// book with the SAME exposure and no information?
//
// Everything here is decided at the close of day t from data through t and
// filled at the open of t+1, held to the open of t+2 — the engine's
// convention, so results are comparable.
package sleeve

import (
	"fmt"
	"math"
	"math/rand"
	"sort"
	"time"
)

// Obs is one symbol on one decision day: the sizing units the rule asks for
// (forecast / percentage vol, i.e. cash weight before normalization) and the
// return the position earns.
type Obs struct {
	Sym int32
	// U is the pre-normalization cash weight, one per rule, NaN where that
	// rule is not defined yet (warm-up) — each rule therefore runs on its own
	// eligible set, and so does its control.
	U []float64
	// Ret is open(t+2)/open(t+1) - 1: what a position decided at this close
	// actually earns, with the fill delay already in it.
	Ret float64
}

// Day is one decision day's eligible cross-section.
type Day struct {
	Date time.Time
	Obs  []Obs
}

// Config fixes every choice that could otherwise be tuned after seeing a
// result. See research/preregistrations/ for the registered values.
type Config struct {
	// CostBpsRoundTrip is charged as half per side against turnover.
	CostBpsRoundTrip float64
	// Seeds for the cross-sectional shuffle control; results are averaged
	// over them. Declared in the pre-registration, never drawn at random.
	Seeds []int64
}

// Book is one traded book's daily record.
type Book struct {
	Name     string
	Dates    []time.Time
	Gross    []float64 // daily return before costs
	Net      []float64 // daily return after costs
	Turnover []float64 // sum of absolute weight changes, one-way
}

// RuleResult holds a rule's book and the two matched controls run on exactly
// the same days, the same eligible symbols and the same gross exposure.
type RuleResult struct {
	Rule string
	// Signal is the rule's own book; EqualWeight is control C1 ("just be long
	// this universe"); Shuffled is control C2, the same weights permuted
	// across the day's eligible symbols, averaged over Config.Seeds;
	// StableShuffled is control C3, the same permutation idea with a fixed
	// per-symbol key so the control HOLDS its positions instead of re-drawing
	// them every morning.
	//
	// C3 exists because C2, as pre-registered, re-shuffles independently each
	// day and therefore turns its whole book over daily (~160%/day measured).
	// That is not a control, it is a straw man: it loses to anything on costs
	// alone. C3 keeps C2's exposure profile and its ignorance of which stock
	// is which, while trading like a book someone might actually run.
	Signal, EqualWeight, Shuffled, StableShuffled Book
	Days                                          int
	MeanEligible                                  float64
}

// Run walks the days in order and trades every book side by side.
func Run(days []Day, ruleNames []string, cfg Config) ([]RuleResult, error) {
	if len(cfg.Seeds) == 0 {
		return nil, fmt.Errorf("sleeve: no shuffle seeds configured — the control must be reproducible, not randomly seeded per run")
	}
	sort.Slice(days, func(i, j int) bool { return days[i].Date.Before(days[j].Date) })

	out := make([]RuleResult, len(ruleNames))
	for k, name := range ruleNames {
		out[k] = RuleResult{
			Rule:           name,
			Signal:         Book{Name: "signal"},
			EqualWeight:    Book{Name: "equal-weight control"},
			Shuffled:       Book{Name: "shuffled control"},
			StableShuffled: Book{Name: "stable-shuffle control"},
		}
		// Books carry their previous weights across days so turnover is real
		// turnover, not a fresh book every morning.
		var prevSignal, prevEqual weights
		prevShuffled := make([]weights, len(cfg.Seeds))
		prevStable := make([]weights, len(cfg.Seeds))
		rngs := make([]*rand.Rand, len(cfg.Seeds))
		for s, seed := range cfg.Seeds {
			rngs[s] = rand.New(rand.NewSource(seed))
		}

		var eligibleTotal, eligibleDays float64
		for _, d := range days {
			syms, u, rets := sliceRule(d, k)
			if len(syms) < 2 {
				continue
			}
			eligibleTotal += float64(len(syms))
			eligibleDays++

			w := normalize(u)
			prevSignal = step(&out[k].Signal, d.Date, syms, w, rets, prevSignal, cfg)

			eq := make([]float64, len(syms))
			for i := range eq {
				eq[i] = 1 / float64(len(syms))
			}
			prevEqual = step(&out[k].EqualWeight, d.Date, syms, eq, rets, prevEqual, cfg)

			// The shuffle control permutes the SAME weight vector across the
			// same names: identical exposure, identical concentration, zero
			// information about which stock. Averaged over the declared seeds.
			var gross, net, turn float64
			for s := range cfg.Seeds {
				sw := append([]float64(nil), w...)
				rngs[s].Shuffle(len(sw), func(a, b int) { sw[a], sw[b] = sw[b], sw[a] })
				var one Book
				prevShuffled[s] = step(&one, d.Date, syms, sw, rets, prevShuffled[s], cfg)
				gross += one.Gross[0]
				net += one.Net[0]
				turn += one.Turnover[0]
			}
			n := float64(len(cfg.Seeds))
			out[k].Shuffled.Dates = append(out[k].Shuffled.Dates, d.Date)
			out[k].Shuffled.Gross = append(out[k].Shuffled.Gross, gross/n)
			out[k].Shuffled.Net = append(out[k].Shuffled.Net, net/n)
			out[k].Shuffled.Turnover = append(out[k].Shuffled.Turnover, turn/n)

			var sGross, sNet, sTurn float64
			for s, seed := range cfg.Seeds {
				sw := stablePermute(w, syms, seed)
				var one Book
				prevStable[s] = step(&one, d.Date, syms, sw, rets, prevStable[s], cfg)
				sGross += one.Gross[0]
				sNet += one.Net[0]
				sTurn += one.Turnover[0]
			}
			out[k].StableShuffled.Dates = append(out[k].StableShuffled.Dates, d.Date)
			out[k].StableShuffled.Gross = append(out[k].StableShuffled.Gross, sGross/n)
			out[k].StableShuffled.Net = append(out[k].StableShuffled.Net, sNet/n)
			out[k].StableShuffled.Turnover = append(out[k].StableShuffled.Turnover, sTurn/n)
		}
		out[k].Days = len(out[k].Signal.Dates)
		if eligibleDays > 0 {
			out[k].MeanEligible = eligibleTotal / eligibleDays
		}
	}
	return out, nil
}

// stablePermute reassigns the day's weights across the day's symbols using a
// FIXED per-symbol key, so a symbol that draws a big weight today draws a big
// weight tomorrow too. The multiset of weights is untouched; only the question
// "which name" is answered by a coin flip instead of by the rule — and the
// coin is flipped once, not daily.
func stablePermute(w []float64, syms []int32, seed int64) []float64 {
	order := make([]int, len(syms))
	for i := range order {
		order[i] = i
	}
	// Rank the day's symbols by their fixed key, and its weights by size, then
	// pair them off: the largest weight goes to the lowest-keyed name present.
	sort.Slice(order, func(a, b int) bool {
		return symKey(syms[order[a]], seed) < symKey(syms[order[b]], seed)
	})
	sorted := append([]float64(nil), w...)
	sort.Sort(sort.Reverse(sort.Float64Slice(sorted)))
	out := make([]float64, len(w))
	for rank, idx := range order {
		out[idx] = sorted[rank]
	}
	return out
}

// symKey is splitmix64 over (symbol, seed) — a deterministic per-symbol random
// key that costs nothing to recompute and never needs storing.
func symKey(sym int32, seed int64) uint64 {
	x := uint64(sym)*0x9E3779B97F4A7C15 ^ uint64(seed)*0xBF58476D1CE4E5B9
	x ^= x >> 30
	x *= 0xBF58476D1CE4E5B9
	x ^= x >> 27
	x *= 0x94D049BB133111EB
	x ^= x >> 31
	return x
}

// weights is a book's holdings between days, keyed by symbol.
type weights map[int32]float64

// sliceRule extracts the symbols where rule k is defined on this day.
func sliceRule(d Day, k int) (syms []int32, u, rets []float64) {
	for _, o := range d.Obs {
		v := o.U[k]
		if math.IsNaN(v) || math.IsNaN(o.Ret) {
			continue
		}
		syms = append(syms, o.Sym)
		u = append(u, v)
		rets = append(rets, o.Ret)
	}
	return syms, u, rets
}

// normalize scales non-negative sizing units to sum to 1. A day on which every
// forecast is zero (long-only rules clip to 0, so this happens) holds nothing:
// flat is a position, and faking an equal-weight book on those days would
// quietly hand the rule the market's return for free.
func normalize(u []float64) []float64 {
	var sum float64
	for _, v := range u {
		if v > 0 {
			sum += v
		}
	}
	w := make([]float64, len(u))
	if sum <= 0 {
		return w
	}
	for i, v := range u {
		if v > 0 {
			w[i] = v / sum
		}
	}
	return w
}

// step trades one book into the target weights and books its return.
//
// Turnover is measured against the previous day's weights AFTER they drifted
// with returns — a position that stayed put costs nothing even though its
// weight moved, which is the difference between a turnover number and a
// fantasy.
func step(b *Book, date time.Time, syms []int32, w, rets []float64, prev weights, cfg Config) weights {
	var turn float64
	held := make(map[int32]bool, len(syms))
	for i, s := range syms {
		held[s] = true
		turn += math.Abs(w[i] - prev[s])
	}
	for s, pw := range prev {
		if !held[s] {
			turn += math.Abs(pw) // liquidated
		}
	}

	var gross float64
	next := make(weights, len(syms))
	for i, s := range syms {
		gross += w[i] * rets[i]
		next[s] = w[i] * (1 + rets[i])
	}
	// Renormalize the drifted book so tomorrow's turnover compares like with
	// like; the level of the book is carried by the return series itself.
	var sum float64
	for _, v := range next {
		sum += v
	}
	if sum > 0 {
		for s := range next {
			next[s] /= sum
		}
	}

	cost := turn * cfg.CostBpsRoundTrip / 2 / 10000
	b.Dates = append(b.Dates, date)
	b.Gross = append(b.Gross, gross)
	b.Net = append(b.Net, gross-cost)
	b.Turnover = append(b.Turnover, turn)
	return next
}
