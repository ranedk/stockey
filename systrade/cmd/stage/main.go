// Command stage prints Weinstein stage classifications (internal/stage) for
// one or more NSE tickers. REPORTING ONLY — this is regime-tagged output for
// a human to read (docs/rule_ideas.md innovation #7, Law 16); it does not
// touch sizing, positions, or any other command in this repo.
//
//	stage -tickers RELIANCE,TCS           latest stage + recent history
//	stage -tickers RELIANCE -history 12   last 12 weekly reads instead of 6
package main

import (
	"context"
	"flag"
	"fmt"
	"math"
	"os"
	"strings"

	"github.com/joho/godotenv"

	"github.com/ranedk/systrader/internal/core"
	"github.com/ranedk/systrader/internal/stage"
	"github.com/ranedk/systrader/internal/store"
)

func main() {
	_ = godotenv.Load()
	tickers := flag.String("tickers", "", "comma-separated NSE tickers (required)")
	history := flag.Int("history", 6, "number of most recent weekly reads to print per ticker")
	flag.Parse()
	if strings.TrimSpace(*tickers) == "" {
		fmt.Println("usage: stage -tickers RELIANCE,TCS [-history N]")
		os.Exit(2)
	}

	ctx := context.Background()
	st, err := store.Open(ctx)
	if err != nil {
		panic(err)
	}
	defer st.Close()

	for _, tk := range strings.Split(*tickers, ",") {
		tk = strings.TrimSpace(tk)
		if tk == "" {
			continue
		}
		report(ctx, st, tk, *history)
	}
}

func report(ctx context.Context, st *store.Store, ticker string, history int) {
	daily, err := st.AdjustedCloses(ctx, ticker)
	if err != nil {
		fmt.Printf("%s: skip (%v)\n", ticker, err)
		return
	}
	weeklyClose := core.ResampleWeeklyLast(daily)
	if weeklyClose.Len() < stage.MAWindowWeeks+stage.SlopeWindowWeeks {
		fmt.Printf("%s: skip (only %d weekly bars, need >= %d for the first stage read)\n",
			ticker, weeklyClose.Len(), stage.MAWindowWeeks+stage.SlopeWindowWeeks)
		return
	}

	var volPtr *core.Series
	if dailyVol, err := st.AdjustedVolume(ctx, ticker); err == nil {
		weeklyVol := core.ResampleWeeklySum(dailyVol)
		if weeklyVol.Len() == weeklyClose.Len() {
			volPtr = &weeklyVol
		}
		// A length mismatch (gappy volume history) just means no volume
		// confirmation for this ticker — Classify is nil-safe for that.
	}

	cs := stage.Classify(weeklyClose, volPtr)
	latest, _ := stage.Latest(cs)
	fmt.Printf("\n=== %s ===  latest: %s  (week of %s)\n", ticker, latest.Stage, latest.Time.Format("2006-01-02"))
	fmt.Printf("  close=%.2f  ma30=%.2f  slope4w=%s  volRatio10w=%s\n",
		latest.Close, latest.MA30, fmtPct(latest.MASlopePct), fmtRatio(latest.VolumeRatio))

	start := len(cs) - history
	if start < 0 {
		start = 0
	}
	fmt.Println("  recent:")
	for _, c := range cs[start:] {
		fmt.Printf("    %s  %-22s close=%.2f ma30=%.2f slope4w=%s\n",
			c.Time.Format("2006-01-02"), c.Stage, c.Close, c.MA30, fmtPct(c.MASlopePct))
	}
}

func fmtPct(v float64) string {
	if math.IsNaN(v) {
		return "n/a"
	}
	return fmt.Sprintf("%+.2f%%", v)
}

func fmtRatio(v float64) string {
	if math.IsNaN(v) {
		return "n/a"
	}
	return fmt.Sprintf("%.2fx", v)
}
