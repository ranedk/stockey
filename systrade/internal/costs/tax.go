package costs

import "math"

// Indian tax on listed equity (STT paid), FY 2025-26 onward. Facts, not a model.
const (
	STCGRate = 0.20  // held 12 months or less
	LTCGRate = 0.125 // held longer, on gains above the yearly exemption
	// LTCGExemption is rupees of long-term gain a year that pay no tax.
	LTCGExemption = 125000.0
	// YearDays is 12 months in trading days: the line a holding must cross to be long-term.
	YearDays = 252
)

// LongTermShare is the share of a book's realised GAINS that are long-term, for a
// book rebalanced every holdDays trading days that sells a fraction churn of its names
// at each rebalance. This is a MODEL: each name is sold at any rebalance with
// probability churn, independent of its history (a geometric holding life), and its
// gain grows in proportion to how long it was held. A name sold at the k-th
// rebalance is long-term when k·holdDays > YearDays, so with q = 1-churn and
// m = floor(YearDays/holdDays)+1 the gain-weighted share is q^(m-1)·(churn·(m-1)+1).
//
// It is what makes the holding period matter for tax: a monthly book replacing two
// thirds of itself realises essentially nothing long-term (q^12 ~ 2e-6), and neither
// does a quarterly one (1.2%). Only low churn -- a rank buffer, A2 -- moves it.
func LongTermShare(holdDays int, churn float64) float64 {
	if holdDays <= 0 || churn <= 0 || churn > 1 || math.IsNaN(churn) {
		return math.NaN()
	}
	m := YearDays/holdDays + 1
	q := 1 - churn
	return math.Pow(q, float64(m-1)) * (churn*float64(m-1) + 1)
}

// AnnualTax is the tax, in rupees, on a year's realised gain of gain rupees of which
// longTermShare is long-term. Losses pay nothing (and carry forward; not modelled).
func AnnualTax(gain, longTermShare float64) float64 {
	if gain <= 0 || math.IsNaN(longTermShare) {
		return 0
	}
	st := gain * (1 - longTermShare)
	lt := gain * longTermShare
	return STCGRate*st + LTCGRate*math.Max(0, lt-LTCGExemption)
}
