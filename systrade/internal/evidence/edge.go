package evidence

import (
	"fmt"
	"math"
	"sort"
)

// Edge is a candidate's advantage over one control, observation by
// observation — normally months, for the reason sleeve.Monthly gives.
type Edge struct {
	N        int
	MeanDiff float64 // mean of candidate − control
	T        float64 // the plain paired t, comparable with older reports
	Won      float64 // share of observations the candidate beat the control
	Level    float64 // coverage of the interval below, e.g. 0.90
	Low      float64 // bootstrap percentile interval for MeanDiff
	High     float64
	// P is the one-sided bootstrap p-value of "no edge": how often a series
	// with this dependence and no mean difference shows a mean at least this
	// large. P, not T, is what goes into the family's FDR.
	P float64
}

// PairedEdge measures candidate − control on aligned series. Paired, never two
// separate means: both books hold the same universe on the same days, and the
// market move they share cancels only when the difference is taken first.
func PairedEdge(candidate, control []float64, bs Bootstrap, level float64) (Edge, error) {
	if len(candidate) != len(control) {
		return Edge{}, fmt.Errorf("evidence: paired series must be aligned (%d vs %d observations) — align on dates first", len(candidate), len(control))
	}
	if level <= 0 || level >= 1 {
		return Edge{}, fmt.Errorf("evidence: interval level must be in (0,1), got %v", level)
	}
	n := len(candidate)
	if err := bs.check(n); err != nil {
		return Edge{}, err
	}
	d := make([]float64, n)
	var won float64
	for i := range d {
		d[i] = candidate[i] - control[i]
		if d[i] > 0 {
			won++
		}
	}
	e := Edge{N: n, Level: level, Won: won / float64(n), MeanDiff: mean(d)}
	switch sd := stddev(d); {
	case sd > 0:
		e.T = e.MeanDiff / (sd / math.Sqrt(float64(n)))
	case e.MeanDiff != 0:
		// A difference with no variance is degenerate, not insignificant —
		// the same convention as sleeve.PairedMonthly.
		e.T = math.Copysign(math.Inf(1), e.MeanDiff)
	}

	means := make([]float64, 0, bs.Reps)
	exceed := 0
	err := bs.Each(n, func(_ int, idx []int) {
		var s float64
		for _, j := range idx {
			s += d[j]
		}
		m := s / float64(n)
		means = append(means, m)
		// Re-centred on the observed mean, the resampled mean is a draw from
		// a world with this series' dependence and no edge at all.
		if m-e.MeanDiff >= e.MeanDiff {
			exceed++
		}
	})
	if err != nil {
		return Edge{}, err
	}
	e.P = float64(exceed+1) / float64(bs.Reps+1)
	sort.Float64s(means)
	e.Low = quantile(means, (1-level)/2)
	e.High = quantile(means, 1-(1-level)/2)
	return e, nil
}

// ScaleToRisk levers the candidate's returns to the reference's realised
// volatility — the comparison LEDGER row 24 made standard. A variant that
// raises return by raising risk has done nothing that holding more of the
// incumbent would not also do, so it must be read at the incumbent's risk.
//
// It is a READING device, not a tradable overlay: the leverage is fixed with
// hindsight over the whole sample. Costs scale with the book, so net returns
// are scaled as they stand.
func ScaleToRisk(candidate, reference []float64) (scaled []float64, leverage float64) {
	sc, sr := stddev(candidate), stddev(reference)
	if sc == 0 {
		return append([]float64(nil), candidate...), math.NaN()
	}
	leverage = sr / sc
	scaled = make([]float64, len(candidate))
	for i, r := range candidate {
		scaled[i] = r * leverage
	}
	return scaled, leverage
}
