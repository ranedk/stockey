// Package paper keeps the forward record for a frozen strategy: the orders it
// would place, the book it would hold, and what it earned against its
// benchmarks — day by day, from the day it was frozen.
//
// This is the gate `docs/RESEARCH_PROTOCOL.md` and bible Law 4 both point at.
// Everything else in this repo scores a rule against history that has already
// happened, and history can be mined. A forward record cannot: every day it
// gains one observation nobody chose.
//
// The track is recomputed from the start date on every run rather than updated
// incrementally. That makes it idempotent (running twice changes nothing),
// self-healing (a day the job did not run fills itself in), and auditable (the
// whole record is a pure function of the spec, the price data and the start
// date). It costs a few seconds and removes an entire class of drift between
// what the database says and what the strategy actually did.
package paper

import (
	"fmt"
	"math"
	"sort"
	"time"
)

// Spec is the frozen configuration. It mirrors
// docs/strategies/2026-09-08_trend_quintile.md; changing any field here means
// a different strategy, a new spec document and a new clock.
type Spec struct {
	Name  string
	Start time.Time
	// Signal names the selection signal cmd/paper builds. It is part of the
	// spec because a frozen strategy run with a different signal is a
	// different strategy wearing its name.
	Signal string
	// SignalLabel says the same in words, for the screener.
	SignalLabel string
	// Doc is the spec document this configuration mirrors.
	Doc            string
	MinTurnover    float64 // 60-bar median traded value, INR
	TurnoverWindow int
	Quantile       int // 5 = hold the top fifth by forecast
	// HoldCount fixes the number of names instead, when non-zero. A quintile
	// of a growing universe is a growing book; a fixed count is not, and the
	// two answer different questions about concentration.
	HoldCount int
	// Mode decides how the book uses its two signals: one of them, a switch
	// between them on market breadth, or a constant blend of both.
	Mode SelectionMode
	// SwitchBreadth is the share of the universe in an uptrend below which a
	// switching book uses its SECOND signal instead of its first.
	SwitchBreadth float64
	// Variants names the scores a book-blend holds books of (ModeBookBlend),
	// in the order Obs.Signals carries them; VariantWeights are their fixed
	// shares of the book, from the handcrafting tree.
	Variants       []string
	VariantWeights []float64
	// Composite marks a track built from other registered tracks' variants:
	// its qualifications duplicate theirs, so the cross-strategy view leaves it out.
	Composite      bool
	RebalanceEvery int // trading days between rebalances
	// KeepMultiple is the rank buffer (TODO A2): when > 1, a name already held
	// is kept until it falls outside the top N*KeepMultiple rather than the top
	// N, and the free slots go to the best new names. 0 or 1 = no buffer, the
	// original rule. Fewer trades means less cost and less short-term tax; a
	// frozen strategy's buffer is part of its spec like any other field.
	KeepMultiple     int
	CostBpsRoundTrip float64 // charged as half per side on turnover
	RandomSeed       int64   // for the random-ranking benchmark

	// Overlay decides HOW MUCH of the book to own. Selection answers "which
	// stocks"; this answers "how much", and they are separate questions —
	// which is the whole reason the earlier washout test found nothing. It
	// could only ever change what was held, never how much, so the one lever
	// that addresses a crash was not on the table.
	Overlay Overlay
	// TargetVol is the annualised volatility the vol-scaling overlay aims at.
	// Set to the strategy's own long-run realised volatility, so it describes
	// what the book already does rather than being a number chosen to make a
	// backtest look better.
	TargetVol float64
	// VolLookback is the window of the strategy's OWN daily returns used to
	// estimate current volatility (126 days ~ six months, the published
	// convention).
	VolLookback int
	// TrendWindow is the moving average the market-regime filter uses on an
	// equal-weight index of the eligible universe (200 days, the canonical
	// filter, not a number we picked).
	TrendWindow int
	// ConstantExposure is the fixed fraction OverlayConstant holds. Set it to
	// the average exposure of the overlay being tested, so the comparison is
	// like for like.
	ConstantExposure float64
	// Stop is the per-position exit rule. Law 8 says a systems trader needs
	// none — "the forecast is the entry and the exit" — and that argument
	// assumes a forecast that adjusts continuously, so a position shrinks as
	// its trend weakens. This book's selection is frozen for twenty days at a
	// time, which is exactly the condition the argument does not cover, so the
	// claim is worth testing rather than assuming.
	Stop StopKind
	// StopLevel is the fractional distance for a fixed-percentage stop, or the
	// multiple of annualised volatility for a vol-scaled one.
	StopLevel float64
	// RandomExitRate drives the control: exit positions at the same rate as a
	// real stop but chosen at random. Without it, "stops cut the drawdown"
	// cannot be told apart from "exiting anything at this rate cuts the
	// drawdown", which is the mistake row 20 caught for exposure rules.
	RandomExitRate float64
	// ExposureDaily decides WHEN the overlay may act: every day, or only on
	// the selection rebalance clock.
	//
	// Not a free parameter so much as a fork in the design, and both ends are
	// defensible: acting daily catches a crash as it happens, acting on the
	// rebalance clock keeps the turnover of a 160-name book from eating the
	// benefit. The published volatility-scaling work rescales monthly, so
	// daily is the deviation, not the default.
	ExposureDaily bool
}

// StopKind is the per-position exit rule.
type StopKind int

const (
	// StopNone is Law 8's own answer: the rebalance is the only exit.
	StopNone StopKind = iota
	// StopFixed exits when the close falls StopLevel below the ENTRY price.
	StopFixed
	// StopTrailing exits when the close falls StopLevel below the position's
	// highest close since entry.
	StopTrailing
	// StopVolFixed and StopVolTrailing are the same two rules with the
	// distance set to StopLevel x the position's own annualised volatility,
	// so a jumpy stock is given more room than a quiet one. Carver's own
	// stop-loss convention is half the annualised volatility.
	StopVolFixed
	StopVolTrailing
	// StopRandom exits positions at RandomExitRate per position per day,
	// chosen by a fixed hash rather than by price. The control.
	StopRandom
)

