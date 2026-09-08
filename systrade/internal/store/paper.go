package store

import (
	"context"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/ranedk/systrader/internal/paper"
)

// Paper-trading tables. systrader-owned (`systrader_` prefix, per
// docs/DATA_CONTRACT.md) and rewritten wholesale on every run: the track is a
// pure function of the spec and the price history, so the write path is
// delete-then-insert inside one transaction rather than an incremental update
// nobody could later audit.

// EnsurePaperTables creates the paper-trading tables if absent (idempotent).
func (s *Store) EnsurePaperTables(ctx context.Context) error {
	_, err := s.pool.Exec(ctx, `
		CREATE TABLE IF NOT EXISTS systrader_paper_nav (
			strategy    text        NOT NULL,
			book        text        NOT NULL,
			date        date        NOT NULL,
			nav         double precision NOT NULL,
			ret         double precision NOT NULL,
			turnover    double precision NOT NULL,
			cost        double precision NOT NULL,
			holdings    integer     NOT NULL,
			rebalanced  boolean     NOT NULL,
			PRIMARY KEY (strategy, book, date)
		);
		CREATE TABLE IF NOT EXISTS systrader_paper_holding (
			strategy text NOT NULL,
			book     text NOT NULL,
			date     date NOT NULL,
			symbol   text NOT NULL,
			weight   double precision NOT NULL,
			PRIMARY KEY (strategy, book, date, symbol)
		);
		CREATE TABLE IF NOT EXISTS systrader_paper_order (
			strategy    text NOT NULL,
			book        text NOT NULL,
			date        date NOT NULL,
			symbol      text NOT NULL,
			side        text NOT NULL,
			from_weight double precision NOT NULL,
			to_weight   double precision NOT NULL,
			fill_price  double precision,
			PRIMARY KEY (strategy, book, date, symbol)
		);
		CREATE TABLE IF NOT EXISTS systrader_paper_pending (
			strategy    text NOT NULL,
			computed_at timestamptz NOT NULL,
			based_on    date NOT NULL,
			due         boolean NOT NULL,
			days_to_due integer NOT NULL,
			symbol      text NOT NULL,
			side        text NOT NULL,
			from_weight double precision NOT NULL,
			to_weight   double precision NOT NULL,
			ref_price   double precision,
			PRIMARY KEY (strategy, symbol)
		);
		CREATE INDEX IF NOT EXISTS idx_paper_nav_strategy_date
			ON systrader_paper_nav (strategy, date);
		CREATE INDEX IF NOT EXISTS idx_paper_order_strategy_date
			ON systrader_paper_order (strategy, date DESC);`)
	return err
}

// SavePaperTrack replaces the whole record for one strategy. Holdings are
// stored for the last computed day only — the orders carry the history of what
// changed, and a daily holdings snapshot would be the same information at
// several hundred times the size.
func (s *Store) SavePaperTrack(ctx context.Context, tr *paper.Track) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	name := tr.Spec.Name
	for _, table := range []string{"systrader_paper_nav", "systrader_paper_holding", "systrader_paper_order"} {
		if _, err := tx.Exec(ctx, "DELETE FROM "+table+" WHERE strategy = $1", name); err != nil {
			return err
		}
	}

	var navRows [][]any
	var holdRows [][]any
	var orderRows [][]any
	for book, b := range tr.Books {
		for _, p := range b.NAV {
			navRows = append(navRows, []any{name, book, p.Date, p.NAV, p.Return, p.Turnover, p.Cost, p.Holdings, p.Rebalanced})
		}
		if len(b.NAV) > 0 {
			last := b.NAV[len(b.NAV)-1].Date
			for sym, w := range b.Holdings {
				holdRows = append(holdRows, []any{name, book, last, sym, w})
			}
		}
		for _, o := range b.Orders {
			orderRows = append(orderRows, []any{name, book, o.Date, o.Symbol, o.Side, o.FromWeight, o.ToWeight, nilIfZero(o.FillPrice)})
		}
	}

	if _, err := tx.CopyFrom(ctx, pgx.Identifier{"systrader_paper_nav"},
		[]string{"strategy", "book", "date", "nav", "ret", "turnover", "cost", "holdings", "rebalanced"},
		pgx.CopyFromRows(navRows)); err != nil {
		return err
	}
	if _, err := tx.CopyFrom(ctx, pgx.Identifier{"systrader_paper_holding"},
		[]string{"strategy", "book", "date", "symbol", "weight"},
		pgx.CopyFromRows(holdRows)); err != nil {
		return err
	}
	if _, err := tx.CopyFrom(ctx, pgx.Identifier{"systrader_paper_order"},
		[]string{"strategy", "book", "date", "symbol", "side", "from_weight", "to_weight", "fill_price"},
		pgx.CopyFromRows(orderRows)); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// SavePending replaces the strategy's order sheet for the next session.
