package handcraft

import (
	"math"
	"testing"
)

func near(t *testing.T, what string, got, want, tol float64) {
	t.Helper()
	if math.Abs(got-want) > tol {
		t.Errorf("%s = %.4f, want %.4f", what, got, want)
	}
}

// uniform builds an n x n correlation matrix with every off-diagonal at c.
func uniform(n int, c float64) [][]float64 {
	m := make([][]float64, n)
	for i := range m {
		m[i] = make([]float64, n)
		for j := range m[i] {
			m[i][j] = c
		}
		m[i][i] = 1
	}
	return m
}

func set(m [][]float64, i, j int, c float64) { m[i][j], m[j][i] = c, c }

func TestTable8RowsSurviveEveryRelabelling(t *testing.T) {
	for _, row := range table8 {
		// Position p holds the table's member perm[p]; the pair between two
		// positions must carry the table's correlation for their members.
		for _, perm := range [][3]int{{0, 1, 2}, {0, 2, 1}, {1, 0, 2}, {1, 2, 0}, {2, 0, 1}, {2, 1, 0}} {
			c := func(a, b int) float64 {
				x, y := perm[a], perm[b]
				if x > y {
					x, y = y, x
				}
				switch {
				case x == 0 && y == 1:
					return row.ab
				case x == 0 && y == 2:
					return row.ac
				default:
					return row.bc
				}
			}
			w, got, err := TripletWeights(c(0, 1), c(0, 2), c(1, 2))
			if err != nil {
				t.Fatal(err)
			}
			if got != row.row {
				t.Errorf("row %d relabelled %v matched row %d", row.row, perm, got)
			}
			for p := 0; p < 3; p++ {
				near(t, "relabelled weight", w[p], row.w[perm[p]], 1e-12)
			}
		}
	}
}

func TestRoundingFollowsTheBooksOwnExamples(t *testing.T) {
	cases := map[float64]float64{
		-0.3: 0, -0.2: 0, 0.07: 0, 0.18: 0, // p. 79 and table 48's inter-asset correlations
		0.8: 0.9, 0.6: 0.5,
		0.70: 0.5, // chapter 8: EWMAC 16/64 at 0.7 goes to row 11
		0.87: 0.9,
	}
	for in, want := range cases {
		got, err := RoundCorrelation(in)
		if err != nil || got != want {
			t.Errorf("RoundCorrelation(%v) = %v, %v; want %v", in, got, err, want)
		}
	}
	if _, err := RoundCorrelation(math.NaN()); err == nil {
		t.Error("an unknown correlation was rounded instead of refused")
	}
}

func TestBondsAndEquitiesExample(t *testing.T) {
	// p. 79-80: US bonds D, S&P E, NASDAQ F with DE -0.3, DF -0.2, EF 0.8
	// -> row 6 -> D 46%, E 27%, F 27%.
	w, row, err := TripletWeights(-0.3, -0.2, 0.8)
	if err != nil {
		t.Fatal(err)
	}
	if row != 6 {
		t.Errorf("row %d, want 6", row)
	}
	near(t, "US bonds", w[0], 0.46, 1e-12)
	near(t, "S&P 500", w[1], 0.27, 1e-12)
	near(t, "NASDAQ", w[2], 0.27, 1e-12)
}

func TestLargerExampleTable11(t *testing.T) {
	// Tables 10-11: sixteen assets in four levels. Correlations are only
	// used in the US bonds group (2y/20y 0.5, 2y/30y 0.5, 20y/30y 0.9 -> row
	// 10); everything else is a group of two, or three with similar
	// correlations.
	names := []string{"Barclays", "HSBC", "RBS", "Tesco", "Sainsbury", "JPM", "Citi", "BofA",
		"Safeway", "Walmart", "Costco", "UK10", "UK20", "US2", "US20", "US30"}
	c := uniform(len(names), 0.5)
	set(c, 14, 15, 0.9)
	l := func(i int) Node { return Leaf(names[i], i) }
	tree := Group("portfolio",
		Group("equities",
			Group("UK equities", Group("UK banks", l(0), l(1), l(2)), Group("UK retail", l(3), l(4))),
			Group("US equities", Group("US banks", l(5), l(6), l(7)), Group("US retail", l(8), l(9), l(10))),
		),
		Group("bonds", Group("UK bonds", l(11), l(12)), Group("US bonds", l(13), l(14), l(15))),
	)
	w, _, err := Weights(tree, c)
	if err != nil {
		t.Fatal(err)
	}
	want := []float64{4.2, 4.2, 4.2, 6.3, 6.3, 4.2, 4.2, 4.2, 4.2, 4.2, 4.2, 12.5, 12.5, 10.5, 7.3, 7.3}
	var sum float64
	for i := range want {
		// The book prints one decimal, rounding half up: 29% x 25% = 7.25%
		// appears as 7.3%.
		near(t, names[i], 100*w[i], want[i], 0.0501)
		sum += w[i]
	}
	near(t, "total", sum, 1, 1e-12)
}

func TestForecastWeightsTable17AndTheirMultiplier(t *testing.T) {
	// Chapter 8: EWMAC 16/32/64 and carry, correlations from table 19.
	// Weights 21/8/21/50 (table 17), FDM 1.31 by the precise formula.
	c := [][]float64{
		{1, 0.9, 0.6, 0.25},
		{0.9, 1, 0.9, 0.25},
		{0.6, 0.9, 1, 0.25},
		{0.25, 0.25, 0.25, 1},
	}
	tree := Group("rules",
		Group("EWMAC", Leaf("EWMAC16", 0), Leaf("EWMAC32", 1), Leaf("EWMAC64", 2)),
		Leaf("carry", 3),
	)
	w, _, err := Weights(tree, c)
	if err != nil {
		t.Fatal(err)
	}
	for i, want := range []float64{0.21, 0.08, 0.21, 0.50} {
		near(t, "table 17 weight", w[i], want, 1e-12)
	}
	near(t, "FDM", DiversificationMultiplier(w, c, 2.5), 1.31, 0.005)
}

