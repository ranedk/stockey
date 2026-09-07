package coi

import (
	"math/rand"
	"testing"
	"time"

	"github.com/ranedk/systrader/internal/bars"
)

func mk(o, h, l, c float64, day int) bars.Bar {
	return bars.Bar{
		Date: time.Date(2020, 1, 1, 0, 0, 0, 0, time.UTC).AddDate(0, 0, day),
		Open: o, High: h, Low: l, Close: c, Vol: 1e6,
	}
}

// filler bars drift gently upward so nothing else in the window trips the
// pattern; the test injects the setup it wants to see.
func filler(n int, start float64) []bars.Bar {
	out := make([]bars.Bar, n)
	p := start
	for i := range out {
		p += 0.5
		out[i] = mk(p, p+0.4, p-0.4, p+0.2, i)
	}
	return out
}

func TestDetectFindsTextbookSetup(t *testing.T) {
	b := filler(40, 100)
	n := len(b)
	// C-1: a down candle. C0: lower high AND lower low. C+1: higher high,
	// higher low, and closes above C0's high.
	b = append(b,
		mk(130, 131, 126, 127, n),   // C-1 down candle
		mk(127, 129, 124, 125, n+1), // C0 pivot low
		mk(126, 133, 125.5, 132, n+2),
	)
	b = append(b, filler(3, 132)...)

	p := DefaultParams()
	ind := Compute(b, p)
	got := Detect(bars.Series{Symbol: "TEST", Bars: b}, ind, p)
	if len(got) != 1 {
		t.Fatalf("want exactly 1 setup, got %d", len(got))
	}
	if got[0].C0 != n+1 {
		t.Fatalf("want C0 at %d, got %d", n+1, got[0].C0)
	}
}

func TestDetectRejectsWeakConfirmation(t *testing.T) {
	b := filler(40, 100)
	n := len(b)
	// Same setup, but C+1 closes BELOW C0's high — no change of intent.
	b = append(b,
		mk(130, 131, 126, 127, n),
		mk(127, 129, 124, 125, n+1),
		mk(126, 130, 125.5, 128.5, n+2), // close 128.5 < C0 high 129
	)
	b = append(b, filler(3, 128)...)

	p := DefaultParams()
	if got := Detect(bars.Series{Symbol: "TEST", Bars: b}, Compute(b, p), p); len(got) != 0 {
		t.Fatalf("want no setup when C+1 fails to close above C0's high, got %d", len(got))
	}
}

// TestNoLookAhead is the property TRADING_BIBLE.md Part IV.3 demands: a setup
// confirmed at bar j must be detectable from data ending at bar j, and must
// not change when later bars arrive.
//
// It matters here more than usual because the original screener finds pivots
// by scanning BACKWARDS from the end of the series with a running-minimum
// state machine. Whether a given bar is emitted as a pivot can depend on the
// state the scan was in when it reached that bar — which depends on where the
// scan started, which is the future.
func TestNoLookAhead(t *testing.T) {
	for _, p := range []Params{
		{MACDFast: 6, MACDSlow: 13, MACDSignal: 5, BBWindow: 20, BBMult: 2, EMAShort: 10, EMALong: 20, RescanTerminator: false},
		{MACDFast: 6, MACDSlow: 13, MACDSignal: 5, BBWindow: 20, BBMult: 2, EMAShort: 10, EMALong: 20, RescanTerminator: true},
	} {
		name := "original"
		if p.RescanTerminator {
			name = "rescan-fixed"
		}
		t.Run(name, func(t *testing.T) {
			rng := rand.New(rand.NewSource(7))
			b := randomWalk(rng, 600)
			full := setupIndex(bars.Series{Symbol: "T", Bars: b}, p)

			mismatches := 0
			for cut := 300; cut < len(b); cut += 7 {
				prefix := b[:cut]
				got := setupIndex(bars.Series{Symbol: "T", Bars: prefix}, p)
				for c0 := range got {
					// Only setups fully confirmed inside the prefix are
					// comparable; the last bar can still change.
					if c0+1 >= cut-1 {
						continue
					}
					if !full[c0] {
						mismatches++
					}
				}
				for c0 := range full {
					if c0+1 < cut-1 && !got[c0] {
						mismatches++
					}
				}
			}
			if mismatches > 0 {
				t.Errorf("%d setups changed when future bars were added or removed — the detector is not causal", mismatches)
			}
		})
	}
}

