package main

// `slice stage`: Stan Weinstein's stage analysis on NSE, pre-registered in
// research/preregistrations/2026-09-15_weinstein.md (LEDGER rows 37-38).
//
//	slice stage -part buckets    step 1: each stage's stocks against a
//	                             same-size random group (kill screen)
//	slice stage -part strategy   step 2: three trading versions, and their
//	                             overlap with the momentum lookback blend
//	slice stage -part rotation   industry rotation (row 53, 2026-10-01 pre-registration)
//	slice stage -part followups  the two follow-ups from row 39's map: early
//	                             Stage 2 and a faithful breakout (row 40)
//	slice stage -part explore -event stage2|breakout
//	                             EXPLORATION: where did it work? The signal cut
//	                             by every slicing trait plus four of Weinstein's
//	                             own (LEDGER row 39) — a map, never a verdict

import (
	"context"
	"flag"
	"fmt"
	"math"
	"sort"
	"time"

	"github.com/ranedk/systrader/internal/bars"
	"github.com/ranedk/systrader/internal/core"
	"github.com/ranedk/systrader/internal/evidence"
	"github.com/ranedk/systrader/internal/explore"
	"github.com/ranedk/systrader/internal/rotation"
	"github.com/ranedk/systrader/internal/sleeve"
	"github.com/ranedk/systrader/internal/stage"
	"github.com/ranedk/systrader/internal/store"
	"github.com/ranedk/systrader/internal/traits"
)

// Weinstein's breakout confirmation (1988), fixed in the pre-registration.
const (
	breakoutVolume = 2.0 // breakout-week volume at least twice its 10-week average
	mansfieldWeeks = 52  // relative strength against its own 52-week average
	weinsteinWeek  = 5   // trading days between rebalances: Weinstein reviews weekly
)

// weeks is one symbol's weekly view: the daily index of each week's last bar
// and that week's stage reading.
type weeks struct {
	end []int
	cls []stage.Classification
}

// weeklyView resamples daily bars to ISO weeks — last close, summed volume —
// and classifies them.
func weeklyView(b []bars.Bar) weeks {
	var w weeks
	var times []time.Time
	var closes, vols []float64
	var vol float64
	for i := range b {
		vol += b[i].Vol
		if i+1 < len(b) {
			y, k := b[i].Date.ISOWeek()
			y2, k2 := b[i+1].Date.ISOWeek()
			if y == y2 && k == k2 {
				continue
			}
		}
		w.end = append(w.end, i)
		times = append(times, b[i].Date)
		closes = append(closes, b[i].Close)
		vols = append(vols, vol)
		vol = 0
	}
	if len(times) == 0 {
		return w
	}
	v := core.New(times, vols)
	w.cls = stage.Classify(core.New(times, closes), &v)
	return w
}

// daily spreads weekly values onto the daily bars: each day carries the
// value of the last week COMPLETED on or before it, so a decision at a close
// never sees a later close.
func (w weeks) daily(n int, val []float64) []float64 {
	out := make([]float64, n)
	k := -1
	for i := 0; i < n; i++ {
		for k+1 < len(w.end) && w.end[k+1] <= i {
			k++
		}
		if k < 0 {
			out[i] = math.NaN()
			continue
		}
		out[i] = val[k]
	}
	return out
}

// stageMembership is 1 in the weeks a name is in stage s, 0 in other known
// stages, NaN while its stage is unknown (warm-up).
func stageMembership(cls []stage.Classification, s stage.Stage) []float64 {
	out := make([]float64, len(cls))
	for k, c := range cls {
		switch {
		case c.Stage == stage.StageUnknown:
			out[k] = math.NaN()
		case c.Stage == s:
			out[k] = 1
		}
	}
	return out
}

// mansfield is Mansfield relative strength per week: the stock's price over
// the market's, against that ratio's own 52-week average, minus one. NaN until
// 52 weeks of the ratio exist.
func mansfield(cls []stage.Classification, level func(time.Time) float64) []float64 {
	n := len(cls)
	ratio := make([]float64, n)
	for k, c := range cls {
		ratio[k] = math.NaN()
		if m := level(c.Time); m > 0 && c.Close > 0 {
			ratio[k] = c.Close / m
		}
	}
	out := make([]float64, n)
	for k := range cls {
		out[k] = math.NaN()
		if k+1 < mansfieldWeeks || math.IsNaN(ratio[k]) {
			continue
		}
		var sum float64
		ok := true
		for j := k + 1 - mansfieldWeeks; j <= k; j++ {
			if math.IsNaN(ratio[j]) {
				ok = false
				break
			}
			sum += ratio[j]
		}
		if ok {
			out[k] = ratio[k]/(sum/mansfieldWeeks) - 1
		}
	}
	return out
}

