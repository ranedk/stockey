package research

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func d(y, m, day int) time.Time { return time.Date(y, time.Month(m), day, 0, 0, 0, 0, time.UTC) }

func TestWalkForwardExpandingWindows(t *testing.T) {
	ws := WalkForward(d(2012, 1, 1), d(2019, 12, 31), 4, 1)
	if len(ws) == 0 {
		t.Fatal("no windows")
	}
	for i, w := range ws {
		if !w.FitStart.Equal(d(2012, 1, 1)) {
			t.Fatalf("window %d: fit start moved (expanding windows only, Law 4)", i)
		}
		if !w.FitEnd.Before(w.ValEnd) {
			t.Fatalf("window %d: validation must follow fit (%v ≥ %v)", i, w.FitEnd, w.ValEnd)
		}
		if i > 0 && !ws[i-1].ValEnd.Equal(w.FitEnd) {
			t.Fatalf("window %d: fit must absorb prior validation period", i)
		}
	}
	last := ws[len(ws)-1]
	if !last.ValEnd.Equal(d(2019, 12, 31)) {
		t.Fatalf("last window should reach train end, got %v", last.ValEnd)
	}
}

func TestHoldoutBurnsExactlyOnce(t *testing.T) {
	path := filepath.Join(t.TempDir(), "burns.json")
	r, err := OpenRegistry(path)
	if err != nil {
		t.Fatal(err)
	}
	if r.Burned("partB-2020-2024") {
		t.Fatal("fresh holdout reported burned")
	}
	if err := r.Burn("partB-2020-2024", "cfg-abc123", "ensemble v1"); err != nil {
		t.Fatal(err)
	}
	if err := r.Burn("partB-2020-2024", "cfg-def456", "second try"); err == nil {
		t.Fatal("second burn of the same holdout MUST fail (Law 4)")
	}
	// Persistence: a fresh registry from the same file still refuses.
	r2, err := OpenRegistry(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := r2.Burn("partB-2020-2024", "cfg-xyz", ""); err == nil {
		t.Fatal("burn registry must persist across processes")
	}
}

func TestCountMWithBatches(t *testing.T) {
	ledger := `# Ledger
| # | Date | Rule / variation | Story | Dataset | Result | Decision |
|---|------|------------------|-------|---------|--------|----------|
| 1 | 2026-07-01 | ewmac16 | under-reaction | nifty | t=1.2 | keep |
| 2 | 2026-07-02 | timesfm scan (trials=30) | forecastability sweep | fno stocks | 2 survivors | investigate |
`
	path := filepath.Join(t.TempDir(), "LEDGER.md")
	if err := os.WriteFile(path, []byte(ledger), 0o644); err != nil {
		t.Fatal(err)
	}
	m, err := CountM(path)
	if err != nil {
		t.Fatal(err)
	}
	if m != 31 { // 1 + 30
		t.Fatalf("CountM = %d, want 31 (batch row counts its trials)", m)
	}
}

func TestSplitFirewall(t *testing.T) {
	s := Split{
		TrainStart: d(2012, 1, 1), TrainEnd: d(2019, 12, 31),
		HoldoutStart: d(2020, 1, 1), HoldoutEnd: d(2024, 12, 31),
	}
	if !s.InTrain(d(2015, 6, 1)) || s.InTrain(d(2020, 6, 1)) {
		t.Fatal("train membership wrong")
	}
	if !s.InHoldout(d(2022, 1, 1)) || s.InHoldout(d(2019, 12, 31)) {
		t.Fatal("holdout membership wrong")
	}
}

func TestCountM_SumsEveryTrialsDeclarationOnARow(t *testing.T) {
	// Row 9 of the real ledger reports two batches on one line. Counting only
	// the first understated M by 174 trials.
	dir := t.TempDir()
	path := filepath.Join(dir, "LEDGER.md")
	body := `| # | Date | Rule | Story | Dataset | Result | Decision |
|---|------|------|-------|---------|--------|----------|
| 1 | d | plain row, no declaration | s | ds | r | dec |
| 2 | d | two batches (trials=585) and (trials=174) | s | ds | r | dec |
| 3 | d | characterisation only, trials=0 | s | ds | r | dec |
`
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := CountM(path)
	if err != nil {
		t.Fatal(err)
	}
	if want := 1 + 585 + 174 + 0; got != want {
		t.Errorf("CountM = %d, want %d (1 undeclared + 585 + 174 + an explicit zero)", got, want)
	}
}
