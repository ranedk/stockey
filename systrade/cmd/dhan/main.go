// Command dhan is an ops CLI for the Dhan broker integration.
//
//	dhan funds                      available balance/margin
//	dhan holdings                   demat holdings
//	dhan positions                  open positions
//	dhan hist -sec 1333 -seg NSE_EQ -inst EQUITY -from 2016-01-01
//	                                fetch daily history (one-off inspection)
//	dhan backfill [-group etf|index|futures|all] [-from 2015-01-01]
//	                                fetch gap series into systrader_ohlcv_daily
//
// Auth: reuses stockey's cached access token (see internal/broker/dhan).
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/ranedk/systrader/internal/broker/dhan"
	"github.com/ranedk/systrader/internal/store"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Println("usage: dhan funds|holdings|positions|hist [flags]")
		os.Exit(1)
	}
	c, err := dhan.New()
	if err != nil {
		fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	switch os.Args[1] {
	case "funds":
		dump(c.FundLimit(ctx))
	case "holdings":
		dump(c.Holdings(ctx))
	case "positions":
		dump(c.Positions(ctx))
	case "hist":
		fs := flag.NewFlagSet("hist", flag.ExitOnError)
		sec := fs.String("sec", "", "securityId (see master_dhan_instruments)")
		seg := fs.String("seg", "NSE_EQ", "exchangeSegment: NSE_EQ|NSE_FNO|MCX_COMM|IDX_I")
		inst := fs.String("inst", "EQUITY", "instrument: EQUITY|INDEX|FUTIDX|FUTCOM")
		from := fs.String("from", "2016-01-01", "from date")
		to := fs.String("to", time.Now().Format("2006-01-02"), "to date")
		_ = fs.Parse(os.Args[2:])
		f, _ := time.Parse("2006-01-02", *from)
		t, _ := time.Parse("2006-01-02", *to)
		cd, err := c.HistoricalDaily(ctx, *sec, *seg, *inst, f, t)
		if err != nil {
			fatal(err)
		}
		fmt.Printf("%d candles\n", len(cd.Close))
		for i := range cd.Close {
			if i >= len(cd.Close)-5 { // show the tail
				fmt.Printf("%s  o=%.2f h=%.2f l=%.2f c=%.2f v=%.0f\n",
					time.Unix(int64(cd.Timestamp[i]), 0).Format("2006-01-02"),
					cd.Open[i], cd.High[i], cd.Low[i], cd.Close[i], cd.Volume[i])
			}
		}
	case "backfill":
		fs := flag.NewFlagSet("backfill", flag.ExitOnError)
		group := fs.String("group", "all", "etf|index|futures|all")
		from := fs.String("from", "2015-01-01", "history start when nothing stored yet")
		_ = fs.Parse(os.Args[2:])
		start, err := time.Parse("2006-01-02", *from)
		if err != nil {
			fatal(err)
		}
		bctx, bcancel := context.WithTimeout(context.Background(), 30*time.Minute)
		defer bcancel()
		if err := backfill(bctx, c, *group, start); err != nil {
			fatal(err)
		}
	default:
		fatal(fmt.Errorf("unknown subcommand %q", os.Args[1]))
	}
}

// target is one series to keep filled in systrader_ohlcv_daily.
type target struct {
	ticker     string
	securityID int64
	segment    string
	instrument string
	expiry     *time.Time
}

// Curated spot universe (docs/instruments_india.md). Security ids come from
// master_dhan_instruments (NSE rows) and are stable; futures are NOT listed
// here — they're resolved per-contract from the master at run time.
var etfTargets = []target{
	{ticker: "NIFTYBEES", securityID: 10576, segment: "NSE_EQ", instrument: "EQUITY"},
	{ticker: "JUNIORBEES", securityID: 10939, segment: "NSE_EQ", instrument: "EQUITY"},
	{ticker: "BANKBEES", securityID: 11439, segment: "NSE_EQ", instrument: "EQUITY"},
	{ticker: "GOLDBEES", securityID: 14428, segment: "NSE_EQ", instrument: "EQUITY"},
	{ticker: "SILVERBEES", securityID: 8080, segment: "NSE_EQ", instrument: "EQUITY"},
	{ticker: "LTGILTBEES", securityID: 17700, segment: "NSE_EQ", instrument: "EQUITY"},
	{ticker: "GILT5YBEES", securityID: 3172, segment: "NSE_EQ", instrument: "EQUITY"},
	{ticker: "MON100", securityID: 22739, segment: "NSE_EQ", instrument: "EQUITY"},
}

