// Command run is the daily production runner (Ch-15 "daily process"):
// load config + latest data → compute today's target positions → print the
// order sheet. It never places orders itself in v1 (open question B8).
//
// Usage: run -config config.json -data ./data
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"

	"github.com/ranedk/systrader/internal/combine"
	"github.com/ranedk/systrader/internal/core"
	"github.com/ranedk/systrader/internal/data"
	"github.com/ranedk/systrader/internal/portfolio"
	"github.com/ranedk/systrader/internal/rules"
	"github.com/ranedk/systrader/internal/sizing"
)

type InstrumentCfg struct {
	Symbol          string  `json:"symbol"`
	File            string  `json:"file"`      // CSV under -data dir
	CarryFile       string  `json:"carryFile"` // optional: date,annCarryPriceUnits
	PointValue      float64 `json:"pointValue"`
	Block           float64 `json:"block"`
	LongOnly        bool    `json:"longOnly"`
	Weight          float64 `json:"weight"`
	SpreadPoints    float64 `json:"spreadPoints"`
	FeePerBlock     float64 `json:"feePerBlock"`
	PercentValueFee float64 `json:"percentValueFee"`
	CurrentPosition float64 `json:"currentPosition"` // what you hold now
}

type RuleCfg struct {
	Name   string  `json:"name"` // "ewmac16", "ewmac32", "ewmac64", "carry", ...
	Weight float64 `json:"weight"`
}

type Cfg struct {
	Capital      float64         `json:"capital"`
	VolTargetPct float64         `json:"volTargetPct"`
	FDM          float64         `json:"fdm"`
	IDM          float64         `json:"idm"`
	Rules        []RuleCfg       `json:"rules"`
	Instruments  []InstrumentCfg `json:"instruments"`
}

func main() {
	cfgPath := flag.String("config", "config.json", "config file")
	dataDir := flag.String("data", "data", "directory with CSV price files")
	flag.Parse()

	raw, err := os.ReadFile(*cfgPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "No config found (%v).\n\nCreate %s per the schema in cmd/run/main.go,\nput daily CSVs (date,open,high,low,close,volume) in %s/, then rerun.\n",
			err, *cfgPath, *dataDir)
		os.Exit(1)
	}
	var cfg Cfg
	if err := json.Unmarshal(raw, &cfg); err != nil {
		fatal("parsing config: %v", err)
	}
	if cfg.VolTargetPct <= 0 || cfg.VolTargetPct > 0.5 {
		fatal("volTargetPct %.2f violates Law 10 (0 < t ≤ 0.5)", cfg.VolTargetPct)
	}

	dailyTarget := sizing.DailyCashVolTarget(cfg.Capital, cfg.VolTargetPct)
	fmt.Printf("Capital ₹%.0f | vol target %.0f%% | daily cash vol target ₹%.0f\n\n",
		cfg.Capital, cfg.VolTargetPct*100, dailyTarget)
	fmt.Printf("%-12s %9s %9s %10s %10s %10s %8s\n",
		"symbol", "price", "forecast", "volScalar", "target", "current", "TRADE")

	for _, ic := range cfg.Instruments {
		prices, err := data.LoadCSV(filepath.Join(*dataDir, ic.File))
		if err != nil {
			fatal("loading %s: %v", ic.Symbol, err)
		}
		inst := &data.Instrument{
			Meta: data.Meta{Symbol: ic.Symbol, PointValue: ic.PointValue,
				Block: ic.Block, LongOnly: ic.LongOnly, SpreadPoints: ic.SpreadPoints,
				FeePerBlock: ic.FeePerBlock, PercentValueFee: ic.PercentValueFee},
			Prices: prices,
		}
		if ic.CarryFile != "" {
			cs, err := data.LoadCSV(filepath.Join(*dataDir, ic.CarryFile))
			if err != nil {
				fatal("loading carry for %s: %v", ic.Symbol, err)
			}
			inst.AnnCarry = &cs
		}

		vol := core.PriceUnitVol(prices, 36, 10)
		fcs := make([]core.Series, len(cfg.Rules))
		wts := make([]float64, len(cfg.Rules))
		for i, rc := range cfg.Rules {
			fcs[i] = rules.Forecast(mustRule(rc.Name), inst, vol)
			wts[i] = rc.Weight
		}
		comb := combine.Combined(fcs, wts, cfg.FDM)

		f, okF := comb.Last()
		v, okV := vol.Last()
		price := prices.Values[prices.Len()-1]
		if !okF || !okV {
			fmt.Printf("%-12s insufficient data (need ~%d+ bars)\n", ic.Symbol, 260)
			continue
		}
		vs := sizing.VolScalar(dailyTarget, v*ic.PointValue)
		target := portfolio.Target(sizing.Subsystem(vs, f), ic.Weight, cfg.IDM)
		newPos := portfolio.ApplyInertia(ic.CurrentPosition, target, ic.Block)
		trade := newPos - ic.CurrentPosition
		action := "HOLD"
		if trade != 0 {
			action = fmt.Sprintf("%+.0f", trade)
		}
		if !portfolio.PassesFourBlockTest(vs, ic.Weight, cfg.IDM, ic.Block) {
			action += " ⚠4blk"
		}
		fmt.Printf("%-12s %9.1f %9.1f %10.2f %10.2f %10.1f %8s\n",
			ic.Symbol, price, f, vs, target, ic.CurrentPosition, action)
	}
}

func mustRule(name string) rules.Rule {
	switch name {
	case "ewmac2":
		return rules.EWMAC{Fast: 2}
	case "ewmac4":
		return rules.EWMAC{Fast: 4}
	case "ewmac8":
		return rules.EWMAC{Fast: 8}
	case "ewmac16":
		return rules.EWMAC{Fast: 16}
	case "ewmac32":
		return rules.EWMAC{Fast: 32}
	case "ewmac64":
		return rules.EWMAC{Fast: 64}
	case "carry":
		return rules.Carry{}
	}
	fatal("unknown rule %q", name)
	return nil
}

func fatal(format string, args ...any) {
	fmt.Fprintf(os.Stderr, format+"\n", args...)
	os.Exit(1)
}