func (s *Store) SavePending(ctx context.Context, strategy string, sheet paper.PendingSheet) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if _, err := tx.Exec(ctx, `DELETE FROM systrader_paper_pending WHERE strategy = $1`, strategy); err != nil {
		return err
	}
	now := time.Now().UTC()
	var rows [][]any
	for _, o := range sheet.Orders {
		rows = append(rows, []any{strategy, now, sheet.BasedOn, sheet.Due, sheet.DaysToDue,
			o.Symbol, o.Side, o.FromWeight, o.ToWeight, nilIfZero(o.FillPrice)})
	}
	if _, err := tx.CopyFrom(ctx, pgx.Identifier{"systrader_paper_pending"},
		[]string{"strategy", "computed_at", "based_on", "due", "days_to_due",
			"symbol", "side", "from_weight", "to_weight", "ref_price"},
		pgx.CopyFromRows(rows)); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// PaperPending is the stored order sheet for the next session.
type PaperPending struct {
	ComputedAt time.Time       `json:"computed_at"`
	BasedOn    time.Time       `json:"based_on"`
	Due        bool            `json:"due"`
	DaysToDue  int             `json:"days_to_due"`
	Orders     []PaperOrderRow `json:"orders"`
}

// PendingSheet returns the strategy's next-session order sheet.
func (s *Store) PendingSheet(ctx context.Context, strategy string) (PaperPending, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT computed_at, based_on, due, days_to_due, symbol, side, from_weight, to_weight, ref_price
		FROM systrader_paper_pending WHERE strategy = $1
		ORDER BY side ASC, symbol ASC`, strategy)
	if err != nil {
		return PaperPending{}, err
	}
	defer rows.Close()
	var out PaperPending
	for rows.Next() {
		var o PaperOrderRow
		if err := rows.Scan(&out.ComputedAt, &out.BasedOn, &out.Due, &out.DaysToDue,
			&o.Symbol, &o.Side, &o.FromWeight, &o.ToWeight, &o.FillPrice); err != nil {
			return out, err
		}
		o.Date = out.BasedOn
		out.Orders = append(out.Orders, o)
	}
	return out, rows.Err()
}

func nilIfZero(v float64) any {
	if v == 0 {
		return nil
	}
	return v
}

// PaperNavRow is one day of one book, as stored.
type PaperNavRow struct {
	Book       string    `json:"book"`
	Date       time.Time `json:"date"`
	NAV        float64   `json:"nav"`
	Return     float64   `json:"return"`
	Turnover   float64   `json:"turnover"`
	Cost       float64   `json:"cost"`
	Holdings   int       `json:"holdings"`
	Rebalanced bool      `json:"rebalanced"`
}

// PaperNav returns every book's daily record for a strategy, oldest first.
func (s *Store) PaperNav(ctx context.Context, strategy string) ([]PaperNavRow, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT book, date, nav, ret, turnover, cost, holdings, rebalanced
		FROM systrader_paper_nav WHERE strategy = $1
		ORDER BY date ASC, book ASC`, strategy)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []PaperNavRow
	for rows.Next() {
		var r PaperNavRow
		if err := rows.Scan(&r.Book, &r.Date, &r.NAV, &r.Return, &r.Turnover, &r.Cost, &r.Holdings, &r.Rebalanced); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// PaperHoldingRow is one position in the current book.
type PaperHoldingRow struct {
	Symbol string  `json:"symbol"`
	Weight float64 `json:"weight"`
}

// PaperHoldings returns one book's latest positions, heaviest first.
func (s *Store) PaperHoldings(ctx context.Context, strategy, book string) ([]PaperHoldingRow, time.Time, error) {
	// max() over no rows is NULL, which is the normal state of a strategy
	// frozen today and not yet trading — a nullable scan, not an error.
	var latest *time.Time
	if err := s.pool.QueryRow(ctx, `
		SELECT max(date) FROM systrader_paper_holding WHERE strategy = $1 AND book = $2`,
		strategy, book).Scan(&latest); err != nil {
		return nil, time.Time{}, err
	}
	if latest == nil {
		return nil, time.Time{}, nil
	}
	asOf := *latest
	rows, err := s.pool.Query(ctx, `
		SELECT symbol, weight FROM systrader_paper_holding
		WHERE strategy = $1 AND book = $2 AND date = $3
		ORDER BY weight DESC, symbol ASC`, strategy, book, asOf)
	if err != nil {
		return nil, asOf, err
	}
	defer rows.Close()
	var out []PaperHoldingRow
	for rows.Next() {
		var r PaperHoldingRow
		if err := rows.Scan(&r.Symbol, &r.Weight); err != nil {
			return nil, asOf, err
		}
		out = append(out, r)
	}
	return out, asOf, rows.Err()
}

// PaperOrderRow is one intended trade.
type PaperOrderRow struct {
	Date       time.Time `json:"date"`
	Symbol     string    `json:"symbol"`
	Side       string    `json:"side"`
	FromWeight float64   `json:"from_weight"`
	ToWeight   float64   `json:"to_weight"`
	FillPrice  *float64  `json:"fill_price"`
}

// PaperOrders returns the most recent rebalance's orders for one book.
func (s *Store) PaperOrders(ctx context.Context, strategy, book string) ([]PaperOrderRow, time.Time, error) {
	var latest *time.Time
	if err := s.pool.QueryRow(ctx, `
		SELECT max(date) FROM systrader_paper_order WHERE strategy = $1 AND book = $2`,
		strategy, book).Scan(&latest); err != nil {
		return nil, time.Time{}, err
	}
	if latest == nil {
		return nil, time.Time{}, nil
	}
	asOf := *latest
	rows, err := s.pool.Query(ctx, `
		SELECT date, symbol, side, from_weight, to_weight, fill_price
		FROM systrader_paper_order
		WHERE strategy = $1 AND book = $2 AND date = $3
		ORDER BY side ASC, symbol ASC`, strategy, book, asOf)
	if err != nil {
		return nil, asOf, err
	}
	defer rows.Close()
	var out []PaperOrderRow
	for rows.Next() {
		var r PaperOrderRow
		if err := rows.Scan(&r.Date, &r.Symbol, &r.Side, &r.FromWeight, &r.ToWeight, &r.FillPrice); err != nil {
			return nil, asOf, err
		}
		out = append(out, r)
	}
	return out, asOf, rows.Err()
}

// PaperStrategies lists the tracked strategy names.
func (s *Store) PaperStrategies(ctx context.Context) ([]string, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT strategy FROM systrader_paper_pending
		UNION
		SELECT strategy FROM systrader_paper_nav
		ORDER BY 1`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var n string
		if err := rows.Scan(&n); err != nil {
			return nil, err
		}
		out = append(out, n)
	}
	return out, rows.Err()
}
