from __future__ import annotations

import argparse
import json

import pandas as pd

from advisory import dashboard, execution_engine, llm_event_evaluator, master_pipeline, news_overlay_engine, news_theme_engine, news_watch, pipeline, portfolio_engine, position_lifecycle, risk_engine, rule_engine, setup_registry, setup_trace, symbol_trace, watchlist_builder


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


def test_event_normalization_produces_taxonomy_and_transition():
    event_row = pd.Series(
        {
            "subject": "Company wins major order from defence ministry",
            "filed_under_category": "Announcements",
            "concise_summary_text": "The company received a large order award.",
            "categories_json": '["orders"]',
        }
    )
    parsed = llm_event_evaluator.EventEvaluation(
        what_happened="The company won a major order.",
        sentiment="positive",
        materiality="high",
        setup_effect="strengthens",
        governance_risk="none",
        balance_sheet_risk="none",
        execution_risk="low",
        investable_now=True,
        verdict="continue",
        event_class="ORDER_WIN",
        state_transition_hint="UPGRADE_TO_PASS_NOW",
        score_impact=0.4,
        confidence=0.9,
        rationale="Large positive order supports the setup.",
        source_trace=["subject"],
        key_risks=[],
    )

    event_class, transition, score_impact = llm_event_evaluator.normalize_event_evaluation(event_row, parsed)
    assert event_class == "ORDER_WIN"
    assert transition == "UPGRADE_TO_PASS_NOW"
    assert score_impact > 0


def test_event_normalization_prefers_llm_event_class_over_keyword_fallback():
    event_row = pd.Series(
        {
            "subject": "Disclosure of material issue",
            "filed_under_category": "Disclosure of material issue",
            "concise_summary_text": "NCLT uploaded the first motion order in a scheme of amalgamation.",
            "categories_json": '["AMALGAMATION"]',
        }
    )
    parsed = llm_event_evaluator.EventEvaluation(
        what_happened="Procedural update in an amalgamation process.",
        sentiment="mixed",
        materiality="medium",
        setup_effect="neutral",
        governance_risk="low",
        balance_sheet_risk="none",
        execution_risk="low",
        investable_now=False,
        verdict="review_manual",
        event_class="OTHER",
        state_transition_hint="REVIEW_MANUAL",
        score_impact=0.0,
        confidence=0.6,
        rationale="This is procedural rather than a clean taxonomy fit.",
        source_trace=["summary"],
        key_risks=[],
    )

    event_class, transition, score_impact = llm_event_evaluator.normalize_event_evaluation(event_row, parsed)
    assert event_class == "OTHER"
    assert transition == "REVIEW_MANUAL"
    assert score_impact < 0


def test_event_normalization_does_not_misclassify_director_update_as_order_win():
    event_row = pd.Series(
        {
            "subject": "Change in Director(s)",
            "filed_under_category": "Change in Director(s)",
            "concise_summary_text": "The company said its director will continue to hold charge under ministry order no. 123.",
            "categories_json": '["WORK_ORDER_CONTRACT"]',
        }
    )
    parsed = llm_event_evaluator.EventEvaluation(
        what_happened="Administrative continuation of director responsibilities.",
        sentiment="neutral",
        materiality="low",
        setup_effect="neutral",
        governance_risk="low",
        balance_sheet_risk="none",
        execution_risk="none",
        investable_now=True,
        verdict="continue",
        event_class="OTHER",
        state_transition_hint="NO_CHANGE",
        score_impact=0.0,
        confidence=0.8,
        rationale="This is a routine management update.",
        source_trace=["subject"],
        key_risks=[],
    )

    event_class, transition, score_impact = llm_event_evaluator.normalize_event_evaluation(event_row, parsed)
    assert event_class == "OTHER"
    assert transition == "NO_CHANGE"
    assert score_impact == 0.0



