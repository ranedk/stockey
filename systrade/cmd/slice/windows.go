package main

import (
	"flag"
	"fmt"
	"math"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/ranedk/systrader/internal/backtest"
	"github.com/ranedk/systrader/internal/bars"
	"github.com/ranedk/systrader/internal/evidence"
	"github.com/ranedk/systrader/internal/research"
	"github.com/ranedk/systrader/internal/sleeve"
)

// Cross-sectional momentum across time windows, exactly as pre-registered in
// research/preregistrations/2026-09-11_momentum_windows.md: six formation
// windows from the literature × five holding periods, each hold run as
// staggered books (Jegadeesh-Titman), on the exploration years only.
//
// The question is whether momentum is a PLATEAU across windows or a spike at
// one, never which window is best — picking the best cell is Law 5's
// pick-the-winner. The verdict rule was written down before any result.

const month = 21 // trading days

// windowSignal is a trailing total return over t−From → t−To (trading days),
// times Sign: +1 buys recent winners, −1 buys recent losers.
type windowSignal struct {
	Name     string
	From, To int
	Sign     float64
}

var momentumWindows = []windowSignal{
	{"mom3", 3 * month, 0, 1},
	{"mom6", 6 * month, 0, 1},
	{"mom9", 9 * month, 0, 1},
	{"mom12", 12 * month, 0, 1},
	{"mom12_1", 12 * month, 1 * month, 1},
	{"mom12_7", 12 * month, 7 * month, 1},
}

// The known-sign controls: both reversals should buy losers profitably.
var reversalWindows = []windowSignal{
	{"rev1", 1 * month, 0, -1},
	{"rev36", 36 * month, 12 * month, -1},
}

var holdMonths = []int{1, 2, 3, 6, 12}

type windowCell struct {
	sig        windowSignal
	k          int
	signal, eq sleeve.Book
	sh         sleeve.Book
	phases     int
}

