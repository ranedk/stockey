package traits

import "math"

// The slicer needs every trait on every day, not every 20th. These are the
// rolling forms of the point functions in traits.go — same windows, same
// minimums, same answers (traits_test.go checks each against its point form).

func nanSlice(n int) []float64 {
	out := make([]float64, n)
	for i := range out {
		out[i] = math.NaN()
	}
	return out
}

// RollingBetaIdio is BetaIdio at every index, by running sums in O(n).
func RollingBetaIdio(ret, mkt []float64, window, minValid int) (beta, idio []float64) {
	n := len(ret)
	beta, idio = nanSlice(n), nanSlice(n)
	var c, sx, sy, sxx, sxy, syy float64
	push := func(i int, sign float64) {
		x, y := mkt[i], ret[i]
		if math.IsNaN(x) || math.IsNaN(y) {
			return
		}
		c += sign
		sx += sign * x
		sy += sign * y
		sxx += sign * x * x
		sxy += sign * x * y
		syy += sign * y * y
	}
	for i := 0; i < n; i++ {
		push(i, 1)
		if i >= window {
			push(i-window, -1)
		}
		if i < window-1 || int(c+0.5) < minValid {
			continue
		}
		vx := sxx - sx*sx/c
		if vx <= 0 {
			continue
		}
		cxy := sxy - sx*sy/c
		b := cxy / vx
		beta[i] = b
		if c > 2 {
			idio[i] = math.Sqrt(math.Max(0, (syy-sy*sy/c)-b*cxy) / (c - 2))
		}
	}
	return beta, idio
}

// RollingDistFromHigh is DistFromHigh at every index, with a monotonic queue
// holding the window's running maximum.
func RollingDistFromHigh(high, close []float64, window int) []float64 {
	out := nanSlice(len(close))
	var q []int
	for i := range close {
		for len(q) > 0 && q[0] <= i-window {
			q = q[1:]
		}
		for len(q) > 0 && high[q[len(q)-1]] <= high[i] {
			q = q[:len(q)-1]
		}
		q = append(q, i)
		if i < window-1 || close[i] <= 0 {
			continue
		}
		if hi := high[q[0]]; hi > 0 {
			out[i] = close[i]/hi - 1
		}
	}
	return out
}

// RollingMedian is Median at every index. The windows used here are short
// (20 days), so sorting each one is cheaper than anything clever.
func RollingMedian(x []float64, window, minValid int) []float64 {
	out := nanSlice(len(x))
	for i := window - 1; i < len(x); i++ {
		out[i] = Median(x, i, window, minValid)
	}
	return out
}

// RollingCount is Count at every index.
func RollingCount(x []float64, v float64, window int) []float64 {
	out := nanSlice(len(x))
	c := 0
	for i := range x {
		if x[i] == v {
			c++
		}
		if i >= window && x[i-window] == v {
			c--
		}
		if i >= window-1 {
			out[i] = float64(c)
		}
	}
	return out
}