func (s StopKind) String() string {
	switch s {
	case StopFixed:
		return "fixed from entry"
	case StopTrailing:
		return "trailing from high"
	case StopVolFixed:
		return "vol-scaled from entry"
	case StopVolTrailing:
		return "vol-scaled trailing"
	case StopRandom:
		return "random exits (control)"
	default:
		return "none"
	}
}

// ParseStop maps a flag value to a stop rule.
func ParseStop(s string) (StopKind, error) {
	switch s {
	case "", "none":
		return StopNone, nil
	case "fixed":
		return StopFixed, nil
	case "trailing":
		return StopTrailing, nil
	case "volfixed":
		return StopVolFixed, nil
	case "voltrailing":
		return StopVolTrailing, nil
	case "random":
		return StopRandom, nil
	}
	return StopNone, fmt.Errorf("paper: unknown stop %q (none|fixed|trailing|volfixed|voltrailing|random)", s)
}

// SelectionMode decides how a book uses its two signals.
type SelectionMode int

const (
	// ModeSingle ranks by the primary signal and ignores the second.
	ModeSingle SelectionMode = iota
	// ModeSwitch ranks by the SECOND signal on days when market breadth is
	// below SwitchBreadth, and by the first otherwise.
	ModeSwitch
	// ModeBlend always holds half the book from each signal. It is the CONTROL
	// for ModeSwitch: two signals held together diversify, and diversification
	// alone will improve a book whether or not the switching does anything.
	// Without this, "the switch works" cannot be told from "owning both works".
	ModeBlend
	// ModeBookBlend holds several variants' own books at fixed weights — each
	// variant's top quantile by its own score — and adds them up. A blend of
	// BOOKS, not of signals: variants whose raw scores have different spreads
	// (a 6-month and a 12-month return) cannot be averaged without the widest
	// one dominating, and each variant's book is exactly what research
	// measured (LEDGER row 30).
	ModeBookBlend
)

func (m SelectionMode) String() string {
	switch m {
	case ModeSwitch:
		return "switch on breadth"
	case ModeBlend:
		return "constant blend (control)"
	case ModeBookBlend:
		return "blend of variant books"
	default:
		return "single signal"
	}
}

// ParseMode maps a flag value to a selection mode.
func ParseMode(s string) (SelectionMode, error) {
	switch s {
	case "", "single":
		return ModeSingle, nil
	case "switch":
		return ModeSwitch, nil
	case "blend":
		return ModeBlend, nil
	case "bookblend":
		return ModeBookBlend, nil
	}
	return ModeSingle, fmt.Errorf("paper: unknown mode %q (single|switch|blend|bookblend)", s)
}

// breadthOf is the share of the eligible universe in an uptrend — the regime
// reading a switching book acts on. Counted from the same cross-section the
// book selects from, so it needs no second data source and no alignment.
func breadthOf(d Day) float64 {
	var up, n float64
	for _, o := range d.Obs {
		if !o.Eligible {
			continue
		}
		n++
		if o.AboveSMA {
			up++
		}
	}
	if n == 0 {
		return 1
	}
	return up / n
}

// Overlay is the exposure rule.
type Overlay int

const (
	// OverlayNone is always fully invested — the unprotected book.
	OverlayNone Overlay = iota
	// OverlayVolScaled holds less when the strategy's own volatility runs
	// above target. Momentum volatility is persistent, and crashes come out
	// of its high-volatility stretches, so this shrinks the book BEFORE the
	// worst of a crash rather than after it.
	OverlayVolScaled
	// OverlayRegimeFloor is flat whenever the universe's own equal-weight
	// index sits below its 200-day average. The canonical trend filter.
	OverlayRegimeFloor
	// OverlayBoth takes the smaller of the two exposures.
	OverlayBoth
	// OverlayConstant holds a fixed fraction of the book every day, and is the
	// CONTROL every other overlay has to beat.
	//
	// Any rule that reduces average exposure will reduce drawdown — that is
	// arithmetic, not skill. The question is whether it reduces drawdown MORE
	// than simply owning that much all the time would, and whether it gives up
	// less return doing it. Without this control, "cut the drawdown by a
	// quarter" reads as a discovery when it may only mean "owned a quarter
	// less". Law 3, applied to exposure instead of selection.
	OverlayConstant
)

func (o Overlay) String() string {
	switch o {
	case OverlayVolScaled:
		return "vol-scaled"
	case OverlayRegimeFloor:
		return "regime-floor"
	case OverlayBoth:
		return "vol-scaled + regime-floor"
	case OverlayConstant:
		return "constant exposure (control)"
	default:
		return "none"
	}
}

// ParseOverlay maps a flag value to an overlay.
func ParseOverlay(s string) (Overlay, error) {
	switch s {
	case "", "none":
		return OverlayNone, nil
	case "vol":
		return OverlayVolScaled, nil
	case "regime":
		return OverlayRegimeFloor, nil
	case "both":
		return OverlayBoth, nil
	case "constant":
		return OverlayConstant, nil
	}
	return OverlayNone, fmt.Errorf("paper: unknown overlay %q (none|vol|regime|both)", s)
}

// FrozenSpec is the configuration signed off on 2026-09-08. The Rs 10 crore
// floor is deliberate: the Rs 1 crore version backtests better and cannot
// absorb capital.
func FrozenSpec() Spec {
	return Spec{
		Name:             "trend-quintile",
		Start:            time.Date(2026, 9, 8, 0, 0, 0, 0, time.UTC),
		Signal:           "ewmac32",
		SignalLabel:      "EWMAC 32/128 — exponential moving-average crossover, volatility-normalised, long-only",
		Doc:              "docs/strategies/2026-09-08_trend_quintile.md",
		MinTurnover:      1e8,
		TurnoverWindow:   60,
		Quantile:         5,
		RebalanceEvery:   20,
		CostBpsRoundTrip: 50,
		RandomSeed:       20260908,

		// Overlay defaults. None of these three numbers was searched for:
		// 126 days is the published six-month convention for the volatility
		// estimate, 200 days is the canonical trend filter, and the 20% target
		// is the strategy's OWN long-run realised volatility measured over
		// 2013-2026 (19.85%, rounded) — a description of what the book does,
		// not a level chosen to flatter a backtest.
		Overlay:          OverlayNone,
		TargetVol:        0.20,
		VolLookback:      126,
		TrendWindow:      200,
		ExposureDaily:    false,
		ConstantExposure: 1,
	}
}

