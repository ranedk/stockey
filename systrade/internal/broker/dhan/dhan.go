// Package dhan is a minimal client for the Dhan HQ v2 REST API.
//
// AUTH DESIGN: systrader does NOT implement the browser login. stockey owns
// that flow (Chrome via CDP + Playwright + TOTP → consent → access token,
// cached in $STOCKEY_DIR/.cache/dhan_access_token.json). We reuse that cache
// (DHAN_TOKEN_CACHE). If the token is missing/expired, LoadToken returns an
// error telling the operator to refresh via stockey. One login, two systems.
//
// SAFETY: PlaceOrder refuses to send anything unless LIVE_ORDERS=yes
// (Bible Law 16 — lazy in operation; no accidental live trading).
package dhan

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"time"

	"github.com/joho/godotenv"
)

type Client struct {
	BaseURL  string
	ClientID string
	Token    string
	HTTP     *http.Client
}

type tokenCache struct {
	DhanClientID string `json:"dhanClientId"`
	AccessToken  string `json:"accessToken"`
	ExpiryTime   string `json:"expiryTime"`
}

// LoadToken reads the stockey-managed token cache and validates expiry.
func LoadToken() (token, clientID string, err error) {
	_ = godotenv.Load()
	path := os.Getenv("DHAN_TOKEN_CACHE")
	if path == "" {
		return "", "", fmt.Errorf("dhan: DHAN_TOKEN_CACHE not set")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", "", fmt.Errorf("dhan: token cache unreadable (%w) — refresh via stockey: cd $STOCKEY_DIR && python -m data.dhanlive.web_login", err)
	}
	var tc tokenCache
	if err := json.Unmarshal(raw, &tc); err != nil {
		return "", "", fmt.Errorf("dhan: token cache corrupt: %w", err)
	}
	// The cache writes a bare IST wall-clock time. Parsed as UTC it reads 5.5
	// hours later than it is, so an expired token looks live for most of an
	// evening — the same class of bug as BarDate's.
	exp, err := time.ParseInLocation("2006-01-02T15:04:05", tc.ExpiryTime, IST)
	if err != nil {
		exp, err = time.Parse(time.RFC3339, tc.ExpiryTime)
	}
	if err != nil {
		return "", "", fmt.Errorf("dhan: cannot parse token expiry %q", tc.ExpiryTime)
	}
	if time.Now().Add(5 * time.Minute).After(exp) {
		return "", "", fmt.Errorf("dhan: token expired at %s — refresh via stockey login (CDP chrome must be running)", tc.ExpiryTime)
	}
	cid := tc.DhanClientID
	if cid == "" {
		cid = os.Getenv("DHAN_CLIENT_ID")
	}
	return tc.AccessToken, cid, nil
}

func New() (*Client, error) {
	token, cid, err := LoadToken()
	if err != nil {
		return nil, err
	}
	base := os.Getenv("DHAN_BASE_URL")
	if base == "" {
		base = "https://api.dhan.co/v2"
	}
	return &Client{
		BaseURL:  base,
		ClientID: cid,
		Token:    token,
		HTTP:     &http.Client{Timeout: 30 * time.Second},
	}, nil
}

func (c *Client) do(ctx context.Context, method, path string, body, out any) error {
	var rd io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return err
		}
		rd = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.BaseURL+path, rd)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	req.Header.Set("access-token", c.Token)
	req.Header.Set("client-id", c.ClientID)
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 300 {
		return fmt.Errorf("dhan %s %s: HTTP %d: %s", method, path, resp.StatusCode, string(raw))
	}
	if out != nil {
		return json.Unmarshal(raw, out)
	}
	return nil
}

// --- Read APIs -------------------------------------------------------------

// FundLimit returns available margin/balance.
func (c *Client) FundLimit(ctx context.Context) (map[string]any, error) {
	var out map[string]any
	err := c.do(ctx, http.MethodGet, "/fundlimit", nil, &out)
	return out, err
}

// Holdings returns demat holdings (the ETF sleeve reads this).
func (c *Client) Holdings(ctx context.Context) ([]map[string]any, error) {
	var out []map[string]any
	err := c.do(ctx, http.MethodGet, "/holdings", nil, &out)
	return out, err
}

// Positions returns open F&O/intraday positions (the futures sleeve reads this).
func (c *Client) Positions(ctx context.Context) ([]map[string]any, error) {
	var out []map[string]any
	err := c.do(ctx, http.MethodGet, "/positions", nil, &out)
	return out, err
}

// HistoricalDaily fetches daily OHLCV for a security — used to BACKFILL
// series the stockey db lacks (index futures, MCX, gilt ETFs).
// exchangeSegment: NSE_EQ, NSE_FNO, MCX_COMM, IDX_I…; instrument: EQUITY,
// INDEX, FUTIDX, FUTCOM… (see master_dhan_instruments for valid pairs).
type Candles struct {
	Open      []float64 `json:"open"`
	High      []float64 `json:"high"`
	Low       []float64 `json:"low"`
	Close     []float64 `json:"close"`
	Volume    []float64 `json:"volume"`
	Timestamp []float64 `json:"timestamp"` // epoch seconds; Dhan returns floats
}