func TestInstrumentMultiplierTable48(t *testing.T) {
	// Chapter 15: the staunch systems trader's six futures, table 46's
	// correlations and table 48's final weights give an IDM of 1.89.
	c := uniform(6, 0.07)
	set(c, 0, 1, 0.35) // Eurodollar / T-note
	set(c, 2, 3, 0.42) // Euro Stoxx / V2X
	set(c, 3, 4, 0.14) // V2X / MXP
	set(c, 4, 5, 0.18) // MXP / corn
	w := []float64{0.117, 0.117, 0.20, 0.098, 0.233, 0.233}
	near(t, "IDM", DiversificationMultiplier(w, c, 2.5), 1.89, 0.005)
}

func TestMultiplierRuleOfThumbTable18(t *testing.T) {
	cases := []struct {
		n    int
		c    float64
		want float64
	}{{2, 0.5, 1.15}, {3, 0.25, 1.41}, {3, 0, 1.73}, {5, 0.5, 1.29}, {50, 0.25, 1.94}, {20, 0.5, 1.38}}
	// Not the 0.75 column: the book labels table 18 an approximation, and that
	// column runs up to ~3% above the exact formula (10 assets: 1.17 printed,
	// 1.136 exact) and is not even monotone in the number of assets (1.12,
	// 1.10, 1.15 for three, four, five). The exact formula on p. 297 is what
	// the book says to use when you can, and what this package computes.
	for _, k := range cases {
		w := make([]float64, k.n)
		for i := range w {
			w[i] = 1 / float64(k.n)
		}
		near(t, "table 18", DiversificationMultiplier(w, uniform(k.n, k.c), math.Inf(1)), k.want, 0.006)
	}
	near(t, "negative correlation floored at zero", DiversificationMultiplier([]float64{0.5, 0.5}, uniform(2, -0.9), 2.5), math.Sqrt(2), 1e-12)
	if got := DiversificationMultiplier(make([]float64, 20), uniform(20, 0), 2.5); got != 1 {
		t.Errorf("zero weights: got %v", got)
	}
}

func TestTable12ReadsTheBookAndInterpolatesBetweenRows(t *testing.T) {
	near(t, "A at -0.5", SharpeFactor(-0.5, Certain), 0.32, 1e-12)
	near(t, "A at +0.25", SharpeFactor(0.25, Certain), 1.48, 1e-12)
	near(t, "B at -0.10", SharpeFactor(-0.10, LongHistory), 0.95, 1e-12)
	near(t, "B at +0.50", SharpeFactor(0.50, LongHistory), 1.35, 1e-12)
	near(t, "A halfway 0.05-0.10", SharpeFactor(0.075, Certain), 1.15, 1e-12)
	near(t, "A beyond the table", SharpeFactor(0.9, Certain), 1.83, 1e-12)
	for _, d := range []float64{-0.5, -0.1, 0.3} {
		if SharpeFactor(d, ShortHistory) != 1 {
			t.Error("column C must never adjust")
		}
	}
}

func TestAdjustForSharpeKeepsTheGroupsTotal(t *testing.T) {
	w := AdjustForSharpe([]float64{0.25, 0.25}, []float64{0.5, 0}, Certain)
	near(t, "total", w[0]+w[1], 0.5, 1e-12)
	// Differences of +0.25/-0.25 against the average: factors 1.48 and 0.60.
	near(t, "ratio", w[0]/w[1], 1.48/0.60, 1e-12)
	same := AdjustForSharpe([]float64{0.3, 0.7}, []float64{0.4, 0.1}, ShortHistory)
	near(t, "short history leaves weights alone", same[0], 0.3, 1e-12)
}

func TestFourOrMoreWithoutIdenticalCorrelationsMustBeSplit(t *testing.T) {
	c := uniform(4, 0.5)
	set(c, 0, 1, 0.9)
	tree := Group("four", Leaf("a", 0), Leaf("b", 1), Leaf("c", 2), Leaf("d", 3))
	if _, _, err := Weights(tree, c); err == nil {
		t.Error("an uneven group of four was weighted instead of refused")
	}
	w, _, err := Weights(tree, uniform(4, 0.5))
	if err != nil {
		t.Fatal(err)
	}
	near(t, "even four", w[3], 0.25, 1e-12)
}

func TestTreeRefusesLeavesMissingOrTwice(t *testing.T) {
	c := uniform(3, 0.5)
	if _, _, err := Weights(Group("g", Leaf("a", 0), Leaf("b", 1)), c); err == nil {
		t.Error("a matrix row left out of the tree went unnoticed")
	}
	if _, _, err := Weights(Group("g", Leaf("a", 0), Leaf("a again", 0), Leaf("c", 2)), c); err == nil {
		t.Error("a leaf used twice went unnoticed")
	}
}

func TestClusterDoesNotChain(t *testing.T) {
	c := uniform(3, 0)
	set(c, 0, 1, 0.8)
	set(c, 1, 2, 0.8)
	groups := Cluster(3, func(a, b int) float64 { return c[a][b] }, 0.7)
	if len(groups) != 2 {
		t.Errorf("A~B, B~C, A!~C gave %v — single-linkage chaining", groups)
	}
}