// holdPositions walks the weeks: enter where enter says, exit on a weekly
// close below the 30-week average. 1 held, 0 not, NaN while the stage is
// unknown and nothing is held.
func holdPositions(cls []stage.Classification, enter func(k int) bool) []float64 {
	out := make([]float64, len(cls))
	held := false
	for k, c := range cls {
		if held && c.Close < c.MA30 {
			held = false
		}
		if !held && c.Stage != stage.StageUnknown && enter(k) {
			held = true
		}
		switch {
		case held:
			out[k] = 1
		case c.Stage == stage.StageUnknown:
			out[k] = math.NaN()
		}
	}
	return out
}

// enterStage2 is version (a): any Stage 2 week.
func enterStage2(cls []stage.Classification) func(int) bool {
	return func(k int) bool { return cls[k].Stage == stage.Stage2Advancing }
}

// enterBreakout is versions (b) and (c): the week a name turns Stage 1 ->
// Stage 2 on at least twice its average volume with positive Mansfield
// relative strength — and, when marketOK is set, while the market allows it.
func enterBreakout(cls []stage.Classification, rs []float64, marketOK func(time.Time) bool) func(int) bool {
	return func(k int) bool {
		return k > 0 && cls[k].Stage == stage.Stage2Advancing && cls[k-1].Stage == stage.Stage1Basing &&
			cls[k].VolumeRatio >= breakoutVolume && rs[k] > 0 &&
			(marketOK == nil || marketOK(cls[k].Time))
	}
}

// market is the equal-weight index of the eligible universe, as a level and
// as its own weekly stage.
type market struct {
	dates   []time.Time
	level   []float64
	wkEnd   []time.Time
	wkStage []stage.Stage
}

func newMarket(ret map[time.Time]float64) *market {
	m := &market{}
	for d := range ret {
		m.dates = append(m.dates, d)
	}
	sort.Slice(m.dates, func(i, j int) bool { return m.dates[i].Before(m.dates[j]) })
	lvl := 100.0
	for _, d := range m.dates {
		lvl *= 1 + ret[d]
		m.level = append(m.level, lvl)
	}
	if len(m.dates) == 0 {
		return m
	}
	wk := core.ResampleWeeklyLast(core.New(m.dates, m.level))
	for _, c := range stage.Classify(wk, nil) {
		m.wkEnd = append(m.wkEnd, c.Time)
		m.wkStage = append(m.wkStage, c.Stage)
	}
	return m
}

// levelAt is the index level at the last market date on or before t.
func (m *market) levelAt(t time.Time) float64 {
	i := sort.Search(len(m.dates), func(i int) bool { return m.dates[i].After(t) }) - 1
	if i < 0 {
		return math.NaN()
	}
	return m.level[i]
}

// stageAt is the market's stage as of its last week completed on or before t.
func (m *market) stageAt(t time.Time) stage.Stage {
	i := sort.Search(len(m.wkEnd), func(i int) bool { return m.wkEnd[i].After(t) }) - 1
	if i < 0 {
		return stage.StageUnknown
	}
	return m.wkStage[i]
}

func stageScorers() []scorer {
	var out []scorer
	for _, s := range []stage.Stage{stage.Stage1Basing, stage.Stage2Advancing, stage.Stage3Topping, stage.Stage4Declining} {
		s := s
		out = append(out, scorer{Name: fmt.Sprintf("stage%d", int(s)), Direct: true, Score: func(b []bars.Bar) []float64 {
			w := weeklyView(b)
			return w.daily(len(b), stageMembership(w.cls, s))
		}})
	}
	return out
}

func weinsteinScorers(m *market) []scorer {
	version := func(name string, rule func(w weeks) func(int) bool) scorer {
		return scorer{Name: name, Direct: true, Score: func(b []bars.Bar) []float64 {
			w := weeklyView(b)
			return w.daily(len(b), holdPositions(w.cls, rule(w)))
		}}
	}
	return []scorer{
		version("a-hold-stage2", func(w weeks) func(int) bool { return enterStage2(w.cls) }),
		version("b-breakout", func(w weeks) func(int) bool {
			return enterBreakout(w.cls, mansfield(w.cls, m.levelAt), nil)
		}),
		version("c-breakout-mkt", func(w weeks) func(int) bool {
			return enterBreakout(w.cls, mansfield(w.cls, m.levelAt), func(t time.Time) bool {
				return m.stageAt(t) == stage.Stage2Advancing
			})
		}),
	}
}

