from __future__ import annotations

import argparse
import json

import pandas as pd

from advisory import dashboard, execution_engine, news_watch, pipeline, portfolio_engine, position_lifecycle, setup_trace, symbol_trace


def test_portfolio_engine_overlap_cap(monkeypatch):
    allocations = pd.DataFrame(
        [
            {
                "published_on": pd.Timestamp("2026-03-22T09:00:00Z"),
                "asof_date": pd.Timestamp("2026-03-22T00:00:00Z"),
                "setup_id": "TEST",
                "setup_name": "Test",
                "symbol": "HDFCBANK",
                "company_master_id": "nse:HDFCBANK",
                "unique_id": "u1",
                "confidence": 0.90,
                "conviction_bucket": "high",
                "risk_bucket": "medium",
                "suggested_allocation_inr": 40000.0,
                "allocation_pct_of_adv20d": 0.0001,
                "stop_price": 100.0,
                "invalidation_price": 95.0,
                "invalidation_rule": "rule",
                "notes": None,
                "context_snapshot_json": "{}",
            },
            {
                "published_on": pd.Timestamp("2026-03-22T10:00:00Z"),
                "asof_date": pd.Timestamp("2026-03-22T00:00:00Z"),
                "setup_id": "TEST",
                "setup_name": "Test",
                "symbol": "ICICIBANK",
                "company_master_id": "nse:ICICIBANK",
                "unique_id": "u2",
                "confidence": 0.88,
                "conviction_bucket": "high",
                "risk_bucket": "medium",
                "suggested_allocation_inr": 35000.0,
                "allocation_pct_of_adv20d": 0.0001,
                "stop_price": 100.0,
                "invalidation_price": 95.0,
                "invalidation_rule": "rule",
                "notes": None,
                "context_snapshot_json": "{}",
            },
            {
                "published_on": pd.Timestamp("2026-03-22T11:00:00Z"),
                "asof_date": pd.Timestamp("2026-03-22T00:00:00Z"),
                "setup_id": "TEST",
                "setup_name": "Test",
                "symbol": "RELIANCE",
                "company_master_id": "nse:RELIANCE",
                "unique_id": "u3",
                "confidence": 0.80,
                "conviction_bucket": "medium",
                "risk_bucket": "medium_high",
                "suggested_allocation_inr": 30000.0,
                "allocation_pct_of_adv20d": 0.0001,
                "stop_price": 100.0,
                "invalidation_price": 95.0,
                "invalidation_rule": "rule",
                "notes": None,
                "context_snapshot_json": "{}",
            },
        ]
    )

    monkeypatch.setattr(portfolio_engine, "load_allocations", lambda **kwargs: allocations.copy())
    monkeypatch.setattr(
        portfolio_engine,
        "build_overlap_map",
        lambda symbols: {
            "HDFCBANK": ("peer_cluster:1", "peer_cluster"),
            "ICICIBANK": ("peer_cluster:1", "peer_cluster"),
            "RELIANCE": ("symbol:RELIANCE", "symbol_only"),
        },
    )

    df = portfolio_engine.build_portfolio_orders(
        config=portfolio_engine.PortfolioConfig(
            capital_inr=120000.0,
            max_positions=3,
            single_position_cap_pct=1.0,
            per_setup_cap_pct=1.0,
            max_positions_per_overlap_group=1,
        )
    )

    status_map = df.set_index("symbol")["portfolio_status"].to_dict()
    reason_map = df.set_index("symbol")["portfolio_reason"].to_dict()
    assert status_map["HDFCBANK"] == "approved"
    assert status_map["ICICIBANK"] == "deferred"
    assert reason_map["ICICIBANK"] == "overlap_cap"
    assert status_map["RELIANCE"] == "approved"


def test_position_lifecycle_classification_paths():
    hold_row = pd.Series(
        {
            "entry_price": 100.0,
            "current_price": 106.0,
            "stop_price": 95.0,
            "invalidation_price": 90.0,
            "pnl_pct": 6.0,
            "days_held": 5,
        }
    )
    status, _, action, _ = position_lifecycle.classify_position(
        hold_row,
        review_stale_days=20,
        tighten_stop_gain_pct=10.0,
        trim_winner_gain_pct=15.0,
    )
    assert status == "open"
    assert action == "hold"

    exit_row = pd.Series(
        {
            "entry_price": 100.0,
            "current_price": 88.0,
            "stop_price": 95.0,
            "invalidation_price": 90.0,
            "pnl_pct": -12.0,
            "days_held": 21,
        }
    )
    status, _, action, _ = position_lifecycle.classify_position(
        exit_row,
        review_stale_days=20,
        tighten_stop_gain_pct=10.0,
        trim_winner_gain_pct=15.0,
    )
    assert status == "exit_review"
    assert action == "exit_invalidation"


