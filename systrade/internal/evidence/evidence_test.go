package evidence

import (
	"math"
	"math/rand"
	"testing"
)

func near(t *testing.T, what string, got, want, tol float64) {
	t.Helper()
	if math.Abs(got-want) > tol {
		t.Errorf("%s = %v, want %v ± %v", what, got, want, tol)
	}
}

func noise(seed int64, n int, sd float64) []float64 {
	rng := rand.New(rand.NewSource(seed))
	out := make([]float64, n)
	for i := range out {
		out[i] = rng.NormFloat64() * sd
	}
	return out
}

func bs() Bootstrap { return Bootstrap{MeanBlock: 5, Reps: 2000, Seed: 7} }

func TestNormInvInvertsNormCDF(t *testing.T) {
	for _, p := range []float64{0.001, 0.025, 0.1, 0.5, 0.9, 0.975, 0.999} {
		near(t, "NormCDF(NormInv(p))", NormCDF(NormInv(p)), p, 1e-8)
	}
}

func TestBootstrapIsReproducibleAndKeepsItsBlocks(t *testing.T) {
	const n = 500
	collect := func() [][]int {
		var out [][]int
		b := Bootstrap{MeanBlock: 8, Reps: 50, Seed: 3}
		if err := b.Each(n, func(_ int, idx []int) { out = append(out, append([]int(nil), idx...)) }); err != nil {
			t.Fatal(err)
		}
		return out
	}
	a, b := collect(), collect()
	var runs, breaks float64
	for r := range a {
		for i := range a[r] {
			if a[r][i] != b[r][i] {
				t.Fatal("same seed produced different resamples — the interval would change between runs")
			}
			if a[r][i] < 0 || a[r][i] >= n {
				t.Fatalf("index %d out of range", a[r][i])
			}
			if i > 0 && a[r][i] != (a[r][i-1]+1)%n {
				breaks++
			}
		}
		runs += n
	}
	// A block restarts with probability 1/MeanBlock at each step (a restart
	// can land on the next index by chance, which the count cannot see).
	near(t, "mean block length", runs/(breaks+float64(len(a))), 8, 0.8)
}

func TestBootstrapRefusesAnUndeclaredConfiguration(t *testing.T) {
	for _, b := range []Bootstrap{{MeanBlock: 5}, {Reps: 10}} {
		if err := b.Each(100, func(int, []int) {}); err == nil {
			t.Errorf("%+v accepted", b)
		}
	}
}

func TestPairedEdgeFindsARealEdgeAndNotAFakeOne(t *testing.T) {
	control := noise(1, 156, 0.05) // 13 years of monthly market noise
	real := make([]float64, len(control))
	fake := make([]float64, len(control))
	own := noise(2, len(control), 0.01)
	for i := range control {
		real[i] = control[i] + 0.006 + own[i] // +0.6%/month over the control
		fake[i] = control[i] + own[i] - mean(own)
	}
	e, err := PairedEdge(real, control, bs(), 0.90)
	if err != nil {
		t.Fatal(err)
	}
	if e.P > 0.01 || e.Low <= 0 {
		t.Errorf("a 0.6%%/month edge with 1%% tracking noise went unseen: P=%.3f interval [%.4f, %.4f]", e.P, e.Low, e.High)
	}
	if e.Low > e.MeanDiff || e.High < e.MeanDiff {
		t.Errorf("interval [%v, %v] does not contain the estimate %v", e.Low, e.High, e.MeanDiff)
	}
	f, err := PairedEdge(fake, control, bs(), 0.90)
	if err != nil {
		t.Fatal(err)
	}
	if f.P < 0.2 || f.Low > 0 || f.High < 0 {
		t.Errorf("zero-mean difference read as an edge: P=%.3f interval [%.4f, %.4f]", f.P, f.Low, f.High)
	}
	// The shared market move must cancel: the paired t sees the edge even
	// though the control's own noise is five times the edge.
	if e.T < 3 {
		t.Errorf("paired t = %.2f — the common market move did not cancel", e.T)
	}
}

