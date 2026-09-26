// Package handcraft builds portfolio weights the way Law 6 requires: group by
// correlation, weight within each group from Carver's Table 8, multiply down
// the tree, and adjust for Sharpe ratio only by Table 12 — aggressively for
// costs, which are facts, and not at all on less than ten years of evidence.
// The same package gives the diversification multiplier for either level:
// the FDM over forecast weights, the IDM over instrument weights.
//
// Tables 8 and 12 are transcribed from Systematic Trading (Harriman House,
// 2015), pp. 79 and 86, and every worked example the book gives around them
// is a test here. Where the book leaves a choice open — how to round a
// correlation that sits exactly between two table values, and whether to
// interpolate Table 12 — the choice is written down next to the code.
//
// Nothing here looks at returns in the performance sense. Correlations are
// estimable from realistic samples; Sharpe differences are not (Law 6). The
// bootstrap in internal/evidence is the cross-check, never the source.
package handcraft