func runStage(args []string) {
	fs := flag.NewFlagSet("stage", flag.ExitOnError)
	cache := fs.String("cache", defaultCache, "bar cache file")
	part := fs.String("part", "buckets", "buckets (step 1) | strategy (step 2)")
	from := fs.String("from", "2013-07-01", "first decision date")
	to := fs.String("to", "2021-12-31", "last decision date — the confirmation years stay unread")
	floor := fs.Float64("floor", 1e8, "liquidity floor (Rs 10cr, the tradable universe)")
	costBps := fs.Float64("cost-bps", 50, "round-trip cost in basis points (100 bps is reported alongside)")
	reps := fs.Int("reps", 10000, "bootstrap resamples")
	seed := fs.Int64("seed", 1, "bootstrap seed — declared, never drawn")
	q := fs.Float64("fdr", 0.10, "false-discovery rate within the family")
	readConfirm := fs.Bool("include-confirmation-years", false, "let -to reach 2022 onward")
	noCap := fs.Bool("rotation-no-cap", false, "for -part rotation: the pre-registered sensitivity without the +-50% weekly return cap")
	event := fs.String("event", "stage2", "for -part explore: stage2 (every Stage 2 name) | breakout (the Stage 1->2 breakout day)")
	horizon := fs.Int("horizon", 20, "for -part explore: forward holding period in trading days")
	quantiles := fs.Int("quantiles", 5, "for -part explore: buckets per continuous dimension")
	vsReps := fs.Int("vs-reps", 1000, "for -part explore: block-bootstrap resamples for the 'vs ctl' score")
	fatalIf(fs.Parse(args))
	guardConfirmationYears(mustDate(*to), *readConfirm)
	if *part == "explore" {
		runStageExplore(*cache, *event, mustDate(*from), mustDate(*to), *floor, *horizon, *quantiles, *costBps, *vsReps)
		return
	}
	boot := evidence.Bootstrap{MeanBlock: 5, Reps: *reps, Seed: *seed}
	minBars := (stage.MAWindowWeeks + stage.SlopeWindowWeeks) * 5
	cfg := sleeve.Config{CostBpsRoundTrip: *costBps, Seeds: []int64{1, 2, 3, 4, 5}, RebalanceEvery: weinsteinWeek}

	header := func(title string) {
		fmt.Println(title)
		fmt.Printf("NSE adjusted EQ, Rs %.0f cr floor, %s..%s, weekly stages (a day uses its last COMPLETED week),\n", *floor/1e7, *from, *to)
		fmt.Printf("equal weight, decide at a close, fill at the next open, rebalanced every %d trading days, %.0f bps round trip.\n", weinsteinWeek, *costBps)
		fmt.Println("Controls: the equal-weight universe, and the same number of names at random from the same eligible set, held")
		fmt.Println("(stable shuffle). Exploration years: can kill, never confirm.")
		fmt.Println()
	}

	switch *part {
	case "buckets":
		scs := stageScorers()
		names := scorerNames(scs)
		days := buildScores(*cache, scs, mustDate(*from), mustDate(*to), *floor, minBars)
		res, err := sleeve.Run(days, names, cfg)
		fatalIf(err)
		header("WEINSTEIN STAGES — STEP 1: do the stages sort future returns? — pre-registered, research/preregistrations/2026-09-15_weinstein.md")
		fmt.Printf("  %-8s %26s %26s %13s %13s %11s\n", "", "the book: ret / vol / maxDD", "same-size random group", "edge vs it", "edge vs EW", "at 100 bps")
		fmt.Printf("  %-8s %26s %26s %13s %13s %11s\n", "", "", "", "%/mo (p)", "%/mo (p)", "vs random")
		var s2 evidence.Edge
		for k, r := range res {
			start := firstTraded(r.Signal)
			sb, eb, hb := trimFrom(r.Signal, start), trimFrom(r.EqualWeight, start), trimFrom(r.StableShuffled, start)
			_, ms := sleeve.Monthly(sb)
			_, me := sleeve.Monthly(eb)
			_, mh := sleeve.Monthly(hb)
			vsH, err := evidence.PairedEdge(ms, mh, boot, 0.90)
			fatalIf(err)
			vsE, err := evidence.PairedEdge(ms, me, boot, 0.90)
			fatalIf(err)
			d2 := sleeve.PairedMonthly(doubleCost(sb), doubleCost(hb)).MeanDiff
			a, h := sleeve.Summarize(sb), sleeve.Summarize(hb)
			fmt.Printf("  %-8s %26s %26s %+7.2f (%.3f) %+7.2f (%.3f) %+10.2f\n", names[k],
				fmt.Sprintf("%.1f%% / %.1f%% / %.0f%%", 100*a.AnnReturn, 100*a.AnnVol, 100*a.MaxDD),
				fmt.Sprintf("%.1f%% / %.1f%% / %.0f%%", 100*h.AnnReturn, 100*h.AnnVol, 100*h.MaxDD),
				100*vsH.MeanDiff, vsH.P, 100*vsE.MeanDiff, vsE.P, 100*d2)
			if names[k] == "stage2" {
				s2 = vsH
			}
		}
		e := sleeve.Summarize(trimFrom(res[1].EqualWeight, firstTraded(res[1].Signal)))
		fmt.Printf("\n  equal-weight universe: %.1f%% / %.1f%% / %.0f%%\n\n", 100*e.AnnReturn, 100*e.AnnVol, 100*e.MaxDD)
		verdict := "STOP — the Stage 2 label does not beat a same-size random group; step 2 is not run"
		if s2.MeanDiff > 0 && s2.P < 0.10 {
			verdict = "PROCEED to step 2"
		}
		fmt.Printf("PRE-REGISTERED VERDICT: Stage 2 vs its same-size random group %+.2f%%/month, p = %.3f -> %s\n",
			100*s2.MeanDiff, s2.P, verdict)
		fmt.Println("(proceed only if positive with p < 0.10 at 50 bps)")

	case "strategy":
		mret, _, err := traits.MarketReturns(*cache, *floor)
		fatalIf(err)
		m := newMarket(mret)
		ws := weinsteinScorers(m)
		scs := append(append([]scorer(nil), ws...), memberScorers(momNames, nil)...)
		names := scorerNames(scs)
		days := buildScores(*cache, scs, mustDate(*from), mustDate(*to), *floor, minBars)
		res, err := sleeve.Run(days, names, cfg)
		fatalIf(err)
		idx := make([]int, len(ws))
		for k := range idx {
			idx[k] = k
		}
		vs := judgeVersions(res, idx, momentumExcess(res, len(ws)), boot, *q)

		header("WEINSTEIN STAGES — STEP 2: three trading versions — pre-registered, research/preregistrations/2026-09-15_weinstein.md")
		fmt.Println("(a) hold Stage 2 | (b) Stage 1->2 breakouts on >= 2x volume with Mansfield RS > 0 | (c) (b) only while the")
		fmt.Println("market index is in Stage 2. All exit on a weekly close below the 30-week average.")
		fmt.Println()
		printVersions(vs)

	case "followups":
		mret, _, err := traits.MarketReturns(*cache, *floor)
		fatalIf(err)
		m := newMarket(mret)
		scs := append(followupScorers(m), memberScorers(momNames, nil)...)
		days := buildScores(*cache, scs, mustDate(*from), mustDate(*to), *floor, minBars)
		fdays, entries, meanHeld := followupDays(days)
		names := append([]string{"stage2", "d-early-stage2", "e-faithful-breakout"}, momNames...)
		res, err := sleeve.Run(fdays, names, cfg)
		fatalIf(err)
		vs := judgeVersions(res, []int{1, 2}, momentumExcess(res, 3), boot, *q)

		// (d)'s own question: did dropping the extended names improve on
		// plain Stage 2, on the same universe and clock?
		start := firstTraded(res[1].Signal)
		_, md := sleeve.Monthly(trimFrom(res[1].Signal, start))
		_, mp := sleeve.Monthly(trimFrom(res[0].Signal, start))
		if len(md) != len(mp) {
			fatal(fmt.Errorf("early and plain Stage 2 books misaligned"))
		}
		better, err := evidence.PairedEdge(md, mp, boot, 0.90)
		fatalIf(err)
		plain := sleeve.Summarize(trimFrom(res[0].Signal, start))

		header("WEINSTEIN FOLLOW-UPS — pre-registered, research/preregistrations/2026-09-15_weinstein_followups.md")
		fmt.Println("(d) early Stage 2: every Stage 2 name except, each day, the top fifth of them by 30-week MA slope or by")
		fmt.Println("    distance above the MA. (e) faithful breakout: a weekly close above the prior 30 weeks' highest close,")
		fmt.Println("    out of a flat-MA base, MA not falling, above the MA, >= 2x volume, Mansfield RS > 0; exit on a weekly")
		fmt.Println("    close below the 30-week MA.")
		fmt.Println()
		printVersions(vs)
		fmt.Printf("\nplain Stage 2, same universe and clock: %.1f%% / %.1f%% / %.0f%%\n",
			100*plain.AnnReturn, 100*plain.AnnVol, 100*plain.MaxDD)
		fmt.Printf("(d) vs plain Stage 2, net: %+.2f%%/month, p = %.3f (pre-registered: must be positive with p < 0.10)\n",
			100*better.MeanDiff, better.P)
		var years []int
		for y := range entries {
			years = append(years, y)
		}
		sort.Ints(years)
		fmt.Printf("(e) entries a year:")
		for _, y := range years {
			fmt.Printf(" %d: %d", y, entries[y])
		}
		fmt.Printf(" | mean names held %.1f\n\n", meanHeld)

		d, e := vs[0], vs[1]
		dVerdict := "does not survive"
		switch {
		case d.survivor && better.MeanDiff > 0 && better.P < 0.10:
			dVerdict = "SURVIVES and improves on plain Stage 2"
		case d.survivor:
			dVerdict = "survives, but does not improve on plain Stage 2 — the exclusion is not what works"
		}
		eVerdict := "does not survive"
		if e.survivor {
			eVerdict = "SURVIVES"
		}
		fmt.Printf("PRE-REGISTERED VERDICT: (d) %s; (e) %s.\n", dVerdict, eVerdict)

	case "rotation":
		if *noCap {
			rotation.WeeklyReturnCap = math.Inf(1)
			fmt.Println("SENSITIVITY: no weekly return cap on index members (reported, not deciding)")
		}
		runRotationPart(*cache, mustDate(*from), mustDate(*to), *floor, *costBps, boot, *q, cfg, minBars)

	default:
		fatal(fmt.Errorf("unknown -part %q (buckets | strategy | followups | rotation | explore)", *part))
	}
}

