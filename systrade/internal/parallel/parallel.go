// Package parallel provides a tiny worker-pool map for fanning computations
// across instruments, rule grids, and bootstrap runs.
package parallel

import (
	"runtime"
	"sync"
)

// Map applies f to every item using up to workers goroutines (default:
// GOMAXPROCS), preserving order. f must be pure w.r.t. shared state.
func Map[T any, R any](items []T, workers int, f func(T) R) []R {
	if workers <= 0 {
		workers = runtime.GOMAXPROCS(0)
	}
	if workers > len(items) {
		workers = len(items)
	}
	out := make([]R, len(items))
	if len(items) == 0 {
		return out
	}
	idx := make(chan int)
	var wg sync.WaitGroup
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := range idx {
				out[i] = f(items[i])
			}
		}()
	}
	for i := range items {
		idx <- i
	}
	close(idx)
	wg.Wait()
	return out
}
