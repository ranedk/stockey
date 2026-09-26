package stageapi

import (
	"math"
	"testing"
)

// List's own DB-orchestration logic isn't unit-tested here -- it takes a
// concrete *store.Store with no seam to fake, same as cmd/stage/main.go's
// equally untested store-touching report() function. Smoke-tested against
// the real local DB instead (2026-08-29: 2123/3082 symbols classified,
// 959 excluded stale, stage counts 523/806/254/540 -- sane distribution).
// The pure helpers below are worth locking down regardless.

func TestNanToNil(t *testing.T) {
	if got := nanToNil(math.NaN()); got != nil {
		t.Errorf("nanToNil(NaN) = %v, want nil", got)
	}
	got := nanToNil(1.5)
	if got == nil || *got != 1.5 {
		t.Errorf("nanToNil(1.5) = %v, want pointer to 1.5", got)
	}
}

func TestItoa(t *testing.T) {
	cases := map[int]string{0: "0", 1: "1", 4: "4", 9: "9"}
	for in, want := range cases {
		if got := itoa(in); got != want {
			t.Errorf("itoa(%d) = %q, want %q", in, got, want)
		}
	}
	if got := itoa(10); got != "?" {
		t.Errorf("itoa(10) = %q, want \"?\" (stage numbers never reach double digits)", got)
	}
}