func setupIndex(s bars.Series, p Params) map[int]bool {
	return setupIndexVariant(s, p, Full)
}

func setupIndexVariant(s bars.Series, p Params, v Variant) map[int]bool {
	out := map[int]bool{}
	for _, x := range DetectVariant(s, Compute(s.Bars, p), p, v) {
		out[x.C0] = true
	}
	return out
}

// TestVariantsAreCausal repeats the look-ahead property for every relaxation
// of the pattern. Falling is the one that matters most: it claims to be
// knowable a full day earlier than the others, so a leak there would show up
// as an edge that no live system could ever have captured.
func TestVariantsAreCausal(t *testing.T) {
	p := DefaultParams()
	for _, v := range []Variant{Full, Weak, Early, Falling} {
		t.Run(v.String(), func(t *testing.T) {
			rng := rand.New(rand.NewSource(11))
			b := randomWalk(rng, 500)
			full := setupIndexVariant(bars.Series{Symbol: "T", Bars: b}, p, v)
			bad := 0
			for cut := 260; cut < len(b); cut += 11 {
				got := setupIndexVariant(bars.Series{Symbol: "T", Bars: b[:cut]}, p, v)
				for c0 := range got {
					if c0+2 < cut && !full[c0] {
						bad++
					}
				}
				for c0 := range full {
					if c0+2 < cut && !got[c0] {
						bad++
					}
				}
			}
			if bad > 0 {
				t.Errorf("%s: %d setups changed with future data — not causal", v, bad)
			}
		})
	}
}

// TestFallingConfirmsEarlier pins the timing claim that makes the "enter
// earlier" experiment meaningful: Falling is knowable at C0's own close,
// every other variant needs the bar after it.
func TestFallingConfirmsEarlier(t *testing.T) {
	rng := rand.New(rand.NewSource(3))
	b := randomWalk(rng, 400)
	p := DefaultParams()
	ind := Compute(b, p)
	s := bars.Series{Symbol: "T", Bars: b}

	for _, v := range []Variant{Full, Weak, Early} {
		for _, x := range DetectVariant(s, ind, p, v) {
			if x.Confirm != x.C0+1 {
				t.Fatalf("%s: want Confirm == C0+1, got %d vs C0 %d", v, x.Confirm, x.C0)
			}
		}
	}
	got := DetectVariant(s, ind, p, Falling)
	if len(got) == 0 {
		t.Fatal("falling variant found nothing on a 400-bar random walk")
	}
	for _, x := range got {
		if x.Confirm != x.C0 {
			t.Fatalf("falling: want Confirm == C0, got %d vs %d", x.Confirm, x.C0)
		}
	}
}

func randomWalk(rng *rand.Rand, n int) []bars.Bar {
	out := make([]bars.Bar, n)
	px := 100.0
	for i := range out {
		px *= 1 + rng.NormFloat64()*0.02
		if px < 1 {
			px = 1
		}
		o := px * (1 + rng.NormFloat64()*0.005)
		c := px * (1 + rng.NormFloat64()*0.005)
		h := max64(o, c) * (1 + absf(rng.NormFloat64())*0.005)
		l := min64(o, c) * (1 - absf(rng.NormFloat64())*0.005)
		out[i] = mk(o, h, l, c, i)
	}
	return out
}

func absf(x float64) float64 {
	if x < 0 {
		return -x
	}
	return x
}
func max64(a, b float64) float64 {
	if a > b {
		return a
	}
	return b
}
func min64(a, b float64) float64 {
	if a < b {
		return a
	}
	return b
}
