// Package costs is what a trade in NSE cash equity actually costs at Dhan:
// the statutory charges and the DP charge (facts, from Dhan's published
// schedule, checked against https://dhan.co/pricing on 2026-09-12) and market
// impact (the square-root law, a labelled MODEL). The spread is absent on
// purpose: every order here goes into the opening auction, which pays none
// (LEDGER row 35, research/reports/2026-09-12_fill_costs.txt).
package costs

import "math"

// Dhan's equity-delivery schedule, as fractions of trade value unless noted.
const (
	STT      = 0.001       // 0.1% on buy and on sell
	Stamp    = 0.00015     // 0.015% on buys only
	Exchange = 0.000030699 // NSE transaction charge, both sides
	SEBI     = 0.000001    // 0.0001% of turnover, both sides
	// IPFT: Dhan's page prints 0.0000001%; NSE levies Rs 10 a crore, 0.0001%.
	// The larger is kept — the difference is 0.02 bps a round trip.
	IPFT = 0.000001
	GST  = 0.18 // on brokerage (nil for delivery) + exchange + SEBI + IPFT
	// DPCharge is rupees per stock sold per day: Rs 12.50 + GST, however small
	// the sale.
	DPCharge = 12.5 * (1 + GST)
	// ImpactY is the square-root law's coefficient: impact = Y·σ·√(Q/V).
	ImpactY = 1.0
)

// Buy is the statutory cost, in rupees, of buying value rupees.
func Buy(value float64) float64 {
	return value * (STT + Stamp + Exchange + SEBI + IPFT + GST*(Exchange+SEBI+IPFT))
}

// Sell is the statutory cost, in rupees, of selling value rupees, DP charge
// included.
func Sell(value float64) float64 {
	if value <= 0 {
		return 0
	}
	return value*(STT+Exchange+SEBI+IPFT+GST*(Exchange+SEBI+IPFT)) + DPCharge
}

// StatutoryRoundTrip is buying and selling the same value, before the DP
// charge, as a fraction of that value.
func StatutoryRoundTrip() float64 {
	return 2*(STT+Exchange+SEBI+IPFT) + Stamp + GSTOnCharges()
}

// GSTOnCharges is the GST part of StatutoryRoundTrip.
func GSTOnCharges() float64 { return GST * 2 * (Exchange + SEBI + IPFT) }

// DPFraction is the DP charge as a fraction of a position of q rupees.
func DPFraction(q float64) float64 { return DPCharge / q }

// Impact is the modelled market impact, in rupees, of trading value rupees in
// a name that trades dayValue rupees a day with daily volatility sigma (a
// fraction). One side.
func Impact(value, dayValue, sigma float64) float64 {
	if value <= 0 || dayValue <= 0 || sigma <= 0 || math.IsNaN(sigma) {
		return 0
	}
	return value * ImpactY * sigma * math.Sqrt(value/dayValue)
}
