package futures

import (
	"fmt"
	"math"
	"time"

	"github.com/ranedk/systrader/internal/core"
)

// Options fixes the judgement calls in one place so a report and a backtest
// cannot disagree about what "the NIFTY continuous series" means.
type Options struct {
	// Sigmas: robust sigmas of divergence from the reference that mark a
	// splice. Only used when a reference series is supplied.
	Sigmas float64
	// SnapWindow: days either side of a calendar expiry within which the
	// ladder may move a roll.
	SnapWindow int
}

// DefaultOptions are the values every caller should use unless it has a
// specific reason not to — a series stitched with different settings is a
// different series and must not be compared with one stitched with these.
func DefaultOptions() Options { return Options{Sigmas: 20, SnapWindow: 4} }

// Built is everything derivable about one underlying's curve, including the
// reasons it might not be usable. Unusable is non-empty when the data cannot
// support a continuous series; callers must check it before reading Adjusted.
type Built struct {
	Underlying string
	Curve      Curve
	Rolls      []time.Time
	RollSource string
	Gaps       []Gap
	Adjusted   core.Series // Panama back-adjusted front series
	Carry      core.Series // price units per year, splice-free
	GapYears   float64     // years between adjacent curve positions
	LadderMons int
	Warnings   []string
	Unusable   string
	// Diagnostics from the reference detector, kept even when it finds
	// nothing: "no rolls found" and "no rolls exist" are different claims.
	RefMaxSigma float64
	RefAboveBar int
	BasisFrac   float64 // median |second/front - 1|, the splice's size
}

// Build runs the whole pipeline: clean the slots, establish roll dates, stitch,
// and derive carry. reference may be empty; if it is, only the calendar+ladder
// detector is available and the result says so.
//
// It refuses rather than guesses. A curve with one position, roll dates that
// cannot be established, a hole in the roll sequence with no reference to
// check it against, or a splice that cannot be measured on the same day all
// set Unusable.
func Build(underlying string, slots []Slot, reference core.Series, opts Options) Built {
	b := Built{Underlying: underlying, Curve: Clean(underlying, slots)}
	if len(b.Curve.Positions) < 2 {
		b.Unusable = "a curve needs a front and a second position: both Panama and carry read the ladder"
		return b
	}
	front, second := b.Curve.Positions[0].Closes, b.Curve.Positions[1].Closes
	b.BasisFrac = medianBasis(front, second)

	var expiries []time.Time
	for _, p := range b.Curve.Positions {
		expiries = append(expiries, p.Expiry)
	}
	b.LadderMons = LadderMonths(expiries)
	b.GapYears = ExpiryGapYears(expiries)
	if b.LadderMons <= 0 || math.IsNaN(b.GapYears) {
		b.Unusable = "cannot read the contract cycle from the listed expiries"
		return b
	}

	var refRolls []time.Time
	if reference.Len() > 0 {
		refRolls = FromReference(front, reference, opts.Sigmas)
		b.RefMaxSigma, b.RefAboveBar = ReferenceSeparation(front, reference, opts.Sigmas)
	}
	calRolls := SnapToLadder(
		CalendarCandidates(front.Times, b.Curve.Positions[0].Expiry, b.LadderMons),
		front, second, opts.SnapWindow)

	cycleDays := b.GapYears * 365
	if worst, irregular := spacingOutlier(calRolls, cycleDays); irregular {
		b.Warnings = append(b.Warnings, fmt.Sprintf(
			"roll spacing reaches %.0f days against a %.0f-day cycle — there is a hole in this history",
			worst, cycleDays))
		if reference.Len() == 0 {
			b.Unusable = "irregular rolls and no reference series to check them against"
			return b
		}
	}

	b.Rolls, b.RollSource = refRolls, "reference"
	if len(b.Rolls) == 0 {
		b.Rolls, b.RollSource = calRolls, "calendar+ladder"
	}
	if len(b.Rolls) == 0 {
		b.Unusable = "no roll dates could be established by either detector"
		return b
	}

	adj, gaps, err := Panama(front, second, b.Rolls)
	if err != nil {
		b.Unusable = err.Error()
		return b
	}
	b.Adjusted, b.Gaps = adj, gaps

	carry, err := Carry(front, second, b.GapYears)
	if err != nil {
		b.Unusable = err.Error()
		return b
	}
	b.Carry = carry

	// Did the stitch actually remove the splices? Measured in the series' own
	// daily-move units, so it needs no reference. A series that still jumps on
	// roll days is worse than no series: it would put a splice straight into
	// an EWMAC forecast.
	rawZ, adjZ, ordZ := RollDayJump(front, adj, b.Rolls)
	if adjZ > 2*ordZ {
		b.Unusable = fmt.Sprintf("splices remain after stitching: roll-day move is %.2fx a typical day (ordinary %.2fx)", adjZ, ordZ)
		return b
	}
	if rawZ <= ordZ {
		b.Warnings = append(b.Warnings, fmt.Sprintf(
			"the raw series shows no roll-day jump (%.2fx vs %.2fx ordinary) — either these are not the roll dates, or this stream was already adjusted",
			rawZ, ordZ))
	}
	return b
}

// RollDayJump reports the mean absolute daily change on roll days, in units of
// the median absolute daily change, for the raw and adjusted series — plus the
// same for ordinary days as the yardstick.
func RollDayJump(raw, adj core.Series, rolls []time.Time) (rawZ, adjZ, ordZ float64) {
	isRoll := make(map[time.Time]bool, len(rolls))
	for _, r := range rolls {
		isRoll[r] = true
	}
	split := func(s core.Series) (roll, ord []float64) {
		var all []float64
		for i := 1; i < s.Len(); i++ {
			d := math.Abs(s.Values[i] - s.Values[i-1])
			all = append(all, d)
			if isRoll[s.Times[i]] {
				roll = append(roll, d)
			} else {
				ord = append(ord, d)
			}
		}
		scale := median(all)
		if scale <= 0 {
			return nil, nil
		}
		for i := range roll {
			roll[i] /= scale
		}
		for i := range ord {
			ord[i] /= scale
		}
		return roll, ord
	}
	rawRoll, rawOrd := split(raw)
	adjRoll, _ := split(adj)
	return mean(rawRoll), mean(adjRoll), mean(rawOrd)
}

func medianBasis(front, second core.Series) float64 {
	idx := make(map[time.Time]float64, second.Len())
	for i, d := range second.Times {
		idx[d] = second.Values[i]
	}
	var steps []float64
	for i, d := range front.Times {
		if p2, ok := idx[d]; ok && front.Values[i] > 0 && p2 > 0 {
			steps = append(steps, math.Abs(p2/front.Values[i]-1))
		}
	}
	return median(steps)
}

func spacingOutlier(rolls []time.Time, cycleDays float64) (worst float64, irregular bool) {
	for i := 1; i < len(rolls); i++ {
		if d := rolls[i].Sub(rolls[i-1]).Hours() / 24; d > worst {
			worst = d
		}
	}
	return worst, cycleDays > 0 && worst > 2*cycleDays
}

func mean(x []float64) float64 {
	if len(x) == 0 {
		return math.NaN()
	}
	var s float64
	for _, v := range x {
		s += v
	}
	return s / float64(len(x))
}