func runWindows(args []string) {
	fs := flag.NewFlagSet("windows", flag.ExitOnError)
	cache := fs.String("cache", defaultCache, "bar cache file")
	from := fs.String("from", "2013-07-01", "first decision date")
	to := fs.String("to", "2021-12-31", "last decision date — the confirmation years stay unread")
	floor := fs.Float64("floor", 1e8, "liquidity floor, 60-bar median traded value (Rs 10cr, the tradable universe)")
	costBps := fs.Float64("cost-bps", 50, "round-trip cost in basis points (100 bps is reported alongside)")
	reps := fs.Int("reps", 10000, "bootstrap resamples")
	seed := fs.Int64("seed", 1, "bootstrap seed — declared, never drawn")
	q := fs.Float64("fdr", 0.10, "false-discovery rate within each family")
	readConfirm := fs.Bool("include-confirmation-years", false, "let -to reach 2022 onward")
	ledger := fs.String("ledger", "research/LEDGER.md", "ledger, for the workspace-wide bar printed as context")
	fatalIf(fs.Parse(args))
	guardConfirmationYears(mustDate(*to), *readConfirm)

	sigs := append(append([]windowSignal(nil), momentumWindows...), reversalWindows...)
	names := make([]string, len(sigs))
	for i, s := range sigs {
		names[i] = s.Name
	}
	days := buildWindows(*cache, sigs, mustDate(*from), mustDate(*to), *floor)
	if len(days) == 0 {
		fatal(fmt.Errorf("no eligible days"))
	}

	books := staggeredBooks(days, names, holdMonths, *costBps)
	var cells []windowCell
	for _, k := range holdMonths {
		for _, sg := range sigs {
			b := books[sg.Name][k]
			cells = append(cells, windowCell{sig: sg, k: k, signal: b.Signal, eq: b.Equal, sh: b.Shuffle, phases: b.Phases})
		}
	}

	boot := evidence.Bootstrap{MeanBlock: 5, Reps: *reps, Seed: *seed}
	type scored struct {
		windowCell
		sum        sleeve.Summary
		eEq, eSh   evidence.Edge
		eq2, sh2   float64
		deciding   string
		excess     []float64
		p          float64
		family     string
		q, dsr     float64
		survivor   bool
		monthsUsed int
	}
	var all []*scored
	for _, c := range cells {
		_, sig := sleeve.Monthly(c.signal)
		_, eq := sleeve.Monthly(c.eq)
		_, sh := sleeve.Monthly(c.sh)
		if len(sig) != len(eq) || len(sig) != len(sh) || len(sig) < 12 {
			fatal(fmt.Errorf("%s held %d: books misaligned or too short (%d/%d/%d months)", c.sig.Name, c.k, len(sig), len(eq), len(sh)))
		}
		eEq, err := evidence.PairedEdge(sig, eq, boot, 0.90)
		fatalIf(err)
		eSh, err := evidence.PairedEdge(sig, sh, boot, 0.90)
		fatalIf(err)
		ctl, e, deciding := eq, eEq, "equal-wt"
		if eSh.P > eEq.P {
			ctl, e, deciding = sh, eSh, "shuffle"
		}
		ex := make([]float64, len(sig))
		for i := range sig {
			ex[i] = sig[i] - ctl[i]
		}
		fam := "momentum"
		if c.sig.Sign < 0 {
			fam = "reversal"
		}
		all = append(all, &scored{
			windowCell: c, sum: sleeve.Summarize(c.signal), eEq: eEq, eSh: eSh,
			eq2:      sleeve.PairedMonthly(doubleCost(c.signal), doubleCost(c.eq)).MeanDiff,
			sh2:      sleeve.PairedMonthly(doubleCost(c.signal), doubleCost(c.sh)).MeanDiff,
			deciding: deciding, excess: ex, p: e.P, family: fam, monthsUsed: len(sig),
		})
	}
	for _, fam := range []string{"momentum", "reversal"} {
		var members []evidence.Member
		var idx []*scored
		for _, s := range all {
			if s.family == fam {
				members = append(members, evidence.Member{Name: s.sig.Name, Returns: s.excess, P: s.p})
				idx = append(idx, s)
			}
		}
		verdicts, err := evidence.Judge(members, len(members), *q)
		fatalIf(err)
		for i, v := range verdicts {
			s := idx[i]
			s.q, s.dsr = v.Q, v.Deflated.DSR
			s.survivor = v.Discovery && s.eEq.MeanDiff > 0 && s.eSh.MeanDiff > 0
		}
	}
	lookup := map[string]*scored{}
	for _, s := range all {
		lookup[fmt.Sprintf("%s/%d", s.sig.Name, s.k)] = s
	}

	fmt.Println("MOMENTUM ACROSS TIME WINDOWS — pre-registered, research/preregistrations/2026-09-11_momentum_windows.md")
	fmt.Printf("NSE adjusted EQ, Rs %.0f cr floor, top quintile, equal weight, fills at the next open, %s..%s\n", *floor/1e7, *from, *to)
	fmt.Printf("%.0f bps round trip (100 bps shown separately); a K-month hold = K staggered books a month apart, averaged\n", *costBps)
	fmt.Println("Exploration years only: this can kill a window family, never confirm one.")
	fmt.Println()
	grid := func(title string, sigs []windowSignal, cell func(*scored) string) {
		fmt.Println(title)
		fmt.Printf("  %-9s", "")
		for _, k := range holdMonths {
			fmt.Printf(" %12s", fmt.Sprintf("hold %dm", k))
		}
		fmt.Println()
		for _, sg := range sigs {
			fmt.Printf("  %-9s", sg.Name)
			for _, k := range holdMonths {
				fmt.Printf(" %12s", cell(lookup[fmt.Sprintf("%s/%d", sg.Name, k)]))
			}
			fmt.Println()
		}
		fmt.Println()
	}
	grid("net annual return (the book itself)", momentumWindows, func(s *scored) string { return fmt.Sprintf("%.1f%%", 100*s.sum.AnnReturn) })
	grid("edge over the TOUGHER control, %/month at 50 bps  (✓ = beats both controls and survives FDR q=0.10)", momentumWindows, func(s *scored) string {
		m := " "
		if s.survivor {
			m = "✓"
		}
		e := s.eEq.MeanDiff
		if s.deciding == "shuffle" {
			e = s.eSh.MeanDiff
		}
		return fmt.Sprintf("%+.2f%s", 100*e, m)
	})
	grid("same edge at 100 bps (worse of the two controls)", momentumWindows, func(s *scored) string {
		return fmt.Sprintf("%+.2f", 100*math.Min(s.eq2, s.sh2))
	})
	grid("deflated Sharpe on the excess, 30 trials (0.95 is the customary bar)", momentumWindows, func(s *scored) string { return fmt.Sprintf("%.3f", s.dsr) })
	grid("one-way turnover per day", momentumWindows, func(s *scored) string { return fmt.Sprintf("%.2f%%", 100*s.sum.MeanTurnover) })

	// The pre-registered verdict.
	survivors := 0
	rows, cols := map[string]bool{}, map[int]bool{}
	for _, s := range all {
		if s.family == "momentum" && s.survivor {
			survivors++
			rows[s.sig.Name] = true
			cols[s.k] = true
		}
	}
	verdict := "IN BETWEEN"
	switch {
	case survivors >= 20 && len(rows) >= 2 && len(cols) >= 2:
		verdict = "PLATEAU"
	case survivors < 10 || len(rows) <= 1 || len(cols) <= 1:
		verdict = "SPIKE"
	}
	fmt.Printf("PRE-REGISTERED VERDICT: %d of 30 momentum cells beat both controls and survive FDR, spanning %d of 6 formation windows and %d of 5 holding periods -> %s\n",
		survivors, len(rows), len(cols), verdict)
	fmt.Println("(plateau: >= 20 survivors not confined to one row or column; spike: < 10, or all in one row or column)")
	fmt.Println()

	grid("KNOWN-SIGN CONTROLS (their own family of 10): edge over the tougher control, %/month  (✓ = survives)", reversalWindows, func(s *scored) string {
		m := " "
		if s.survivor {
			m = "✓"
		}
		e := s.eEq.MeanDiff
		if s.deciding == "shuffle" {
			e = s.eSh.MeanDiff
		}
		return fmt.Sprintf("%+.2f%s", 100*e, m)
	})
	fmt.Println("Both reversals buy LOSERS; the literature says both should earn a positive edge.")
	eqRef := lookup["mom12_1/1"]
	es := sleeve.Summarize(eqRef.eq)
	fmt.Printf("\nReference: the equal-weight universe (held 1m) %.1f%%/yr at %.1f%% vol over %d months.\n", 100*es.AnnReturn, 100*es.AnnVol, eqRef.monthsUsed)
	if m, err := research.CountM(*ledger); err == nil {
		fmt.Printf("Workspace-wide Bonferroni bar, context only (amended Law 2): t = %.2f at M = %d.\n", backtest.BonferroniBar(m), m)
	}
	var list []string
	for _, s := range all {
		if s.family == "momentum" && s.survivor {
			list = append(list, fmt.Sprintf("%s/%dm", s.sig.Name, s.k))
		}
	}
	sort.Strings(list)
	fmt.Printf("Surviving cells: %s\n", strings.Join(list, ", "))
}

