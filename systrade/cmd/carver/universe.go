package main

import (
	"context"
	"fmt"
	"math"
	"time"

	"github.com/ranedk/systrader/internal/core"
	"github.com/ranedk/systrader/internal/data"
	"github.com/ranedk/systrader/internal/futures"
	"github.com/ranedk/systrader/internal/store"
)

// The universe and its cost model, fixed by
// research/preregistrations/2026-09-07_ewmac_carry_two_sleeve.md. Changing any
// number here makes a different experiment.
type spec struct {
	symbol     string
	pointValue float64 // account currency per 1.0 of quoted price, per block
	spread     float64 // full bid-ask in price points; half is paid per side
	feePerLot  float64
	pctFee     float64
	longOnly   bool
	// underlying names the futures curve to stitch; empty means the symbol is
	// an ETF read straight from systrader_ohlcv_daily.
	underlying string
}

// GOLDM is a 100g contract quoted per 10g, so a 1-rupee move is worth 10
// rupees; SILVERM is 5kg quoted per kg. The instrument master's lot_size says
// 1 for both — it counts contracts, not units — which is why these are here
// and not read from the database.
var futuresSleeve = []spec{
	{symbol: "NIFTY", underlying: "NIFTY", pointValue: 75, spread: 1.0, feePerLot: 20, pctFee: 0.0002},
	{symbol: "BANKNIFTY", underlying: "BANKNIFTY", pointValue: 35, spread: 3.0, feePerLot: 20, pctFee: 0.0002},
	{symbol: "GOLDM", underlying: "GOLDM", pointValue: 10, spread: 10.0, feePerLot: 20, pctFee: 0.0002},
	{symbol: "SILVERM", underlying: "SILVERM", pointValue: 5, spread: 150.0, feePerLot: 20, pctFee: 0.0002},
}

// ETF spreads are set as a fraction of each one's own typical price rather than
// a flat number of paise, since they range from Rs 28 to Rs 720.
var etfSleeve = []spec{
	{symbol: "NIFTYBEES", longOnly: true, pointValue: 1, pctFee: 0.0007},
	{symbol: "JUNIORBEES", longOnly: true, pointValue: 1, pctFee: 0.0007},
	{symbol: "BANKBEES", longOnly: true, pointValue: 1, pctFee: 0.0007},
	{symbol: "GOLDBEES", longOnly: true, pointValue: 1, pctFee: 0.0007},
	{symbol: "MON100", longOnly: true, pointValue: 1, pctFee: 0.0007},
	{symbol: "LTGILTBEES", longOnly: true, pointValue: 1, pctFee: 0.0007},
	{symbol: "GILT5YBEES", longOnly: true, pointValue: 1, pctFee: 0.0007},
	{symbol: "SILVERBEES", longOnly: true, pointValue: 1, pctFee: 0.0007},
}

const etfSpreadFraction = 0.0005 // 5 bps of price, converted to points below

// loadSleeve builds instruments for one sleeve over [from, to]. Futures
// instruments carry a stitched price series, a matching stitched open series
// and a carry series; ETFs carry price and open only.
func loadSleeve(ctx context.Context, st *store.Store, specs []spec, from, to time.Time, costMult float64) ([]*data.Instrument, []string, error) {
	var out []*data.Instrument
	var notes []string
	for _, sp := range specs {
		var closes, opens, carry core.Series
		if sp.underlying != "" {
			raw, err := st.FuturesSlots(ctx, sp.underlying)
			if err != nil {
				return nil, nil, err
			}
			slots := make([]futures.Slot, 0, len(raw))
			for _, r := range raw {
				slots = append(slots, futures.Slot{Ticker: r.Ticker, Expiry: r.Expiry, Closes: r.Closes})
			}
			var ref core.Series
			if refName := references[sp.underlying]; refName != "" {
				if ref, err = st.BackfillCloses(ctx, refName); err != nil {
					return nil, nil, err
				}
			}
			b := futures.Build(sp.underlying, slots, ref, futures.DefaultOptions())
			if b.Unusable != "" {
				notes = append(notes, fmt.Sprintf("%s EXCLUDED: %s", sp.symbol, b.Unusable))
				continue
			}
			front := b.Curve.Positions[0]
			rawOpens, err := st.BackfillOpens(ctx, front.Ticker)
			if err != nil {
				return nil, nil, err
			}
			closes = b.Adjusted
			// The opens take the SAME splice adjustment as the closes: they are
			// the same contract on the same day.
			opens = futures.ApplyGaps(core.AlignByTime(closes, rawOpens), b.Gaps)
			carry = b.Carry
			notes = append(notes, fmt.Sprintf("%s: %d splices from %s rolls, carry over %.0f days",
				sp.symbol, len(b.Gaps), b.RollSource, b.GapYears*365))
		} else {
			var err error
			if closes, err = st.BackfillCloses(ctx, sp.symbol); err != nil {
				return nil, nil, err
			}
			rawOpens, err := st.BackfillOpens(ctx, sp.symbol)
			if err != nil {
				return nil, nil, err
			}
			opens = core.AlignByTime(closes, rawOpens)
		}

		closes, opens, carry = window(closes, from, to), window(opens, from, to), window(carry, from, to)
		if closes.Len() < 250 {
			notes = append(notes, fmt.Sprintf("%s EXCLUDED: only %d bars in the window", sp.symbol, closes.Len()))
			continue
		}

		spread := sp.spread
		if sp.underlying == "" {
			spread = etfSpreadFraction * medianOf(closes.Values)
		}
		inst := &data.Instrument{
			Meta: data.Meta{
				Symbol: sp.symbol, PointValue: sp.pointValue, Block: 1, LongOnly: sp.longOnly,
				SpreadPoints: spread * costMult, FeePerBlock: sp.feePerLot * costMult,
				PercentValueFee: sp.pctFee * costMult,
			},
			Prices: closes,
			Opens:  &opens,
		}
		if carry.Len() > 0 {
			inst.AnnCarry = &carry
		}
		out = append(out, inst)
	}
	return out, notes, nil
}

func window(s core.Series, from, to time.Time) core.Series {
	if s.Len() == 0 {
		return s
	}
	var times []time.Time
	var vals []float64
	for i, d := range s.Times {
		if d.Before(from) || d.After(to) {
			continue
		}
		times = append(times, d)
		vals = append(vals, s.Values[i])
	}
	return core.New(times, vals)
}

func medianOf(x []float64) float64 {
	var finite []float64
	for _, v := range x {
		if !math.IsNaN(v) && v > 0 {
			finite = append(finite, v)
		}
	}
	if len(finite) == 0 {
		return math.NaN()
	}
	for i := 1; i < len(finite); i++ { // insertion sort: these are short
		for j := i; j > 0 && finite[j] < finite[j-1]; j-- {
			finite[j], finite[j-1] = finite[j-1], finite[j]
		}
	}
	return finite[len(finite)/2]
}
