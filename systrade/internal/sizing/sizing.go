// Package sizing translates forecasts into subsystem positions (Bible Laws
// 10–11). This is the ONLY place capital and risk appetite enter the system.
package sizing

import "math"

// DailyCashVolTarget converts the annual percentage volatility target on
// current capital into the daily cash risk budget (÷ √256 ≈ 16).
func DailyCashVolTarget(capital, volTargetPct float64) float64 {
	return capital * volTargetPct / 16.0
}

// VolScalar: how many blocks consume the whole daily risk budget at forecast
// +10. instValueVol = price-unit vol × point value × FX (daily σ of one block
// in account currency).
func VolScalar(dailyCashVolTarget, instValueVol float64) float64 {
	if instValueVol <= 0 || math.IsNaN(instValueVol) {
		return math.NaN()
	}
	return dailyCashVolTarget / instValueVol
}

// Subsystem position = vol scalar × forecast ÷ 10 (unrounded).
func Subsystem(volScalar, forecast float64) float64 {
	if math.IsNaN(volScalar) || math.IsNaN(forecast) {
		return math.NaN()
	}
	return volScalar * forecast / 10.0
}