func scorerNames(scs []scorer) []string {
	out := make([]string, len(scs))
	for i, s := range scs {
		out[i] = s.Name
	}
	return out
}

func pearson(a, b []float64) float64 {
	n := float64(len(a))
	if len(a) < 3 || len(a) != len(b) {
		return math.NaN()
	}
	var sa, sb float64
	for i := range a {
		sa += a[i]
		sb += b[i]
	}
	ma, mb := sa/n, sb/n
	var cov, va, vb float64
	for i := range a {
		cov += (a[i] - ma) * (b[i] - mb)
		va += (a[i] - ma) * (a[i] - ma)
		vb += (b[i] - mb) * (b[i] - mb)
	}
	if va == 0 || vb == 0 {
		return math.NaN()
	}
	return cov / math.Sqrt(va*vb)
}

// Weinstein's own attributes, carried in explore.Obs.Extra for every name.
const (
	xWkVolRatio = iota // the last completed week's volume over its 10-week average
	xMansfield         // Mansfield relative strength
	xMASlope           // 30-week average's 4-week slope, %
	xAboveMA           // close over the 30-week average, minus one
)

// weinsteinEvent marks, on every daily bar, whether the name is in Stage 2
// ("stage2") or broke out of Stage 1 in the week that completed that day
// ("breakout", version (b)'s entry: >= 2x volume, Mansfield RS > 0) — and
// leaves a name out (-1) while its stage is unknown. Every value is the last
// COMPLETED week's, so nothing sees a later close.
func weinsteinEvent(kind string, m *market) eventFn {
	return func(b []bars.Bar) ([]int8, [][8]float64) {
		n := len(b)
		w := weeklyView(b)
		rs := mansfield(w.cls, m.levelAt)
		nw := len(w.cls)
		stg, vr, sl, ab := make([]float64, nw), make([]float64, nw), make([]float64, nw), make([]float64, nw)
		for k, c := range w.cls {
			stg[k], vr[k], sl[k] = float64(c.Stage), c.VolumeRatio, c.MASlopePct
			ab[k] = math.NaN()
			if c.MA30 > 0 {
				ab[k] = c.Close/c.MA30 - 1
			}
		}
		dStage, dVR, dRS, dSl, dAb := w.daily(n, stg), w.daily(n, vr), w.daily(n, rs), w.daily(n, sl), w.daily(n, ab)
		breakout := map[int]bool{}
		if kind == "breakout" {
			enter := enterBreakout(w.cls, rs, nil)
			for k := range w.cls {
				if enter(k) {
					breakout[w.end[k]] = true
				}
			}
		}
		fired := make([]int8, n)
		extra := make([][8]float64, n)
		for i := 0; i < n; i++ {
			extra[i][xWkVolRatio], extra[i][xMansfield], extra[i][xMASlope], extra[i][xAboveMA] = dVR[i], dRS[i], dSl[i], dAb[i]
			if math.IsNaN(dStage[i]) || stage.Stage(dStage[i]) == stage.StageUnknown {
				fired[i] = -1
				continue
			}
			switch {
			case kind == "stage2" && stage.Stage(dStage[i]) == stage.Stage2Advancing:
				fired[i] = 1
			case kind == "breakout" && breakout[i]:
				fired[i] = 1
			}
		}
		return fired, extra
	}
}