// SpeedBlendSpec is the configuration frozen on 2026-09-10: trend-quintile
// in every respect but the signal, which blends the three slow EWMAC speeds
// instead of picking one (Law 5). Same universe, same clock, same costs, and
// the same random-ranking seed, so the two tracks share identical control
// books and differ ONLY in what they select.
func SpeedBlendSpec() Spec {
	s := FrozenSpec()
	s.Name = "trend-speed-blend"
	s.Start = time.Date(2026, 9, 10, 0, 0, 0, 0, time.UTC)
	s.Signal = "trend-speed-blend"
	s.SignalLabel = "EWMAC 16/64, 32/128 and 64/256 blended 40/16/44 (handcrafted), FDM 1.10 — volatility-normalised, long-only"
	s.Doc = "docs/strategies/2026-09-10_trend_speed_blend.md"
	return s
}

// MomentumLookbackBlendSpec is the configuration frozen on 2026-09-12: the
// literature's momentum — trailing total return, not EWMAC — at the four
// lookbacks whose one-month-hold cells survived LEDGER row 30, held as a blend
// of their books (ModeBookBlend) at handcrafted weights: Table 8 row 3 over
// the groups {6m}, {9m}, {12m, 12-minus-1} (the last two correlate 0.94),
// Table 12 column A on costs, whole percent
// (research/reports/2026-09-12_lookback_blend_construction.txt). Rebalanced
// monthly. Same universe, costs and random-ranking seed as the other tracks.
func MomentumLookbackBlendSpec() Spec {
	s := FrozenSpec()
	s.Name = "momentum-lookback-blend"
	s.Start = time.Date(2026, 9, 12, 0, 0, 0, 0, time.UTC)
	s.Signal = "momentum-lookback-blend"
	s.SignalLabel = "Trailing 6, 9, 12 and 12-minus-1 month returns — each variant's top quintile, held 33/33/17/17 (handcrafted), long-only, monthly"
	s.Doc = "docs/strategies/2026-09-12_momentum_lookback_blend.md"
	s.Mode = ModeBookBlend
	s.Variants = []string{"mom6", "mom9", "mom12", "mom12_1"}
	s.VariantWeights = []float64{0.33, 0.33, 0.17, 0.17}
	s.RebalanceEvery = 21
	return s
}

// LowVolBlendSpec is the configuration frozen on 2026-09-12: the low-risk
// measures whose one-month-hold cells survived LEDGER row 32 — 3/6/12-month
// volatility, 1-year beta and 1-year residual volatility — each variant's
// LOWEST-risk quintile, held as a blend of books at handcrafted weights: Table
// 8 row 3 over the groups {the three volatility windows, correlating
// 0.94-0.97}, {beta}, {residual volatility}, thirds again inside the
// volatility group; Table 12 column A on costs; whole percent
// (research/reports/2026-09-12_lowvol_blend_construction.txt). Monthly.
func LowVolBlendSpec() Spec {
	s := FrozenSpec()
	s.Name = "low-volatility-blend"
	s.Start = time.Date(2026, 9, 12, 0, 0, 0, 0, time.UTC)
	s.Signal = "low-volatility-blend"
	s.SignalLabel = "Lowest-risk fifth by 3/6/12-month volatility, 1-year beta and 1-year residual volatility — held 11/11/11/33/34 (handcrafted), long-only, monthly"
	s.Doc = "docs/strategies/2026-09-12_low_volatility_blend.md"
	s.Mode = ModeBookBlend
	s.Variants = []string{"vol3", "vol6", "vol12", "beta12", "idio12"}
	s.VariantWeights = []float64{0.11, 0.11, 0.11, 0.33, 0.34}
	s.RebalanceEvery = 21
	return s
}

// MomentumLowVolCombinationSpec is the configuration frozen on 2026-09-12:
// the momentum and low-volatility tracks' books together as one book of their
// nine variants. A two-branch handcrafting tree — each branch at its own
// track's frozen weights — split by Table 8 row 2 (halves) and tilted by Table
// 12 column A on the branches' costs (0.088 vs 0.067 SR/yr: 49/51), whole
// percent over the members (research/reports/2026-09-12_combination_construction.txt).
// The branches' excess returns correlate -0.12. Monthly. Composite: the
// cross-strategy view shows its members under their own tracks.
func MomentumLowVolCombinationSpec() Spec {
	s := FrozenSpec()
	s.Name = "momentum-lowvol-combination"
	s.Start = time.Date(2026, 9, 12, 0, 0, 0, 0, time.UTC)
	s.Signal = "momentum-lowvol-combination"
	s.SignalLabel = "Momentum lookback blend 48% + low-volatility blend 52%, one book of their nine variants (handcrafted), long-only, monthly"
	s.Doc = "docs/strategies/2026-09-12_momentum_lowvol_combination.md"
	s.Mode = ModeBookBlend
	s.Variants = []string{"mom6", "mom9", "mom12", "mom12_1", "vol3", "vol6", "vol12", "beta12", "idio12"}
	s.VariantWeights = []float64{0.16, 0.16, 0.08, 0.08, 0.06, 0.06, 0.06, 0.17, 0.17}
	s.RebalanceEvery = 21
	s.Composite = true
	return s
}

// Specs lists every strategy with a forward record, in the order they began.
func Specs() []Spec {
	return []Spec{FrozenSpec(), SpeedBlendSpec(), MomentumLookbackBlendSpec(), LowVolBlendSpec(), MomentumLowVolCombinationSpec()}
}

// SpecFor finds a registered strategy by name.
func SpecFor(name string) (Spec, bool) {
	for _, s := range Specs() {
		if s.Name == name {
			return s, true
		}
	}
	return Spec{}, false
}

// Book names. The strategy is tracked against both benchmarks from day one,
// because "it made 12%" means nothing without them.
const (
	BookStrategy = "strategy"
	BookEqual    = "equal-weight"
	BookRandom   = "random-ranking"
	// BookAccount is the strategy book in rupees: AccountCapital in whole
	// shares, Law 12 inertia and Dhan's real charges (internal/capacity,
	// LEDGER row 36). The same targets; what a real account would hold.
	BookAccount = "account"
)

