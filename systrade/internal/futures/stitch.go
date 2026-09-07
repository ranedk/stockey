package futures

import (
	"fmt"
	"math"
	"sort"
	"time"

	"github.com/ranedk/systrader/internal/core"
)

// A roll is the day a slot's underlying contract changed generation. Nothing
// in the data marks it, so it has to be found.
//
// Two detectors, because the evidence available differs by underlying:
//
//   - FromReference: the index futures case. NIFTY and BANKNIFTY have their
//     spot index in the same table, and a splice shows up as the futures
//     series moving when spot did not.
//   - FromLadder: the commodity case, where no spot series exists. It asks
//     which of two readings of the day is the smaller move: front-to-front
//     (no roll) or front-to-yesterday's-second (rolled). Uses only the slots
//     themselves.
//
// Where both can run they must agree, and `futures rolls -validate` measures
// that agreement on NIFTY and BANKNIFTY before FromLadder is trusted on gold.

// MinRollSpacing is the shortest gap allowed between two detected rolls. These
// contracts roll monthly; anything closer is the same roll detected twice or a
// data glitch.
const MinRollSpacing = 15

// ReferenceSeparation reports the largest robust-sigma divergence the
// reference detector can see, and how many days clear a given threshold. A
// detector that returns nothing is either looking at a series with no splices
// or at one whose splices are smaller than its noise — and those two cases
// must never be confused, so the caller can print this alongside.
func ReferenceSeparation(front, reference core.Series, sigmas float64) (maxZ float64, above int) {
	_, div := divergence(front, reference)
	if len(div) == 0 {
		return math.NaN(), 0
	}
	scale := mad(div) * 1.4826
	if scale <= 0 {
		return math.NaN(), 0
	}
	for _, d := range div {
		z := math.Abs(d) / scale
		if z > maxZ {
			maxZ = z
		}
		if z > sigmas {
			above++
		}
	}
	return maxZ, above
}

// FromReference finds rolls as outliers in the front slot's return relative to
// a reference series that has no splices (index spot). sigmas is how many
// robust standard deviations of the divergence a day must exceed.
func FromReference(front, reference core.Series, sigmas float64) []time.Time {
	dates, div := divergence(front, reference)
	if len(div) == 0 {
		return nil
	}
	// Robust scale: MAD, not the standard deviation, because the very outliers
	// being detected would inflate the latter and hide themselves.
	scale := mad(div) * 1.4826
	if scale <= 0 {
		return nil
	}
	scores := make([]float64, len(div))
	for i, d := range div {
		scores[i] = math.Abs(d) / scale
	}
	return pickPeaks(dates, scores, sigmas, MinRollSpacing)
}

// FromLadder finds rolls using only the curve. On a roll day the front slot's
// own day-over-day move contains the basis and is therefore LARGER than the
// move measured against yesterday's second position — which is what the
// position actually earned. On any other day the reverse holds.
func FromLadder(front, second core.Series) []time.Time {
	idx2 := make(map[time.Time]float64, second.Len())
	for i, d := range second.Times {
		idx2[d] = second.Values[i]
	}
	var dates []time.Time
	var scores []float64
	for i := 1; i < front.Len(); i++ {
		p, c := front.Values[i-1], front.Values[i]
		prev2, ok := idx2[front.Times[i-1]]
		if !ok || p <= 0 || c <= 0 || prev2 <= 0 {
			continue
		}
		same := math.Abs(math.Log(c / p))      // read as "no roll"
		shift := math.Abs(math.Log(c / prev2)) // read as "rolled"
		dates = append(dates, front.Times[i])
		scores = append(scores, same-shift)
	}
	if len(scores) == 0 {
		return nil
	}
	// Scores are bimodal: about -step on an ordinary day and about +step on a
	// roll, where step is the ladder's basis. Half a step therefore sits in
	// the empty middle. Thresholding AT a full step would land on the peak of
	// the roll cluster and lose half the rolls, which is exactly what the
	// first version of this did.
	step := median(absAll(scores))
	return pickPeaks(dates, scores, math.Max(step/2, 1e-5), MinRollSpacing)
}