// runStageExplore is the slicer on a Weinstein signal: EXPLORATION, so a map
// of where it worked — every slicing trait plus Weinstein's own — and never a
// verdict (docs/RESEARCH_PROTOCOL.md, Track 1).
func runStageExplore(cache, kind string, from, to time.Time, floor float64, horizon, q int, costBps float64, reps int) {
	if kind != "stage2" && kind != "breakout" {
		fatal(fmt.Errorf("unknown -event %q (stage2 | breakout)", kind))
	}
	ctx := context.Background()
	st, err := store.Open(ctx)
	fatalIf(err)
	defer st.Close()
	sectors, err := st.SectorCodes(ctx)
	fatalIf(err)
	mcaps, err := st.MarketCaps(ctx) // names sector buckets only; not a slicing trait
	fatalIf(err)
	tc, err := traits.LoadContext(ctx, st, cache, floor, from, to)
	fatalIf(err)
	mret, _, err := traits.MarketReturns(cache, floor)
	fatalIf(err)

	days := build(cache, nil, weinsteinEvent(kind, newMarket(mret)), horizon, from, to, floor, sectors, tc)
	sort.Slice(days, func(i, j int) bool { return days[i].Date.Before(days[j].Date) })
	dims := append(universalDims(q),
		explore.QuantileDimension("weekly volume / 10-wk avg", q, func(o explore.Obs) float64 { return o.Extra[xWkVolRatio] }),
		explore.RankDimension("Mansfield RS", q, func(o explore.Obs) float64 { return o.Extra[xMansfield] }),
		explore.RankDimension("30-week MA slope", q, func(o explore.Obs) float64 { return o.Extra[xMASlope] }),
		explore.RankDimension("above the 30-week MA", q, func(o explore.Obs) float64 { return o.Extra[xAboveMA] }),
	)
	title := map[string]string{"stage2": "Weinstein Stage 2 (every name in it)", "breakout": "Weinstein breakout (Stage 1->2, >= 2x volume, Mansfield RS > 0)"}[kind]
	res := explore.RunEvent(days, dims, title, horizon)
	printResult(res, deviations(res, horizon, reps), costBps, mcaps, sectors, true)
}