def test_execution_engine_builds_planned_orders(monkeypatch):
    portfolio_orders = pd.DataFrame(
        [
            {
                "published_on": pd.Timestamp("2026-03-23T09:00:00Z"),
                "asof_date": pd.Timestamp("2026-03-23T00:00:00Z"),
                "setup_id": "TEST",
                "symbol": "HDFCBANK",
                "unique_id": "uid-1",
                "company_master_id": "nse:HDFCBANK",
                "approved_allocation_inr": 40000.0,
            }
        ]
    )
    latest_closes = pd.DataFrame(
        [{"symbol": "HDFCBANK", "date": pd.Timestamp("2026-03-23T00:00:00Z"), "close": 800.0}]
    )

    monkeypatch.setattr(execution_engine, "load_portfolio_orders", lambda **kwargs: portfolio_orders.copy())
    monkeypatch.setattr(execution_engine, "load_latest_closes", lambda symbols, asof_date: latest_closes.copy())
    monkeypatch.setattr(
        execution_engine,
        "resolve_dhan_identity",
        lambda symbol, exchange, asset_type="stock": {
            "security_id": 1333,
            "exchange_segment": "NSE_EQ",
        },
    )

    df = execution_engine.build_execution_orders(asof_date=pd.Timestamp("2026-03-23T00:00:00Z"))
    row = df.iloc[0]
    assert row["execution_status"] == "planned"
    assert row["quantity"] == 50
    assert row["security_id"] == 1333
    assert row["exchange_segment"] == "NSE_EQ"
    assert len(row["correlation_id"]) <= 30


def test_pipeline_portfolio_stage_outputs_json(monkeypatch, capsys):
    portfolio_df = pd.DataFrame(
        [
            {
                "published_on": pd.Timestamp("2026-03-23T09:00:00Z"),
                "symbol": "HDFCBANK",
                "approved_allocation_inr": 40000.0,
            }
        ]
    )

    monkeypatch.setattr(
        pipeline,
        "parse_args",
        lambda: argparse.Namespace(
            date=None,
            symbols=None,
            setup_ids=None,
            start_at="portfolio",
            stop_at="portfolio",
            rebuild=False,
            skip_peer_sync=False,
            include_watch=False,
            include_news=False,
            include_lifecycle=False,
            include_execution=False,
            live_execution=False,
            execution_reconcile=False,
            eval_include_evaluated=False,
            portfolio_capital_inr=300000.0,
            portfolio_max_positions=5,
            portfolio_single_position_cap_pcnt=0.35,
            portfolio_single_position_cap_pct=0.35,
            portfolio_per_setup_cap_pct=0.50,
            portfolio_max_positions_per_overlap_group=1,
            event_model=None,
            dry_run=True,
        ),
    )
    monkeypatch.setattr(pipeline, "build_portfolio_orders", lambda **kwargs: portfolio_df.copy())

    rc = pipeline.main()
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"
    assert "portfolio" in out["stages"]
    assert out["stages"]["portfolio"]["row_count"] == 1


def test_news_watch_matches_symbol_in_title(monkeypatch):
    watchlist = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-03-24T00:00:00Z"),
                "setup_id": "TEST",
                "setup_name": "Test",
                "symbol": "IGL",
                "company_master_id": "nse:IGL",
                "watch_reasons_json": '["test"]',
            }
        ]
    )
    news_items = pd.DataFrame(
        [
            {
                "source_name": "Economic Times RSS",
                "feed_name": "stocks",
                "guid": "guid-1",
                "title": "Castrol India, IGL among 5 energy stocks that hit 52-week lows",
                "link": "https://example.test/igl",
                "description": "A short ET item.",
                "categories_json": "[]",
                "published_on": pd.Timestamp("2026-03-24T11:12:46Z"),
            }
        ]
    )

    monkeypatch.setattr(
        news_watch,
        "load_watch_company_meta",
        lambda company_master_ids: pd.DataFrame(
            [
                {
                    "company_master_id": "nse:IGL",
                    "nse_ticker": "IGL",
                    "bse_ticker": None,
                    "company_name": "Indraprastha Gas Ltd.",
                }
            ]
        ),
    )

    df = news_watch.build_news_events(watchlist=watchlist, news_items=news_items)
    assert len(df) == 1
    row = df.iloc[0]
    assert row["symbol"] == "IGL"
    assert row["event_source"] == "economic_times_rss"
    assert row["match_score"] >= 4.0