// AccountCapital is the paper account each forward track keeps: Rs 1 crore,
// the capital at which every track's book survived whole shares and real
// costs (LEDGER row 36). Paper money, per track, set by the operator
// 2026-09-13.
const AccountCapital = 1e7

// Obs is one symbol on one date: what it was worth and what the rule thought.
type Obs struct {
	Symbol    string
	Open      float64
	Close     float64
	PrevClose float64
	Forecast  float64
	Turnover  float64
	Eligible  bool
	// AnnVol is the symbol's annualised volatility at the previous close, used
	// only by the vol-scaled stops: a jumpy stock needs more room than a quiet
	// one before a fall means anything.
	AnnVol float64
	// Forecast2 is the alternate signal, for a book that switches or blends.
	Forecast2 float64
	// Signals are a book-blend's variant scores, in Spec.Variants order, NaN
	// where a variant is undefined (not enough history yet).
	Signals []float64
	// AboveSMA is the name's own 200-day trend state, from which the day's
	// breadth — the share of the universe in an uptrend — is counted.
	AboveSMA bool
}

// Day is one trading date's cross-section.
type Day struct {
	Date time.Time
	Obs  []Obs
}

// NavPoint is one book's record for one day.
type NavPoint struct {
	Date       time.Time
	NAV        float64
	Return     float64
	Turnover   float64 // one-way, as a fraction of the book
	Cost       float64 // charged that day, as a fraction of NAV
	Holdings   int
	Rebalanced bool
	// Exposure is the share of capital actually invested that day; the rest
	// sits in cash earning nothing, which is deliberately conservative.
	Exposure float64
}

// Order is one intended trade at a rebalance. Weights are fractions of NAV;
// the fill price is the open of that day, which is what the backtest assumed
// and what a human placing this at the open would get.
type Order struct {
	Date       time.Time
	Book       string
	Symbol     string
	Side       string // BUY, SELL, EXIT
	FromWeight float64
	ToWeight   float64
	FillPrice  float64
	// InShares marks an order of the rupee account (BookAccount): counted in
	// whole shares at the raw price, with the rupees traded and what Dhan
	// charges for it.
	InShares   bool
	FromShares float64
	ToShares   float64
	ValueRs    float64
	CostRs     float64
}

// Entry is what a position cost and what it is worth now — the two numbers a
// human wants beside a ticker.
//
// EntryPrice is the fill at which the position was FIRST opened, and it
// survives later top-ups and trims: "up 8% since we bought it" is the question
// people actually ask, and a weighted-average cost that moves every rebalance
// answers a different one.
type Entry struct {
	Date      time.Time
	Price     float64
	LastPrice float64
	// HighPrice is the highest close since entry, for a trailing stop.
	HighPrice float64
}

// Return is the position's gain since it was opened.
func (e Entry) Return() float64 {
	if e.Price <= 0 || e.LastPrice <= 0 {
		return 0
	}
	return e.LastPrice/e.Price - 1
}

// Book is one tracked portfolio.
type Book struct {
	Name string
	// Shares is the rupee account's share count per name; nil for the
	// weight books, which hold fractions of NAV.
	Shares   map[string]float64
	NAV      []NavPoint
	Holdings map[string]float64 // weights as of the last computed day
	Entries  map[string]Entry   // cost basis and last mark, same keys as Holdings
	Orders   []Order
	// pendingExits are positions whose stop was breached at yesterday's close
	// and which are sold at today's open — the same decide-at-close,
	// fill-at-next-open convention as everything else here. Filling at the
	// stop price itself would assume an intraday fill this daily data cannot
	// support, and would flatter every result.
	pendingExits map[string]bool
	// Stopped counts exits taken by the stop rule rather than by a rebalance.
	Stopped int
	// members are the names each variant chose at the last rebalance (index 0
	// for a single-signal book) -- what the rank buffer keeps. Per variant, so
	// a name held for one variant is never carried into another's sleeve.
	members []map[string]bool
}

// Members returns the strategy book's per-variant selection at its last
// rebalance -- what Pending needs to apply the rank buffer to live orders.
func (t *Track) Members() []map[string]bool {
	if b, ok := t.Books[BookStrategy]; ok {
		return b.members
	}
	return nil
}

// Track is the whole forward record.
type Track struct {
	Spec      Spec
	Books     map[string]*Book
	Exposure  []ExposurePoint
	Dates     []time.Time
	NextRebal time.Time // zero if the next rebalance date is not yet known
	LastRebal time.Time
}