def test_event_normalization_keeps_positive_non_investable_event_out_of_reject_state():
    event_row = pd.Series(
        {
            "subject": "Press Release",
            "filed_under_category": "Press Release",
            "concise_summary_text": "The company launched a major new therapy and expanded its product lineup.",
            "categories_json": '["product_launch"]',
        }
    )
    parsed = llm_event_evaluator.EventEvaluation(
        what_happened="A meaningful product launch strengthens the setup but is not enough for immediate entry.",
        sentiment="positive",
        materiality="medium",
        setup_effect="strengthens",
        governance_risk="none",
        balance_sheet_risk="none",
        execution_risk="medium",
        investable_now=False,
        verdict="continue",
        event_class="OTHER",
        state_transition_hint="RAISE_SCORE_ONLY",
        score_impact=0.0,
        confidence=0.7,
        rationale="Constructive event, but not a standalone trigger.",
        source_trace=["summary"],
        key_risks=[],
    )

    event_class, transition, score_impact = llm_event_evaluator.normalize_event_evaluation(event_row, parsed)
    assert event_class == "OTHER"
    assert transition == "RAISE_SCORE_ONLY"
    assert score_impact > 0


def test_news_theme_engine_normalizes_richer_investment_theme_schema(tmp_path):
    config_path = tmp_path / "investment_themes.yaml"
    config_path.write_text(
        """
themes:
  - theme_id: DEFENSE_INDIGENISATION
    name: Defense Indigenisation
    status: active
    description: Defense manufacturing theme.
    classification:
      market_cap_fit: [large_cap, mid_cap]
      holding_profile: structural
      holding_period_days: {min: 90, max: 365}
      risk_level: medium
      exit_trigger_types: [thesis, technical]
    detection:
      keywords:
        include: [defense, order, procurement]
        exclude: [rumor]
      match:
        title_weight: 3
        description_weight: 1
        min_hit_score: 2
        max_titles_for_reason: 2
    routing:
      primary_pipeline: event_opportunity_pipeline
      downstream_agents: [news_theme_expert, screener_designer]
      output_mode: idea_candidates
      priority: 80
    screener_templates:
      - template_id: defense-indigenisation-v1
        provider: screenerin
        slug: defense-indigenisation-v1
        label: DEFENSE INDIGENISATION V1
        holding_horizon_note: 1 to 5 years
        entry_style: breakout_or_trend_continuation
        query: |
          Market Capitalization > 1000
    decay:
      model: medium
      half_life_days: 45
    portfolio_guidance:
      positive_sectors: [defense, aerospace]
      ideal_screener_logic: order wins and quality balance sheet
      invalidation_signals: [delayed orders]
""",
        encoding="utf-8",
    )

    themes = news_theme_engine.load_theme_config(config_path=str(config_path))
    assert len(themes) == 1
    theme = themes[0]
    assert theme["theme_id"] == "DEFENSE_INDIGENISATION"
    assert theme["recommended_agent_roles"] == ["news_theme_expert", "screener_designer"]
    assert theme["recommended_pipeline_branches"] == ["event_opportunity_pipeline"]
    assert theme["market_cap_fit"] == ["large_cap", "mid_cap"]
    assert theme["ideal_screener_logic"] == "order wins and quality balance sheet"
    assert theme["negative_keywords"] == ["rumor"]
    assert theme["suggested_screeners"][0]["screener_query"] == "Market Capitalization > 1000"


def test_news_theme_engine_active_mapping_carries_roles_and_pipeline(monkeypatch):
    monkeypatch.setattr(
        news_theme_engine,
        "build_theme_recommendations",
        lambda **kwargs: {
            "asof_date": pd.Timestamp("2026-03-31T00:00:00Z"),
            "recommendations": [
                {
                    "theme_id": "DEFENSE_INDIGENISATION",
                    "recommended_agent_roles": ["news_theme_expert", "risk_portfolio_expert"],
                    "recommended_pipeline_branches": ["event_opportunity_pipeline"],
                },
                {
                    "theme_id": "POWER_GRID_STORAGE",
                    "recommended_agent_roles": ["screener_designer"],
                    "recommended_pipeline_branches": ["theme_watch_pipeline"],
                },
            ],
            "error": None,
        },
    )
    monkeypatch.setattr(
        news_theme_engine,
        "list_theme_screeners",
        lambda theme_id=None: pd.DataFrame(
            [
                {"theme_id": "DEFENSE_INDIGENISATION", "screener_slug": "defense-indigenisation-v1", "is_active": True},
                {"theme_id": "POWER_GRID_STORAGE", "screener_slug": "power-grid-storage-v1", "is_active": True},
            ]
        ),
    )

    mapping = news_theme_engine.load_active_theme_screener_mapping(asof_date=pd.Timestamp("2026-03-31T00:00:00Z"))
    assert mapping["theme_ids"] == ["DEFENSE_INDIGENISATION", "POWER_GRID_STORAGE"]
    assert mapping["screener_slugs"] == ["defense-indigenisation-v1", "power-grid-storage-v1"]
    assert mapping["recommended_agent_roles"] == ["news_theme_expert", "risk_portfolio_expert", "screener_designer"]
    assert mapping["recommended_pipeline_branches"] == ["event_opportunity_pipeline", "theme_watch_pipeline"]