// momNames and momWeights are the frozen momentum lookback blend, run beside
// the Weinstein books to measure how much they overlap.
var (
	momNames   = []string{"mom6", "mom9", "mom12", "mom12_1"}
	momWeights = []float64{0.33, 0.33, 0.17, 0.17}
)

// momentumExcess is the momentum blend's daily gross excess over its
// equal-weight book, its members sitting at res[first:first+4].
func momentumExcess(res []sleeve.RuleResult, first int) map[time.Time]float64 {
	out := map[time.Time]float64{}
	for j := range momNames {
		r := res[first+j]
		for i, d := range r.Signal.Dates {
			out[d] += momWeights[j] * (r.Signal.Gross[i] - r.EqualWeight.Gross[i])
		}
	}
	return out
}

// version is one trading version's judgement.
type version struct {
	name          string
	sum, eqS, shS sleeve.Summary
	raw, mEq, mSh evidence.Edge
	m2, corr      float64
	excess        []float64
	p, qv, dsr    float64
	deciding      string
	survivor      bool
}

// judgeVersions applies the pre-registered rule to the books at idx, as one
// family: matched-risk edge over the tougher control, FDR within the family,
// the 100-bps edge not negative against either control; plus the overlap with
// the momentum blend.
func judgeVersions(res []sleeve.RuleResult, idx []int, momExcess map[time.Time]float64, boot evidence.Bootstrap, q float64) []*version {
	var vs []*version
	for _, k := range idx {
		r := res[k]
		start := firstTraded(r.Signal)
		sb, eb, hb := trimFrom(r.Signal, start), trimFrom(r.EqualWeight, start), trimFrom(r.StableShuffled, start)
		_, ms := sleeve.Monthly(sb)
		_, me := sleeve.Monthly(eb)
		_, mse := sleeve.Monthly(atRisk(sb, eb))
		_, msh := sleeve.Monthly(atRisk(sb, hb))
		_, mh := sleeve.Monthly(hb)
		if len(ms) < 12 || len(ms) != len(me) || len(ms) != len(mh) {
			fatal(fmt.Errorf("%s: books misaligned or too short", r.Rule))
		}
		v := &version{name: r.Rule, sum: sleeve.Summarize(sb), eqS: sleeve.Summarize(eb), shS: sleeve.Summarize(hb)}
		var err error
		v.raw, err = evidence.PairedEdge(ms, me, boot, 0.90)
		fatalIf(err)
		v.mEq, err = evidence.PairedEdge(mse, me, boot, 0.90)
		fatalIf(err)
		v.mSh, err = evidence.PairedEdge(msh, mh, boot, 0.90)
		fatalIf(err)
		scaled, ctl, e := mse, me, v.mEq
		v.deciding = "equal-wt"
		if v.mSh.P > v.mEq.P {
			scaled, ctl, e, v.deciding = msh, mh, v.mSh, "random"
		}
		v.excess = make([]float64, len(scaled))
		for i := range scaled {
			v.excess[i] = scaled[i] - ctl[i]
		}
		v.p = e.P
		v.m2 = math.Inf(1)
		for _, ref := range []sleeve.Book{eb, hb} {
			v.m2 = math.Min(v.m2, sleeve.PairedMonthly(atRisk(doubleCost(sb), doubleCost(ref)), doubleCost(ref)).MeanDiff)
		}
		var a, b []float64
		for i, d := range sb.Dates {
			if x, ok := momExcess[d]; ok {
				a = append(a, sb.Gross[i]-eb.Gross[i])
				b = append(b, x)
			}
		}
		v.corr = pearson(a, b)
		vs = append(vs, v)
	}
	members := make([]evidence.Member, len(vs))
	for i, v := range vs {
		members[i] = evidence.Member{Name: v.name, Returns: v.excess, P: v.p}
	}
	verdicts, err := evidence.Judge(members, len(members), q)
	fatalIf(err)
	for i, vd := range verdicts {
		v := vs[i]
		v.qv, v.dsr = vd.Q, vd.Deflated.DSR
		v.survivor = vd.Discovery && v.mEq.MeanDiff > 0 && v.mSh.MeanDiff > 0 && v.m2 >= 0
	}
	return vs
}

