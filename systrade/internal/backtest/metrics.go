package backtest

import (
	"fmt"
	"math"
	"strings"
)

// Metrics summarize a backtest per Bible engineering corollary 4: never a
// bare Sharpe — always with skew, drawdown, turnover, cost drag, and the
// multiple-testing verdict.
type Metrics struct {
	Days         int
	AnnReturnPct float64 // geometric-mean daily % return, annualized
	AnnVolPct    float64 // of daily % returns on running equity
	Sharpe       float64
	TStat        float64
	Skew         float64
	MaxDDPct     float64 // max % drawdown from peak equity
	CostDragSR   float64 // annual costs expressed in Sharpe units
	HaircutSR    float64 // Sharpe × 0.75 (Law 7 pessimism factor) — NOT a deflated Sharpe; that is evidence.DeflatedSharpe
	// Busted is true if compounding capital hit zero/negative before the run
	// ended. BustedDay is the index where that happened (-1 if never).
	// AnnReturnPct/AnnVolPct/Sharpe/TStat/Skew/CostDragSR are computed ONLY
	// over the days before the bust when Busted is true — see ComputeMetrics.
	Busted    bool
	BustedDay int
}

// ComputeMetrics measures everything in PERCENT RETURNS ON RUNNING EQUITY.
// Dividing compounded cash P&L by initial capital inflates vol and drawdown
// by however much the account grew (the pre-2026-07-26 bug: a 20%-vol system
// reported 75% vol and a 349% "drawdown").
func ComputeMetrics(res *Result, capital float64) Metrics {
	pnl := res.Daily.Values
	eq := res.Equity.Values
	n := len(pnl)
	if n == 0 || capital <= 0 || len(eq) != n {
		return Metrics{}
	}

	// BUG FOUND LIVE 2026-08-22 (code review): once compounding capital
	// floors to 0 (engine.go's bust handling), every subsequent day computed
	// base<=0 and silently left rets[i] at Go's zero value — an exact 0%
	// return — instead of marking it invalid. That drags Sharpe/AnnVolPct/
	// Skew toward "flat and safe" in precisely the metrics this file's own
	// header comment says must never be inflated OR deflated, masking a
	// blow-up as a quiet, low-risk run. A "return" on zero capital is
	// undefined, not zero — once busted, there is nothing left to compound or
	// lose. Stop computing return-days at the bust; Busted/BustedDay make it
	// impossible for Report() to print a misleadingly clean number without
	// also saying why. MaxDDPct still uses the FULL equity series (correctly
	// shows -100% on a bust — that's real information, not noise to exclude).
	bustedDay := -1
	for i := range pnl {
		base := capital
		if i > 0 {
			base = eq[i-1]
		}
		if base <= 0 {
			bustedDay = i
			break
		}
	}
	validDays := n
	if bustedDay >= 0 {
		validDays = bustedDay
	}

	m := Metrics{Days: n, Busted: bustedDay >= 0, BustedDay: bustedDay}
	m.MaxDDPct = maxDrawdownPct(eq, capital) * 100
	if validDays == 0 {
		return m // busted on day 0 — nothing precedes it to measure
	}

	rets := make([]float64, validDays)
	var avgEquity float64
	for i := 0; i < validDays; i++ {
		base := capital
		if i > 0 {
			base = eq[i-1]
		}
		rets[i] = pnl[i] / base
		avgEquity += eq[i]
	}
	avgEquity /= float64(validDays)
	mean, sd := meanStd(rets)
	m.AnnReturnPct = mean * 256 * 100
	m.AnnVolPct = sd * 16 * 100
	if sd > 0 {
		m.Sharpe = mean / sd * 16
		m.TStat = mean / (sd / math.Sqrt(float64(validDays)))
	}
	m.Skew = skew(rets, mean, sd)
	m.HaircutSR = m.Sharpe * 0.75

	var costCash float64
	for _, ir := range res.Instruments {
		costCash += ir.CostCash
	}
	years := float64(validDays) / 256.0
	annVolCash := sd * 16 * avgEquity
	if years > 0 && annVolCash > 0 {
		m.CostDragSR = (costCash / years) / annVolCash
	}
	return m
}

func (m Metrics) Report() string {
	var b strings.Builder
	if m.Busted {
		fmt.Fprintf(&b, "⚠⚠ BUSTED at day %d of %d — capital hit zero. Every statistic below is computed ONLY over the %d days before the bust and does NOT represent the full run.\n",
			m.BustedDay, m.Days, m.BustedDay)
	}
	fmt.Fprintf(&b, "days=%d  annRet=%.1f%%  annVol=%.1f%%  SR=%.2f (x0.75 haircut %.2f)\n",
		m.Days, m.AnnReturnPct, m.AnnVolPct, m.Sharpe, m.HaircutSR)
	fmt.Fprintf(&b, "tStat=%.2f  skew=%.2f  maxDD=%.1f%%  costDrag=%.3f SR/yr", m.TStat, m.Skew, m.MaxDDPct, m.CostDragSR)
	if m.CostDragSR > 0.13 {
		b.WriteString("  ⚠ SPEED LIMIT BREACHED (Law 13: max 0.13)")
	}
	if m.Sharpe > 1.0 {
		b.WriteString("\n⚠ SR > 1.0: assume bug → look-ahead → hidden skew → over-fitting (Law 7)")
	}
	return b.String()
}

func meanStd(x []float64) (float64, float64) {
	n := float64(len(x))
	if n == 0 {
		return 0, 0
	}
	var s float64
	for _, v := range x {
		s += v
	}
	mean := s / n
	var ss float64
	for _, v := range x {
		d := v - mean
		ss += d * d
	}
	if n < 2 {
		return mean, 0
	}
	return mean, math.Sqrt(ss / (n - 1))
}

func skew(x []float64, mean, sd float64) float64 {
	if sd == 0 || len(x) == 0 {
		return 0
	}
	var s3 float64
	for _, v := range x {
		d := (v - mean) / sd
		s3 += d * d * d
	}
	return s3 / float64(len(x))
}

// maxDrawdownPct: worst peak-to-trough decline of the equity curve, as a
// fraction of the peak (initial capital seeds the first peak).
func maxDrawdownPct(equity []float64, initial float64) float64 {
	peak := initial
	var maxDD float64
	for _, e := range equity {
		if e > peak {
			peak = e
		}
		if peak > 0 {
			if dd := (peak - e) / peak; dd > maxDD {
				maxDD = dd
			}
		}
	}
	return maxDD
}