def test_master_pipeline_builds_sub_agent_workflow_with_pending_screeners(monkeypatch):
    monkeypatch.setattr(
        master_pipeline,
        "build_overlay_state",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "asof_date": pd.Timestamp("2026-03-31T00:00:00Z"),
                    "base_regime": "RISK_OFF",
                    "overlay_name": "OIL_SHOCK",
                    "overlay_intensity": 1.0,
                    "overlay_reason": "oil news cluster",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        master_pipeline,
        "build_theme_recommendations",
        lambda **kwargs: {
            "asof_date": pd.Timestamp("2026-03-31T00:00:00Z"),
            "recommendations": [
                {
                    "theme_id": "ENERGY_SUPPLY_SHOCK",
                    "theme_name": "Energy Supply Shock",
                    "theme_intensity": 0.9,
                    "theme_reason": "crude spike",
                    "recommended_agent_roles": ["news_theme_expert", "screener_designer"],
                    "recommended_pipeline_branches": ["event_opportunity_pipeline"],
                    "suggested_screeners": [
                        {
                            "slug": "news-energy-supply-shock-beneficiaries-v1",
                            "screener_name": "NEWS ENERGY SUPPLY SHOCK BENEFICIARIES V1",
                            "screener_query": "Market Capitalization > 2000",
                        }
                    ],
                }
            ],
        },
    )
    monkeypatch.setattr(
        master_pipeline,
        "load_active_theme_screener_mapping",
        lambda **kwargs: {
            "theme_ids": ["ENERGY_SUPPLY_SHOCK"],
            "screener_slugs": [],
            "recommended_agent_roles": ["news_theme_expert", "screener_designer"],
            "recommended_pipeline_branches": ["event_opportunity_pipeline"],
        },
    )
    monkeypatch.setattr(master_pipeline, "list_theme_screeners", lambda *args, **kwargs: pd.DataFrame())

    workflow = master_pipeline.build_sub_agent_workflow(asof_date=pd.Timestamp("2026-03-31T00:00:00Z"))
    assert workflow["base_regime"] == "RISK_OFF"
    assert workflow["news_overlay"] == "OIL_SHOCK"
    assert "data_pipeline_expert" in workflow["recommended_agent_roles"]
    assert "event_opportunity_pipeline" in workflow["pipeline_branches"]
    assert workflow["themes"][0]["pending_screeners"][0]["slug"] == "news-energy-supply-shock-beneficiaries-v1"
    assert any(task["agent_role"] == "screener_designer" for task in workflow["tasks"])


def test_portfolio_priority_rewards_positive_event_transition():
    base = pd.Series(
        {
            "confidence": 0.8,
            "conviction_bucket": "medium",
            "risk_bucket": "medium",
            "suggested_allocation_inr": 50000.0,
            "allocation_pct_of_adv20d": 0.0001,
            "score_impact": 0.0,
            "state_transition_hint": "NO_CHANGE",
        }
    )
    upgraded = base.copy()
    upgraded["score_impact"] = 0.3
    upgraded["state_transition_hint"] = "UPGRADE_TO_PASS_NOW"

    assert portfolio_engine.compute_priority_score(upgraded) > portfolio_engine.compute_priority_score(base)