var indexTargets = []target{
	{ticker: "NIFTY", securityID: 13, segment: "IDX_I", instrument: "INDEX"},
	{ticker: "BANKNIFTY", securityID: 25, segment: "IDX_I", instrument: "INDEX"},
}

// Futures underlyings we stitch later: NSE index + MCX minis (two-sleeve doc).
var futuresUnderlyings = []string{"NIFTY", "BANKNIFTY", "GOLDM", "SILVERM", "CRUDEOILM"}

func backfill(ctx context.Context, c *dhan.Client, group string, defaultStart time.Time) error {
	st, err := store.Open(ctx)
	if err != nil {
		return err
	}
	defer st.Close()
	if err := st.EnsureBackfillTable(ctx); err != nil {
		return err
	}

	var targets []target
	if group == "etf" || group == "all" {
		targets = append(targets, etfTargets...)
	}
	if group == "index" || group == "all" {
		targets = append(targets, indexTargets...)
	}
	if group == "futures" || group == "all" {
		contracts, err := st.ListFuturesContracts(ctx, futuresUnderlyings)
		if err != nil {
			return err
		}
		for _, fc := range contracts {
			exp := fc.Expiry
			targets = append(targets, target{
				// Ticker embeds the expiry so every contract is a distinct
				// series (MCX reuses symbol_name across expiries).
				ticker:     fmt.Sprintf("%s-%s", fc.Underlying, exp.Format("2006-01-02")),
				securityID: fc.SecurityID,
				segment:    fc.Segment,
				instrument: fc.Instrument,
				expiry:     &exp,
			})
		}
	}
	if len(targets) == 0 {
		return fmt.Errorf("backfill: unknown group %q", group)
	}

	today := time.Now()
	okN, skipN, failN := 0, 0, 0
	for _, t := range targets {
		from := defaultStart
		if last, err := st.BackfillMaxDate(ctx, t.securityID); err != nil {
			return err
		} else if !last.IsZero() {
			from = last.AddDate(0, 0, 1)
		}
		to := today
		if t.expiry != nil && t.expiry.Before(to) {
			to = *t.expiry
		}
		if !from.Before(to.AddDate(0, 0, 1)) {
			skipN++
			continue // already current (or contract fully covered)
		}
		cd, err := c.HistoricalDaily(ctx, fmt.Sprint(t.securityID), t.segment, t.instrument, from, to)
		if err != nil {
			// DH-905 = nothing to serve for this security id: expired token
			// slots and far contracts that haven't traded yet. Expected.
			if strings.Contains(err.Error(), "DH-905") {
				fmt.Printf("skip %-24s no data at Dhan (dead/untraded slot)\n", t.ticker)
				skipN++
			} else {
				fmt.Printf("FAIL %-24s %v\n", t.ticker, err)
				failN++
			}
			time.Sleep(300 * time.Millisecond)
			continue
		}
		rows := make([]store.BackfillRow, 0, len(cd.Close))
		for i := range cd.Close {
			ts := time.Unix(int64(cd.Timestamp[i]), 0).In(time.Local)
			rows = append(rows, store.BackfillRow{
				Ticker: t.ticker, SecurityID: t.securityID,
				Segment: t.segment, Instrument: t.instrument, Expiry: t.expiry,
				Date: time.Date(ts.Year(), ts.Month(), ts.Day(), 0, 0, 0, 0, time.UTC),
				Open: cd.Open[i], High: cd.High[i], Low: cd.Low[i],
				Close: cd.Close[i], Volume: cd.Volume[i],
			})
		}
		n, err := st.UpsertBackfill(ctx, rows)
		if err != nil {
			return fmt.Errorf("upsert %s: %w", t.ticker, err)
		}
		if n > 0 {
			fmt.Printf("ok   %-24s +%d bars (%s → %s)\n", t.ticker, n,
				rows[0].Date.Format("2006-01-02"), rows[len(rows)-1].Date.Format("2006-01-02"))
			okN++
		} else {
			skipN++
		}
		time.Sleep(300 * time.Millisecond) // stay far inside Dhan's data-API rate limit
	}
	fmt.Printf("backfill done: %d updated, %d current/empty, %d failed\n", okN, skipN, failN)
	return nil
}

func dump(v any, err ...error) {
	if len(err) > 0 && err[0] != nil {
		fatal(err[0])
	}
	b, _ := json.MarshalIndent(v, "", "  ")
	fmt.Println(string(b))
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "error:", err)
	os.Exit(1)
}
