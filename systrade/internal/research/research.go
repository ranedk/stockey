// Package research enforces the experimental discipline of the trading bible
// and adaptive_ensemble_strategy.md §3: train/holdout firewalls, walk-forward
// windows inside the train set, a persistent holdout-burn registry, and
// multiple-testing (M) accounting against research/LEDGER.md.
//
// The point of this package is that discipline lives in CODE, not in memory:
// a holdout cannot be run twice by accident, and M cannot silently reset.
package research

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"time"
)

// Split is a hard train/verification firewall. All exploration happens in
// [TrainStart, TrainEnd]; the holdout is run once, frozen, then burned.
type Split struct {
	TrainStart   time.Time
	TrainEnd     time.Time
	HoldoutStart time.Time
	HoldoutEnd   time.Time
}

func (s Split) InTrain(t time.Time) bool {
	return !t.Before(s.TrainStart) && !t.After(s.TrainEnd)
}

func (s Split) InHoldout(t time.Time) bool {
	return !t.Before(s.HoldoutStart) && !t.After(s.HoldoutEnd)
}

// Window is one walk-forward fold inside the train set: fit on
// [FitStart, FitEnd], validate on (ValStart, ValEnd]. Without a purge
// ValStart equals FitEnd; with one, the gap between them is the purge.
type Window struct {
	FitStart, FitEnd, ValStart, ValEnd time.Time
}

// InFit reports whether t may be used to fit this fold.
func (w Window) InFit(t time.Time) bool {
	return !t.Before(w.FitStart) && !t.After(w.FitEnd)
}

// InVal reports whether t is scored in this fold.
func (w Window) InVal(t time.Time) bool {
	return t.After(w.ValStart) && !t.After(w.ValEnd)
}

// WalkForward builds expanding-window folds: fit ends grow by stepYears,
// each validated on the following stepYears. (Law 4: expanding windows,
// out-of-sample only.)
func WalkForward(trainStart, trainEnd time.Time, minFitYears, stepYears int) []Window {
	var out []Window
	fitEnd := trainStart.AddDate(minFitYears, 0, 0)
	for {
		valEnd := fitEnd.AddDate(stepYears, 0, 0)
		if valEnd.After(trainEnd) {
			valEnd = trainEnd
		}
		if !fitEnd.Before(valEnd) {
			break
		}
		out = append(out, Window{FitStart: trainStart, FitEnd: fitEnd, ValStart: fitEnd, ValEnd: valEnd})
		if valEnd.Equal(trainEnd) {
			break
		}
		fitEnd = valEnd
	}
	return out
}

// PurgedWalkForward is WalkForward with the fit window cut back purgeDays
// calendar days before each validation block begins (amended Law 4). A fit
// observation's label is its FORWARD return; one dated inside the last
// horizon before the block has a label that reaches into it, so fitting on it
// would score the fold partly on data it was trained on. Set purgeDays to at
// least the label horizon in calendar days (a 20-trading-day return needs
// ~30). The validation blocks are unchanged, so they still tile the sample.
//
// No embargo after the block is needed: windows only expand forward, so no
// fit observation ever comes after a validation block it could leak from.
func PurgedWalkForward(trainStart, trainEnd time.Time, minFitYears, stepYears, purgeDays int) []Window {
	ws := WalkForward(trainStart, trainEnd, minFitYears, stepYears)
	for i := range ws {
		ws[i].FitEnd = ws[i].ValStart.AddDate(0, 0, -purgeDays)
	}
	return ws
}

// --- Holdout burn registry ---------------------------------------------------

type burn struct {
	Name       string    `json:"name"`
	ConfigHash string    `json:"configHash"`
	BurnedAt   time.Time `json:"burnedAt"`
	Note       string    `json:"note,omitempty"`
}

// Registry persists holdout burns to a JSON file that should live in the
// repo (research/holdout_burns.json) and be committed: deleting it is the
// same self-deception as deleting LEDGER rows.
type Registry struct {
	path  string
	burns []burn
}

func OpenRegistry(path string) (*Registry, error) {
	r := &Registry{path: path}
	raw, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return r, nil
	}
	if err != nil {
		return nil, err
	}
	if err := json.Unmarshal(raw, &r.burns); err != nil {
		return nil, fmt.Errorf("research: burn registry corrupt (%s): %w", path, err)
	}
	return r, nil
}

// Burned reports whether a named holdout has already been consumed.
func (r *Registry) Burned(name string) bool {
	for _, b := range r.burns {
		if b.Name == name {
			return true
		}
	}
	return false
}

// Burn records a holdout run. It FAILS if the holdout was already burned —
// a second run is not verification, it is iteration on the holdout (Law 4).
func (r *Registry) Burn(name, configHash, note string) error {
	for _, b := range r.burns {
		if b.Name == name {
			return fmt.Errorf("research: holdout %q was burned %s (config %s) — it is contaminated; genuinely new verification needs new out-of-sample time",
				name, b.BurnedAt.Format("2006-01-02"), b.ConfigHash)
		}
	}
	r.burns = append(r.burns, burn{Name: name, ConfigHash: configHash, BurnedAt: time.Now(), Note: note})
	raw, err := json.MarshalIndent(r.burns, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(r.path, raw, 0o644)
}

// --- LEDGER M-accounting -----------------------------------------------------

// CountM counts experiment rows in research/LEDGER.md (markdown table rows
// after the header/separator). Batch rows may declare "trials=N" in the story
// column to count a whole scan (e.g. a TimesFM forecastability sweep over 200
// symbols) as N trials, per law_1_story.md batching.
//
// Two details that a naive reading gets wrong, both found by comparing this
// counter against the M values written into the ledger's own prose:
//
//   - A row may declare trials MORE THAN ONCE when it reports two batches
//     (row 9 does: 585 monitor runs and 174 split runs). Every declaration on
//     the row counts. Reading only the first silently discarded 174 trials.
//   - "trials=0" is legal and means zero. Rows that consult no performance
//     number — a forecast-scalar fit, a correlation study — test no hypothesis
//     and must not inflate the bar. A row with no declaration at all still
//     counts 1: an ordinary experiment.
func CountM(ledgerPath string) (int, error) {
	f, err := os.Open(ledgerPath)
	if err != nil {
		return 0, err
	}
	defer f.Close()
	m := 0
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 64*1024), 4*1024*1024) // ledger rows run long
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if !strings.HasPrefix(line, "|") || strings.HasPrefix(line, "|-") ||
			strings.HasPrefix(line, "| #") || strings.HasPrefix(line, "|--") ||
			strings.Contains(line, "---|") {
			continue
		}
		declared := false
		rest := line
		for {
			k := strings.Index(rest, "trials=")
			if k < 0 {
				break
			}
			rest = rest[k+len("trials="):]
			var n int
			if _, err := fmt.Sscanf(rest, "%d", &n); err != nil || n < 0 {
				continue
			}
			declared = true
			m += n
		}
		if !declared {
			m++
		}
	}
	return m, sc.Err()
}

// AppendTrial appends one experiment row to the LEDGER (append-only; the
// file's own rules forbid deletion). Set trials>1 for a batch row.
func AppendTrial(ledgerPath, rule, story, dataset, result, decision string, trials int) error {
	f, err := os.OpenFile(ledgerPath, os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	defer f.Close()
	if trials > 1 {
		story = fmt.Sprintf("%s (trials=%d)", story, trials)
	}
	_, err = fmt.Fprintf(f, "| - | %s | %s | %s | %s | %s | %s |\n",
		time.Now().Format("2006-01-02"), rule, story, dataset, result, decision)
	return err
}