def test_watchlist_builder_applies_event_state_transition(monkeypatch):
    monkeypatch.setattr(
        watchlist_builder,
        "load_candidate_rows",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "asof_date": pd.Timestamp("2026-03-24T00:00:00Z"),
                    "setup_id": "TEST",
                    "setup_name": "Test",
                    "regime_name": "STABLE",
                    "symbol": "ABC",
                    "company_master_id": "nse:ABC",
                    "screener_slug": "demo",
                    "rank": 1,
                    "candidate_state": "WATCH_EVENT",
                    "setup_score": 0.55,
                    "watch_reason_detail": "waiting for event",
                    "entry_style": "RETEST_OF_PRIOR_BREAKOUT",
                    "attractive_price_low": 100.0,
                    "attractive_price_high": 105.0,
                    "invalidation_price": 95.0,
                    "entry_note": "wait",
                    "near_miss_flag": False,
                    "watch_enabled": True,
                    "watch_reasons": '["results"]',
                }
            ]
        ),
    )
    monkeypatch.setattr(
        watchlist_builder,
        "load_latest_event_transitions",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "setup_id": "TEST",
                    "symbol": "ABC",
                    "event_class": "RESULTS_POSITIVE",
                    "state_transition_hint": "UPGRADE_TO_PASS_NOW",
                    "score_impact": 0.3,
                }
            ]
        ),
    )
    monkeypatch.setattr(watchlist_builder, "sql_to_df", lambda *args, **kwargs: pd.DataFrame())

    df = watchlist_builder.build_watchlist()
    row = df.iloc[0]
    assert row["candidate_state"] == "WATCH_EVENT"
    assert row["current_state"] == "PASS_NOW"
    assert row["watch_status"] == "active"
    assert row["last_event_class"] == "RESULTS_POSITIVE"


def test_watchlist_builder_promotes_raise_score_only_when_adjusted_score_crosses_threshold(monkeypatch):
    monkeypatch.setattr(
        watchlist_builder,
        "load_candidate_rows",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "asof_date": pd.Timestamp("2026-03-24T00:00:00Z"),
                    "setup_id": "TEST",
                    "setup_name": "Test",
                    "regime_name": "STABLE",
                    "symbol": "ABC",
                    "company_master_id": "nse:ABC",
                    "screener_slug": "demo",
                    "rank": 1,
                    "candidate_state": "WATCH_EVENT",
                    "setup_score": 0.55,
                    "watch_reason_detail": "waiting for event",
                    "entry_style": "RETEST_OF_PRIOR_BREAKOUT",
                    "attractive_price_low": 100.0,
                    "attractive_price_high": 105.0,
                    "invalidation_price": 95.0,
                    "entry_note": "wait",
                    "near_miss_flag": False,
                    "watch_enabled": True,
                    "watch_reasons": '["results"]',
                }
            ]
        ),
    )
    monkeypatch.setattr(
        watchlist_builder,
        "load_latest_event_transitions",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "setup_id": "TEST",
                    "symbol": "ABC",
                    "event_class": "OTHER",
                    "state_transition_hint": "RAISE_SCORE_ONLY",
                    "score_impact": 0.2,
                }
            ]
        ),
    )
    monkeypatch.setattr(
        watchlist_builder,
        "load_setup_thresholds",
        lambda: {"TEST": {"pass_now": 0.68, "watch_breakout": 0.58, "watch_event": 0.48}},
    )
    monkeypatch.setattr(watchlist_builder, "sql_to_df", lambda *args, **kwargs: pd.DataFrame())

    df = watchlist_builder.build_watchlist()
    row = df.iloc[0]
    assert row["current_state"] == "PASS_NOW"
    assert row["watch_status"] == "active"
    assert row["last_state_transition_hint"] == "RAISE_SCORE_ONLY"


