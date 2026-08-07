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
// [FitStart, FitEnd], validate on (FitEnd, ValEnd].
type Window struct {
	FitStart, FitEnd, ValEnd time.Time
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
		out = append(out, Window{FitStart: trainStart, FitEnd: fitEnd, ValEnd: valEnd})
		if valEnd.Equal(trainEnd) {
			break
		}
		fitEnd = valEnd
	}
	return out
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
// after the header/separator). Batch rows may declare "trials=N" in the
// story column to count a whole scan (e.g. a TimesFM forecastability sweep
// over 200 symbols) as N trials, per law_1_story.md batching.
func CountM(ledgerPath string) (int, error) {
	f, err := os.Open(ledgerPath)
	if err != nil {
		return 0, err
	}
	defer f.Close()
	m := 0
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if !strings.HasPrefix(line, "|") || strings.HasPrefix(line, "|-") ||
			strings.HasPrefix(line, "| #") || strings.HasPrefix(line, "|--") ||
			strings.Contains(line, "---|") {
			continue
		}
		n := 1
		if k := strings.Index(line, "trials="); k >= 0 {
			fmt.Sscanf(line[k:], "trials=%d", &n)
			if n < 1 {
				n = 1
			}
		}
		m += n
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
