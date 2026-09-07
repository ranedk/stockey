// Package stageapi turns internal/stage's per-ticker classifier into a
// whole-universe listing, for the HTTP API (cmd/api) that serves the
// screener/ Nuxt frontend's stage-analysis page. REPORTING ONLY, same
// posture as internal/stage itself: this package only reads and labels,
// never sizes or trades.
package stageapi

import (
	"context"
	"math"
	"sort"
	"sync"
	"time"

	"github.com/ranedk/systrader/internal/core"
	"github.com/ranedk/systrader/internal/stage"
	"github.com/ranedk/systrader/internal/store"
)

// Row is one ticker's latest stage read, JSON-shaped for the frontend.
// SlopePct/VolumeRatio are pointers, not bare float64: encoding/json errors
// out hard on NaN ("json: unsupported value: NaN"), and stage.Classify
// legitimately returns NaN (no volume series supplied, or -- pre-filtered
// out here, but defensively -- insufficient slope warm-up). nil -> JSON null,
// same NaN-to-null translation stockey's own _clean_records does on the
// Python side of this API.
type Row struct {
	Ticker      string   `json:"ticker"`
	Stage       int      `json:"stage"`       // 1..4 per stage.Stage (StageUnknown rows are excluded before this point)
	StageLabel  string   `json:"stage_label"` // e.g. "Stage 2 (Advancing)"
	Close       float64  `json:"close"`
	MA30        float64  `json:"ma30"`
	SlopePct    *float64 `json:"slope_pct"`
	VolumeRatio *float64 `json:"volume_ratio"` // null when no volume series was available
	AsOf        string   `json:"as_of"`        // ISO date of the latest weekly bar used
}

// nanToNil turns a NaN into a nil pointer, anything else into a pointer to
// itself -- see Row's own doc comment for why this matters for JSON.
func nanToNil(v float64) *float64 {
	if math.IsNaN(v) {
		return nil
	}
	return &v
}

// Result is the full listing plus enough bookkeeping for the frontend (and a
// human) to see what was excluded and why, rather than a silent partial list.
type Result struct {
	Rows          []Row          `json:"rows"`
	TotalUniverse int            `json:"total_universe"` // symbols with >= MinBars daily bars
	IncludedCount int            `json:"included_count"`
	ExcludedStale int            `json:"excluded_stale"`  // last daily bar older than MaxStaleDays
	ExcludedShort int            `json:"excluded_short"`  // not enough weekly history for a first read
	CountsByStage map[string]int `json:"counts_by_stage"` // "1".."4" -> count, over IncludedCount rows only
	AsOfNow       string         `json:"as_of_now"`       // wall-clock time the listing was computed
}

// Options controls universe scope and freshness. Zero value uses sane
// defaults via WithDefaults.
type Options struct {
	MinBars      int   // minimum daily bars to even consider a symbol (default 260, ~1yr)
	MaxStaleDays int   // exclude a symbol whose latest daily bar is older than this (default 21)
	Stages       []int // filter to these stage numbers (1-4); nil/empty = all classified stages
	Concurrency  int   // worker pool size for the per-symbol DB fetch+classify (default 12)
}

func (o Options) withDefaults() Options {
	if o.MinBars <= 0 {
		o.MinBars = 260
	}
	if o.MaxStaleDays <= 0 {
		o.MaxStaleDays = 21
	}
	if o.Concurrency <= 0 {
		o.Concurrency = 12
	}
	return o
}

// List computes the latest stage classification for every symbol in
// store.AdjustedSymbols (the point-in-time-honest EQ universe), filters out
// stale/delisted-looking names and names too short for a first classified
// read, and returns the rest sorted by ticker. now is passed in rather than
// read internally so callers can hold it fixed for one consistent listing.
func List(ctx context.Context, st *store.Store, opts Options, now time.Time) (Result, error) {
	opts = opts.withDefaults()

	symbols, err := st.AdjustedSymbols(ctx, opts.MinBars)
	if err != nil {
		return Result{}, err
	}

	stageFilter := make(map[int]bool, len(opts.Stages))
	for _, s := range opts.Stages {
		stageFilter[s] = true
	}

	type outcome struct {
		row   Row
		ok    bool
		stale bool
		short bool
	}
	results := make([]outcome, len(symbols))

	sem := make(chan struct{}, opts.Concurrency)
	var wg sync.WaitGroup
	for i, symbol := range symbols {
		wg.Add(1)
		sem <- struct{}{}
		go func(i int, symbol string) {
			defer wg.Done()
			defer func() { <-sem }()
			results[i] = classifyOne(ctx, st, symbol, opts, now)
		}(i, symbol)
	}
	wg.Wait()

	res := Result{
		TotalUniverse: len(symbols),
		CountsByStage: map[string]int{"1": 0, "2": 0, "3": 0, "4": 0},
		AsOfNow:       now.UTC().Format(time.RFC3339),
	}
	for _, o := range results {
		switch {
		case o.stale:
			res.ExcludedStale++
		case o.short:
			res.ExcludedShort++
		case o.ok:
			res.IncludedCount++
			res.CountsByStage[itoa(o.row.Stage)]++
			if len(stageFilter) == 0 || stageFilter[o.row.Stage] {
				res.Rows = append(res.Rows, o.row)
			}
		}
	}
	sort.Slice(res.Rows, func(i, j int) bool { return res.Rows[i].Ticker < res.Rows[j].Ticker })
	return res, nil
}

// classifyOne never returns an error to the caller -- a single symbol's DB
// hiccup or thin history degrades that one row to "short", not the whole
// listing. Errors are swallowed deliberately here for that reason; a widescale
// DB outage still surfaces since AdjustedSymbols itself would fail first.
func classifyOne(ctx context.Context, st *store.Store, symbol string, opts Options, now time.Time) struct {
	row   Row
	ok    bool
	stale bool
	short bool
} {
	type out = struct {
		row   Row
		ok    bool
		stale bool
		short bool
	}

	daily, err := st.AdjustedCloses(ctx, symbol)
	if err != nil || daily.Len() == 0 {
		return out{short: true}
	}
	lastDaily := daily.Times[daily.Len()-1]
	if now.Sub(lastDaily) > time.Duration(opts.MaxStaleDays)*24*time.Hour {
		return out{stale: true}
	}

	weeklyClose := core.ResampleWeeklyLast(daily)
	if weeklyClose.Len() < stage.MAWindowWeeks+stage.SlopeWindowWeeks {
		return out{short: true}
	}

	var volPtr *core.Series
	if dailyVol, err := st.AdjustedVolume(ctx, symbol); err == nil && dailyVol.Len() > 0 {
		weeklyVol := core.ResampleWeeklySum(dailyVol)
		volPtr = &weeklyVol
	}

	cs := stage.Classify(weeklyClose, volPtr)
	latest, ok := stage.Latest(cs)
	if !ok || latest.Stage == stage.StageUnknown {
		return out{short: true}
	}

	return out{ok: true, row: Row{
		Ticker:      symbol,
		Stage:       int(latest.Stage),
		StageLabel:  latest.Stage.String(),
		Close:       latest.Close,
		MA30:        latest.MA30,
		SlopePct:    nanToNil(latest.MASlopePct),
		VolumeRatio: nanToNil(latest.VolumeRatio),
		AsOf:        latest.Time.Format("2006-01-02"),
	}}
}

func itoa(n int) string {
	digits := "0123456789"
	if n < 10 {
		return string(digits[n])
	}
	return "?"
}