def test_setup_registry_normalizes_multi_screener_overlay_config(tmp_path):
    config_path = tmp_path / "advisory_setups.yaml"
    config_path.write_text(
        """
setups:
  - setup_id: TEST_SETUP
    setup_name: Test setup
    screeners: [screen-a, screen-b]
    screener_mode: intersection
    allowed_regimes: [STABLE]
    allowed_overlays: [NONE, TARIFF_PRESSURE]
    blocked_overlays: [GEOPOLITICAL_RISK]
    overlay_screeners:
      TARIFF_PRESSURE:
        add: [screen-c]
        remove: [screen-a]
""".strip(),
        encoding="utf-8",
    )

    setups = setup_registry.load_setup_registry(str(config_path))
    row = setups[0]
    assert row["screeners"] == ["screen-a", "screen-b"]
    assert row["screener_mode"] == "intersection"
    assert row["allowed_overlays"] == ["NONE", "TARIFF_PRESSURE"]
    assert row["blocked_overlays"] == ["GEOPOLITICAL_RISK"]
    assert row["overlay_screeners"]["TARIFF_PRESSURE"]["add"] == ["screen-c"]
    assert row["overlay_screeners"]["TARIFF_PRESSURE"]["remove"] == ["screen-a"]


def test_news_overlay_engine_classifies_tariff_overlay():
    regime_row = {"regime_name": "STABLE_BUT_TARIFF_RISING", "tariff_pressure_flag": True}
    news_rows = pd.DataFrame(
        [
            {"title": "Tariff pressure rises on imports", "description": "New import duty and tariff measures announced."},
            {"title": "Trade war concerns increase", "description": "Tariff commentary dominates market outlook."},
        ]
    )
    overlay_name, intensity, reason, source_count = news_overlay_engine.classify_overlay(regime_row, news_rows)
    assert overlay_name == "TARIFF_PRESSURE"
    assert intensity > 0
    assert source_count == 2
    assert "tariff" in reason.lower()


def test_rule_engine_resolves_overlay_screeners_and_blocks_disallowed_overlay():
    setup = {
        "setup_id": "TEST",
        "screeners": ["screen-a", "screen-b"],
        "screener_mode": "union",
        "allowed_regimes": ["STABLE"],
        "allowed_overlays": ["NONE"],
        "blocked_overlays": ["GEOPOLITICAL_RISK"],
        "overlay_screeners": {
            "TARIFF_PRESSURE": {"add": ["screen-c"], "remove": ["screen-a"]},
        },
        "score_thresholds": {"pass_now": 0.68, "watch_breakout": 0.58, "watch_event": 0.48, "near_miss_gap": 0.05},
    }
    active_screeners, mode, theme_ids = rule_engine.resolve_setup_screeners(setup, "TARIFF_PRESSURE")
    assert active_screeners == ["screen-b", "screen-c"]
    assert mode == "union"
    assert theme_ids == []

    row = pd.Series(
        {
            "company_master_id": "nse:ABC",
            "adj_close": 100.0,
            "fundamentals_freshness_status": "fresh",
            "market_cap": 100000.0,
            "avg_traded_value_20d": 1000000000.0,
            "breakout_extension_pct": 1.0,
            "pass_above_dma_20": True,
            "pass_above_dma_50": True,
            "pass_above_dma_200": True,
            "rs_vs_benchmark": 0.1,
            "rs_vs_sector": 0.1,
        }
    )
    candidate_state, _, rejections = rule_engine.evaluate_setup_row(
        row,
        regime_name="STABLE",
        overlay_name="GEOPOLITICAL_RISK",
        setup=setup,
    )
    assert candidate_state == "REJECT"
    assert any(item["reason_code"] == "overlay_not_allowed" for item in rejections)


def test_risk_engine_keeps_investable_review_manual_as_allocated(monkeypatch):
    monkeypatch.setattr(
        risk_engine,
        "load_event_evaluations",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "published_on": pd.Timestamp("2026-03-24T00:00:00Z"),
                    "asof_date": pd.Timestamp("2026-03-20T00:00:00Z"),
                    "setup_id": "DEFENSIVE_REGIME_POSITION_V1",
                    "setup_name": "Defensive regime position",
                    "symbol": "ABC",
                    "company_master_id": "nse:ABC",
                    "unique_id": "ABC-1",
                    "evaluation_status": "completed",
                    "verdict": "review_manual",
                    "investable_now": True,
                    "materiality": "low",
                    "setup_effect": "neutral",
                    "event_class": "OTHER",
                    "state_transition_hint": "REVIEW_MANUAL",
                    "score_impact": 0.0,
                    "confidence": 0.8,
                    "sentiment": "neutral",
                    "governance_risk": "none",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        risk_engine,
        "load_point_in_time_context",
        lambda symbol, published_on: {
            "avg_traded_value_20d": 1_000_000_000.0,
            "adj_close": 100.0,
            "atr_20": 5.0,
            "dma_20": 98.0,
            "dma_50": 95.0,
            "dma_200": 90.0,
            "rs_vs_benchmark": 0.1,
            "rs_vs_sector": 0.1,
        },
    )

    df = risk_engine.build_allocations(asof_date=pd.Timestamp("2026-03-20T00:00:00Z"))
    row = df.iloc[0]
    assert row["allocation_status"] == "allocated"
    assert row["suggested_allocation_inr"] > 0
    assert "manual review" in str(row["notes"]).lower()


