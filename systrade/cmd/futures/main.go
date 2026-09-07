// Command futures turns the raw Dhan slot histories into a curve a backtest
// may use — or refuses to, loudly, when the data cannot carry it.
//
//	futures report    per underlying: what cleaning kept and threw away, the
//	                  rolls each detector found, the stitched series and its
//	                  carry, and every reason the result might be unusable
//
// All the judgement lives in internal/futures.Build, so a backtest reading
// these series gets exactly what this report describes. Nothing is written to
// the database: a stored continuous series is a stale one the moment the next
// roll happens.
package main

import (
	"context"
	"flag"
	"fmt"
	"math"
	"os"
	"strings"
	"time"

	"github.com/ranedk/systrader/internal/core"
	"github.com/ranedk/systrader/internal/futures"
	"github.com/ranedk/systrader/internal/store"
)

// references pairs each underlying with a series that has no splices in it, so
// a roll shows up as the futures moving when the reference did not. Index
// futures get their own spot index. Gold and silver get the physically-backed
// ETF — the same asset in the same currency, close enough to see a splice, not
// close enough to trade against. Crude has nothing.
var references = map[string]string{
	"NIFTY":     "NIFTY",
	"BANKNIFTY": "BANKNIFTY",
	"GOLDM":     "GOLDBEES",
	"SILVERM":   "SILVERBEES",
	"CRUDEOILM": "",
}

var underlyings = []string{"NIFTY", "BANKNIFTY", "GOLDM", "SILVERM", "CRUDEOILM"}

func main() {
	fs := flag.NewFlagSet("report", flag.ExitOnError)
	only := fs.String("underlying", "", "restrict to one underlying")
	sigmas := fs.Float64("sigmas", futures.DefaultOptions().Sigmas, "robust sigmas of divergence that mark a splice")
	window := fs.Int("snap-window", futures.DefaultOptions().SnapWindow, "days either side of a calendar expiry the ladder may move a roll")
	args := os.Args[1:]
	if len(args) > 0 && !strings.HasPrefix(args[0], "-") {
		if args[0] != "report" {
			fmt.Fprintln(os.Stderr, "usage: futures report [flags]")
			os.Exit(2)
		}
		args = args[1:]
	}
	check(fs.Parse(args))

	ctx := context.Background()
	st, err := store.Open(ctx)
	check(err)
	defer st.Close()

	opts := futures.Options{Sigmas: *sigmas, SnapWindow: *window}
	usable := 0
	for _, u := range underlyings {
		if *only != "" && *only != u {
			continue
		}
		if report(ctx, st, u, opts) {
			usable++
		}
	}
	fmt.Printf("%d underlying(s) produced a usable continuous series.\n", usable)
}

