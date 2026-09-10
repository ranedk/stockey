package evidence

import (
	"fmt"
	"math"
	"sort"
)

const eulerGamma = 0.5772156649015329

// Moments returns x's per-observation Sharpe ratio (mean/sd, NOT annualised),
// skewness, and kurtosis (NOT excess: a normal series has 3).
func Moments(x []float64) (sr, skew, kurt float64) {
	n := float64(len(x))
	if n < 2 {
		return math.NaN(), math.NaN(), math.NaN()
	}
	m := mean(x)
	var m2, m3, m4 float64
	for _, v := range x {
		d := v - m
		m2 += d * d
		m3 += d * d * d
		m4 += d * d * d * d
	}
	m2, m3, m4 = m2/n, m3/n, m4/n
	if m2 <= 0 {
		return math.NaN(), math.NaN(), math.NaN()
	}
	return m / stddev(x), m3 / math.Pow(m2, 1.5), m4 / (m2 * m2)
}

// ProbabilisticSharpe is the probability that the true Sharpe ratio exceeds
// benchmark, given a per-period sr observed over n periods with the given
// skew and (non-excess) kurtosis — Bailey & López de Prado (2012). Negative
// skew and fat tails widen the error on a Sharpe ratio, which is how a book
// that quietly sells crash insurance looks brilliant for years.
func ProbabilisticSharpe(sr, benchmark float64, n int, skew, kurt float64) float64 {
	if n < 2 {
		return math.NaN()
	}
	v := 1 - skew*sr + (kurt-1)/4*sr*sr
	if v <= 0 {
		return math.NaN()
	}
	return NormCDF((sr - benchmark) * math.Sqrt(float64(n-1)) / math.Sqrt(v))
}

// ExpectedMaxSharpe is the Sharpe ratio the best of `trials` configurations
// shows with NO skill at all, when their Sharpe ratios spread across the
// family with variance varSR (per-period units). It is what the best
// configuration has to beat — not zero.
func ExpectedMaxSharpe(trials int, varSR float64) float64 {
	if trials <= 1 || varSR <= 0 {
		return 0
	}
	n := float64(trials)
	return math.Sqrt(varSR) * ((1-eulerGamma)*NormInv(1-1/n) + eulerGamma*NormInv(1-1/(n*math.E)))
}

// Deflated is a Sharpe ratio judged against the search that produced it.
type Deflated struct {
	SR        float64 // observed, per period
	Benchmark float64 // ExpectedMaxSharpe of the family's search
	Trials    int
	// DSR is the probability the true Sharpe ratio exceeds Benchmark. 0.95 is
	// the customary bar; 0.5 means the observed Sharpe is exactly what the
	// search would have turned up from noise.
	DSR float64
}

// DeflatedSharpe applies Bailey & López de Prado (2014) to one return series
// that was the pick of `trials` configurations whose Sharpe ratios had
// variance varTrialSR.
func DeflatedSharpe(x []float64, trials int, varTrialSR float64) Deflated {
	sr, skew, kurt := Moments(x)
	bench := ExpectedMaxSharpe(trials, varTrialSR)
	return Deflated{
		SR:        sr,
		Benchmark: bench,
		Trials:    trials,
		DSR:       ProbabilisticSharpe(sr, bench, len(x), skew, kurt),
	}
}

// BenjaminiHochberg controls the false-discovery rate at q across one
// family's p-values (amended Law 2: q = 0.10 within the family actually
// searched, not a workspace-wide Bonferroni). It returns which hypotheses are
// discoveries and each one's adjusted p — the smallest q at which it would be
// one. A NaN p counts as 1: an untestable member cannot be a discovery.
func BenjaminiHochberg(p []float64, q float64) (discovery []bool, adjusted []float64) {
	m := len(p)
	order := make([]int, m)
	for i := range order {
		order[i] = i
	}
	pv := func(i int) float64 {
		if math.IsNaN(p[i]) {
			return 1
		}
		return p[i]
	}
	sort.SliceStable(order, func(a, b int) bool { return pv(order[a]) < pv(order[b]) })
	adjusted = make([]float64, m)
	discovery = make([]bool, m)
	run := 1.0
	for k := m - 1; k >= 0; k-- {
		i := order[k]
		run = math.Min(run, pv(i)*float64(m)/float64(k+1))
		adjusted[i] = run
	}
	for i := range adjusted {
		discovery[i] = adjusted[i] <= q
	}
	return discovery, adjusted
}

// Member is one configuration of a searched family: its returns (the same
// frequency for every member) and its p-value against the control that
// decides it — normally PairedEdge's P against the tougher of the controls.
type Member struct {
	Name    string
	Returns []float64
	P       float64
}

// Verdict is one member judged within its family.
type Verdict struct {
	Name      string
	Deflated  Deflated
	Q         float64 // BH-adjusted p
	Discovery bool    // survives FDR at the family's q
}

// Judge applies both corrections the protocol requires to one family.
// trials is the number of configurations EXAMINED, which may exceed
// len(members): a variant looked at and dropped still inflated the best
// Sharpe ratio. The spread of Sharpe ratios is measured on the members given,
// so give every one that was run; a family of one has nothing to deflate
// against and gets a zero benchmark.
func Judge(members []Member, trials int, q float64) ([]Verdict, error) {
	if trials < len(members) {
		return nil, fmt.Errorf("evidence: %d members but only %d trials declared — trials counts every configuration examined", len(members), trials)
	}
	if q <= 0 || q >= 1 {
		return nil, fmt.Errorf("evidence: FDR level must be in (0,1), got %v", q)
	}
	srs := make([]float64, 0, len(members))
	ps := make([]float64, len(members))
	for i, m := range members {
		if sr, _, _ := Moments(m.Returns); !math.IsNaN(sr) {
			srs = append(srs, sr)
		}
		ps[i] = m.P
	}
	varSR := 0.0
	if len(srs) > 1 {
		varSR = stddev(srs) * stddev(srs)
	}
	disc, adj := BenjaminiHochberg(ps, q)
	out := make([]Verdict, len(members))
	for i, m := range members {
		out[i] = Verdict{
			Name:      m.Name,
			Deflated:  DeflatedSharpe(m.Returns, trials, varSR),
			Q:         adj[i],
			Discovery: disc[i],
		}
	}
	return out, nil
}