def test_risk_engine_promoted_pass_now_overrides_conservative_event_investable_flag(monkeypatch):
    monkeypatch.setattr(
        risk_engine,
        "load_event_evaluations",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "published_on": pd.Timestamp("2026-03-24T00:00:00Z"),
                    "asof_date": pd.Timestamp("2026-03-20T00:00:00Z"),
                    "setup_id": "DEFENSIVE_REGIME_POSITION_V1",
                    "setup_name": "Defensive regime position",
                    "symbol": "ABC",
                    "company_master_id": "nse:ABC",
                    "unique_id": "ABC-2",
                    "evaluation_status": "completed",
                    "verdict": "review_manual",
                    "investable_now": False,
                    "materiality": "medium",
                    "setup_effect": "strengthens",
                    "event_class": "OTHER",
                    "state_transition_hint": "RAISE_SCORE_ONLY",
                    "score_impact": 0.2,
                    "confidence": 0.8,
                    "sentiment": "positive",
                    "governance_risk": "none",
                    "has_review_manual": True,
                }
            ]
        ),
    )
    monkeypatch.setattr(
        risk_engine,
        "load_watch_states",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "asof_date": pd.Timestamp("2026-03-20T00:00:00Z"),
                    "setup_id": "DEFENSIVE_REGIME_POSITION_V1",
                    "symbol": "ABC",
                    "candidate_state": "WATCH_EVENT",
                    "current_state": "PASS_NOW",
                    "watch_status": "active",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        risk_engine,
        "load_point_in_time_context",
        lambda symbol, published_on: {
            "avg_traded_value_20d": 1_000_000_000.0,
            "adj_close": 100.0,
            "atr_20": 5.0,
            "dma_20": 98.0,
            "dma_50": 95.0,
            "dma_200": 90.0,
            "rs_vs_benchmark": 0.1,
            "rs_vs_sector": 0.1,
        },
    )

    df = risk_engine.build_allocations(asof_date=pd.Timestamp("2026-03-20T00:00:00Z"))
    row = df.iloc[0]
    assert row["allocation_status"] == "allocated"
    assert row["suggested_allocation_inr"] > 0
    assert "pass_now overrode" in str(row["notes"]).lower()


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
    monkeypatch.setattr(symbol_trace, "load_aggregated_event_decision", lambda symbol, setup_id=None: None)

    trace = symbol_trace.build_trace("HDFCBANK")
    assert trace["stage_summary"]["in_latest_screener"] is True
    assert trace["stage_summary"]["has_technical_snapshot"] is True
    assert trace["stage_summary"]["passed_rule_engine"] is False
    assert trace["decision_summary"]["latest_rejection_reasons"] == ["regime_not_allowed"]


