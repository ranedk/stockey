package sleeve

import (
	"math"
	"time"
)

// Monthly compounds a book's daily net returns into calendar-month returns.
// Monthly, not daily, because the paired statistic against a control must not
// pretend 2,000 daily differences are 2,000 independent observations when the
// two books hold overlapping positions; months are the coarsest unit that
// still leaves enough of them to say anything.
func Monthly(b Book) (months []time.Time, rets []float64) {
	if len(b.Dates) == 0 {
		return nil, nil
	}
	cur := monthKey(b.Dates[0])
	acc := 1.0
	for i, d := range b.Dates {
		if k := monthKey(d); !k.Equal(cur) {
			months, rets = append(months, cur), append(rets, acc-1)
			cur, acc = k, 1.0
		}
		acc *= 1 + b.Net[i]
	}
	return append(months, cur), append(rets, acc-1)
}

func monthKey(t time.Time) time.Time {
	return time.Date(t.Year(), t.Month(), 1, 0, 0, 0, 0, time.UTC)
}

// Summary is the report line for one book.
type Summary struct {
	Days         int
	AnnReturn    float64 // geometric, 252-day year
	AnnVol       float64
	SR           float64
	Skew         float64 // of monthly returns
	MaxDD        float64 // on the compounded net equity curve
	MeanTurnover float64 // per day, one-way
	CostDrag     float64 // annualized return given up to costs
}

func Summarize(b Book) Summary {
	s := Summary{Days: len(b.Net)}
	if s.Days == 0 {
		return s
	}
	equity, peak, dd := 1.0, 1.0, 0.0
	var sum, sumsq, grossSum, turnSum float64
	for i, r := range b.Net {
		equity *= 1 + r
		if equity > peak {
			peak = equity
		}
		if d := equity/peak - 1; d < dd {
			dd = d
		}
		sum += r
		sumsq += r * r
		grossSum += b.Gross[i]
		turnSum += b.Turnover[i]
	}
	n := float64(s.Days)
	mean := sum / n
	variance := math.Max(0, sumsq/n-mean*mean)
	s.AnnVol = math.Sqrt(variance) * math.Sqrt(252)
	s.AnnReturn = math.Pow(equity, 252/n) - 1
	if s.AnnVol > 0 {
		s.SR = mean * 252 / s.AnnVol
	}
	s.MaxDD = dd
	s.MeanTurnover = turnSum / n
	s.CostDrag = (grossSum - sum) / n * 252

	_, m := Monthly(b)
	s.Skew = skew(m)
	return s
}

func skew(x []float64) float64 {
	if len(x) < 3 {
		return math.NaN()
	}
	n := float64(len(x))
	var mean float64
	for _, v := range x {
		mean += v
	}
	mean /= n
	var m2, m3 float64
	for _, v := range x {
		d := v - mean
		m2 += d * d
		m3 += d * d * d
	}
	m2 /= n
	m3 /= n
	if m2 <= 0 {
		return math.NaN()
	}
	return m3 / math.Pow(m2, 1.5)
}

// Paired is the head-to-head statistic: the rule minus its control, month by
// month, on the months both traded.
type Paired struct {
	Months    int
	MeanDiff  float64 // mean monthly difference, in return units
	T         float64
	MonthsWon float64 // share of months the rule beat the control
}

// PairedMonthly compares two books that were run on the same days. Paired, not
// two independent means: both books hold the same universe on the same days,
// so the market move they share is noise that cancels only when the difference
// is taken month by month.
func PairedMonthly(rule, control Book) Paired {
	rm, rr := Monthly(rule)
	cm, cr := Monthly(control)
	byMonth := make(map[time.Time]float64, len(cm))
	for i, m := range cm {
		byMonth[m] = cr[i]
	}
	var diffs []float64
	var won float64
	for i, m := range rm {
		c, ok := byMonth[m]
		if !ok {
			continue
		}
		d := rr[i] - c
		diffs = append(diffs, d)
		if d > 0 {
			won++
		}
	}
	p := Paired{Months: len(diffs)}
	if p.Months < 2 {
		return p
	}
	n := float64(p.Months)
	var sum float64
	for _, d := range diffs {
		sum += d
	}
	p.MeanDiff = sum / n
	var ss float64
	for _, d := range diffs {
		ss += (d - p.MeanDiff) * (d - p.MeanDiff)
	}
	sd := math.Sqrt(ss / (n - 1))
	switch {
	case sd > 0:
		p.T = p.MeanDiff / (sd / math.Sqrt(n))
	case p.MeanDiff != 0:
		// A difference with no variance at all is degenerate, not
		// insignificant. Reporting 0 here would read as "no effect", which is
		// the opposite of what the data says.
		p.T = math.Inf(1)
		if p.MeanDiff < 0 {
			p.T = math.Inf(-1)
		}
	}
	p.MonthsWon = won / n
	return p
}

// GrossBook re-presents a book with costs removed, so a paired comparison can
// separate "the rule picks worse stocks" from "the rule pays more to trade".
// Diagnostic only: a book you cannot trade for free is not a book.
func GrossBook(b Book) Book {
	out := Book{Name: b.Name + " (gross)", Dates: b.Dates, Gross: b.Gross, Net: b.Gross, Turnover: b.Turnover}
	return out
}
