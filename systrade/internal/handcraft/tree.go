package handcraft

import (
	"fmt"
	"math"
	"strings"
)

// Node is one level of a handcrafting tree: a leaf (an asset, rule variation
// or instrument, by its index into the correlation matrix) or a group.
type Node struct {
	Name     string
	Leaf     int
	Children []Node
}

// Leaf names one member of the correlation matrix.
func Leaf(name string, index int) Node { return Node{Name: name, Leaf: index} }

// Group gathers children that will share one weight at the level above.
func Group(name string, children ...Node) Node { return Node{Name: name, Children: children} }

// Step is the working for one group, in the form Carver's tables show it.
type Step struct {
	Group   string
	Members []string
	Rule    string // which Table 8 row decided it
	Weights []float64
}

// Weights walks the tree and returns one weight per row of corr, with the
// working. Within a group the weights come from its size and, for three or
// more, from the correlations between its children; between two groups, the
// correlation is the simple average of the pairs across them (footnote 66).
// Every leaf must appear exactly once.
func Weights(root Node, corr [][]float64) ([]float64, []Step, error) {
	w := make([]float64, len(corr))
	seen := make([]bool, len(corr))
	var steps []Step
	var walk func(n Node, share float64) error
	walk = func(n Node, share float64) error {
		if len(n.Children) == 0 {
			if n.Leaf < 0 || n.Leaf >= len(corr) {
				return fmt.Errorf("handcraft: leaf %q has index %d outside the %d-row matrix", n.Name, n.Leaf, len(corr))
			}
			if seen[n.Leaf] {
				return fmt.Errorf("handcraft: leaf %q appears twice in the tree", n.Name)
			}
			seen[n.Leaf] = true
			w[n.Leaf] = share
			return nil
		}
		within, rule, err := groupWeights(n.Children, corr)
		if err != nil {
			return fmt.Errorf("%s: %w", n.Name, err)
		}
		names := make([]string, len(n.Children))
		for i, c := range n.Children {
			names[i] = c.Name
		}
		steps = append(steps, Step{Group: n.Name, Members: names, Rule: rule, Weights: within})
		for i, c := range n.Children {
			if err := walk(c, share*within[i]); err != nil {
				return err
			}
		}
		return nil
	}
	if err := walk(root, 1); err != nil {
		return nil, nil, err
	}
	for i, ok := range seen {
		if !ok {
			return nil, nil, fmt.Errorf("handcraft: matrix row %d is in no group — every member needs a place in the tree", i)
		}
	}
	return w, steps, nil
}

func groupWeights(children []Node, corr [][]float64) ([]float64, string, error) {
	switch k := len(children); k {
	case 1:
		return []float64{1}, "row 1", nil
	case 2:
		return []float64{0.5, 0.5}, "row 2", nil
	case 3:
		ab := between(children[0], children[1], corr)
		ac := between(children[0], children[2], corr)
		bc := between(children[1], children[2], corr)
		t, row, err := TripletWeights(ab, ac, bc)
		if err != nil {
			return nil, "", err
		}
		return t[:], fmt.Sprintf("row %d (%.2f/%.2f/%.2f)", row, ab, ac, bc), nil
	default:
		// Row 3 covers any size if every rounded correlation is the same;
		// otherwise row 4 says to split further, and guessing would hide
		// exactly the structure handcrafting exists to respect.
		first, err := RoundCorrelation(between(children[0], children[1], corr))
		if err != nil {
			return nil, "", err
		}
		for i := 0; i < k; i++ {
			for j := i + 1; j < k; j++ {
				r, err := RoundCorrelation(between(children[i], children[j], corr))
				if err != nil {
					return nil, "", err
				}
				if r != first {
					return nil, "", fmt.Errorf("handcraft: a group of %d without identical correlations — split it into groups of three or fewer (Table 8 row 4)", k)
				}
			}
		}
		out := make([]float64, k)
		for i := range out {
			out[i] = 1 / float64(k)
		}
		return out, "row 3", nil
	}
}

// between is the simple average correlation over every leaf pair across two
// nodes.
func between(a, b Node, corr [][]float64) float64 {
	la, lb := leaves(a), leaves(b)
	var sum float64
	for _, i := range la {
		for _, j := range lb {
			sum += corr[i][j]
		}
	}
	return sum / float64(len(la)*len(lb))
}

func leaves(n Node) []int {
	if len(n.Children) == 0 {
		return []int{n.Leaf}
	}
	var out []int
	for _, c := range n.Children {
		out = append(out, leaves(c)...)
	}
	return out
}

// String renders the working one group per line.
func (s Step) String() string {
	parts := make([]string, len(s.Members))
	for i, m := range s.Members {
		parts[i] = fmt.Sprintf("%s %.0f%%", m, 100*s.Weights[i])
	}
	return fmt.Sprintf("%-14s %-28s %s", s.Group, s.Rule, strings.Join(parts, ", "))
}

// DiversificationMultiplier is 1/√(w'Cw) (book p. 297): the FDM for forecast
// weights over forecast correlations, the IDM for instrument weights over
// subsystem return correlations. Negative correlations are floored at zero
// (Law 9: anti-correlation must never inflate leverage), an unknown one is
// taken as 1 (Law 20: assume the worst), and the result is held to [1, max].
func DiversificationMultiplier(w []float64, corr [][]float64, max float64) float64 {
	if len(w) == 0 || len(corr) != len(w) {
		return 1
	}
	var sum float64
	for i := range w {
		for j := range w {
			c := corr[i][j]
			switch {
			case i == j:
				c = 1
			case math.IsNaN(c):
				c = 1
			case c < 0:
				c = 0
			}
			sum += w[i] * w[j] * c
		}
	}
	if sum <= 0 {
		return 1
	}
	return math.Min(max, math.Max(1, 1/math.Sqrt(sum)))
}

// Cluster groups k members by COMPLETE linkage: a member joins a group only
// if it reaches thresh against EVERY member already in it. Single linkage
// chains (A~B and B~C put A and C together even when they are unrelated),
// which on the rule library merged trend and mean reversion into one bucket
// through a string of intermediate rules. Absolute value on purpose: a rule
// that is the negative of another carries the same information.
func Cluster(k int, corr func(a, b int) float64, thresh float64) [][]int {
	var groups [][]int
	for i := 0; i < k; i++ {
		placed := false
		for g := range groups {
			fits := true
			for _, j := range groups[g] {
				if v := corr(i, j); math.IsNaN(v) || math.Abs(v) < thresh {
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
	return groups
}
