package evidence

import (
	"fmt"
	"math"
	"math/rand"
)

// Bootstrap configures a stationary block bootstrap (Politis & Romano 1994).
//
// Resampling single observations would destroy exactly the structure that
// makes trading returns hard to judge: a momentum book has runs of good and
// bad months, and an i.i.d. bootstrap treats one lucky run as many independent
// lucky months. Blocks of random, geometrically distributed length keep runs
// intact while leaving the resampled series stationary, which fixed-length
// blocks do not.
type Bootstrap struct {
	// MeanBlock is the expected block length in observations. DefaultBlock
	// gives the usual n^(1/3) rate when nothing better is known.
	MeanBlock float64
	// Reps is the number of resamples.
	Reps int
	// Seed makes every interval reproducible. Like sleeve.Config.Seeds it is
	// declared, never drawn: a statistic that changes between runs of the same
	// data invites re-running until it looks good.
	Seed int64
}

// DefaultBlock is the n^(1/3) rate for the expected block length: long enough
// to carry short-range dependence, short enough to leave many blocks.
func DefaultBlock(n int) float64 {
	return math.Max(1, math.Round(math.Cbrt(float64(n))))
}

func (b Bootstrap) check(n int) error {
	switch {
	case b.Reps < 1:
		return fmt.Errorf("evidence: bootstrap needs Reps >= 1, got %d", b.Reps)
	case b.MeanBlock < 1:
		return fmt.Errorf("evidence: bootstrap needs MeanBlock >= 1, got %v", b.MeanBlock)
	case n < 2:
		return fmt.Errorf("evidence: bootstrap needs at least 2 observations, got %d", n)
	}
	return nil
}

// Each draws Reps resamples of the positions 0..n-1 and calls fn with each.
// Apply the SAME idx to every series that must stay aligned — a strategy and
// its control, or every column of a weight estimation — so the resample keeps
// their cross-correlation, which is the whole content of a paired test.
// idx is reused between calls; copy it to keep it.
func (b Bootstrap) Each(n int, fn func(rep int, idx []int)) error {
	if err := b.check(n); err != nil {
		return err
	}
	rng := rand.New(rand.NewSource(b.Seed))
	restart := 1 / b.MeanBlock
	idx := make([]int, n)
	for rep := 0; rep < b.Reps; rep++ {
		cur := rng.Intn(n)
		for i := range idx {
			if i > 0 {
				if rng.Float64() < restart {
					cur = rng.Intn(n)
				} else {
					// Wrap around: the circular form keeps every position
					// equally likely, so the ends of the sample are not
					// under-drawn.
					cur = (cur + 1) % n
				}
			}
			idx[i] = cur
		}
		fn(rep, idx)
	}
	return nil
}