// pickPeaks keeps scores above `threshold`, then thins them so no two survivors
// sit within `spacing` days — the larger score wins.
func pickPeaks(dates []time.Time, scores []float64, threshold float64, spacing int) []time.Time {
	type cand struct {
		t time.Time
		s float64
	}
	var cs []cand
	for i, s := range scores {
		if s > threshold {
			cs = append(cs, cand{dates[i], s})
		}
	}
	sort.Slice(cs, func(i, j int) bool { return cs[i].s > cs[j].s })
	var out []time.Time
	for _, c := range cs {
		ok := true
		for _, t := range out {
			if math.Abs(c.t.Sub(t).Hours()/24) < float64(spacing) {
				ok = false
				break
			}
		}
		if ok {
			out = append(out, c.t)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Before(out[j]) })
	return out
}

// Gap is one roll's measured splice.
type Gap struct {
	Date  time.Time
	Basis float64 // second - front, on the day BEFORE the roll
}

// Panama back-adjusts the front slot into a continuous price series.
//
// The gap at a roll is taken from the SAME DAY's curve — the second position's
// price minus the front's, on the day before the roll — never from a
// difference across the splice itself. That is the whole point: on the day
// before the roll the contract we are about to hold is quoted right there in
// position 2, so the adjustment needs no assumption about what the market did
// overnight.
//
// Prices before each roll are lifted by the cumulative basis of every roll
// that follows, so differences (and therefore P&L and price-unit volatility)
// are exact everywhere. Percentage returns on the adjusted series are NOT
// meaningful far back in history, and in deep contango the adjusted price can
// go negative — this is the known cost of Panama, and the reason Carver's
// framework sizes on price-unit vol rather than percentage returns.
func Panama(front, second core.Series, rolls []time.Time) (core.Series, []Gap, error) {
	if front.Len() == 0 {
		return core.Series{}, nil, fmt.Errorf("futures: empty front series")
	}
	idx2 := make(map[time.Time]float64, second.Len())
	for i, d := range second.Times {
		idx2[d] = second.Values[i]
	}
	prevDate := make(map[time.Time]time.Time, front.Len())
	for i := 1; i < front.Len(); i++ {
		prevDate[front.Times[i]] = front.Times[i-1]
	}

	gaps := make([]Gap, 0, len(rolls))
	for _, r := range rolls {
		pd, ok := prevDate[r]
		if !ok {
			continue // a roll on the first bar has nothing to adjust
		}
		p2, ok2 := idx2[pd]
		p1 := valueAt(front, pd)
		if !ok2 || p2 <= 0 || p1 <= 0 {
			return core.Series{}, nil, fmt.Errorf("futures: roll %s has no second-position price on %s — cannot measure the splice without differencing across it",
				r.Format("2006-01-02"), pd.Format("2006-01-02"))
		}
		gaps = append(gaps, Gap{Date: r, Basis: p2 - p1})
	}
	sort.Slice(gaps, func(i, j int) bool { return gaps[i].Date.Before(gaps[j].Date) })

	return ApplyGaps(front, gaps), gaps, nil
}

// ApplyGaps back-adjusts any series with splices measured elsewhere — the
// opens, say, when the gaps were measured on the closes. A bar's open and
// close belong to the same contract, so they take the same adjustment; using
// separately measured gaps for each would put a fake overnight move in.
//
// Walking backwards, each bar carries the basis of every roll STRICTLY ahead
// of it. The roll bar itself must not include its own basis: that is precisely
// what makes its difference from the previous bar come out as the real
// overnight P&L rather than the splice.
func ApplyGaps(s core.Series, gaps []Gap) core.Series {
	out := make([]float64, s.Len())
	cum := 0.0
	g := len(gaps) - 1
	for i := s.Len() - 1; i >= 0; i-- {
		for g >= 0 && gaps[g].Date.After(s.Times[i]) {
			cum += gaps[g].Basis
			g--
		}
		out[i] = s.Values[i] + cum
	}
	return core.New(s.Times, out)
}

