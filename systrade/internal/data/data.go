// Package data defines instruments and loads price data.
package data

import (
	"encoding/csv"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/ranedk/systrader/internal/core"
)

// Meta describes the contract economics of one instrument.
type Meta struct {
	Symbol string
	// PointValue: account-currency P&L for a 1.0 move in quoted price, per
	// block (e.g. NIFTY futures: lot 75 → PointValue 75; a cash equity block
	// of 1 share → 1).
	PointValue float64
	// Block: minimum tradable position increment, in blocks (normally 1).
	Block float64
	// LongOnly: cash equities/ETFs that cannot be shorted overnight in India.
	// Forecasts are clipped to [0, +20] for these.
	LongOnly bool

	// Cost model, all in account currency unless noted.
	SpreadPoints    float64 // full bid-ask spread in price points; we pay half per side
	FeePerBlock     float64 // commission etc. per block per side
	PercentValueFee float64 // fraction of traded value per side (STT/stamp), e.g. 0.0002
}

// Instrument bundles metadata with its price history and optional carry data.
type Instrument struct {
	Meta   Meta
	Prices core.Series
	// Opens: session opening prices aligned to Prices dates (optional).
	// When present and the engine runs ExecuteAtOpen, fills happen at the
	// open AFTER the decision close instead of at the decision close itself.
	Opens *core.Series
	// AnnCarry: expected return if prices never move, in PRICE UNITS PER YEAR
	// (e.g. futures: (nearer − traded)/year-gap; equity: (div yield − funding) × price).
	// nil if unavailable → the carry rule emits NaN and is renormalized away.
	AnnCarry *core.Series
}

// CostPerBlockCash returns the one-side cash cost of trading one block at price p.
func (m Meta) CostPerBlockCash(p float64) float64 {
	return m.SpreadPoints/2*m.PointValue + m.FeePerBlock + m.PercentValueFee*p*m.PointValue
}

// StandardizedCostSR is Carver's cost per round trip in Sharpe-ratio units:
// 2×C ÷ (16 × instrument currency volatility). priceVol is daily vol in price units.
func (m Meta) StandardizedCostSR(price, priceVol float64) float64 {
	icv := priceVol * m.PointValue
	if icv <= 0 {
		return 0
	}
	return 2 * m.CostPerBlockCash(price) / (16 * icv)
}

// LoadCSV reads date,...,close[,...] daily bars. Accepts either
// "date,close" or "date,open,high,low,close[,volume]" with or without a
// header row. Dates: 2006-01-02.
func LoadCSV(path string) (core.Series, error) {
	f, err := os.Open(path)
	if err != nil {
		return core.Series{}, err
	}
	defer f.Close()
	rd := csv.NewReader(f)
	rd.FieldsPerRecord = -1
	recs, err := rd.ReadAll()
	if err != nil {
		return core.Series{}, err
	}
	var times []time.Time
	var vals []float64
	for i, rec := range recs {
		if len(rec) < 2 {
			continue
		}
		t, terr := time.Parse("2006-01-02", strings.TrimSpace(rec[0]))
		if terr != nil {
			if i == 0 {
				continue // header
			}
			return core.Series{}, fmt.Errorf("%s line %d: bad date %q", path, i+1, rec[0])
		}
		closeIdx := 1
		if len(rec) >= 5 {
			closeIdx = 4 // OHLCV layout
		}
		v, verr := strconv.ParseFloat(strings.TrimSpace(rec[closeIdx]), 64)
		if verr != nil {
			return core.Series{}, fmt.Errorf("%s line %d: bad close %q", path, i+1, rec[closeIdx])
		}
		times = append(times, t)
		vals = append(vals, v)
	}
	if len(times) == 0 {
		return core.Series{}, fmt.Errorf("%s: no rows parsed", path)
	}
	return core.New(times, vals), nil
}
