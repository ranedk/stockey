// Command api serves a small HTTP API over systrader's REPORTING-ONLY
// research outputs, for the screener/ Nuxt frontend (a sibling repo already
// wired to stockey's separate fundamentals API on port 8000 -- this is a
// second, independent backend the same frontend also calls, not a proxy
// through stockey; stockey owns no research/signal logic per its own
// CLAUDE.md boundary).
//
// Endpoints:
//
//	GET /api/stage         internal/stage's Weinstein classifier over the
//	                       full adjusted-price universe (reporting only)
//	GET /api/paper         every tracked paper strategy, forward record first
//	GET /api/paper/{name}  one strategy: order sheet, book, NAV vs benchmarks
//	GET /api/paper/qualifiers  every stock qualifying for any strategy variant, and which
//	GET /api/rotation      industry rotation: market, industries by RS, candidates, run stats
//	                       (latest weekly snapshot from `cmd/rotation snapshot`; reporting only)
//	GET /api/rotation/industry/{code}  one industry and every member
//
// NOT hardened for public exposure -- no auth, permissive CORS. Personal
// single-user tool meant to run on localhost/trusted network next to the
// Nuxt dev server, same trust model stockey's fundamentals API documents
// for itself.
//
//	go run ./cmd/api                    # listens on :8090
//	API_PORT=9000 go run ./cmd/api      # override
package main

import (
	"context"
	"encoding/json"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/joho/godotenv"

	"github.com/ranedk/systrader/internal/paperapi"
	"github.com/ranedk/systrader/internal/rotation"
	"github.com/ranedk/systrader/internal/stageapi"
	"github.com/ranedk/systrader/internal/store"
)

func main() {
	_ = godotenv.Load()
	port := getenv("API_PORT", "8090")

	ctx := context.Background()
	st, err := store.Open(ctx)
	if err != nil {
		log.Fatalf("api: cannot open store: %v", err)
	}
	defer st.Close()

	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/health", handleHealth)
	mux.HandleFunc("GET /api/stage", handleStage(st))
	mux.HandleFunc("GET /api/paper", handlePaperList(st))
	mux.HandleFunc("GET /api/paper/qualifiers", handlePaperQualifiers(st))
	mux.HandleFunc("GET /api/paper/{name}", handlePaperDetail(st))
	mux.HandleFunc("GET /api/rotation", handleRotation(st))
	mux.HandleFunc("GET /api/rotation/industry/{code}", handleRotationIndustry(st))

	addr := ":" + port
	log.Printf("systrader api listening on %s", addr)
	if err := http.ListenAndServe(addr, withCORS(mux)); err != nil {
		log.Fatal(err)
	}
}

// handlePaperList serves every tracked strategy. The forward record sorts
// first even when it is empty: an empty forward record is the honest state of
// a strategy frozen today, and burying it under an in-sample reference curve
// would be the whole point of the exercise thrown away.
func handlePaperList(st *store.Store) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		out, err := paperapi.List(r.Context(), st)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"strategies": out})
	}
}

// handlePaperQualifiers serves, for every stock that qualifies for any variant
// of any tracked strategy, which ones — the screener's cross-strategy view.
func handlePaperQualifiers(st *store.Store) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		out, err := paperapi.Qualifications(r.Context(), st)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, out)
	}
}

func handlePaperDetail(st *store.Store) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		name := r.PathValue("name")
		out, err := paperapi.Detail(r.Context(), st, name)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, out)
	}
}

func handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// handleStage serves GET /api/stage?stages=1,2,3,4&min_bars=260&max_stale_days=21.
// All params optional; stageapi.Options.withDefaults fills in the rest.
func handleStage(st *store.Store) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query()
		opts := stageapi.Options{
			MinBars:      atoiOr(q.Get("min_bars"), 0),
			MaxStaleDays: atoiOr(q.Get("max_stale_days"), 0),
			Stages:       parseStages(q.Get("stages")),
		}
		result, err := stageapi.List(r.Context(), st, opts, time.Now())
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, result)
	}
}

func parseStages(raw string) []int {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil
	}
	var out []int
	for _, part := range strings.Split(raw, ",") {
		if n, err := strconv.Atoi(strings.TrimSpace(part)); err == nil && n >= 1 && n <= 4 {
			out = append(out, n)
		}
	}
	return out
}

func atoiOr(raw string, def int) int {
	if raw == "" {
		return def
	}
	n, err := strconv.Atoi(raw)
	if err != nil {
		return def
	}
	return n
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

// withCORS is deliberately permissive (Access-Control-Allow-Origin: *) --
// see package doc comment on the trust model this assumes.
func withCORS(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func getenv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

// handleRotation serves the latest rotation snapshot as computed by `cmd/rotation snapshot`
// (docs/SECTOR_ROTATION_PRD.md). The full stock list is ~2,000 rows; ?all=1 returns it,
// otherwise only candidates are sent and the industry page fetches its own members.
func handleRotation(st *store.Store) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		snap, ok := loadRotation(w, r, st)
		if !ok {
			return
		}
		if r.URL.Query().Get("all") != "1" {
			var cands []rotation.StockView
			for _, s := range snap.Stocks {
				if s.Candidate {
					cands = append(cands, s)
				}
			}
			snap.Stocks = cands
		}
		writeJSON(w, http.StatusOK, snap)
	}
}

func handleRotationIndustry(st *store.Store) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		snap, ok := loadRotation(w, r, st)
		if !ok {
			return
		}
		code := r.PathValue("code")
		var ind *rotation.IndustryView
		for i := range snap.Industries {
			if snap.Industries[i].Code == code {
				ind = &snap.Industries[i]
			}
		}
		if ind == nil {
			writeJSON(w, http.StatusNotFound, map[string]string{"error": "unknown industry " + code})
			return
		}
		var members []rotation.StockView
		for _, s := range snap.Stocks {
			if s.IndustryCode == code {
				members = append(members, s)
			}
		}
		writeJSON(w, http.StatusOK, map[string]any{"as_of": snap.AsOf, "market": snap.Market, "industry": ind, "stocks": members})
	}
}

func loadRotation(w http.ResponseWriter, r *http.Request, st *store.Store) (rotation.Snapshot, bool) {
	var snap rotation.Snapshot
	raw, _, err := st.LatestRotationSnapshot(r.Context())
	if err != nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "no rotation snapshot yet: run `go run ./cmd/rotation snapshot` (" + err.Error() + ")"})
		return snap, false
	}
	if err := json.Unmarshal(raw, &snap); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return snap, false
	}
	return snap, true
}
