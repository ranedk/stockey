package dhan

import (
	"testing"
	"time"
)

// The regression this file exists for: every bar in systrader_ohlcv_daily was
// filed one calendar day early because the epoch was read through time.Local
// on a UTC host. A fifth of them landed on Sundays.
func TestBarDate_UsesTheISTCalendarDayNotTheHostClock(t *testing.T) {
	// Midnight IST on Friday 2026-08-07 is 18:30 UTC on Thursday the 6th —
	// the exact timestamp that produced the corruption.
	epoch := float64(time.Date(2026, 8, 7, 0, 0, 0, 0, IST).Unix())
	got := BarDate(epoch)
	want := time.Date(2026, 8, 7, 0, 0, 0, 0, time.UTC)
	if !got.Equal(want) {
		t.Errorf("BarDate = %s, want %s", got.Format("2006-01-02"), want.Format("2006-01-02"))
	}
}

func TestBarDate_MondayBarNeverLandsOnSunday(t *testing.T) {
	// Monday 2026-08-10, the bar that used to be stamped Sunday the 9th.
	for _, hour := range []int{0, 9, 15, 23} {
		epoch := float64(time.Date(2026, 8, 10, hour, 15, 0, 0, IST).Unix())
		got := BarDate(epoch)
		if wd := got.Weekday(); wd == time.Sunday || wd == time.Saturday {
			t.Errorf("bar at %02d:15 IST filed on a %s (%s)", hour, wd, got.Format("2006-01-02"))
		}
		if got.Day() != 10 {
			t.Errorf("bar at %02d:15 IST filed on day %d, want 10", hour, got.Day())
		}
	}
}

func TestBarDate_IsIndependentOfTheHostTimezone(t *testing.T) {
	// Whatever the host is set to, the answer must not move.
	epoch := float64(time.Date(2026, 8, 7, 9, 15, 0, 0, IST).Unix())
	want := BarDate(epoch)
	for _, zone := range []*time.Location{time.UTC, time.FixedZone("HST", -10*3600), time.FixedZone("NZST", 12*3600)} {
		saved := time.Local
		time.Local = zone
		got := BarDate(epoch)
		time.Local = saved
		if !got.Equal(want) {
			t.Errorf("host in %v gave %s, want %s", zone, got.Format("2006-01-02"), want.Format("2006-01-02"))
		}
	}
}