// Compute walks the calendar and produces the record. days must be sorted
// ascending and contain only dates on or after spec.Start.
func Compute(spec Spec, days []Day) (*Track, error) {
	if spec.RebalanceEvery < 1 {
		return nil, fmt.Errorf("paper: RebalanceEvery must be at least 1")
	}
	if spec.Quantile < 2 {
		return nil, fmt.Errorf("paper: Quantile must be at least 2")
	}
	if spec.KeepMultiple > 1 && spec.Mode == ModeBlend {
		return nil, fmt.Errorf("paper: the rank buffer is not defined for ModeBlend (half the book from each signal)")
	}
	tr := &Track{Spec: spec, Books: map[string]*Book{}}
	for _, n := range []string{BookStrategy, BookEqual, BookRandom} {
		tr.Books[n] = &Book{Name: n, Holdings: map[string]float64{},
			Entries: map[string]Entry{}, pendingExits: map[string]bool{}}
	}
	navs := map[string]float64{BookStrategy: 100, BookEqual: 100, BookRandom: 100}

	// Exposure inputs, both built from information available BEFORE the day
	// they are used on:
	//   baseRet    the unprotected book's daily returns, for the vol estimate.
	//              Scaling off the SCALED book's own volatility would be
	//              self-referential — halve the book and its volatility halves,
	//              which then says to double it again.
	//   marketIdx  an equal-weight index of the eligible universe, the "market"
	//              this strategy actually fishes in, for the trend filter.
	var baseRet []float64
	marketIdx := []float64{100}
	lastExposure := 1.0

	for di, d := range days {
		tr.Dates = append(tr.Dates, d.Date)
		rebalance := di%spec.RebalanceEvery == 0
		if rebalance {
			tr.LastRebal = d.Date
		}
		// Exposure for today, from yesterday's information. On the rebalance
		// clock it is only allowed to move on a rebalance day; in between the
		// book keeps whatever exposure it last set.
		exposure := exposureFor(spec, baseRet, marketIdx)
		if !spec.ExposureDaily && !rebalance && di > 0 {
			exposure = lastExposure
		}
		lastExposure = exposure
		tr.Exposure = append(tr.Exposure, ExposurePoint{Date: d.Date, Exposure: exposure})
		prices := make(map[string]Obs, len(d.Obs))
		for _, o := range d.Obs {
			prices[o.Symbol] = o
		}

		for _, name := range []string{BookStrategy, BookEqual, BookRandom} {
			b := tr.Books[name]

			// Overnight: yesterday's book carried to today's open.
			rPre := weightedReturn(b.Holdings, prices, func(o Obs) float64 {
				if o.PrevClose <= 0 || o.Open <= 0 {
					return 0
				}
				return o.Open/o.PrevClose - 1
			})
			drifted := drift(b.Holdings, prices, func(o Obs) float64 {
				if o.PrevClose <= 0 || o.Open <= 0 {
					return 1
				}
				return o.Open / o.PrevClose
			})

			// `held` is what the book actually owned at the open, and every
			// trade today is measured against it. Deleting stopped names from
			// `drifted` and then comparing `drifted` with itself made stop-outs
			// free — the exits happened and no turnover was ever charged.
			held := drifted

			// Yesterday's stop breaches are sold at today's open, before
			// anything else happens.
			if name == BookStrategy && len(b.pendingExits) > 0 {
				after := make(map[string]float64, len(drifted))
				for sym, w := range drifted {
					if b.pendingExits[sym] {
						b.Stopped++
						continue
					}
					after[sym] = w
				}
				drifted = after
				b.pendingExits = map[string]bool{}
			}

			var turnover, cost float64
			target := drifted
			// Selection changes only on the rebalance clock. Exposure is
			// checked EVERY day: a crash filter that waits nineteen days for
			// the next rebalance is not a crash filter.
			if rebalance {
				if name == BookStrategy {
					target, b.members = selectWeights(name, spec, d, b.members)
				} else {
					target = targetWeights(name, spec, d)
				}
			}
			if name == BookStrategy {
				target = rescale(target, exposure)
			}
			if changed(held, target) {
				turnover = turnoverBetween(held, target)
				cost = turnover * spec.CostBpsRoundTrip / 2 / 10000
				if rebalance {
					b.Orders = append(b.Orders, ordersBetween(d.Date, name, held, target, prices)...)
				}
			}

			// The rest of the session, held at the new weights.
			rPost := weightedReturn(target, prices, func(o Obs) float64 {
				if o.Open <= 0 || o.Close <= 0 {
					return 0
				}
				return o.Close/o.Open - 1
			})
			end := drift(target, prices, func(o Obs) float64 {
				if o.Open <= 0 || o.Close <= 0 {
					return 1
				}
				return o.Close / o.Open
			})

			prev := navs[name]
			navs[name] = prev * (1 + rPre) * (1 - cost) * (1 + rPost)
			bookExposure := 1.0
			if name == BookStrategy {
				bookExposure = exposure
			}
			b.NAV = append(b.NAV, NavPoint{
				Date: d.Date, NAV: navs[name], Return: navs[name]/prev - 1,
				Turnover: turnover, Cost: cost, Holdings: len(end), Rebalanced: rebalance,
				Exposure: bookExposure,
			})
			b.Holdings = end
			updateEntries(b, end, prices, d.Date, rebalance)
			if name == BookStrategy {
				b.pendingExits = breachedStops(spec, b, prices, di)
			}
		}

		// Today's readings, available from tomorrow onwards.
		baseRet = append(baseRet, unscaledReturn(spec, d, prices))
		marketIdx = append(marketIdx, marketIdx[len(marketIdx)-1]*(1+equalWeightReturn(d)))
	}
	if len(days) > 0 {
		tr.NextRebal = nextRebalanceIndexDate(days, spec.RebalanceEvery)
	}
	return tr, nil
}

// updateEntries keeps each position's cost basis in step with the book: a name
// that just arrived records what it was bought at, a name that left is
// forgotten, and everything still held is marked to today's close.
func updateEntries(b *Book, weights map[string]float64, prices map[string]Obs, date time.Time, rebalance bool) {
	for sym := range b.Entries {
		if _, held := weights[sym]; !held {
			delete(b.Entries, sym)
		}
	}
	for sym := range weights {
		o, ok := prices[sym]
		e, existed := b.Entries[sym]
		if !existed {
			// New position. Weights only change at a rebalance, so the fill is
			// that day's open; the close is a fallback for the impossible case
			// of a name appearing without one.
			price := 0.0
			if ok {
				price = o.Open
				if price <= 0 {
					price = o.Close
				}
			}
			e = Entry{Date: date, Price: price, LastPrice: price, HighPrice: price}
		}
		if ok && o.Close > 0 {
			e.LastPrice = o.Close
			if o.Close > e.HighPrice {
				e.HighPrice = o.Close
			}
		}
		b.Entries[sym] = e
	}
}

// breachedStops lists the positions whose stop was hit at today's close. They
// are sold at tomorrow's open.
//
// A stop that never fires and a stop rule that is switched off look identical
// in the output, so the caller reports Book.Stopped alongside the returns:
// "the stop did not help" and "the stop never triggered" are different claims.
func breachedStops(spec Spec, b *Book, prices map[string]Obs, dayIndex int) map[string]bool {
	out := map[string]bool{}
	if spec.Stop == StopNone {
		return out
	}
	for sym := range b.Holdings {
		e, ok := b.Entries[sym]
		if !ok || e.LastPrice <= 0 {
			continue
		}
		if spec.Stop == StopRandom {
			// Deterministic pseudo-random exits at the configured rate: the
			// control for "would exiting ANYTHING this often have helped?"
			if spec.RandomExitRate > 0 &&
				float64(symKey(sym, spec.RandomSeed+int64(dayIndex))%1_000_000)/1_000_000 < spec.RandomExitRate {
				out[sym] = true
			}
			continue
		}

		var reference, distance float64
		switch spec.Stop {
		case StopFixed:
			reference, distance = e.Price, spec.StopLevel
		case StopTrailing:
			reference, distance = e.HighPrice, spec.StopLevel
		case StopVolFixed:
			reference, distance = e.Price, spec.StopLevel*prices[sym].AnnVol
		case StopVolTrailing:
			reference, distance = e.HighPrice, spec.StopLevel*prices[sym].AnnVol
		}
		if reference <= 0 || distance <= 0 || math.IsNaN(distance) {
			continue
		}
		if e.LastPrice <= reference*(1-distance) {
			out[sym] = true
		}
	}
	return out
}

