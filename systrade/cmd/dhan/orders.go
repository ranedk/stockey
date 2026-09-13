package main

import (
	"context"
	"flag"
	"fmt"
	"math"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/ranedk/systrader/internal/broker/dhan"
	"github.com/ranedk/systrader/internal/execution"
	"github.com/ranedk/systrader/internal/paper"
	"github.com/ranedk/systrader/internal/store"
)

// ltpTolerance bounds how far Dhan's last price for a security id may sit
// from the sheet's reference price before the batch is refused: the check
// that a security id really is the stock the sheet means.
const ltpTolerance = 0.15

// runOrders is `dhan orders`: turn a paper track's order sheet into the Dhan
// orders the Rs 1 crore account needs — delivery market orders for the next
// session's pre-open auction — check them against the sheet, and log them.
//
// A dry run by default, and a dry run needs no token: it is the stability
// gate's S5 evidence and runs every night after the paper job. Sending
// anything takes -live AND LIVE_ORDERS=yes AND a due, fresh, fully mapped
// sheet for ONE strategy; the broker client refuses again on its own.
func runOrders(args []string) {
	fs := flag.NewFlagSet("orders", flag.ExitOnError)
	strategy := fs.String("strategy", "all", "forward track to build orders for, or 'all' (dry runs only)")
	live := fs.Bool("live", false, "send the orders (also needs LIVE_ORDERS=yes)")
	fatalIfErr(fs.Parse(args))

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()
	st, err := store.Open(ctx)
	fatalIfErr(err)
	defer st.Close()
	fatalIfErr(st.EnsureExecTables(ctx))

	var names []string
	if *strategy == "all" {
		if *live {
			fatal(fmt.Errorf("a live run sends one strategy's orders; name it with -strategy"))
		}
		for _, s := range paper.Specs() {
			names = append(names, s.Name)
		}
	} else {
		if _, ok := paper.SpecFor(*strategy); !ok {
			fatal(fmt.Errorf("no registered strategy %q", *strategy))
		}
		names = []string{*strategy}
	}

	ids, err := st.EquityInstruments(ctx)
	fatalIfErr(err)
	now := time.Now()
	hol, err := st.TradingHolidays(ctx, now.AddDate(0, 0, -30), now)
	fatalIfErr(err)
	session := execution.LastClosedSession(now, hol)

	mode := "dry"
	if *live {
		mode = "live"
	}
	fmt.Printf("DHAN ORDERS — %s; last closed NSE session %s\n", strings.ToUpper(map[string]string{"dry": "dry run", "live": "live"}[mode]),
		session.Format("2006-01-02"))
	if _, _, err := dhan.LoadToken(); err != nil {
		fmt.Printf("token: %v (a live run would stop here)\n", err)
	} else {
		fmt.Println("token: valid")
	}

	failed := false
	for _, name := range names {
		sheet, err := st.PendingSheet(ctx, name)
		fatalIfErr(err)
		b := execution.Build(name, sheet, ids)
		if !sheet.BasedOn.Equal(session) {
			b.Problems = append(b.Problems, fmt.Sprintf("sheet priced off %s, but the last closed session is %s — stale (did the sync and paper run?)",
				sheet.BasedOn.Format("2006-01-02"), session.Format("2006-01-02")))
		}
		report(b)

		rows := logRows(b, mode)
		if *live {
			rows = sendLive(ctx, b, rows)
		}
		fatalIfErr(st.SaveExecBatch(ctx, store.ExecBatch{Strategy: name, BasedOn: b.BasedOn, Mode: mode, Due: b.Due,
			Orders: len(b.Orders), SheetTrades: b.SheetTrades, BuyRs: b.BuyRs, SellRs: b.SellRs,
			Problems: strings.Join(b.Problems, "; ")}, rows))
		if !b.OK() {
			failed = true
		}
	}
	if failed {
		os.Exit(1)
	}
}

func report(b execution.Batch) {
	state := "hold day — shown, would not be sent"
	if b.Due {
		state = "REBALANCE DUE — these go to the next pre-open auction"
	}
	var sells int
	for _, o := range b.Orders {
		if o.Side == "SELL" {
			sells++
		}
	}
	fmt.Printf("\n%s — sheet priced off %s, %s\n", b.Strategy, b.BasedOn.Format("2006-01-02"), state)
	fmt.Printf("  %d orders for %d sheet trades: %d sells Rs %.0f, %d buys Rs %.0f (CNC, MARKET, AMO PRE_OPEN)\n",
		len(b.Orders), b.SheetTrades, sells, b.SellRs, len(b.Orders)-sells, b.BuyRs)
	for i, o := range b.Orders {
		if i >= 8 {
			fmt.Printf("  ... and %d more\n", len(b.Orders)-8)
			break
		}
		fmt.Printf("  %-4s %-14s %6d sh  sec %-6s  ~Rs %9.0f  %s\n", o.Side, o.Symbol, o.Quantity, o.SecurityID, o.ValueRs, o.CorrelationID)
	}
	if b.OK() {
		fmt.Println("  reconciled: every sheet trade has exactly one order; no problems")
		return
	}
	for _, p := range b.Problems {
		fmt.Println("  PROBLEM:", p)
	}
}