// Carry is the annualized price-unit carry Carver's rule expects: what the
// position earns if the curve holds still and the front rolls up (or down) to
// the second position's price.
//
// Splice-safe by construction — both legs are read on the same date, so a roll
// cancels out of the number entirely (docs/data_notes.md's one usable futures
// signal). yearGap is the fraction of a year between the two positions'
// expiries; derive it from the detected roll spacing rather than assuming.
func Carry(front, second core.Series, yearGap float64) (core.Series, error) {
	if yearGap <= 0 {
		return core.Series{}, fmt.Errorf("futures: yearGap must be positive, got %v", yearGap)
	}
	idx2 := make(map[time.Time]float64, second.Len())
	for i, d := range second.Times {
		idx2[d] = second.Values[i]
	}
	out := make([]float64, front.Len())
	for i, d := range front.Times {
		p1 := front.Values[i]
		p2, ok := idx2[d]
		if !ok || p1 <= 0 || p2 <= 0 {
			out[i] = math.NaN()
			continue
		}
		out[i] = (p1 - p2) / yearGap
	}
	return core.New(front.Times, out), nil
}

// Agreement counts how many of a's dates appear in b within tolDays — the
// measurement that decides whether the ladder detector may be trusted on
// underlyings where no reference series exists.
func Agreement(a, b []time.Time, tolDays int) (matched, onlyA, onlyB int) {
	used := make([]bool, len(b))
	for _, ta := range a {
		hit := -1
		for j, tb := range b {
			if used[j] {
				continue
			}
			if math.Abs(ta.Sub(tb).Hours()/24) <= float64(tolDays) {
				hit = j
				break
			}
		}
		if hit >= 0 {
			used[hit] = true
			matched++
		} else {
			onlyA++
		}
	}
	for _, u := range used {
		if !u {
			onlyB++
		}
	}
	return matched, onlyA, onlyB
}

func divergence(a, b core.Series) ([]time.Time, []float64) {
	idx := make(map[time.Time]float64, b.Len())
	for i, d := range b.Times {
		idx[d] = b.Values[i]
	}
	var dates []time.Time
	var out []float64
	for i := 1; i < a.Len(); i++ {
		ap, ac := a.Values[i-1], a.Values[i]
		bp, okp := idx[a.Times[i-1]]
		bc, okc := idx[a.Times[i]]
		if !okp || !okc || ap <= 0 || ac <= 0 || bp <= 0 || bc <= 0 {
			continue
		}
		dates = append(dates, a.Times[i])
		out = append(out, math.Log(ac/ap)-math.Log(bc/bp))
	}
	return dates, out
}

func valueAt(s core.Series, d time.Time) float64 {
	for i, t := range s.Times {
		if t.Equal(d) {
			return s.Values[i]
		}
	}
	return math.NaN()
}

func mad(x []float64) float64 {
	m := median(x)
	dev := make([]float64, len(x))
	for i, v := range x {
		dev[i] = math.Abs(v - m)
	}
	return median(dev)
}

func absAll(x []float64) []float64 {
	out := make([]float64, len(x))
	for i, v := range x {
		out[i] = math.Abs(v)
	}
	return out
}

// --- Calendar detection -----------------------------------------------------
//
// FromLadder alone is weak, and provably so: a positional ladder re-labels
// itself at a roll, so nothing in it moves except the front's own price, and
// that is only distinguishable from an ordinary market move if the market move
// is known independently. The synthetic test measures the damage — roughly
// two thirds of rolls found when the basis is three times daily volatility,
// which is already a friendlier ratio than any real contract offers.
//
// The way out for an underlying with no spot series: these contracts expire on
// a fixed day of the month, so WHICH month a roll falls in is never in doubt.
// Take the calendar's answer for the month and let the ladder pick the day
// within a small window.

