// Package policy sets the split between two strategy books over time.
//
// The live combination track (LEDGER row 34) holds the momentum blend and the
// low-volatility blend at a FIXED 48/52, set from Law 6 inputs only — the
// branches' cost of trading and their correlation, never their returns. This
// package implements the alternatives that pre-registration
// research/preregistrations/2026-09-23_combination_policies.md tests against
// it: a policy that moves the split on realised performance (Hedge), one that
// moves it on realised risk (risk parity), and one that moves it on the market
// regime (a purged walk-forward ridge).
//
// Every policy here sees only what was knowable before the decision it makes.
// That is the one invariant the package's tests exist to defend: each policy
// is handed the whole history and returns the whole weight path, which is
// convenient and also exactly how look-ahead gets written by accident, so
// `TestNoLookAhead` re-runs every policy on histories truncated at each
// rebalance and requires the same weights.
package policy

import (
	"fmt"
	"math"
	"time"
)

// History is the branch data a policy may read. Branch 0 is the one whose
// weight a policy returns; branch 1 takes the rest.
type History struct {
	// Dates are the trading days of the daily records.
	Dates []time.Time
	// Daily[b][i] is branch b's net return on day i.
	Daily [][]float64
	// Rebalance[k] is the daily index at which decision k takes effect. The
	// weight chosen for decision k may read Daily up to, but not including,
	// that index.
	Rebalance []int
	// Features[k] are the exogenous conditioning variables known at decision
	// k (market trend, market volatility, breadth, ...). Only the conditional
	// policies read them; they may be nil for the others.
	Features [][]float64
}

// Periods returns each branch's compounded net return over each holding
// period — period k running from decision k to decision k+1. The last period
// is the tail after the final decision, which may be short.
func (h History) Periods() [][]float64 {
	out := make([][]float64, len(h.Daily))
	for b := range h.Daily {
		out[b] = make([]float64, len(h.Rebalance))
		for k, start := range h.Rebalance {
			end := len(h.Daily[b])
			if k+1 < len(h.Rebalance) {
				end = h.Rebalance[k+1]
			}
			acc := 1.0
			for i := start; i < end && i < len(h.Daily[b]); i++ {
				acc *= 1 + h.Daily[b][i]
			}
			out[b][k] = acc - 1
		}
	}
	return out
}

func (h History) check() error {
	if len(h.Daily) != 2 {
		return fmt.Errorf("policy: need exactly two branches, got %d", len(h.Daily))
	}
	if len(h.Daily[0]) != len(h.Daily[1]) {
		return fmt.Errorf("policy: branches misaligned: %d vs %d days", len(h.Daily[0]), len(h.Daily[1]))
	}
	if len(h.Dates) != len(h.Daily[0]) {
		return fmt.Errorf("policy: %d dates for %d days", len(h.Dates), len(h.Daily[0]))
	}
	for k, r := range h.Rebalance {
		if r < 0 || r >= len(h.Daily[0]) {
			return fmt.Errorf("policy: rebalance %d out of range (%d days)", k, len(h.Daily[0]))
		}
		if k > 0 && r <= h.Rebalance[k-1] {
			return fmt.Errorf("policy: rebalance indices not increasing at %d", k)
		}
	}
	return nil
}

// Policy decides branch 0's share at every rebalance.
type Policy interface {
	Name() string
	// Weights returns one weight per rebalance, each in [0,1].
	Weights(h History) ([]float64, error)
}

// Incumbent is the weight the live combination track holds (LEDGER row 34):
// 48% momentum, 52% low risk. Policies that cannot yet decide — too little
// history for their window, before their first walk-forward fold — fall back
// to it, so a policy is never credited or charged for a period in which it
// has nothing to say.
const Incumbent = 0.48

// Fixed is the incumbent policy: one weight, never moved.
type Fixed struct {
	W     float64
	Label string
}

func (f Fixed) Name() string {
	if f.Label != "" {
		return f.Label
	}
	return fmt.Sprintf("fixed %.0f/%.0f", 100*f.W, 100*(1-f.W))
}