def test_symbol_trace_surfaces_aggregated_event_decision(monkeypatch):
    monkeypatch.setattr(symbol_trace, "load_latest_screener_rows", lambda symbol, setup_id=None: pd.DataFrame())
    monkeypatch.setattr(
        symbol_trace,
        "latest_single_row",
        lambda table_name, **kwargs: {
            "advisory_technical_daily": None,
            "advisory_fundamentals_daily": None,
            "advisory_candidates": {"setup_id": "TEST", "candidate_state": "WATCH_EVENT"},
            "advisory_watchlist": {"setup_id": "TEST", "current_state": "PASS_NOW"},
            "advisory_event_evaluations": {
                "setup_id": "TEST",
                "verdict": "review_manual",
                "event_class": "OTHER",
                "state_transition_hint": "REVIEW_MANUAL",
                "event_source": "announcement",
            },
            "advisory_allocations": {"setup_id": "TEST", "allocation_status": "allocated"},
            "advisory_portfolio_orders": {"setup_id": "TEST", "portfolio_status": "approved"},
            "advisory_position_lifecycle": None,
            "advisory_execution_orders": None,
        }.get(table_name),
    )
    monkeypatch.setattr(symbol_trace, "load_latest_rejections", lambda symbol, setup_id=None: pd.DataFrame())
    monkeypatch.setattr(symbol_trace, "latest_rows", lambda table_name, **kwargs: pd.DataFrame())
    monkeypatch.setattr(
        symbol_trace,
        "load_aggregated_event_decision",
        lambda symbol, setup_id=None: {
            "effective_event_verdict": "continue",
            "effective_event_class": "OTHER",
            "effective_state_transition_hint": "RAISE_SCORE_ONLY",
            "effective_event_score_impact": 0.15,
            "effective_investable_now": True,
            "effective_event_source": "announcement",
            "raw_event_count": 2,
        },
    )

    trace = symbol_trace.build_trace("ABC", setup_id="TEST")
    assert trace["stage_summary"]["has_aggregated_event_decision"] is True
    assert trace["decision_summary"]["latest_event_verdict"] == "review_manual"
    assert trace["decision_summary"]["effective_event_verdict"] == "continue"
    assert trace["decision_summary"]["effective_state_transition_hint"] == "RAISE_SCORE_ONLY"
    assert trace["decision_summary"]["effective_raw_event_count"] == 2


def test_setup_trace_builds_funnel_summary(monkeypatch):
    monkeypatch.setattr(setup_trace, "resolve_asof_date", lambda setup_id, requested_date=None: pd.Timestamp("2026-03-24T00:00:00Z"))
    monkeypatch.setattr(setup_trace, "load_setup_regime", lambda asof_date: {"regime_name": "RISK_OFF"})
    monkeypatch.setattr(setup_trace, "load_market_overlay", lambda asof_date: {"overlay_name": "NONE", "overlay_reason": "no overlay"})
    monkeypatch.setattr(
        setup_trace,
        "load_latest_setup_screener",
        lambda setup_id, overlay_name=None: pd.DataFrame([{"ticker": "A"}, {"ticker": "B"}, {"ticker": "C"}]),
    )

    def fake_load_setup_rows(table_name, setup_id, asof_date, limit=50):
        mapping = {
            "advisory_candidates": pd.DataFrame(
                [
                    {
                        "symbol": "A",
                        "candidate_state": "PASS_NOW",
                        "setup_score": 0.72,
                        "technical_score": 0.80,
                        "fundamental_score": 0.70,
                        "near_miss_flag": False,
                    }
                ]
            ),
            "advisory_candidate_rejections": pd.DataFrame(
                [
                    {"reason_code": "regime_not_allowed", "is_near_miss": False},
                    {"reason_code": "liquidity_far_below_min", "is_near_miss": True},
                ]
            ),
            "advisory_watchlist": pd.DataFrame([{"symbol": "A", "candidate_state": "PASS_NOW"}]),
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
    monkeypatch.setattr(setup_trace, "load_top_rejection_reasons", lambda setup_id, asof_date: {"regime_not_allowed": 1, "liquidity_far_below_min": 1})

    trace = setup_trace.build_trace("LARGECAP_BREAKOUT_POSITION_V1")
    assert trace["stage_summary"]["screener_universe_count"] == 3
    assert trace["stage_summary"]["candidate_count"] == 1
    assert trace["stage_summary"]["news_event_count"] == 1
    assert trace["stage_summary"]["watch_state_counts"] == {"PASS_NOW": 1}
    assert trace["stage_summary"]["avg_setup_score"] == 0.72
    assert trace["stage_summary"]["near_miss_count"] == 1
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
                "watch_state_counts": {"PASS_NOW": 1},
                "avg_setup_score": 0.7,
                "avg_technical_score": 0.75,
                "avg_fundamental_score": 0.65,
                "near_miss_count": 1,
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