func logRows(b execution.Batch, mode string) []store.ExecOrder {
	rows := make([]store.ExecOrder, len(b.Orders))
	for i, o := range b.Orders {
		rows[i] = store.ExecOrder{CorrelationID: o.CorrelationID, Mode: mode, Strategy: b.Strategy, BasedOn: b.BasedOn,
			Symbol: o.Symbol, Side: o.Side, Quantity: o.Quantity, SecurityID: o.SecurityID,
			RefPrice: o.RefPrice, ValueRs: o.ValueRs, Status: mode}
	}
	return rows
}

// sendLive places a checked batch. Every refusal leaves the rows marked
// "refused" with the reason, so the log says what did not happen and why.
func sendLive(ctx context.Context, b execution.Batch, rows []store.ExecOrder) []store.ExecOrder {
	refuse := func(why string) []store.ExecOrder {
		fmt.Println("  LIVE REFUSED:", why)
		for i := range rows {
			rows[i].Status, rows[i].Response = "refused", why
		}
		return rows
	}
	switch {
	case os.Getenv("LIVE_ORDERS") != "yes":
		return refuse("LIVE_ORDERS is not yes")
	case !b.OK():
		return refuse("the batch has problems")
	case !b.Due:
		return refuse("no rebalance is due")
	case len(b.Orders) == 0:
		return rows
	}
	c, err := dhan.New()
	if err != nil {
		return refuse(err.Error())
	}

	// A security id that is not the stock the sheet means would buy the
	// wrong company: Dhan's own last price must agree with the sheet's.
	var secs []int
	for _, o := range b.Orders {
		n, _ := strconv.Atoi(o.SecurityID)
		secs = append(secs, n)
	}
	ltp, err := c.LTP(ctx, map[string][]int{"NSE_EQ": secs})
	if err != nil {
		return refuse("LTP check failed: " + err.Error())
	}
	for _, o := range b.Orders {
		p, ok := lastPrice(ltp, o.SecurityID)
		if !ok || o.RefPrice <= 0 || math.Abs(p/o.RefPrice-1) > ltpTolerance {
			return refuse(fmt.Sprintf("%s: Dhan's last price %.2f for security %s is not the sheet's %.2f", o.Symbol, p, o.SecurityID, o.RefPrice))
		}
	}

	funds, err := c.FundLimit(ctx)
	if err != nil {
		return refuse("funds check failed: " + err.Error())
	}
	bal, ok := number(funds, "availabelBalance", "availableBalance") // Dhan spells it the first way
	if !ok || b.BuyRs > bal {
		return refuse(fmt.Sprintf("buys need Rs %.0f; available balance %.0f", b.BuyRs, bal))
	}

	book, err := c.Orders(ctx)
	if err != nil {
		return refuse("order book read failed: " + err.Error())
	}
	placed := map[string]bool{}
	for _, o := range book {
		if cid, ok := o["correlationId"].(string); ok {
			placed[cid] = true
		}
	}

	for i, o := range b.Orders {
		if placed[o.CorrelationID] {
			rows[i].Status = "skipped-existing"
			continue
		}
		resp, err := c.PlaceOrder(ctx, dhan.Order{TransactionType: o.Side, ExchangeSegment: "NSE_EQ",
			ProductType: "CNC", OrderType: "MARKET", Validity: "DAY", SecurityID: o.SecurityID,
			Quantity: o.Quantity, CorrelationID: o.CorrelationID, AfterMarketOrder: true, AmoTime: "PRE_OPEN"})
		if err != nil {
			rows[i].Status, rows[i].Response = "error", err.Error()
			fmt.Printf("  LIVE STOPPED at %s: %v\n", o.Symbol, err)
			for j := i + 1; j < len(rows); j++ {
				rows[j].Status, rows[j].Response = "not-sent", "an earlier order failed"
			}
			return rows
		}
		rows[i].Status = "placed"
		if id, ok := resp["orderId"].(string); ok {
			rows[i].BrokerOrderID = id
		}
		if s, ok := resp["orderStatus"].(string); ok {
			rows[i].Response = s
		}
	}
	fmt.Printf("  LIVE: %d orders sent to the pre-open auction\n", len(b.Orders))
	return rows
}

// lastPrice digs one security's last price out of Dhan's LTP response,
// {"data": {"NSE_EQ": {"2885": {"last_price": 1234.5}}}}.
func lastPrice(resp map[string]any, sec string) (float64, bool) {
	data, _ := resp["data"].(map[string]any)
	seg, _ := data["NSE_EQ"].(map[string]any)
	q, _ := seg[sec].(map[string]any)
	return number(q, "last_price")
}

func number(m map[string]any, keys ...string) (float64, bool) {
	for _, k := range keys {
		if v, ok := m[k].(float64); ok {
			return v, true
		}
	}
	return 0, false
}

func fatalIfErr(err error) {
	if err != nil {
		fatal(err)
	}
}