func (f Fixed) Weights(h History) ([]float64, error) {
	if err := h.check(); err != nil {
		return nil, err
	}
	w := make([]float64, len(h.Rebalance))
	for k := range w {
		w[k] = f.W
	}
	return w, nil
}

// HedgeScale is the size of a monthly return advantage that doubles-and-then-
// some a branch's share: at one percent a month ahead, exp(1) ≈ 2.7 times the
// weight, i.e. a 73/27 split. Declared before the run, and chosen because it
// is the size of the edges this ledger actually measures (rows 30-34 report
// 0.4-0.9%/month), not fitted to anything.
const HedgeScale = 0.01

// Hedge is multiplicative weights (Freund-Schapire) on the branches' realised
// period returns: a branch's share is proportional to exp(its exponentially
// weighted mean return / HedgeScale). HalfLife, in periods, is the only knob
// — it says how far back the policy remembers.
//
// This is selection over time, which Law 5 says destroys value; it is tested
// because the README names it and because "we expected it to fail" is only a
// claim if it is written down and run.
type Hedge struct {
	HalfLife float64 // in rebalance periods
}

func (hg Hedge) Name() string { return fmt.Sprintf("hedge hl=%.0fm", hg.HalfLife) }

func (hg Hedge) Weights(h History) ([]float64, error) {
	if err := h.check(); err != nil {
		return nil, err
	}
	if hg.HalfLife <= 0 {
		return nil, fmt.Errorf("policy: hedge half-life must be positive")
	}
	lambda := math.Pow(0.5, 1/hg.HalfLife)
	per := h.Periods()
	out := make([]float64, len(h.Rebalance))
	// ewma[b] is the exponentially weighted mean of branch b's completed
	// period returns; wsum is the shared weight normaliser.
	ewma := []float64{0, 0}
	var wsum float64
	for k := range out {
		if k == 0 {
			out[k] = Incumbent // nothing has been observed yet
			continue
		}
		// Period k-1 completed at this decision; fold it in.
		wsum = lambda*wsum + 1
		for b := range ewma {
			ewma[b] = lambda*ewma[b] + per[b][k-1]
		}
		m0, m1 := ewma[0]/wsum, ewma[1]/wsum
		// Softmax of the two means, written as a logistic on their difference
		// so it cannot overflow.
		out[k] = 1 / (1 + math.Exp(-(m0-m1)/HedgeScale))
	}
	return out, nil
}

// RiskParity gives each branch the share that equalises its contribution of
// risk: weight proportional to 1/volatility, both measured over the same
// trailing window of daily returns. It reads risk, which Law 6 calls
// estimable, and no return at all.
type RiskParity struct {
	Window int // trading days of daily returns
	MinObs int // fewest observations that may set a weight
}

func (rp RiskParity) Name() string { return fmt.Sprintf("risk parity %dd", rp.Window) }

func (rp RiskParity) Weights(h History) ([]float64, error) {
	if err := h.check(); err != nil {
		return nil, err
	}
	if rp.Window <= 1 {
		return nil, fmt.Errorf("policy: risk-parity window must exceed one day")
	}
	min := rp.MinObs
	if min == 0 {
		min = rp.Window / 2
	}
	out := make([]float64, len(h.Rebalance))
	for k, at := range h.Rebalance {
		lo := at - rp.Window
		if lo < 0 {
			lo = 0
		}
		if at-lo < min {
			out[k] = Incumbent
			continue
		}
		s0, ok0 := stddev(h.Daily[0][lo:at])
		s1, ok1 := stddev(h.Daily[1][lo:at])
		// minVol keeps rounding error out of the ratio: a branch whose daily
		// volatility reads below a millionth of a basis point is not a branch
		// with very little risk, it is a window with no information in it.
		const minVol = 1e-12
		if !ok0 || !ok1 || s0 < minVol || s1 < minVol {
			out[k] = Incumbent
			continue
		}
		out[k] = (1 / s0) / (1/s0 + 1/s1)
	}
	return out, nil
}

