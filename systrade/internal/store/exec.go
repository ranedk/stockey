package store

import (
	"context"
	"time"
)

// Instrument is one NSE cash-equity security as Dhan knows it.
type Instrument struct {
	Symbol     string
	SecurityID int64
	TickSize   float64
	FreezeQty  float64 // exchange freeze quantity; 0 when not published
}

// EquityInstruments returns, for every symbol on the latest EQ bhavcopy, the
// Dhan securities it could be. Dhan's master is matched on the EQ series
// only (the same stock has other ids in BE, bonds and so on) and on EITHER
// the ticker or the bhavcopy's ISIN: a renamed stock keeps its ISIN while
// Dhan's row can keep the old ticker (SELAN for ANTELOPUS). Only the
// best-matching candidates come back — both keys over one — and a symbol
// with more than one of those gets them all, so the caller can refuse to
// guess. stockey's master never closes a row that disappears from Dhan's
// file, which is why "valid_to IS NULL" alone is not enough.
func (s *Store) EquityInstruments(ctx context.Context) (map[string][]Instrument, error) {
	rows, err := s.pool.Query(ctx, `
		WITH cur AS (
			SELECT symbol, isin FROM nseindia_ohlcv
			WHERE series = 'EQ'
			  AND date = (SELECT max(date) FROM nseindia_ohlcv WHERE date > now() - interval '30 days'))
		SELECT DISTINCT c.symbol, m.security_id,
		       coalesce(m.tick_size, 0)::float8, coalesce(m.sm_freeze_qty, 0)::float8,
		       (m.underlying_symbol = c.symbol)::int + (m.isin IS NOT DISTINCT FROM c.isin)::int
		FROM cur c
		JOIN master_dhan_instruments m
		  ON m.exch_id = 'NSE' AND m.segment = 'E' AND m.instrument = 'EQUITY'
		 AND m.series = 'EQ' AND m.valid_to IS NULL
		 AND (m.underlying_symbol = c.symbol OR m.isin = c.isin)`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	best := map[string]int{}
	out := map[string][]Instrument{}
	for rows.Next() {
		var in Instrument
		var score int
		if err := rows.Scan(&in.Symbol, &in.SecurityID, &in.TickSize, &in.FreezeQty, &score); err != nil {
			return nil, err
		}
		switch {
		case score > best[in.Symbol]:
			best[in.Symbol] = score
			out[in.Symbol] = []Instrument{in}
		case score == best[in.Symbol]:
			out[in.Symbol] = append(out[in.Symbol], in)
		}
	}
	return out, rows.Err()
}

// TradingHolidays returns NSE cash-market holidays in [from, to].
func (s *Store) TradingHolidays(ctx context.Context, from, to time.Time) (map[time.Time]bool, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT date FROM nseindia_holidays WHERE type = 'CM' AND date >= $1 AND date <= $2`, from, to)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[time.Time]bool{}
	for rows.Next() {
		var d time.Time
		if err := rows.Scan(&d); err != nil {
			return nil, err
		}
		d = d.UTC()
		out[time.Date(d.Year(), d.Month(), d.Day(), 0, 0, 0, 0, time.UTC)] = true
	}
	return out, rows.Err()
}

// LatestCloseDate is the most recent date the adjusted price table holds —
// what a fresh order sheet must be priced off. Bounded to the last 30 days:
// on stockey's side the table is a hypertable view, and an unbounded max()
// decompresses all of it.
func (s *Store) LatestCloseDate(ctx context.Context) (time.Time, error) {
	var d *time.Time
	err := s.pool.QueryRow(ctx, `
		SELECT max(date) FROM advisory_adjusted_ohlcv_daily
		WHERE series = 'EQ' AND date > now() - interval '30 days'`).Scan(&d)
	if err != nil || d == nil {
		return time.Time{}, err
	}
	return time.Date(d.Year(), d.Month(), d.Day(), 0, 0, 0, 0, time.UTC), nil
}

// ExecBatch is one run of the order builder over one strategy's sheet.
type ExecBatch struct {
	Strategy    string
	BasedOn     time.Time
	Mode        string // dry | live
	Due         bool
	Orders      int
	SheetTrades int
	BuyRs       float64
	SellRs      float64
	Problems    string
}

// ExecOrder is one order the builder produced, and what became of it.
type ExecOrder struct {
	CorrelationID string
	Mode          string
	Strategy      string
	BasedOn       time.Time
	Symbol        string
	Side          string
	Quantity      int
	SecurityID    string
	RefPrice      float64
	ValueRs       float64
	Status        string // dry | placed | skipped-existing | rejected | error
	BrokerOrderID string
	Response      string
}

// EnsureExecTables creates the execution log (idempotent). systrader-owned.
func (s *Store) EnsureExecTables(ctx context.Context) error {
	_, err := s.pool.Exec(ctx, `
		CREATE TABLE IF NOT EXISTS systrader_exec_batch (
			strategy     text NOT NULL,
			based_on     date NOT NULL,
			mode         text NOT NULL,
			due          boolean NOT NULL,
			orders       integer NOT NULL,
			sheet_trades integer NOT NULL,
			buy_rs       double precision NOT NULL,
			sell_rs      double precision NOT NULL,
			problems     text NOT NULL,
			created_at   timestamptz NOT NULL DEFAULT now(),
			PRIMARY KEY (strategy, based_on, mode)
		);
		CREATE TABLE IF NOT EXISTS systrader_exec_order (
			correlation_id  text NOT NULL,
			mode            text NOT NULL,
			strategy        text NOT NULL,
			based_on        date NOT NULL,
			symbol          text NOT NULL,
			side            text NOT NULL,
			quantity        integer NOT NULL,
			security_id     text NOT NULL,
			ref_price       double precision,
			value_rs        double precision,
			status          text NOT NULL,
			broker_order_id text,
			response        text,
			created_at      timestamptz NOT NULL DEFAULT now(),
			PRIMARY KEY (correlation_id, mode)
		);`)
	return err
}

// SaveExecBatch records a run. A dry run of the same sheet replaces the
// previous dry run; live rows are only ever added or updated, never deleted.
func (s *Store) SaveExecBatch(ctx context.Context, b ExecBatch, orders []ExecOrder) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if _, err := tx.Exec(ctx, `
		INSERT INTO systrader_exec_batch (strategy, based_on, mode, due, orders, sheet_trades, buy_rs, sell_rs, problems)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
		ON CONFLICT (strategy, based_on, mode) DO UPDATE SET due = EXCLUDED.due, orders = EXCLUDED.orders,
			sheet_trades = EXCLUDED.sheet_trades, buy_rs = EXCLUDED.buy_rs, sell_rs = EXCLUDED.sell_rs,
			problems = EXCLUDED.problems, created_at = now()`,
		b.Strategy, b.BasedOn, b.Mode, b.Due, b.Orders, b.SheetTrades, b.BuyRs, b.SellRs, b.Problems); err != nil {
		return err
	}
	if b.Mode == "dry" {
		if _, err := tx.Exec(ctx, `DELETE FROM systrader_exec_order WHERE strategy = $1 AND based_on = $2 AND mode = 'dry'`,
			b.Strategy, b.BasedOn); err != nil {
			return err
		}
	}
	for _, o := range orders {
		if _, err := tx.Exec(ctx, `
			INSERT INTO systrader_exec_order (correlation_id, mode, strategy, based_on, symbol, side, quantity,
				security_id, ref_price, value_rs, status, broker_order_id, response)
			VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
			ON CONFLICT (correlation_id, mode) DO UPDATE SET status = EXCLUDED.status,
				broker_order_id = EXCLUDED.broker_order_id, response = EXCLUDED.response, created_at = now()`,
			o.CorrelationID, o.Mode, o.Strategy, o.BasedOn, o.Symbol, o.Side, o.Quantity, o.SecurityID,
			nilIfZero(o.RefPrice), nilIfZero(o.ValueRs), o.Status, nilIfEmpty(o.BrokerOrderID), nilIfEmpty(o.Response)); err != nil {
			return err
		}
	}
	return tx.Commit(ctx)
}

func nilIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}
