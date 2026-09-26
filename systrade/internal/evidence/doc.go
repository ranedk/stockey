// Package evidence is the statistics half of the matched-control harness: the
// part that turns a strategy's returns and its controls' returns into a
// verdict.
//
// The controls themselves stay where the books are built — internal/sleeve
// (equal-weight, shuffled, stable-shuffle), internal/paper (equal-weight,
// random ranking), cmd/carver (always-long, time-shifted forecasts) — because
// a control must be traded by the same code as the book it controls, or it is
// not matched. What was missing was the arithmetic docs/RESEARCH_PROTOCOL.md
// requires of a confirmation; every command had been reinventing a plain
// paired t-test instead:
//
//   - a stationary block bootstrap, because month-to-month edges are
//     autocorrelated and an i.i.d. test on them overstates confidence;
//   - the paired edge against a control, with a bootstrap interval and p;
//   - the matched-risk comparison LEDGER row 24 made standard: a variant that
//     raises volatility along with return must be read at the incumbent's risk;
//   - the deflated Sharpe ratio (Bailey & López de Prado 2014) against the
//     number of configurations tried in the family. NOT backtest.Metrics'
//     old "DeflatedSR", which was Law 7's flat 0.75 haircut and is now named
//     HaircutSR, for what it is;
//   - Benjamini-Hochberg FDR within the family (amended Law 2, q = 10%);
//   - Carver's bootstrap weight estimation (appendix C), as the cross-check
//     on handcrafted weights (Law 6) — weights.go says why an equal-means
//     version was tried and retired.
//
// Everything is a pure function over aligned []float64 return series. Nothing
// here knows what a Book is, so every harness can use it.
package evidence