// TiltPerSD is how far one standard deviation of a conditional policy's
// prediction moves the split: twenty points. TiltFloor and TiltCeil bound it,
// so a conditional policy tilts the incumbent and never abandons a branch.
const (
	TiltPerSD = 0.20
	TiltFloor = 0.20
	TiltCeil  = 0.80
)

// Ridge is the conditional policy: a ridge regression of the NEXT period's
// difference of branch returns on the features known at the decision, fit on
// an expanding window and scored only on periods after it, with the overlap
// of the forward return purged. The prediction is standardised on the fit
// window and mapped to a tilt around the incumbent.
//
// Folds come from the caller (research.PurgedWalkForward), as fit-end /
// validation-end dates: FitEnd is already cut back by the purge.
type Ridge struct {
	Lambda float64 // shrinkage on standardised features
	Folds  []Fold
	// LagPeriods shifts the features FURTHER back by this many rebalances,
	// which is the time-shifted control: the same machinery reading stale
	// conditioning. Zero for the real policy.
	LagPeriods int
	Label      string
}

// Fold is one expanding-window fit and the block it scores.
type Fold struct {
	FitEnd   time.Time // last decision date whose label may be fit on
	ValStart time.Time // first decision date scored by this fit
	ValEnd   time.Time
}

func (r Ridge) Name() string {
	if r.Label != "" {
		return r.Label
	}
	if r.LagPeriods > 0 {
		return fmt.Sprintf("ridge (features lagged %d)", r.LagPeriods)
	}
	return "ridge"
}

func (r Ridge) Weights(h History) ([]float64, error) {
	if err := h.check(); err != nil {
		return nil, err
	}
	if len(h.Features) != len(h.Rebalance) {
		return nil, fmt.Errorf("policy: %d feature rows for %d rebalances", len(h.Features), len(h.Rebalance))
	}
	if len(h.Features) == 0 {
		return nil, nil
	}
	nf := len(h.Features[0])
	for k, f := range h.Features {
		if len(f) != nf {
			return nil, fmt.Errorf("policy: feature row %d has %d columns, want %d", k, len(f), nf)
		}
	}
	per := h.Periods()
	// Label k is the difference of the NEXT period's returns, so the last
	// decision has none.
	y := make([]float64, len(h.Rebalance))
	haveY := make([]bool, len(h.Rebalance))
	for k := 0; k+1 < len(h.Rebalance); k++ {
		y[k] = per[0][k+1] - per[1][k+1]
		haveY[k] = true
	}
	// x(k) is the feature row this decision reads, after the control's lag.
	x := func(k int) ([]float64, bool) {
		j := k - r.LagPeriods
		if j < 0 {
			return nil, false
		}
		row := h.Features[j]
		for _, v := range row {
			if math.IsNaN(v) {
				return nil, false
			}
		}
		return row, true
	}

	out := make([]float64, len(h.Rebalance))
	for k := range out {
		out[k] = Incumbent
	}
	for _, fold := range r.Folds {
		var fx [][]float64
		var fy []float64
		for k, at := range h.Rebalance {
			d := h.Dates[at]
			if d.After(fold.FitEnd) || !haveY[k] {
				continue
			}
			row, ok := x(k)
			if !ok {
				continue
			}
			fx, fy = append(fx, row), append(fy, y[k])
		}
		if len(fx) <= nf+1 {
			continue // too little to fit; those decisions keep the incumbent
		}
		mu, sd := standardise(fx)
		beta, ok := ridgeFit(fx, fy, mu, sd, r.Lambda)
		if !ok {
			continue
		}
		// The prediction is standardised on the fit window by the LABEL's
		// spread, not by the spread of the predictions themselves. Dividing
		// by the predictions' own standard deviation rescales any fit to full
		// size, so a model that explains nothing would still tilt the split to
		// its bounds — which is exactly what the synthetic null caught
		// (TestRidgeOnNoiseKeepsNoSystematicTilt). Against the label's spread
		// a weak model predicts near the mean and barely moves the weight,
		// and only a model that forecasts a difference of the size the
		// difference actually has gets the full twenty points.
		ls, ok := stddev(fy)
		if !ok || ls <= 0 {
			continue
		}
		for k, at := range h.Rebalance {
			d := h.Dates[at]
			if !d.After(fold.ValStart) && !d.Equal(fold.ValStart) {
				continue
			}
			if d.After(fold.ValEnd) {
				continue
			}
			row, ok := x(k)
			if !ok {
				continue
			}
			z := (predict(beta, row, mu, sd) - beta[0]) / ls
			out[k] = clamp(Incumbent+TiltPerSD*z, TiltFloor, TiltCeil)
		}
	}
	return out, nil
}