// ExposurePoint records how much of the book was invested on a given day.
type ExposurePoint struct {
	Date     time.Time
	Exposure float64
}

// exposureFor computes today's exposure from data that ends yesterday.
//
// Both rules can only ever REDUCE exposure — neither borrows. A cash equity
// account cannot lever, and a risk overlay that can double the book is a
// different animal with a different failure mode.
func exposureFor(spec Spec, baseRet []float64, marketIdx []float64) float64 {
	e := 1.0
	if spec.Overlay == OverlayVolScaled || spec.Overlay == OverlayBoth {
		if v := realisedVol(baseRet, spec.VolLookback); v > 0 && spec.TargetVol > 0 {
			e = math.Min(e, spec.TargetVol/v)
		}
	}
	if spec.Overlay == OverlayConstant {
		return math.Max(0, math.Min(1, spec.ConstantExposure))
	}
	if spec.Overlay == OverlayRegimeFloor || spec.Overlay == OverlayBoth {
		if !aboveTrend(marketIdx, spec.TrendWindow) {
			e = 0
		}
	}
	return math.Max(0, math.Min(1, e))
}

// realisedVol annualises the standard deviation of the last `window` daily
// returns. Not enough history yet means no opinion, which reads as "fully
// invested" rather than "flat": an overlay that starts by sitting out because
// it has not warmed up would be making a claim it cannot support.
func realisedVol(rets []float64, window int) float64 {
	if window <= 1 || len(rets) < window {
		return 0
	}
	tail := rets[len(rets)-window:]
	var sum float64
	for _, r := range tail {
		sum += r
	}
	mean := sum / float64(len(tail))
	var ss float64
	for _, r := range tail {
		ss += (r - mean) * (r - mean)
	}
	return math.Sqrt(ss/float64(len(tail)-1)) * math.Sqrt(252)
}

// aboveTrend reports whether the market index closed above its own moving
// average. Same "no opinion until warmed up" rule as realisedVol.
func aboveTrend(idx []float64, window int) bool {
	if window <= 1 || len(idx) < window+1 {
		return true
	}
	tail := idx[len(idx)-window:]
	var sum float64
	for _, v := range tail {
		sum += v
	}
	return idx[len(idx)-1] > sum/float64(len(tail))
}

// unscaledReturn is what the top-quintile book would have earned today with no
// overlay — the series the volatility estimate is built from.
func unscaledReturn(spec Spec, d Day, prices map[string]Obs) float64 {
	w := targetWeights(BookStrategy, spec, d)
	if len(w) == 0 {
		return 0
	}
	return weightedReturn(w, prices, func(o Obs) float64 {
		if o.PrevClose <= 0 || o.Close <= 0 {
			return 0
		}
		return o.Close/o.PrevClose - 1
	})
}

// equalWeightReturn is the eligible universe's own equal-weight move today.
func equalWeightReturn(d Day) float64 {
	var sum float64
	var n int
	for _, o := range d.Obs {
		if !o.Eligible || o.PrevClose <= 0 || o.Close <= 0 {
			continue
		}
		sum += o.Close/o.PrevClose - 1
		n++
	}
	if n == 0 {
		return 0
	}
	return sum / float64(n)
}

// rescale makes the weights sum to `exposure` rather than to 1. The remainder
// is cash and earns nothing.
func rescale(w map[string]float64, exposure float64) map[string]float64 {
	var sum float64
	for _, v := range w {
		sum += v
	}
	out := make(map[string]float64, len(w))
	if sum <= 0 || exposure <= 0 {
		return out
	}
	for k, v := range w {
		out[k] = v / sum * exposure
	}
	return out
}

// changed reports whether two weight sets differ enough to be worth a trade.
// The tolerance is a rounding guard, not a no-trade band: a genuine exposure
// change of any size is acted on, and its cost is charged.
func changed(a, b map[string]float64) bool {
	if len(a) != len(b) {
		return true
	}
	for k, v := range a {
		if math.Abs(b[k]-v) > 1e-12 {
			return true
		}
	}
	return false
}

// targetWeights is where the three books differ, and the only place they do.
func targetWeights(book string, spec Spec, d Day) map[string]float64 {
	w, _ := selectWeights(book, spec, d, nil)
	return w
}

// selectWeights is targetWeights with the rank buffer: `prev` is the strategy
// book's per-variant selection at its last rebalance (nil = none), and the new
// selection is returned alongside the weights.
func selectWeights(book string, spec Spec, d Day, prev []map[string]bool) (map[string]float64, []map[string]bool) {
	var eligible []Obs
	for _, o := range d.Obs {
		if o.Eligible {
			eligible = append(eligible, o)
		}
	}
	if len(eligible) < spec.Quantile {
		return map[string]float64{}, nil
	}
	n := len(eligible) / spec.Quantile
	if spec.HoldCount > 0 {
		n = spec.HoldCount
	}
	if n < 1 {
		n = 1
	}
	if n > len(eligible) {
		n = len(eligible)
	}
	switch book {
	case BookEqual:
		// Own the whole eligible universe: the beta control.
		w := 1 / float64(len(eligible))
		out := make(map[string]float64, len(eligible))
		for _, o := range eligible {
			out[o.Symbol] = w
		}
		return out, nil
	case BookRandom:
		// A book of the same size picked by a fixed per-symbol key: same
		// concentration, no information. The key is drawn once per symbol,
		// not once per day, so the book holds its positions like a real one.
		sort.Slice(eligible, func(i, j int) bool {
			return symKey(eligible[i].Symbol, spec.RandomSeed) < symKey(eligible[j].Symbol, spec.RandomSeed)
		})
	default:
		switch spec.Mode {
		case ModeSwitch:
			if breadthOf(d) < spec.SwitchBreadth {
				sort.Slice(eligible, func(i, j int) bool { return eligible[i].Forecast2 > eligible[j].Forecast2 })
			} else {
				sort.Slice(eligible, func(i, j int) bool { return eligible[i].Forecast > eligible[j].Forecast })
			}
		case ModeBlend:
			return blendWeights(eligible, n), nil
		case ModeBookBlend:
			tops := variantTopsBuffered(eligible, spec, prev)
			return weightsFromTops(tops, spec), membersOf(tops)
		default:
			sort.Slice(eligible, func(i, j int) bool { return eligible[i].Forecast > eligible[j].Forecast })
		}
	}
	var held map[string]bool
	if book == BookStrategy && len(prev) > 0 {
		held = prev[0]
	}
	chosen := pickTop(eligible, n, spec.KeepMultiple, held)
	out := make(map[string]float64, len(chosen))
	for _, sym := range chosen {
		out[sym] = 1 / float64(len(chosen))
	}
	return out, membersOf([][]string{chosen})
}