func report(ctx context.Context, st *store.Store, underlying string, opts futures.Options) bool {
	fmt.Printf("========== %s ==========\n", underlying)
	raw, err := st.FuturesSlots(ctx, underlying)
	check(err)
	slots := make([]futures.Slot, 0, len(raw))
	for _, r := range raw {
		slots = append(slots, futures.Slot{Ticker: r.Ticker, Expiry: r.Expiry, Closes: r.Closes})
	}

	var ref core.Series
	refName := references[underlying]
	if refName != "" {
		ref, err = st.BackfillCloses(ctx, refName)
		check(err)
	}

	b := futures.Build(underlying, slots, ref, opts)

	fmt.Printf("%d slots stored -> %d curve positions kept\n", len(slots), len(b.Curve.Positions))
	for i, p := range b.Curve.Positions {
		fmt.Printf("  position %d  %-20s expiry %s  %5d bars  %s..%s  last %.2f\n",
			i+1, p.Ticker, p.Expiry.Format("2006-01-02"), p.Closes.Len(),
			p.Closes.Times[0].Format("2006-01-02"), p.Closes.Times[p.Closes.Len()-1].Format("2006-01-02"),
			p.Closes.Values[p.Closes.Len()-1])
	}
	for _, r := range b.Curve.Rejected {
		fmt.Printf("  REJECTED    %-20s %s\n", r.Ticker, r.Reason)
	}

	if refName == "" {
		fmt.Printf("  reference detector: NO REFERENCE SERIES EXISTS for %s\n", underlying)
	} else {
		fmt.Printf("  reference detector (%s, %.0f sigma): biggest divergence %.1f sigma, %d days clear the bar\n",
			refName, opts.Sigmas, b.RefMaxSigma, b.RefAboveBar)
		if b.RefAboveBar == 0 && len(b.Curve.Positions) >= 2 {
			fmt.Printf("      -> this reference cannot see the splices: the ladder step is only %.0f bps and the\n", 10000*b.BasisFrac)
			fmt.Printf("         pair's day-to-day tracking noise is larger. Absence of detections here is NOT\n")
			fmt.Printf("         evidence that the series is unspliced.\n")
		}
	}
	if b.LadderMons > 0 {
		fmt.Printf("  ladder: positions %d month(s) apart (%.0f days) — %s cycle, step %.0f bps\n",
			b.LadderMons, b.GapYears*365, cycleWord(b.LadderMons), 10000*b.BasisFrac)
	}
	for _, w := range b.Warnings {
		fmt.Printf("  WARNING: %s\n", w)
	}
	if b.Unusable != "" {
		fmt.Printf("  -> UNUSABLE: %s\n\n", b.Unusable)
		return false
	}

	fmt.Printf("  rolls (%s): %d, %s\n", b.RollSource, len(b.Rolls), spacingSummary(b.Rolls))
	var sum float64
	for _, g := range b.Gaps {
		sum += g.Basis
	}
	minAdj, negative := math.Inf(1), 0
	for _, v := range b.Adjusted.Values {
		if v < minAdj {
			minAdj = v
		}
		if v <= 0 {
			negative++
		}
	}
	fmt.Printf("  panama: %d splices, mean basis %.2f, cumulative %.2f, adjusted low %.2f%s\n",
		len(b.Gaps), sum/float64(len(b.Gaps)), sum, minAdj, negativeNote(negative))

	front := b.Curve.Positions[0].Closes
	rawZ, adjZ, ordZ := futures.RollDayJump(front, b.Adjusted, b.Rolls)
	fmt.Printf("  stitch check: roll-day move is %.2fx a typical day raw, %.2fx adjusted (ordinary day %.2fx)\n",
		rawZ, adjZ, ordZ)
	fmt.Printf("  carry: mean %.2f price units/yr over a %.0f-day gap (%s)\n\n",
		meanFinite(b.Carry), b.GapYears*365, contangoWord(meanFinite(b.Carry)))
	return true
}

func negativeNote(n int) string {
	if n == 0 {
		return ""
	}
	return fmt.Sprintf("  [%d adjusted prices <= 0: Panama's known cost, size on price differences not percentages]", n)
}

func cycleWord(months int) string {
	switch months {
	case 1:
		return "monthly"
	case 3:
		return "quarterly"
	default:
		return fmt.Sprintf("%d-monthly", months)
	}
}

func contangoWord(mean float64) string {
	if mean < 0 {
		return "contango — the front is cheaper, so holding rolls up the curve and costs money"
	}
	return "backwardation — the front is dearer, so holding rolls down the curve and pays"
}

func meanFinite(s core.Series) float64 {
	var sum float64
	var n int
	for _, v := range s.Values {
		if !math.IsNaN(v) {
			sum += v
			n++
		}
	}
	if n == 0 {
		return math.NaN()
	}
	return sum / float64(n)
}

func spacingSummary(rolls []time.Time) string {
	if len(rolls) < 2 {
		return "too few to summarize"
	}
	var min, max, sum float64
	min = math.Inf(1)
	for i := 1; i < len(rolls); i++ {
		d := rolls[i].Sub(rolls[i-1]).Hours() / 24
		sum += d
		if d < min {
			min = d
		}
		if d > max {
			max = d
		}
	}
	n := float64(len(rolls) - 1)
	return fmt.Sprintf("spacing %.0f/%.0f/%.0f days (min/mean/max), %s..%s",
		min, sum/n, max, rolls[0].Format("2006-01-02"), rolls[len(rolls)-1].Format("2006-01-02"))
}

func check(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, "futures:", err)
		os.Exit(1)
	}
}