// scorer turns one symbol's bars into a score on every bar, NaN where it is
// undefined; a book holds each day's top quintile by it.
type scorer struct {
	Name  string
	Score func(b []bars.Bar) []float64
}

// windowScorer is a trailing-return signal as a scorer.
func windowScorer(s windowSignal) scorer {
	return scorer{Name: s.Name, Score: func(b []bars.Bar) []float64 {
		out := make([]float64, len(b))
		for i := range out {
			out[i] = math.NaN()
			a, z := i-s.From, i-s.To
			if a < 0 || b[a].Close <= 0 || b[z].Close <= 0 {
				continue
			}
			out[i] = s.Sign * (b[z].Close/b[a].Close - 1)
		}
		return out
	}}
}

// buildWindows builds the trailing-return grid's days (LEDGER row 30).
func buildWindows(cache string, sigs []windowSignal, from, to time.Time, floor float64) []sleeve.Day {
	scs := make([]scorer, len(sigs))
	for i, s := range sigs {
		scs[i] = windowScorer(s)
	}
	return buildScores(cache, scs, from, to, floor, month+3)
}

// buildScores computes every scorer for every eligible name on every decision
// day and marks, per scorer, the day's top quintile (U = 1) against the rest
// of the eligible names it can score (U = 0). A scorer that cannot score a
// name yet leaves it NaN, so each book starts when its data does.
func buildScores(cache string, scs []scorer, from, to time.Time, floor float64, minBars int) []sleeve.Day {
	type obs struct {
		sym int32
		sig []float64
		ret float64
	}
	byDate := map[time.Time][]obs{}
	ids := map[string]int32{}
	var mu sync.Mutex
	fatalIf(bars.ScanParallel(cache, 0, func(ser bars.Series) {
		b := ser.Bars
		n := len(b)
		if n < minBars {
			return
		}
		turn := bars.MedianTurnover(b, turnoverWin)
		arrays := make([][]float64, len(scs))
		for j, sc := range scs {
			arrays[j] = sc.Score(b)
		}
		type row struct {
			d time.Time
			o obs
		}
		var local []row
		for i := 0; i < n-2; i++ {
			d := b[i].Date
			if d.Before(from) || d.After(to) || math.IsNaN(turn[i]) || turn[i] < floor {
				continue
			}
			entry, exit := b[i+1].Open, b[i+2].Open
			if entry <= 0 || exit <= 0 || b[i].Close <= 0 {
				continue
			}
			vals := make([]float64, len(scs))
			any := false
			for j := range scs {
				vals[j] = arrays[j][i]
				if !math.IsNaN(vals[j]) {
					any = true
				}
			}
			if !any {
				continue
			}
			local = append(local, row{d, obs{sig: vals, ret: exit/entry - 1}})
		}
		if len(local) == 0 {
			return
		}
		mu.Lock()
		id, ok := ids[ser.Symbol]
		if !ok {
			id = int32(len(ids) + 1)
			ids[ser.Symbol] = id
		}
		for _, r := range local {
			o := r.o
			o.sym = id
			byDate[r.d] = append(byDate[r.d], o)
		}
		mu.Unlock()
	}))

	remap := stableIDs(ids)
	days := make([]sleeve.Day, 0, len(byDate))
	for d, os := range byDate {
		for i := range os {
			os[i].sym = remap[os[i].sym]
		}
		day := sleeve.Day{Date: d, Obs: make([]sleeve.Obs, len(os))}
		for i, o := range os {
			day.Obs[i] = sleeve.Obs{Sym: o.sym, U: make([]float64, len(scs)), Ret: o.ret}
		}
		for j := range scs {
			var have []int
			for i, o := range os {
				if math.IsNaN(o.sig[j]) {
					day.Obs[i].U[j] = math.NaN()
				} else {
					have = append(have, i)
				}
			}
			if len(have) < 25 { // fewer than five names a quintile: no book today
				for _, i := range have {
					day.Obs[i].U[j] = math.NaN()
				}
				continue
			}
			sort.Slice(have, func(a, c int) bool { return os[have[a]].sig[j] > os[have[c]].sig[j] })
			top := len(have) / 5
			for r, i := range have {
				day.Obs[i].U[j] = boolTo(r < top)
			}
		}
		days = append(days, day)
	}
	sort.Slice(days, func(i, j int) bool { return days[i].Date.Before(days[j].Date) })
	return days
}