// pickTop takes the first n names of an already-ranked list, applying the rank
// buffer: names in `held` still ranked within n*keepMultiple are kept (best
// first, at most n), and the rest of the n slots go to the best-ranked names
// not already chosen. With keepMultiple <= 1 or nothing held it is plain top-n.
func pickTop(ranked []Obs, n, keepMultiple int, held map[string]bool) []string {
	if n > len(ranked) {
		n = len(ranked)
	}
	out := make([]string, 0, n)
	chosen := map[string]bool{}
	if keepMultiple > 1 && len(held) > 0 {
		limit := n * keepMultiple
		if limit > len(ranked) {
			limit = len(ranked)
		}
		for _, o := range ranked[:limit] {
			if held[o.Symbol] && len(out) < n {
				out = append(out, o.Symbol)
				chosen[o.Symbol] = true
			}
		}
	}
	for _, o := range ranked {
		if len(out) >= n {
			break
		}
		if !chosen[o.Symbol] {
			out = append(out, o.Symbol)
			chosen[o.Symbol] = true
		}
	}
	return out
}

func membersOf(tops [][]string) []map[string]bool {
	out := make([]map[string]bool, len(tops))
	for v, t := range tops {
		out[v] = make(map[string]bool, len(t))
		for _, s := range t {
			out[v][s] = true
		}
	}
	return out
}

