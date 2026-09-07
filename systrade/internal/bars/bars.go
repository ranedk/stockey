// Package bars holds daily OHLCV bars and a streaming on-disk cache of them.
//
// Why a cache exists at all: the pattern research in internal/patterns needs
// full adjusted OHLC for the whole NSE universe (~3,900 symbols, ~5.4M bars),
// and every parameter variation re-reads it. Re-querying postgres per
// experiment is exactly the load pattern that has taken this workspace's
// database down before (see the workspace CLAUDE.md's OOM note). The rule
// here is: the database is read ONCE, date-bounded, into a cache file; all
// subsequent work reads the file.
//
// The cache format is deliberately streamable in both directions — one
// symbol's bars are written and read as a unit — so neither building nor
// scanning it ever needs the whole universe resident in memory.
package bars

import (
	"bufio"
	"encoding/binary"
	"fmt"
	"io"
	"math"
	"os"
	"runtime"
	"sort"
	"sync"
	"time"
)

// Bar is one daily OHLCV observation, corporate-action adjusted.
type Bar struct {
	Date                        time.Time
	Open, High, Low, Close, Vol float64
}

// Series is one symbol's bars, ascending by date, deduplicated.
type Series struct {
	Symbol string
	Bars   []Bar
}

const (
	cacheMagic   = "SYSTBAR1"
	fieldsPerBar = 5 // O H L C V
)

// Writer streams symbol series to a cache file.
type Writer struct {
	f  *os.File
	bw *bufio.Writer
	n  int
}

// Create opens path for writing and emits the cache header.
func Create(path string) (*Writer, error) {
	f, err := os.Create(path)
	if err != nil {
		return nil, err
	}
	bw := bufio.NewWriterSize(f, 1<<20)
	if _, err := bw.WriteString(cacheMagic); err != nil {
		f.Close()
		return nil, err
	}
	return &Writer{f: f, bw: bw}, nil
}

// Write appends one symbol's series. Symbols may be written in any order but
// each symbol must be written exactly once.
func (w *Writer) Write(s Series) error {
	var buf [binary.MaxVarintLen64]byte
	put := func(v uint64) error {
		n := binary.PutUvarint(buf[:], v)
		_, err := w.bw.Write(buf[:n])
		return err
	}
	if err := put(uint64(len(s.Symbol))); err != nil {
		return err
	}
	if _, err := w.bw.WriteString(s.Symbol); err != nil {
		return err
	}
	if err := put(uint64(len(s.Bars))); err != nil {
		return err
	}
	for _, b := range s.Bars {
		if err := binary.Write(w.bw, binary.LittleEndian, b.Date.Unix()); err != nil {
			return err
		}
		vals := [fieldsPerBar]float64{b.Open, b.High, b.Low, b.Close, b.Vol}
		if err := binary.Write(w.bw, binary.LittleEndian, vals); err != nil {
			return err
		}
	}
	w.n++
	return nil
}

// Count returns how many symbol series have been written.
func (w *Writer) Count() int { return w.n }

// Close flushes and closes the file.
func (w *Writer) Close() error {
	if err := w.bw.Flush(); err != nil {
		w.f.Close()
		return err
	}
	return w.f.Close()
}

// Scan reads a cache file, calling fn once per symbol series. Only one
// series is resident at a time, so a 300MB cache costs a few MB of RAM.
// fn returning an error stops the scan and returns that error.
func Scan(path string, fn func(Series) error) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	br := bufio.NewReaderSize(f, 1<<20)

	magic := make([]byte, len(cacheMagic))
	if _, err := io.ReadFull(br, magic); err != nil {
		return fmt.Errorf("bars: reading cache header: %w", err)
	}
	if string(magic) != cacheMagic {
		return fmt.Errorf("bars: %s is not a bar cache (bad magic %q)", path, magic)
	}

	for {
		symLen, err := binary.ReadUvarint(br)
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
		sym := make([]byte, symLen)
		if _, err := io.ReadFull(br, sym); err != nil {
			return err
		}
		nBars, err := binary.ReadUvarint(br)
		if err != nil {
			return err
		}
		out := Series{Symbol: string(sym), Bars: make([]Bar, nBars)}
		for i := range out.Bars {
			var unix int64
			if err := binary.Read(br, binary.LittleEndian, &unix); err != nil {
				return err
			}
			var vals [fieldsPerBar]float64
			if err := binary.Read(br, binary.LittleEndian, &vals); err != nil {
				return err
			}
			out.Bars[i] = Bar{
				Date:  time.Unix(unix, 0).UTC(),
				Open:  vals[0],
				High:  vals[1],
				Low:   vals[2],
				Close: vals[3],
				Vol:   vals[4],
			}
		}
		if err := fn(out); err != nil {
			return err
		}
	}
}

// Closes extracts the close series as a plain slice.
func (s Series) Closes() []float64 {
	out := make([]float64, len(s.Bars))
	for i, b := range s.Bars {
		out[i] = b.Close
	}
	return out
}

// Valid reports whether a bar is usable: all prices positive and finite, and
// the high/low actually bracket the open and close. A bar failing this is a
// data error, not a trading opportunity — the pattern rules below compare
// highs and lows across bars, so a bad high silently manufactures signals.
func (b Bar) Valid() bool {
	for _, v := range []float64{b.Open, b.High, b.Low, b.Close} {
		if math.IsNaN(v) || math.IsInf(v, 0) || v <= 0 {
			return false
		}
	}
	return b.High >= b.Low && b.High >= b.Open && b.High >= b.Close &&
		b.Low <= b.Open && b.Low <= b.Close
}

// MedianTurnover is a rolling median of close x volume over `window` bars —
// the point-in-time liquidity read used to keep untradeable microcaps out of
// a research sample. A median, not a mean: one block-deal day in an illiquid
// name lifts a mean enough to let the whole symbol through.
func MedianTurnover(b []Bar, window int) []float64 {
	out := make([]float64, len(b))
	buf := make([]float64, 0, window)
	for i := range b {
		if i < window-1 {
			out[i] = math.NaN()
			continue
		}
		buf = buf[:0]
		for _, x := range b[i-window+1 : i+1] {
			buf = append(buf, x.Close*x.Vol)
		}
		sort.Float64s(buf)
		out[i] = buf[len(buf)/2]
	}
	return out
}

// ScanParallel streams a cache file on one goroutine and fans symbol series
// out to `workers` goroutines, so only a handful of series are resident at
// any time regardless of universe size. fn is called concurrently and must be
// safe for that; ordering of symbols is not preserved.
func ScanParallel(path string, workers int, fn func(Series)) error {
	if workers <= 0 {
		workers = runtime.NumCPU()
	}
	ch := make(chan Series, 64)
	var wg sync.WaitGroup
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for s := range ch {
				fn(s)
			}
		}()
	}
	err := Scan(path, func(s Series) error {
		ch <- s
		return nil
	})
	close(ch)
	wg.Wait()
	return err
}