// cellBooks is one cell's three staggered books.
type cellBooks struct {
	Signal, Equal, Shuffle sleeve.Book
	Phases                 int
}

// staggeredBooks runs every named book at every holding period as K staggered
// books a month apart (Jegadeesh-Titman), each trimmed to the first day it held
// anything, and averages them on their common dates.
func staggeredBooks(days []sleeve.Day, names []string, holds []int, costBps float64) map[string]map[int]cellBooks {
	out := map[string]map[int]cellBooks{}
	for _, n := range names {
		out[n] = map[int]cellBooks{}
	}
	for _, k := range holds {
		var phases [][]sleeve.RuleResult
		for p := 0; p < k && p*month < len(days); p++ {
			res, err := sleeve.Run(days[p*month:], names, sleeve.Config{
				CostBpsRoundTrip: costBps,
				Seeds:            []int64{1, 2, 3, 4, 5},
				RebalanceEvery:   k * month,
			})
			fatalIf(err)
			phases = append(phases, res)
		}
		for j, n := range names {
			var sb, eb, hb []sleeve.Book
			for _, ph := range phases {
				start := firstTraded(ph[j].Signal)
				sb = append(sb, trimFrom(ph[j].Signal, start))
				eb = append(eb, trimFrom(ph[j].EqualWeight, start))
				hb = append(hb, trimFrom(ph[j].StableShuffled, start))
			}
			out[n][k] = cellBooks{Signal: stagger(sb), Equal: stagger(eb), Shuffle: stagger(hb), Phases: len(phases)}
		}
		fmt.Fprintf(os.Stderr, "held %2d months: %d staggered books run\n", k, len(phases))
	}
	return out
}

