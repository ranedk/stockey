package main

// `slice stage -part rotation`: industry rotation, pre-registered in
// research/preregistrations/2026-10-01_industry_rotation.md (LEDGER row 53).

import (
	"context"
	"fmt"
	"math"
	"time"

	"github.com/ranedk/systrader/internal/bars"
	"github.com/ranedk/systrader/internal/evidence"
	"github.com/ranedk/systrader/internal/rotation"
	"github.com/ranedk/systrader/internal/sleeve"
	"github.com/ranedk/systrader/internal/stage"
	"github.com/ranedk/systrader/internal/store"
)

var rotationBooks = []struct {
	name string
	rule rotation.BookRule
}{
	{"P-plain-stage2", rotation.BookRule{}},
	{"R1-rotation", rotation.BookRule{Industry: true}},
	{"R2-rotation-mkt", rotation.BookRule{Industry: true, MarketFilter: true}},
}

// cashSym is a synthetic zero-return line: a sold name's slot sits in cash until the next
// rebalance (pre-registration section 3), which the sleeve -- always fully invested across
// positive units -- cannot express otherwise. stableIDs start at 1, so 0 is free.
const cashSym int32 = 0

func loadRotation(cache string) *rotation.Universe {
	b := rotation.NewBuilder()
	fatalIf(bars.Scan(cache, func(ser bars.Series) error { b.Add(ser); return nil }))
	ctx := context.Background()
	st, err := store.Open(ctx)
	fatalIf(err)
	defer st.Close()
	raw, err := st.RotationMembership(ctx)
	fatalIf(err)
	m := make(map[string]rotation.Membership, len(raw))
	for sym, x := range raw {
		m[sym] = rotation.Membership{Sector: x[0], Industry: x[1]}
	}
	return rotation.Build(b.Panel(), m)
}

// rotationScorer spreads a book's weekly holdings onto each symbol's days: 1 held, 0 a
// known (classified) name not held, NaN while the name's stage is unknown.
func rotationScorer(name string, u *rotation.Universe, weeks []rotation.BookWeek, held []map[string]bool) scorer {
	return scorer{Name: name, Direct: true, ScoreSym: func(sym string, b []bars.Bar) []float64 {
		out := make([]float64, len(b))
		cls := u.StockCls[sym]
		for i, bar := range b {
			out[i] = math.NaN()
			k := u.Panel.CompletedWeek(bar.Date)
			if k < 0 || cls == nil || cls[k].Stage == stage.StageUnknown {
				continue
			}
			out[i] = 0
			if held[k][sym] {
				out[i] = 1
			}
		}
		return out
	}}
}

// withCash appends the cash line to every day: for each rotation column, units of
// BookSize minus the names actually held that day; NaN for every other column.
func withCash(days []sleeve.Day, rotCols int) []sleeve.Day {
	for d := range days {
		u := make([]float64, len(days[d].Obs[0].U))
		for j := range u {
			u[j] = math.NaN()
		}
		for j := 0; j < rotCols; j++ {
			held := 0
			for _, o := range days[d].Obs {
				if o.U[j] == 1 {
					held++
				}
			}
			u[j] = float64(rotation.BookSize - held)
			if u[j] < 0 {
				u[j] = 0
			}
		}
		days[d].Obs = append(days[d].Obs, sleeve.Obs{Sym: cashSym, U: u, Ret: 0})
	}
	return days
}