func TestPairedEdgeWidensForRunsOfLuck(t *testing.T) {
	// The same mean and the same variance, but the second series comes in
	// long runs. An i.i.d. test cannot tell them apart; the block bootstrap
	// must be less sure of the second.
	n := 240
	iid := noise(5, n, 0.02)
	runs := make([]float64, n)
	for i := range runs {
		runs[i] = iid[(i/12)*12] // each draw repeated for a year
	}
	zero := make([]float64, n)
	b := Bootstrap{MeanBlock: 12, Reps: 3000, Seed: 1}
	a, _ := PairedEdge(iid, zero, b, 0.90)
	r, _ := PairedEdge(runs, zero, b, 0.90)
	if r.High-r.Low <= a.High-a.Low {
		t.Errorf("runs of luck got an interval %.4f no wider than independent months %.4f", r.High-r.Low, a.High-a.Low)
	}
}

func TestPairedEdgeRefusesMisalignedSeries(t *testing.T) {
	if _, err := PairedEdge(make([]float64, 10), make([]float64, 9), bs(), 0.9); err == nil {
		t.Error("misaligned series accepted")
	}
}

func TestScaleToRiskMatchesTheReferenceVolatility(t *testing.T) {
	cand, ref := noise(3, 500, 0.03), noise(4, 500, 0.02)
	scaled, lev := ScaleToRisk(cand, ref)
	near(t, "scaled vol", stddev(scaled), stddev(ref), 1e-12)
	near(t, "leverage", lev, stddev(ref)/stddev(cand), 1e-12)
}

func TestProbabilisticSharpeByHand(t *testing.T) {
	// sr 0.1, n 101, normal tails: z = 0.1·10 / sqrt(1 + 2/4·0.01) = 0.99751.
	near(t, "PSR", ProbabilisticSharpe(0.1, 0, 101, 0, 3), NormCDF(0.99751), 1e-4)
	// Negative skew must lower confidence in the same Sharpe ratio.
	if ProbabilisticSharpe(0.1, 0, 101, -1, 6) >= ProbabilisticSharpe(0.1, 0, 101, 0, 3) {
		t.Error("negative skew and fat tails did not widen the error on SR")
	}
}

func TestExpectedMaxSharpeGrowsWithTheSearch(t *testing.T) {
	if ExpectedMaxSharpe(1, 0.01) != 0 {
		t.Error("a single trial has no search to correct for")
	}
	prev := 0.0
	for _, n := range []int{2, 10, 100, 1000} {
		got := ExpectedMaxSharpe(n, 0.01)
		if got <= prev {
			t.Errorf("E[max SR] at %d trials = %v, not above %v", n, got, prev)
		}
		prev = got
	}
	// Bailey & López de Prado's approximation for 1000 trials is ≈3.26σ.
	near(t, "E[max SR] at 1000 trials in σ units", ExpectedMaxSharpe(1000, 1), 3.26, 0.02)
}

func TestDeflationCatchesTheBestOfManyNoiseRuns(t *testing.T) {
	// Fifty configurations of pure noise. The best of them has a respectable
	// raw Sharpe ratio; against the search that produced it, it is nothing.
	var members []Member
	for s := int64(0); s < 50; s++ {
		members = append(members, Member{Returns: noise(100+s, 156, 0.04), P: 0.5})
	}
	vs, err := Judge(members, 50, 0.10)
	if err != nil {
		t.Fatal(err)
	}
	best := vs[0]
	for _, v := range vs {
		if v.Deflated.SR > best.Deflated.SR {
			best = v
		}
	}
	raw := ProbabilisticSharpe(best.Deflated.SR, 0, 156, 0, 3)
	if raw < 0.9 {
		t.Fatalf("test premise failed: best noise run is not impressive undeflated (PSR %.2f)", raw)
	}
	if best.Deflated.DSR > 0.8 {
		t.Errorf("best of 50 noise runs kept DSR %.2f (raw PSR %.2f)", best.Deflated.DSR, raw)
	}
}

func TestBenjaminiHochbergByHand(t *testing.T) {
	disc, adj := BenjaminiHochberg([]float64{0.01, 0.04, 0.03, 0.005, 0.2}, 0.05)
	wantDisc := []bool{true, true, true, true, false}
	wantAdj := []float64{0.025, 0.05, 0.05, 0.025, 0.2}
	for i := range disc {
		if disc[i] != wantDisc[i] {
			t.Errorf("p[%d]: discovery %v, want %v", i, disc[i], wantDisc[i])
		}
		near(t, "adjusted p", adj[i], wantAdj[i], 1e-12)
	}
	d, _ := BenjaminiHochberg([]float64{math.NaN(), 0.001}, 0.1)
	if d[0] || !d[1] {
		t.Errorf("NaN p handling: got %v", d)
	}
}