// firstTraded is the first date a book held anything; before it the signal
// had no history and the book sat in cash.
func firstTraded(b sleeve.Book) time.Time {
	for i, t := range b.Turnover {
		if t > 0 {
			return b.Dates[i]
		}
	}
	return time.Time{}
}

func trimFrom(b sleeve.Book, start time.Time) sleeve.Book {
	i := sort.Search(len(b.Dates), func(i int) bool { return !b.Dates[i].Before(start) })
	return sleeve.Book{Name: b.Name, Dates: b.Dates[i:], Gross: b.Gross[i:], Net: b.Net[i:], Turnover: b.Turnover[i:]}
}

// stagger averages books that differ only in their rebalance phase, on the
// dates all of them cover.
func stagger(bs []sleeve.Book) sleeve.Book {
	type acc struct{ g, n, t float64 }
	sums := map[time.Time]*acc{}
	count := map[time.Time]int{}
	for _, b := range bs {
		for i, d := range b.Dates {
			a := sums[d]
			if a == nil {
				a = &acc{}
				sums[d] = a
			}
			a.g += b.Gross[i]
			a.n += b.Net[i]
			a.t += b.Turnover[i]
			count[d]++
		}
	}
	var dates []time.Time
	for d, c := range count {
		if c == len(bs) {
			dates = append(dates, d)
		}
	}
	sort.Slice(dates, func(i, j int) bool { return dates[i].Before(dates[j]) })
	out := sleeve.Book{Name: bs[0].Name, Dates: dates}
	k := float64(len(bs))
	for _, d := range dates {
		a := sums[d]
		out.Gross = append(out.Gross, a.g/k)
		out.Net = append(out.Net, a.n/k)
		out.Turnover = append(out.Turnover, a.t/k)
	}
	return out
}

// doubleCost re-prices a book at twice its costs: net = gross − 2·(gross − net).
func doubleCost(b sleeve.Book) sleeve.Book {
	out := sleeve.Book{Name: b.Name, Dates: b.Dates, Gross: b.Gross, Turnover: b.Turnover, Net: make([]float64, len(b.Net))}
	for i := range b.Net {
		out.Net[i] = b.Gross[i] - 2*(b.Gross[i]-b.Net[i])
	}
	return out
}

// stableIDs renumbers symbols by name. The bar cache is scanned in parallel,
// so ids handed out in scan order change from run to run — and the
// stable-shuffle control keys each symbol's draw on its id, so the control
// itself changed between identical runs (the lookback screen's stable-shuffle
// book read 8.35%/yr, then 10.64%, on the same data). Every builder feeding
// internal/sleeve renumbers through here.
func stableIDs(ids map[string]int32) map[int32]int32 {
	names := make([]string, 0, len(ids))
	for n := range ids {
		names = append(names, n)
	}
	sort.Strings(names)
	remap := make(map[int32]int32, len(ids))
	for i, n := range names {
		remap[ids[n]] = int32(i + 1)
	}
	return remap
}