func runRotationPart(cache string, from, to time.Time, floor, costBps float64, boot evidence.Bootstrap, q float64, cfg sleeve.Config, minBars int) {
	u := loadRotation(cache)
	start := 0
	for start < len(u.Panel.Weeks) && u.Panel.Weeks[start].Before(from) {
		start++
	}
	start-- // the week completed just before the first decision day decides it
	if start < 0 {
		start = 0
	}
	var scs []scorer
	var weeksByBook [][]rotation.BookWeek
	for _, bk := range rotationBooks {
		w := u.Holdings(bk.rule, start)
		held := make([]map[string]bool, len(w))
		for k := range w {
			held[k] = map[string]bool{}
			for _, s := range w[k].Held {
				held[k][s] = true
			}
		}
		weeksByBook = append(weeksByBook, w)
		scs = append(scs, rotationScorer(bk.name, u, w, held))
	}
	scs = append(scs, memberScorers(momNames, nil)...)
	days := withCash(buildScores(cache, scs, from, to, floor, minBars), len(rotationBooks))
	names := scorerNames(scs)
	res, err := sleeve.Run(days, names, cfg)
	fatalIf(err)

	vs := judgeVersions(res, []int{1, 2}, momentumExcess(res, 3), boot, q)

	// the industry layer's own question: does R beat plain Stage 2 chosen the same way?
	pStart := firstTraded(res[0].Signal)
	_, mp := sleeve.Monthly(trimFrom(res[0].Signal, pStart))
	plain := sleeve.Summarize(trimFrom(res[0].Signal, pStart))
	type vsPlain struct {
		e    evidence.Edge
		ok   bool
		note string
	}
	plainEdges := make([]vsPlain, 2)
	for k := 1; k <= 2; k++ {
		_, mr := sleeve.Monthly(trimFrom(res[k].Signal, pStart))
		if len(mr) != len(mp) {
			plainEdges[k-1].note = fmt.Sprintf("misaligned (%d vs %d months)", len(mr), len(mp))
			continue
		}
		e, err := evidence.PairedEdge(mr, mp, boot, 0.90)
		fatalIf(err)
		plainEdges[k-1] = vsPlain{e: e, ok: e.MeanDiff > 0 && e.P < 0.10}
	}

	// momentum blend book (weighted members), and each R at its risk
	blend := blendBook(res[3:3+len(momNames)], momWeights)
	type vsMom struct {
		e  evidence.Edge
		ok bool
	}
	momEdges := make([]vsMom, 2)
	for k := 1; k <= 2; k++ {
		rb := trimFrom(res[k].Signal, pStart)
		a, b := alignBooks(rb, blend)
		_, ma := sleeve.Monthly(atRisk(a, b))
		_, mb := sleeve.Monthly(b)
		e, err := evidence.PairedEdge(ma, mb, boot, 0.90)
		fatalIf(err)
		momEdges[k-1] = vsMom{e: e, ok: e.MeanDiff >= 0}
	}

	fmt.Println("INDUSTRY ROTATION — pre-registered, research/preregistrations/2026-10-01_industry_rotation.md")
	fmt.Printf("NSE adjusted EQ, Rs %.0f cr floor, %s..%s; weekly decisions on the last completed week, fill at the next open;\n",
		floor/1e7, from.Format("2006-01-02"), to.Format("2006-01-02"))
	fmt.Printf("%d names, max %d per industry, rebalance every %d weeks, 2x buffer, 30-week-MA exits to cash; %.0f bps round trip.\n",
		rotation.BookSize, rotation.PerIndustryCap, rotation.RebalanceWeeks, costBps)
	fmt.Println("R1 = Stage 2 names in LEADING industries (RS26 top fifth + industry Stage 2), by RS26. R2 = R1, flat while the")
	fmt.Println("market index is in Stage 4. P = plain Stage 2 by RS26, same machinery (reference, not a trial).")
	fmt.Println("Industry membership: today's classification applied to all history (named caveat). The cash line is one")
	fmt.Println("zero-return name in the equal-weight control's universe of ~800 (negligible).")
	fmt.Println()
	printVersions(vs)
	fmt.Printf("plain Stage 2 (P): %.1f%% / %.1f%% / %.0f%%, turnover %.0f%%/yr\n\n",
		100*plain.AnnReturn, 100*plain.AnnVol, 100*plain.MaxDD, 100*252*plain.MeanTurnover)
	for k, bk := range rotationBooks[1:] {
		pe, me := plainEdges[k], momEdges[k]
		heldSum, weeksN, off := 0, 0, 0
		for _, w := range weeksByBook[k+1][start:] {
			heldSum += len(w.Held)
			weeksN++
			if w.MarketOff {
				off++
			}
		}
		if pe.note != "" {
			fmt.Printf("%s vs P: %s\n", bk.name, pe.note)
		} else {
			fmt.Printf("%s vs P (net, paired monthly): %+.2f%%/month, p = %.3f  [criterion 2: positive with p < 0.10]\n", bk.name, 100*pe.e.MeanDiff, pe.e.P)
		}
		fmt.Printf("%s vs momentum blend at matched risk: %+.2f%%/month (p = %.3f)  [criterion 3: not below zero]\n", bk.name, 100*me.e.MeanDiff, me.e.P)
		fmt.Printf("%s mean names held %.1f; weeks flat on the market filter %d of %d\n\n", bk.name, float64(heldSum)/float64(weeksN), off, weeksN)
	}
	for k, bk := range rotationBooks[1:] {
		v := vs[k]
		pass := v.survivor && plainEdges[k].ok && momEdges[k].ok
		verdict := "FAILS"
		if pass {
			verdict = "PASSES — candidate forward paper track (with the fundamental filter as a paired variant)"
		}
		fmt.Printf("PRE-REGISTERED VERDICT %s: survives=%v, beats P=%v, not below momentum=%v -> %s\n",
			bk.name, v.survivor, plainEdges[k].ok, momEdges[k].ok, verdict)
	}
}

// blendBook combines the momentum members' net books with their frozen weights, on the
// dates all members share.
func blendBook(rs []sleeve.RuleResult, w []float64) sleeve.Book {
	acc := map[time.Time][3]float64{}
	cnt := map[time.Time]int{}
	for j, r := range rs {
		for i, d := range r.Signal.Dates {
			a := acc[d]
			a[0] += w[j] * r.Signal.Gross[i]
			a[1] += w[j] * r.Signal.Net[i]
			a[2] += w[j] * r.Signal.Turnover[i]
			acc[d] = a
			cnt[d]++
		}
	}
	var dates []time.Time
	for d, c := range cnt {
		if c == len(rs) {
			dates = append(dates, d)
		}
	}
	sortTimes(dates)
	b := sleeve.Book{Name: "momentum-blend"}
	for _, d := range dates {
		a := acc[d]
		b.Dates = append(b.Dates, d)
		b.Gross = append(b.Gross, a[0])
		b.Net = append(b.Net, a[1])
		b.Turnover = append(b.Turnover, a[2])
	}
	return b
}

func sortTimes(t []time.Time) {
	for i := 1; i < len(t); i++ {
		for j := i; j > 0 && t[j].Before(t[j-1]); j-- {
			t[j], t[j-1] = t[j-1], t[j]
		}
	}
}