// IST is the exchange's clock. Written as a fixed offset rather than loaded
// from the tzdata database on purpose: India has never observed DST, so the
// offset is exact for every timestamp Dhan will ever send, and a fixed zone
// cannot fail on a host with no zoneinfo installed.
//
// This constant exists because its absence corrupted an entire table. Dhan
// stamps a daily bar at the START of the IST trading day; read through
// time.Local on a UTC host, 2026-08-07 00:00 IST becomes 2026-08-06 18:30 UTC
// and the bar is filed under the previous calendar day. Every bar in
// systrader_ohlcv_daily was one day early, and one in five landed on a Sunday
// (Monday's bar), until 2026-09-07.
var IST = time.FixedZone("IST", 5*3600+1800)

// BarDate converts one of Dhan's epoch-second candle timestamps into the
// trading date the bar belongs to: the IST calendar date, carried as UTC
// midnight because that is what every date column in this project stores.
// Never derive a bar date any other way — in particular never through
// time.Local, which is whatever the host happens to be set to.
func BarDate(epochSeconds float64) time.Time {
	t := time.Unix(int64(epochSeconds), 0).In(IST)
	return time.Date(t.Year(), t.Month(), t.Day(), 0, 0, 0, 0, time.UTC)
}

func (c *Client) HistoricalDaily(ctx context.Context, securityID, exchangeSegment, instrument string, from, to time.Time) (*Candles, error) {
	body := map[string]any{
		"securityId":      securityID,
		"exchangeSegment": exchangeSegment,
		"instrument":      instrument,
		"oi":              false,
		"fromDate":        from.Format("2006-01-02"),
		"toDate":          to.Format("2006-01-02"),
	}
	var out Candles
	if err := c.do(ctx, http.MethodPost, "/charts/historical", body, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// LTP fetches last traded prices: {"NSE_EQ": [securityID...], ...}.
func (c *Client) LTP(ctx context.Context, req map[string][]int) (map[string]any, error) {
	var out map[string]any
	err := c.do(ctx, http.MethodPost, "/marketfeed/ltp", req, &out)
	return out, err
}

// Orders returns the day's order book (after-market orders included).
func (c *Client) Orders(ctx context.Context) ([]map[string]any, error) {
	var out []map[string]any
	err := c.do(ctx, http.MethodGet, "/orders", nil, &out)
	return out, err
}

// --- Trading (guarded) -------------------------------------------------------

type Order struct {
	TransactionType string  `json:"transactionType"` // BUY | SELL
	ExchangeSegment string  `json:"exchangeSegment"` // NSE_EQ | NSE_FNO | MCX_COMM
	ProductType     string  `json:"productType"`     // CNC | MARGIN | INTRADAY
	OrderType       string  `json:"orderType"`       // MARKET | LIMIT
	Validity        string  `json:"validity"`        // DAY
	SecurityID      string  `json:"securityId"`
	Quantity        int     `json:"quantity"`
	Price           float64 `json:"price,omitempty"`
	// CorrelationID is ours, echoed back by Dhan: it is how a re-run finds
	// an order it already placed and does not place it twice.
	CorrelationID string `json:"correlationId,omitempty"`
	// AfterMarketOrder with AmoTime PRE_OPEN pumps the order into the next
	// session's pre-open call auction — the fill every backtest assumes.
	AfterMarketOrder bool   `json:"afterMarketOrder,omitempty"`
	AmoTime          string `json:"amoTime,omitempty"`
}

// PlaceOrder submits an order — ONLY when LIVE_ORDERS=yes. Otherwise it
// returns the would-be payload as an error so callers can log the dry run.
func (c *Client) PlaceOrder(ctx context.Context, o Order) (map[string]any, error) {
	if os.Getenv("LIVE_ORDERS") != "yes" {
		b, _ := json.Marshal(o)
		return nil, fmt.Errorf("DRY RUN (set LIVE_ORDERS=yes to trade): %s", string(b))
	}
	payload := map[string]any{
		"dhanClientId":    c.ClientID,
		"transactionType": o.TransactionType,
		"exchangeSegment": o.ExchangeSegment,
		"productType":     o.ProductType,
		"orderType":       o.OrderType,
		"validity":        o.Validity,
		"securityId":      o.SecurityID,
		"quantity":        o.Quantity,
	}
	if o.OrderType == "LIMIT" {
		payload["price"] = o.Price
	}
	if o.CorrelationID != "" {
		payload["correlationId"] = o.CorrelationID
	}
	if o.AfterMarketOrder {
		payload["afterMarketOrder"] = true
		payload["amoTime"] = o.AmoTime
	}
	var out map[string]any
	err := c.do(ctx, http.MethodPost, "/orders", payload, &out)
	return out, err
}
