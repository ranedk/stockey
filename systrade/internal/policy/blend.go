package policy

import (
	"fmt"
	"math"
)

// Branch is one strategy book's daily record, in the shape internal/sleeve
// produces: Turnover is the sum of absolute weight changes (one way, so a
// book fully replaced reads 2.0) and Net is Gross less that day's cost.
type Branch struct {
	Gross    []float64
	Net      []float64
	Turnover []float64
}

// Blended is a composite book plus what the policy did to produce it.
type Blended struct {
	Branch
	// Weight[i] is branch 0's share on day i.
	Weight []float64
	// ReallocCost[i] is the cost charged on day i for moving the split, in
	// return units. Zero except on rebalance days.
	ReallocCost []float64
	// MeanWeight is the average share held by branch 0, and WeightTurnover
	// the average fraction of capital moved between branches per rebalance.
	MeanWeight, WeightTurnover float64
}

// Blend holds branch 0 at path[k] and branch 1 at 1-path[k] from rebalance k
// until rebalance k+1, and charges the trades that moving the split requires.
//
// The branch books are summed at those weights, the convention the frozen
// blends' kill screens already use (cmd/slice's screenFamily): a name held by
// both branches is charged twice, which the real book would net, so the cost
// side is conservative. On top of that a policy pays for its own adaptation —
// moving |dw| of capital from one branch to the other sells |dw| and buys
// |dw|, a round trip on that fraction — because a policy that adapts for free
// is not the policy anyone could run. The incumbent pays nothing here: its
// weights never move.
func Blend(br []Branch, path []float64, at []int, costBps float64) (Blended, error) {
	if len(br) != 2 {
		return Blended{}, fmt.Errorf("policy: blend needs two branches, got %d", len(br))
	}
	n := len(br[0].Net)
	if len(br[1].Net) != n {
		return Blended{}, fmt.Errorf("policy: branches misaligned: %d vs %d days", n, len(br[1].Net))
	}
	if len(path) != len(at) {
		return Blended{}, fmt.Errorf("policy: %d weights for %d rebalances", len(path), len(at))
	}
	if len(at) == 0 {
		return Blended{}, fmt.Errorf("policy: no rebalances")
	}
	out := Blended{Branch: Branch{
		Gross: make([]float64, n), Net: make([]float64, n), Turnover: make([]float64, n),
	}, Weight: make([]float64, n), ReallocCost: make([]float64, n)}

	k := 0
	var moved float64
	for i := 0; i < n; i++ {
		for k+1 < len(at) && at[k+1] <= i {
			k++
		}
		w := path[k]
		if w < 0 || w > 1 || math.IsNaN(w) {
			return Blended{}, fmt.Errorf("policy: weight %v out of range at rebalance %d", w, k)
		}
		out.Weight[i] = w
		out.Gross[i] = w*br[0].Gross[i] + (1-w)*br[1].Gross[i]
		out.Net[i] = w*br[0].Net[i] + (1-w)*br[1].Net[i]
		out.Turnover[i] = w*br[0].Turnover[i] + (1-w)*br[1].Turnover[i]
		if i == at[k] && k > 0 {
			dw := math.Abs(path[k] - path[k-1])
			moved += dw
			out.Turnover[i] += 2 * dw
			c := dw * costBps / 10000
			out.ReallocCost[i] = c
			out.Net[i] -= c
		}
	}
	var sum float64
	for _, w := range out.Weight {
		sum += w
	}
	out.MeanWeight = sum / float64(n)
	if len(at) > 1 {
		out.WeightTurnover = moved / float64(len(at)-1)
	}
	return out, nil
}