// LadderMonths is the number of months between adjacent curve positions, read
// off the live expiries. It is NOT always one: NIFTY and GOLDM list monthly
// contracts, but the SILVERM ladder in this data is Aug/Nov/Feb — quarterly.
// Assuming monthly there would have overstated its carry threefold and rolled
// the series twice as often as the contract actually rolls.
func LadderMonths(expiries []time.Time) int {
	if len(expiries) < 2 {
		return 0
	}
	var gaps []float64
	for i := 1; i < len(expiries); i++ {
		months := (expiries[i].Year()-expiries[i-1].Year())*12 +
			int(expiries[i].Month()) - int(expiries[i-1].Month())
		if months > 0 {
			gaps = append(gaps, float64(months))
		}
	}
	if len(gaps) == 0 {
		return 0
	}
	return int(median(gaps))
}

// ExpiryGapYears is the fraction of a year between adjacent curve positions —
// the denominator the carry rule needs. Taken from the expiries themselves,
// never from the roll cadence, because those differ whenever the ladder is not
// monthly.
func ExpiryGapYears(expiries []time.Time) float64 {
	if len(expiries) < 2 {
		return math.NaN()
	}
	var days []float64
	for i := 1; i < len(expiries); i++ {
		if d := expiries[i].Sub(expiries[i-1]).Hours() / 24; d > 0 {
			days = append(days, d)
		}
	}
	if len(days) == 0 {
		return math.NaN()
	}
	return median(days) / 365
}

// CalendarCandidates walks the known front expiry backwards in steps of
// `months` and returns, for each step, the first trading date strictly after
// it — the day the front slot must already hold the next contract.
//
// This uses the exchange's own listing convention rather than a guessed rule:
// the front expiry is a fact from the instrument master, and the step is read
// off the ladder. What it cannot capture is a convention that CHANGED mid
// history (NSE moved index expiry off the last Thursday in 2025), which is
// what the snapping window and the agreement check exist to expose.
func CalendarCandidates(dates []time.Time, frontExpiry time.Time, months int) []time.Time {
	if len(dates) == 0 || months <= 0 {
		return nil
	}
	first := dates[0]
	var expiries []time.Time
	for e := frontExpiry; !e.Before(first); e = e.AddDate(0, -months, 0) {
		expiries = append(expiries, e)
	}
	var out []time.Time
	for _, e := range expiries {
		for _, d := range dates {
			if d.After(e) {
				out = append(out, d)
				break
			}
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Before(out[j]) })
	return dedupeTimes(out)
}

func dedupeTimes(in []time.Time) []time.Time {
	var out []time.Time
	for i, t := range in {
		if i == 0 || !t.Equal(in[i-1]) {
			out = append(out, t)
		}
	}
	return out
}

// SnapToLadder moves each calendar candidate to the day within +/-window that
// looks most like a roll to the ladder test. The calendar decides the month,
// the ladder decides the day: neither is reliable alone here.
func SnapToLadder(candidates []time.Time, front, second core.Series, window int) []time.Time {
	scores := ladderScores(front, second)
	var out []time.Time
	for _, c := range candidates {
		best, bestScore := c, math.Inf(-1)
		for d, s := range scores {
			if math.Abs(d.Sub(c).Hours()/24) <= float64(window) && s > bestScore {
				best, bestScore = d, s
			}
		}
		if math.IsInf(bestScore, -1) {
			continue // no ladder data anywhere near: drop rather than guess
		}
		out = append(out, best)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Before(out[j]) })
	return out
}

// ladderScores is FromLadder's per-day statistic, exposed for snapping.
func ladderScores(front, second core.Series) map[time.Time]float64 {
	idx2 := make(map[time.Time]float64, second.Len())
	for i, d := range second.Times {
		idx2[d] = second.Values[i]
	}
	out := make(map[time.Time]float64, front.Len())
	for i := 1; i < front.Len(); i++ {
		p, c := front.Values[i-1], front.Values[i]
		prev2, ok := idx2[front.Times[i-1]]
		if !ok || p <= 0 || c <= 0 || prev2 <= 0 {
			continue
		}
		out[front.Times[i]] = math.Abs(math.Log(c/p)) - math.Abs(math.Log(c/prev2))
	}
	return out
}