func printVersions(vs []*version) {
	fmt.Printf("  %-19s %26s %9s %26s %26s\n", "", "the book: ret / vol / maxDD", "turnover", "equal-weight", "same-size random")
	for _, v := range vs {
		fmt.Printf("  %-19s %26s %8.0f%% %26s %26s\n", v.name,
			fmt.Sprintf("%.1f%% / %.1f%% / %.0f%%", 100*v.sum.AnnReturn, 100*v.sum.AnnVol, 100*v.sum.MaxDD),
			100*252*v.sum.MeanTurnover,
			fmt.Sprintf("%.1f%% / %.1f%% / %.0f%%", 100*v.eqS.AnnReturn, 100*v.eqS.AnnVol, 100*v.eqS.MaxDD),
			fmt.Sprintf("%.1f%% / %.1f%% / %.0f%%", 100*v.shS.AnnReturn, 100*v.shS.AnnVol, 100*v.shS.MaxDD))
	}
	fmt.Println()
	fmt.Printf("  %-19s %10s %18s %18s %9s %7s %6s %11s  %s\n", "%/month", "raw vs EW", "matched vs EW (p)", "matched vs rnd (p)",
		"100 bps", "FDR q", "DSR", "corr w/ mom", "verdict")
	for _, v := range vs {
		verdict := "does not survive"
		switch {
		case v.survivor && v.corr >= 0.8:
			verdict = "SURVIVES — same bet as momentum (a variant, not a track)"
		case v.survivor:
			verdict = "SURVIVES — distinct from momentum: candidate paper track"
		}
		fmt.Printf("  %-19s %+10.2f %+9.2f (%.3f) %+9.2f (%.3f) %+9.2f %7.3f %6.3f %11.2f  %s\n", v.name,
			100*v.raw.MeanDiff, 100*v.mEq.MeanDiff, v.mEq.P, 100*v.mSh.MeanDiff, v.mSh.P, 100*v.m2, v.qv, v.dsr, v.corr, verdict)
	}
	fmt.Println()
	fmt.Println("Deciding: matched-risk edge over the tougher control; survive = beats both, FDR discovery within the family,")
	fmt.Println("100-bps edge not negative. 'corr w/ mom' = daily gross excess (book - equal-weight) vs the momentum lookback")
	fmt.Println("blend's, run here on the same days (weekly rebalanced); >= 0.8 means the same bet.")
}

