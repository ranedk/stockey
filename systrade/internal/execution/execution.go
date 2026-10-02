// Package execution turns a paper track's order sheet into the orders a Dhan
// account would send: the Rs 1 crore account's share counts, as delivery
// (CNC) market orders pumped into the next session's pre-open call auction —
// the fill every backtest and paper record assumes (LEDGER row 35).
//
// It decides nothing. Quantities are the sheet's, and the sheet is the frozen
// spec's (Law 19); a sheet this package cannot turn into orders exactly is
// refused whole, never trimmed to the part that works.
package execution

import (
	"fmt"
	"hash/fnv"
	"math"
	"sort"
	"strings"
	"time"

	"github.com/ranedk/systrader/internal/store"
)

// Planned is one broker order.
type Planned struct {
	Symbol        string
	Side          string // BUY | SELL
	Quantity      int
	SecurityID    string
	CorrelationID string
	RefPrice      float64 // the sheet's reference price
	ValueRs       float64 // the sheet's rupees for the trade
}

// Batch is a sheet's orders and everything wrong with them.
type Batch struct {
	Strategy    string
	BasedOn     time.Time
	Due         bool
	Orders      []Planned
	SheetTrades int // sheet rows with rupees to trade
	BuyRs       float64
	SellRs      float64
	Problems    []string
	// Notes are facts worth seeing that need no action, e.g. a name NSE moved to trade-for-trade
	// (BE): its order goes to the BE security id, delivery only.
	Notes []string
}

// OK is true when the batch reproduces its sheet exactly and nothing is wrong.
func (b Batch) OK() bool { return len(b.Problems) == 0 }

// unitTolerance bounds how far quantity × reference price may sit from the
// sheet's rupees. The two are priced a few hours apart (the reference is the
// sheet's open, the rupees its close), so they differ by a day's move; a
// quantity off by a factor — a lot size, a split, a unit — differs by far more.
const unitTolerance = 0.25

// Build turns a stored order sheet into broker orders: sells first, so a
// rebalance frees cash before it spends it, then buys; alphabetical within.
func Build(strategy string, sheet store.PaperPending, ids map[string][]store.Instrument) Batch {
	b := Batch{Strategy: strategy, BasedOn: sheet.BasedOn, Due: sheet.Due}
	bad := func(format string, a ...any) { b.Problems = append(b.Problems, fmt.Sprintf(format, a...)) }
	seen := map[string]string{}
	for _, o := range sheet.Orders {
		if o.ValueRs == nil || *o.ValueRs <= 0 {
			continue // weight-only row, or inside Law 12's band: nothing to send
		}
		b.SheetTrades++
		if o.FromShares == nil || o.ToShares == nil {
			bad("%s: rupees to trade but no share counts", o.Symbol)
			continue
		}
		q := math.Round(*o.ToShares) - math.Round(*o.FromShares)
		if q == 0 {
			bad("%s: rupees to trade but no whole-share change", o.Symbol)
			continue
		}
		side := "BUY"
		if q < 0 {
			side = "SELL"
		}
		qty := int(math.Abs(q))

		var secIDs []int64
		var freeze float64
		series := ""
		for _, in := range ids[o.Symbol] {
			secIDs = append(secIDs, in.SecurityID)
			freeze = in.FreezeQty
			series = in.Series
		}
		if series != "" && series != "EQ" {
			b.Notes = append(b.Notes, fmt.Sprintf("%s trades in %s (trade-for-trade, delivery only): order sent to its %s security id",
				o.Symbol, series, series))
		}
		switch len(secIDs) {
		case 0:
			bad("%s: no current Dhan security id (master_dhan_instruments)", o.Symbol)
			continue
		case 1:
		default:
			bad("%s: %d current Dhan security ids %v — refusing to guess", o.Symbol, len(secIDs), secIDs)
			continue
		}
		if freeze > 0 && float64(qty) > freeze {
			bad("%s: %d shares exceeds the freeze quantity %.0f", o.Symbol, qty, freeze)
		}
		var ref float64
		if o.FillPrice != nil {
			ref = *o.FillPrice
		}
		if ref > 0 {
			if off := math.Abs(float64(qty)*ref-*o.ValueRs) / *o.ValueRs; off > unitTolerance {
				bad("%s: %d shares × %.2f is %.0f%% off the sheet's Rs %.0f — a unit error?",
					o.Symbol, qty, ref, 100*off, *o.ValueRs)
			}
		}
		cid := CorrelationID(strategy, sheet.BasedOn, o.Symbol)
		if prev, dup := seen[cid]; dup {
			bad("%s and %s share correlation id %s", prev, o.Symbol, cid)
		}
		seen[cid] = o.Symbol
		p := Planned{Symbol: o.Symbol, Side: side, Quantity: qty, SecurityID: fmt.Sprint(secIDs[0]),
			CorrelationID: cid, RefPrice: ref, ValueRs: *o.ValueRs}
		if side == "BUY" {
			b.BuyRs += p.ValueRs
		} else {
			b.SellRs += p.ValueRs
		}
		b.Orders = append(b.Orders, p)
	}
	sort.Slice(b.Orders, func(i, j int) bool {
		if b.Orders[i].Side != b.Orders[j].Side {
			return b.Orders[i].Side == "SELL"
		}
		return b.Orders[i].Symbol < b.Orders[j].Symbol
	})
	if len(b.Problems) == 0 && len(b.Orders) != b.SheetTrades {
		bad("%d orders for %d sheet trades", len(b.Orders), b.SheetTrades)
	}
	return b
}

// CorrelationID names an order uniquely and deterministically — the same
// strategy, sheet date and symbol always give the same id, which is what lets
// a re-run recognise an order it already placed. Dhan allows 30 characters of
// letters, digits, space, underscore and hyphen: the strategy's initials, the
// sheet's date, the symbol's letters and a short hash of the full symbol, so
// "M&M" and "MM" cannot collide.
func CorrelationID(strategy string, basedOn time.Time, symbol string) string {
	var code strings.Builder
	for _, part := range strings.Split(strategy, "-") {
		if part != "" && code.Len() < 4 {
			code.WriteByte(part[0])
		}
	}
	var clean strings.Builder
	for _, r := range symbol {
		if (r >= 'A' && r <= 'Z') || (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') {
			clean.WriteRune(r)
		}
	}
	sym := clean.String()
	if len(sym) > 12 {
		sym = sym[:12]
	}
	h := fnv.New32a()
	h.Write([]byte(strategy + "|" + symbol))
	return fmt.Sprintf("%s%s-%s-%04x", code.String(), basedOn.Format("060102"), sym, h.Sum32()&0xffff)
}

// IST is the exchange's clock (India has no DST, so a fixed offset is exact).
var IST = time.FixedZone("IST", 5*3600+1800)

// LastClosedSession is the most recent NSE cash session that had closed by
// now: today after 15:30 IST on a trading day, otherwise the trading day
// before. A fresh order sheet is priced off exactly this close. holidays are
// dates at UTC midnight, as every date column here is stored.
func LastClosedSession(now time.Time, holidays map[time.Time]bool) time.Time {
	t := now.In(IST)
	d := time.Date(t.Year(), t.Month(), t.Day(), 0, 0, 0, 0, time.UTC)
	trading := func(d time.Time) bool {
		return d.Weekday() != time.Saturday && d.Weekday() != time.Sunday && !holidays[d]
	}
	closed := t.Hour() > 15 || (t.Hour() == 15 && t.Minute() >= 30)
	if trading(d) && closed {
		return d
	}
	for {
		d = d.AddDate(0, 0, -1)
		if trading(d) {
			return d
		}
	}
}