def test_symbol_trace_builds_stage_summary(monkeypatch):
    monkeypatch.setattr(
        symbol_trace,
        "load_latest_screener_rows",
        lambda symbol, setup_id=None: pd.DataFrame([{"ticker": symbol, "screener_slug": "demo"}]),
    )
    monkeypatch.setattr(
        symbol_trace,
        "latest_single_row",
        lambda table_name, **kwargs: {
            "advisory_technical_daily": {"symbol": "HDFCBANK", "asof_date": pd.Timestamp("2026-03-23T00:00:00Z")},
            "advisory_fundamentals_daily": {"symbol": "HDFCBANK", "asof_date": pd.Timestamp("2026-03-23T00:00:00Z")},
            "advisory_candidates": None,
            "advisory_watchlist": None,
            "advisory_event_evaluations": None,
            "advisory_allocations": None,
            "advisory_portfolio_orders": None,
            "advisory_position_lifecycle": None,
            "advisory_execution_orders": None,
        }.get(table_name),
    )
    monkeypatch.setattr(symbol_trace, "load_latest_rejections", lambda symbol, setup_id=None: pd.DataFrame([{"reason_code": "regime_not_allowed"}]))
    monkeypatch.setattr(
        symbol_trace,
        "latest_rows",
        lambda table_name, **kwargs: pd.DataFrame(),
    )

    trace = symbol_trace.build_trace("HDFCBANK")
    assert trace["stage_summary"]["in_latest_screener"] is True
    assert trace["stage_summary"]["has_technical_snapshot"] is True
    assert trace["stage_summary"]["passed_rule_engine"] is False
    assert trace["decision_summary"]["latest_rejection_reasons"] == ["regime_not_allowed"]


def test_setup_trace_builds_funnel_summary(monkeypatch):
    monkeypatch.setattr(setup_trace, "resolve_asof_date", lambda setup_id, requested_date=None: pd.Timestamp("2026-03-24T00:00:00Z"))
    monkeypatch.setattr(setup_trace, "load_setup_regime", lambda asof_date: {"regime_name": "RISK_OFF"})
    monkeypatch.setattr(
        setup_trace,
        "load_latest_setup_screener",
        lambda setup_id: pd.DataFrame([{"ticker": "A"}, {"ticker": "B"}, {"ticker": "C"}]),
    )

    def fake_load_setup_rows(table_name, setup_id, asof_date, limit=50):
        mapping = {
            "advisory_candidates": pd.DataFrame([{"symbol": "A"}]),
            "advisory_candidate_rejections": pd.DataFrame([{"reason_code": "regime_not_allowed"}, {"reason_code": "liquidity_below_min"}]),
            "advisory_watchlist": pd.DataFrame([{"symbol": "A"}]),
            "advisory_watch_events": pd.DataFrame(),
            "advisory_news_events": pd.DataFrame([{"symbol": "A"}]),
            "advisory_event_evaluations": pd.DataFrame([{"symbol": "A", "verdict": "review_manual", "event_source": "economic_times_rss"}]),
            "advisory_allocations": pd.DataFrame(),
            "advisory_portfolio_orders": pd.DataFrame(),
            "advisory_position_lifecycle": pd.DataFrame(),
            "advisory_execution_orders": pd.DataFrame(),
        }
        return mapping[table_name]

    monkeypatch.setattr(setup_trace, "load_setup_rows", fake_load_setup_rows)
    monkeypatch.setattr(setup_trace, "load_top_rejection_reasons", lambda setup_id, asof_date: {"regime_not_allowed": 1, "liquidity_below_min": 1})

    trace = setup_trace.build_trace("LARGECAP_BREAKOUT_V1")
    assert trace["stage_summary"]["screener_universe_count"] == 3
    assert trace["stage_summary"]["candidate_count"] == 1
    assert trace["stage_summary"]["news_event_count"] == 1
    assert trace["funnel_summary"]["screener_to_candidate"] == round(1 / 3, 4)
    assert trace["decision_summary"]["latest_event_source"] == "economic_times_rss"


def test_dashboard_aggregates_setup_traces(monkeypatch):
    monkeypatch.setattr(
        dashboard,
        "load_setup_registry",
        lambda: [
            {"setup_id": "A", "setup_name": "Setup A", "screener_slug": "screen-a"},
            {"setup_id": "B", "setup_name": "Setup B", "screener_slug": "screen-b"},
        ],
    )
    monkeypatch.setattr(
        dashboard,
        "build_trace",
        lambda setup_id, asof_date=None: {
            "stage_summary": {
                "asof_date": "2026-03-24T00:00:00+00:00",
                "regime_name": "RISK_OFF",
                "screener_universe_count": 10 if setup_id == "A" else 20,
                "candidate_count": 1,
                "rejection_count": 3,
                "watchlist_count": 1,
                "announcement_event_count": 0,
                "news_event_count": 0,
                "evaluation_count": 0,
                "allocation_count": 0,
                "portfolio_count": 0,
                "execution_count": 0,
            },
            "funnel_summary": {
                "screener_to_candidate": 0.1,
                "candidate_to_watchlist": 1.0,
                "evaluation_to_allocation": None,
            },
            "decision_summary": {
                "latest_event_verdict": None,
                "latest_event_source": None,
                "latest_portfolio_status": None,
                "top_rejection_reasons": {"regime_not_allowed": 2},
            },
        },
    )

    df = dashboard.build_dashboard()
    assert len(df) == 2
    assert set(df["setup_id"]) == {"A", "B"}
    assert df.loc[df["setup_id"] == "A", "screener_universe_count"].iloc[0] == 10