func TestJudgeRefusesUndercountedTrials(t *testing.T) {
	m := []Member{{Returns: noise(1, 50, 0.01)}, {Returns: noise(2, 50, 0.01)}}
	if _, err := Judge(m, 1, 0.1); err == nil {
		t.Error("two members judged as one trial")
	}
}

func TestMinVarianceReproducesHandcraftingOnAClearCase(t *testing.T) {
	// Two near-duplicates and one independent rule: handcrafting says the
	// duplicates are one group sharing half, the independent rule takes the
	// other half — 25/25/50.
	c := [][]float64{{1, 0.99, 0}, {0.99, 1, 0}, {0, 0, 1}}
	w := MinVarianceWeights(c)
	near(t, "w[0]", w[0], 0.25, 0.002)
	near(t, "w[1]", w[1], 0.25, 0.002)
	near(t, "w[2]", w[2], 0.50, 0.002)
}

func TestMinVarianceSatisfiesItsOptimalityConditions(t *testing.T) {
	// Random correlation matrices, some with constraints binding: every held
	// weight must see the same marginal variance, and every excluded one at
	// least that much (KKT for the long-only simplex).
	rng := rand.New(rand.NewSource(11))
	for trial := 0; trial < 50; trial++ {
		k := 2 + rng.Intn(6)
		cols := make([][]float64, k)
		base := noise(int64(trial), 300, 1)
		for j := range cols {
			mix := rng.Float64()
			own := noise(int64(1000*trial+j), 300, 1)
			cols[j] = make([]float64, 300)
			for i := range own {
				cols[j][i] = mix*base[i] + (1-mix)*own[i]
			}
		}
		c := Correlation(cols, nil)
		w := MinVarianceWeights(c)
		var sum, lambda float64
		marg := make([]float64, k)
		for i := range c {
			for j := range c[i] {
				marg[i] += c[i][j] * w[j]
			}
			sum += w[i]
			if w[i] < 0 {
				t.Fatalf("negative weight %v", w[i])
			}
		}
		near(t, "Σw", sum, 1, 1e-9)
		for i := range w {
			lambda += w[i] * marg[i] // w'Cw
		}
		for i := range w {
			if w[i] > 1e-6 && math.Abs(marg[i]-lambda) > 1e-6 {
				t.Errorf("trial %d: held weight %d has marginal %v, portfolio %v", trial, i, marg[i], lambda)
			}
			if w[i] <= 1e-6 && marg[i] < lambda-1e-6 {
				t.Errorf("trial %d: excluded weight %d would lower variance (marginal %v < %v)", trial, i, marg[i], lambda)
			}
		}
	}
}

func TestBootstrapWeightsAgreeWithTheTreeAndBracketTheirMean(t *testing.T) {
	n := 260
	a := noise(21, n, 1)
	b := make([]float64, n)
	e := noise(22, n, 0.1)
	for i := range b {
		b[i] = a[i] + e[i] // a near-duplicate of a
	}
	c := noise(23, n, 1)
	est, err := BootstrapWeights([][]float64{a, b, c}, Bootstrap{MeanBlock: DefaultBlock(n), Reps: 300, Seed: 5})
	if err != nil {
		t.Fatal(err)
	}
	near(t, "duplicates' combined weight", est.Mean[0]+est.Mean[1], 0.5, 0.05)
	near(t, "independent rule's weight", est.Mean[2], 0.5, 0.05)
	var sum float64
	for j := range est.Mean {
		sum += est.Mean[j]
		if est.Low[j] > est.Mean[j] || est.High[j] < est.Mean[j] {
			t.Errorf("column %d: mean %v outside [%v, %v]", j, est.Mean[j], est.Low[j], est.High[j])
		}
	}
	near(t, "Σ bootstrapped weights", sum, 1, 1e-9)
}

func TestCorrelationSurvivesAFlatColumn(t *testing.T) {
	c := Correlation([][]float64{{1, 2, 3, 4}, {5, 5, 5, 5}}, nil)
	if math.IsNaN(c[0][1]) || c[0][1] != 0 || c[1][1] != 1 {
		t.Errorf("flat column produced %v", c)
	}
}