// enterFaithful is version (e)'s entry, Weinstein's own buy point: the week
// a name closes above the highest weekly close of the prior 30 weeks (the top
// of its base — the MA's own window), out of a base (last week's MA slope
// inside the flat band), with the MA not falling, the close above the MA,
// at least twice the average volume and positive Mansfield relative strength.
func enterFaithful(cls []stage.Classification, rs []float64) func(int) bool {
	return func(k int) bool {
		if k < stage.MAWindowWeeks {
			return false
		}
		c, prev := cls[k], cls[k-1]
		hi := math.Inf(-1)
		for j := k - stage.MAWindowWeeks; j < k; j++ {
			hi = math.Max(hi, cls[j].Close)
		}
		return c.Close > hi && c.Close > c.MA30 &&
			math.Abs(prev.MASlopePct) <= stage.FlatThresholdPct &&
			c.MASlopePct >= -stage.FlatThresholdPct &&
			c.VolumeRatio >= breakoutVolume && rs[k] > 0
	}
}

// followupScorers carry what the two follow-ups need per name, as sizing
// units straight through (Direct): Stage 2 membership, the 30-week MA slope,
// the distance above it, and the faithful breakout's held position.
// followupDays turns the first three into (d)'s book.
func followupScorers(m *market) []scorer {
	weekly := func(name string, val func(w weeks) []float64) scorer {
		return scorer{Name: name, Direct: true, Score: func(b []bars.Bar) []float64 {
			w := weeklyView(b)
			return w.daily(len(b), val(w))
		}}
	}
	known := func(w weeks, f func(c stage.Classification) float64) []float64 {
		out := make([]float64, len(w.cls))
		for k, c := range w.cls {
			out[k] = math.NaN()
			if c.Stage != stage.StageUnknown && c.MA30 > 0 {
				out[k] = f(c)
			}
		}
		return out
	}
	return []scorer{
		weekly("member", func(w weeks) []float64 { return stageMembership(w.cls, stage.Stage2Advancing) }),
		weekly("slope", func(w weeks) []float64 {
			return known(w, func(c stage.Classification) float64 { return c.MASlopePct })
		}),
		weekly("dist", func(w weeks) []float64 {
			return known(w, func(c stage.Classification) float64 { return c.Close/c.MA30 - 1 })
		}),
		weekly("e-faithful-breakout", func(w weeks) []float64 {
			return holdPositions(w.cls, enterFaithful(w.cls, mansfield(w.cls, m.levelAt)))
		}),
	}
}

// followupDays rebuilds each day's sizing units as [plain Stage 2, (d) early
// Stage 2, (e) faithful breakout, the four momentum members] from the columns
// followupScorers and memberScorers produced, and counts (e)'s entries by
// year and its mean names held.
func followupDays(days []sleeve.Day) ([]sleeve.Day, map[int]int, float64) {
	out := make([]sleeve.Day, len(days))
	entries := map[int]int{}
	prev := map[int32]bool{}
	var heldSum float64
	for di, d := range days {
		var slopes, dists []float64
		for _, o := range d.Obs {
			if o.U[0] == 1 && !math.IsNaN(o.U[1]) && !math.IsNaN(o.U[2]) {
				slopes = append(slopes, o.U[1])
				dists = append(dists, o.U[2])
			}
		}
		cutS, cutD := topFifthCut(slopes), topFifthCut(dists)
		nd := sleeve.Day{Date: d.Date, Obs: make([]sleeve.Obs, len(d.Obs))}
		held := 0
		for i, o := range d.Obs {
			member, early := o.U[0], o.U[0]
			if member == 1 && (o.U[1] >= cutS || o.U[2] >= cutD) {
				early = 0
			}
			e := o.U[3]
			if e == 1 {
				held++
				if !prev[o.Sym] {
					entries[d.Date.Year()]++
				}
			}
			prev[o.Sym] = e == 1
			nd.Obs[i] = sleeve.Obs{Sym: o.Sym, Ret: o.Ret, U: []float64{member, early, e, o.U[4], o.U[5], o.U[6], o.U[7]}}
		}
		heldSum += float64(held)
		out[di] = nd
	}
	mean := 0.0
	if len(days) > 0 {
		mean = heldSum / float64(len(days))
	}
	return out, entries, mean
}

// topFifthCut is the value at which the top fifth of x begins (the n/5
// largest are at or above it); +Inf when there are fewer than five values, so
// nothing is cut from a day too thin to have a top fifth.
func topFifthCut(x []float64) float64 {
	n := len(x)
	if n < 5 {
		return math.Inf(1)
	}
	c := append([]float64(nil), x...)
	sort.Float64s(c)
	return c[n-n/5]
}