// --- arithmetic ---------------------------------------------------------

func stddev(x []float64) (float64, bool) {
	var n, sum float64
	for _, v := range x {
		if math.IsNaN(v) {
			continue
		}
		n, sum = n+1, sum+v
	}
	if n < 2 {
		return 0, false
	}
	m := sum / n
	var ss float64
	for _, v := range x {
		if math.IsNaN(v) {
			continue
		}
		ss += (v - m) * (v - m)
	}
	return math.Sqrt(ss / (n - 1)), true
}

func standardise(x [][]float64) (mu, sd []float64) {
	nf := len(x[0])
	mu, sd = make([]float64, nf), make([]float64, nf)
	for j := 0; j < nf; j++ {
		col := make([]float64, len(x))
		var sum float64
		for i := range x {
			col[i] = x[i][j]
			sum += col[i]
		}
		mu[j] = sum / float64(len(x))
		s, ok := stddev(col)
		if !ok || s <= 0 {
			s = 1 // a constant feature contributes nothing and must not divide by zero
		}
		sd[j] = s
	}
	return mu, sd
}

// ridgeFit solves (Z'Z + lambda I) b = Z'yc on standardised features with the
// label centred, so the intercept is the label's mean and is not penalised.
func ridgeFit(x [][]float64, y, mu, sd []float64, lambda float64) ([]float64, bool) {
	n, nf := len(x), len(mu)
	var ym float64
	for _, v := range y {
		ym += v
	}
	ym /= float64(n)
	a := make([][]float64, nf)
	for i := range a {
		a[i] = make([]float64, nf)
	}
	rhs := make([]float64, nf)
	for i := 0; i < n; i++ {
		z := make([]float64, nf)
		for j := 0; j < nf; j++ {
			z[j] = (x[i][j] - mu[j]) / sd[j]
		}
		for j := 0; j < nf; j++ {
			rhs[j] += z[j] * (y[i] - ym)
			for l := 0; l < nf; l++ {
				a[j][l] += z[j] * z[l]
			}
		}
	}
	for j := 0; j < nf; j++ {
		a[j][j] += lambda
	}
	b, ok := solve(a, rhs)
	if !ok {
		return nil, false
	}
	return append([]float64{ym}, b...), true
}

func predict(beta, row, mu, sd []float64) float64 {
	out := beta[0]
	for j := range mu {
		out += beta[j+1] * (row[j] - mu[j]) / sd[j]
	}
	return out
}

// solve is Gaussian elimination with partial pivoting.
func solve(a [][]float64, b []float64) ([]float64, bool) {
	n := len(b)
	m := make([][]float64, n)
	for i := range m {
		m[i] = append(append([]float64(nil), a[i]...), b[i])
	}
	for c := 0; c < n; c++ {
		p := c
		for r := c + 1; r < n; r++ {
			if math.Abs(m[r][c]) > math.Abs(m[p][c]) {
				p = r
			}
		}
		if math.Abs(m[p][c]) < 1e-12 {
			return nil, false
		}
		m[c], m[p] = m[p], m[c]
		for r := 0; r < n; r++ {
			if r == c {
				continue
			}
			f := m[r][c] / m[c][c]
			for k := c; k <= n; k++ {
				m[r][k] -= f * m[c][k]
			}
		}
	}
	out := make([]float64, n)
	for i := 0; i < n; i++ {
		out[i] = m[i][n] / m[i][i]
	}
	return out, true
}

func clamp(v, lo, hi float64) float64 {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}