// blendWeights holds half the book from each signal's own top names, every
// day. A name topping both lists is held once, at a double weight, which is
// what "both signals like it" should mean.
func blendWeights(eligible []Obs, n int) map[string]float64 {
	half := n / 2
	if half < 1 {
		half = 1
	}
	out := map[string]float64{}
	add := func(o Obs) { out[o.Symbol] += 1 / float64(2*half) }

	byFirst := append([]Obs(nil), eligible...)
	sort.Slice(byFirst, func(i, j int) bool { return byFirst[i].Forecast > byFirst[j].Forecast })
	for _, o := range byFirst[:min(half, len(byFirst))] {
		add(o)
	}
	bySecond := append([]Obs(nil), eligible...)
	sort.Slice(bySecond, func(i, j int) bool { return bySecond[i].Forecast2 > bySecond[j].Forecast2 })
	for _, o := range bySecond[:min(half, len(bySecond))] {
		add(o)
	}
	return out
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

// symKey is splitmix64 over (symbol, seed): a deterministic random ranking
// that never needs storing and never changes between runs.
func symKey(sym string, seed int64) uint64 {
	x := uint64(seed) * 0x9E3779B97F4A7C15
	for i := 0; i < len(sym); i++ {
		x ^= uint64(sym[i])
		x *= 0x100000001B3
	}
	x ^= x >> 30
	x *= 0xBF58476D1CE4E5B9
	x ^= x >> 27
	x *= 0x94D049BB133111EB
	x ^= x >> 31
	return x
}

func weightedReturn(w map[string]float64, prices map[string]Obs, ret func(Obs) float64) float64 {
	var out float64
	for sym, weight := range w {
		o, ok := prices[sym]
		if !ok {
			continue // did not trade today: the position is stale, not sold
		}
		r := ret(o)
		if math.IsNaN(r) {
			continue
		}
		out += weight * r
	}
	return out
}

// drift moves weights by a price factor and renormalizes against the WHOLE
// portfolio, cash included.
//
// The cash sleeve is why this is not a one-liner. Renormalising the holdings to
// sum to 1 quietly reinvests the cash every single day, and an overlay holding
// 94% then has to sell 6% again the next morning: a book that never traded
// showed a full six points of turnover daily, which cost 3.8% a year and made
// volatility scaling look far worse than it is. Found 2026-09-09, by the cost
// being implausible rather than by the numbers looking wrong.
func drift(w map[string]float64, prices map[string]Obs, factor func(Obs) float64) map[string]float64 {
	out := make(map[string]float64, len(w))
	var invested, grown float64
	for sym, weight := range w {
		f := 1.0
		if o, ok := prices[sym]; ok {
			if v := factor(o); !math.IsNaN(v) && v > 0 {
				f = v
			}
		}
		out[sym] = weight * f
		invested += weight
		grown += out[sym]
	}
	// Cash is whatever was not invested, and it earns nothing.
	cash := math.Max(0, 1-invested)
	total := grown + cash
	if total > 0 {
		for sym := range out {
			out[sym] /= total
		}
	}
	return out
}

func turnoverBetween(from, to map[string]float64) float64 {
	var t float64
	for sym, w := range to {
		t += math.Abs(w - from[sym])
	}
	for sym, w := range from {
		if _, held := to[sym]; !held {
			t += math.Abs(w)
		}
	}
	return t
}

func ordersBetween(date time.Time, book string, from, to map[string]float64, prices map[string]Obs) []Order {
	var out []Order
	seen := map[string]bool{}
	emit := func(sym string, a, b float64) {
		if math.Abs(b-a) < 1e-9 {
			return
		}
		side := "BUY"
		switch {
		case b == 0:
			side = "EXIT"
		case b < a:
			side = "SELL"
		}
		out = append(out, Order{Date: date, Book: book, Symbol: sym, Side: side,
			FromWeight: a, ToWeight: b, FillPrice: prices[sym].Open})
	}
	for sym, w := range to {
		seen[sym] = true
		emit(sym, from[sym], w)
	}
	for sym, w := range from {
		if !seen[sym] {
			emit(sym, w, 0)
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Side != out[j].Side {
			return out[i].Side < out[j].Side
		}
		return out[i].Symbol < out[j].Symbol
	})
	return out
}

// nextRebalanceIndexDate reports the date of the next rebalance if it has
// already happened in the data, or the zero time when it is still in the
// future — the caller turns that into "in N trading days".
func nextRebalanceIndexDate(days []Day, every int) time.Time {
	last := len(days) - 1
	next := (last/every + 1) * every
	if next < len(days) {
		return days[next].Date
	}
	return time.Time{}
}

// DaysToNextRebalance counts trading days from the last computed day to the
// next scheduled rebalance.
func (t *Track) DaysToNextRebalance() int {
	if len(t.Dates) == 0 {
		return 0
	}
	last := len(t.Dates) - 1
	next := (last/t.Spec.RebalanceEvery + 1) * t.Spec.RebalanceEvery
	return next - last
}

// Pending computes the order sheet for the NEXT session: the trades that move
// `current` to the spec's target, priced off the most recent close available.
//
// It exists because the useful daily artifact is not the history, it is the
// list of what to place at tomorrow's open — and that list has to be
// computable on day one, when the forward record is still empty and Compute
// has nothing to walk.
//
// `due` says whether the schedule actually calls for a rebalance next session.
// The sheet is produced either way: seeing what the strategy WOULD buy on a
// hold day is how a human keeps an eye on it without touching it.
type PendingSheet struct {
	BasedOn   time.Time // the close these forecasts come from
	Due       bool      // is the next session a scheduled rebalance
	DaysToDue int
	Orders    []Order
	Target    map[string]float64
}

// Pending takes the strategy book's last selection (Track.Members) so a
// rank-buffered strategy's live orders follow the same rule as its record; a
// strategy without a buffer ignores it.
func Pending(spec Spec, latest Day, current map[string]float64, daysToDue int, members ...[]map[string]bool) PendingSheet {
	var prev []map[string]bool
	if len(members) > 0 {
		prev = members[0]
	}
	target, _ := selectWeights(BookStrategy, spec, latest, prev)
	prices := make(map[string]Obs, len(latest.Obs))
	for _, o := range latest.Obs {
		prices[o.Symbol] = o
	}
	return PendingSheet{
		BasedOn:   latest.Date,
		Due:       daysToDue <= 1,
		DaysToDue: daysToDue,
		Orders:    ordersBetween(latest.Date, BookStrategy, current, target, prices),
		Target:    target,
	}
}

// variantTops returns, for each variant, the symbols in its own top quantile
// of the eligible names that variant can score. A variant with too few such
// names that day has no book (nil).
func variantTops(eligible []Obs, spec Spec) [][]string {
	return variantTopsBuffered(eligible, spec, nil)
}

// variantTopsBuffered is variantTops with the rank buffer applied per variant,
// against that variant's own previous selection.
func variantTopsBuffered(eligible []Obs, spec Spec, prev []map[string]bool) [][]string {
	out := make([][]string, len(spec.Variants))
	for v := range spec.Variants {
		var have []Obs
		for _, o := range eligible {
			if v < len(o.Signals) && !math.IsNaN(o.Signals[v]) {
				have = append(have, o)
			}
		}
		if len(have) < spec.Quantile {
			continue
		}
		sort.Slice(have, func(i, j int) bool {
			if have[i].Signals[v] != have[j].Signals[v] {
				return have[i].Signals[v] > have[j].Signals[v]
			}
			return have[i].Symbol < have[j].Symbol
		})
		n := len(have) / spec.Quantile
		if n < 1 {
			n = 1
		}
		var held map[string]bool
		if v < len(prev) {
			held = prev[v]
		}
		out[v] = pickTop(have, n, spec.KeepMultiple, held)
	}
	return out
}

// bookBlendWeights holds each variant's own top quantile at that variant's
// weight. A name topping several variants is held once at the summed weight;
// a variant with no book yet (warm-up) hands its share to the others rather
// than to cash.
func bookBlendWeights(eligible []Obs, spec Spec) map[string]float64 {
	return weightsFromTops(variantTops(eligible, spec), spec)
}

func weightsFromTops(tops [][]string, spec Spec) map[string]float64 {
	var wsum float64
	for v, t := range tops {
		if len(t) > 0 {
			wsum += spec.VariantWeights[v]
		}
	}
	out := map[string]float64{}
	if wsum <= 0 {
		return out
	}
	for v, t := range tops {
		for _, s := range t {
			out[s] += spec.VariantWeights[v] / wsum / float64(len(t))
		}
	}
	return out
}

// Qualification is one stock qualifying for one variant of a strategy on a
// decision day: in that variant's top quantile of the eligible universe.
// Recorded for every strategy, so the screener can show, for any stock, every
// strategy and variant it currently qualifies for.
type Qualification struct {
	Symbol  string
	Variant string
}

// Qualify lists a day's qualifications. A single-signal strategy has one
// variant, its signal; a book-blend has one per variant.
func Qualify(spec Spec, d Day) []Qualification {
	var out []Qualification
	if spec.Mode == ModeBookBlend {
		var eligible []Obs
		for _, o := range d.Obs {
			if o.Eligible {
				eligible = append(eligible, o)
			}
		}
		for v, t := range variantTops(eligible, spec) {
			for _, s := range t {
				out = append(out, Qualification{Symbol: s, Variant: spec.Variants[v]})
			}
		}
	} else {
		name := spec.Signal
		if name == "" {
			name = spec.Name
		}
		for s := range targetWeights(BookStrategy, spec, d) {
			out = append(out, Qualification{Symbol: s, Variant: name})
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Symbol != out[j].Symbol {
			return out[i].Symbol < out[j].Symbol
		}
		return out[i].Variant < out[j].Variant
	})
	return out
}

// TargetWeights is the strategy book's target on a decision day — what a
// capital-aware simulation sizes in rupees.
func TargetWeights(spec Spec, d Day) map[string]float64 {
	return targetWeights(BookStrategy, spec, d)
}
