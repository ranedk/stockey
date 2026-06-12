import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import { defineComponent, h, nextTick, ref, Suspense } from 'vue'

import ManualReviewPage from '../pages/manual-review.vue'
import HomePage from '../pages/index.vue'
import EventsPage from '../pages/events.vue'
import HealthPage from '../pages/health.vue'
import OperatorJourneyPage from '../pages/operator-journey.vue'
import ScreenersPage from '../pages/screeners.vue'
import SymbolPage from '../pages/symbols/[symbol].vue'
import ReasonContractPanel from '../components/ReasonContractPanel.vue'
import SnapshotWarning from '../components/SnapshotWarning.vue'
import SourceWarnings from '../components/SourceWarnings.vue'
import TechnicalDecisionPanel from '../components/TechnicalDecisionPanel.vue'

const metricTileStub = {
  props: ['label', 'value', 'note'],
  template: '<div class="metric"><span>{{ label }}</span><strong>{{ value }}</strong><small>{{ note }}</small></div>'
}

const nuxtLinkStub = {
  props: ['to'],
  template: '<a :href="to"><slot /></a>'
}

const symbolLinkStub = {
  props: ['symbol'],
  template: '<span class="symbol-link">{{ symbol || "-" }}</span>'
}

const statusPillStub = {
  props: ['tone', 'title'],
  template: '<span class="status-pill" :title="title"><slot /></span>'
}

const metaChipStub = {
  props: ['label', 'tone', 'title'],
  template: '<span class="meta-chip" :title="title"><b>{{ label }}</b><slot /></span>'
}

const recordCardStub = {
  props: ['title', 'subtitle', 'record', 'detailPath', 'showSymbol'],
  template: '<article class="record-card"><h3>{{ title }}</h3><p>{{ subtitle }}</p><slot name="badge" /><slot /></article>'
}

const linkButtonStub = {
  props: ['to', 'variant'],
  template: '<a :href="to"><slot /></a>'
}

const apiErrorBannerStub = {
  props: ['title', 'error'],
  template: '<div class="api-error">{{ title }} {{ error?.message || "" }}</div>'
}

const technicalDecisionPanelStub = {
  template: '<section class="technical-decision"></section>'
}

const traceTimelineStub = {
  template: '<section class="trace-timeline"></section>'
}

async function mountAsync(component: unknown) {
  const wrapper = mount(defineComponent({
    render: () => h(Suspense, null, { default: () => h(component as any) })
  }), {
    global: {
      components: {
        ApiErrorBanner: apiErrorBannerStub,
        LinkButton: linkButtonStub,
        MetaChip: metaChipStub,
        MetricTile: metricTileStub,
        NuxtLink: nuxtLinkStub,
        ReasonContractPanel,
        RecordCard: recordCardStub,
        SnapshotWarning,
        SourceWarnings,
        StatusPill: statusPillStub,
        SymbolLink: symbolLinkStub,
        TechnicalDecisionPanel: technicalDecisionPanelStub,
        TraceTimeline: traceTimelineStub
      }
    }
  })
  await flushPromises()
  await nextTick()
  return wrapper
}

describe('operator page safety smoke coverage', () => {
  it('renders technical state, trigger, score buckets, pivot, stop, target, and exit context from nested reason evidence', () => {
    const wrapper = mount(TechnicalDecisionPanel, {
      props: {
        record: {
          action_code: 'BUY',
          recommendation_reason: {
            setup_id: 'TECH_BREAKOUT_V1',
            evidence: {
              technical: {
                technical_state: 'BUY_TRIGGERED',
                entry_trigger_type: 'breakout',
                technical_trigger_note: 'Close above pivot on volume expansion.',
                technical_total_score: 82,
                technical_trend_score: 18,
                technical_structure_score: 22,
                technical_participation_score: 13,
                technical_relative_strength_score: 10,
                technical_tradability_score: 7,
                pivot_price: 245.5
              },
              risk: {
                recommended_stop_price: 228.25,
                recommended_target_price: 280
              },
              lifecycle: {
                active_exit_condition: 'TECHNICAL_FULL_EXIT',
                exit_condition_status: 'armed'
              }
            }
          }
        }
      },
      global: {
        components: {
          StatusPill: statusPillStub
        }
      }
    })
    const text = wrapper.text()

    expect(text).toContain('Technical Decision')
    expect(text).toContain('Close above pivot on volume expansion.')
    expect(text).toContain('BUY_TRIGGERED')
    expect(text).toContain('breakout')
    expect(text).toContain('82/100')
    expect(text).toContain('245.5')
    expect(text).toContain('228.25')
    expect(text).toContain('280')
    expect(text).toContain('Trend:')
    expect(text).toContain('Structure:')
    expect(text).toContain('Participation:')
    expect(text).toContain('RS:')
    expect(text).toContain('Tradability:')
    expect(text).toContain('TECHNICAL_FULL_EXIT')
  })

  it('renders Manual Review item impact and selected decision boundaries', async () => {
    const refreshManualReview = vi.fn(async () => undefined)
    const decideManualReview = vi.fn()
    const manualReviewPayload = {
      generated_at: '2026-06-07T10:00:00+05:30',
      status: 'ok',
      summary: {
        total_items: 1,
        untrimmed_items: 1,
        by_type: { action_review: 1 },
        by_severity: { warning: 1 },
        source_warnings: [
          {
            source: 'manual_review',
            reason: 'stale_visible_rows',
            message: 'Manual review rows are stale.',
            affected_rows: 1
          }
        ]
      },
      items: [
        {
          item_id: 'manual:ABC:1',
          item_type: 'wait_signal_followup',
          review_lane: 'investment_review',
          severity: 'warning',
          status: 'open',
          symbol: 'ABC',
          setup_id: 'event_policy',
          asof_date: '2026-06-07',
          updated_at: '2026-06-07T09:30:00+05:30',
          title: 'Matched follow-up filing',
          reason: 'Result update matched the wait signal.',
          operator_summary: 'Review the fresh clarification before changing action state.',
          operator_questions: ['Is the clarification actionable?'],
          wait_for_events: ['Management clarification'],
          suggested_decision: 'watch_for_event',
          source_table: 'advisory_wait_signals',
          source_key: '42',
          raw: {
            wait_signal_followup: {
              signal_id: 42,
              manual_review_item_id: 'manual:ABC:0',
              condition_type: 'result_update',
              wait_question: 'Wait for result clarification.',
              matched_at: '2026-06-07T09:20:00+05:30',
              evidence_summary: 'Result filing matched requested clarification.'
            }
          }
        }
      ]
    }
    Object.assign(globalThis, {
      useOperatorApi: () => ({
        getManualReview: vi.fn(async () => manualReviewPayload),
        decideManualReview
      }),
      useAsyncData: vi.fn((_key: string, _handler: () => Promise<unknown>) => ({
        data: ref(manualReviewPayload),
        refresh: refreshManualReview,
        pending: ref(false),
        error: ref(null)
      }))
    })

    const wrapper = await mountAsync(ManualReviewPage)
    const text = wrapper.text()

    expect(text).toContain('Source Freshness')
    expect(text).toContain('Manual review rows are stale.')
    expect(text).toContain('Investment follow-up')
    expect(text).toContain('Matched Wait Signal')
    expect(text).toContain('Original review item: manual:ABC:0')
    expect(text).toContain('Creates wait signal')
    expect(text).toContain('Portfolio unchanged · Broker unchanged')
    expect(text).toContain('Creates wait signal')
    expect(decideManualReview).not.toHaveBeenCalled()
  })

  it('renders Action Queue approval/reconciliation and reason-contract safety evidence', async () => {
    const actionRow = {
      symbol: 'XYZ',
      action_code: 'MANUAL_REVIEW',
      action_source: 'event_policy',
      source_action: 'BUY',
      setup_id: 'event_policy:earnings',
      action_reason: 'Positive action blocked by market context.',
      reason_contract_status: 'complete_review_only',
      recommendation_reason: {
        status: 'complete_review_only',
        action_code: 'MANUAL_REVIEW',
        action_source: 'event_policy',
        original_action_code: 'BUY',
        primary_reason: 'Positive action blocked by market context.',
        evidence: {
          macro_regime: {
            market_context_adjustment: 'manual_review',
            market_context_adjustment_reason: 'Risk-off market context blocks positive action.',
            regime_name: 'VOLATILE',
            breadth_trend_alignment_pct: 31,
            risk_off_score: 0.78
          },
          conflict_resolution: {
            source_precedence_reason: 'Event review beats watch candidate.',
            winning_action_code: 'MANUAL_REVIEW',
            winning_action_source: 'event_policy',
            same_symbol_conflict_count: 1,
            losing_candidates: [
              { action_code: 'WATCH', action_source: 'watchlist' }
            ]
          }
        }
      },
      execution_safety_contract: {
        operator_approval_required: true,
        operator_approval_status: 'missing',
        broker_reconciliation_required: true,
        broker_reconciliation_status: 'not_run',
        live_submission_allowed: false,
        source: 'dry_run_order_preview',
        issues: ['Operator approval missing.', 'Broker reconciliation not run.']
      },
      manual_revision_pointers: {
        revision_summary: 'Manual review required before any broker-capable action.',
        manual_checks: ['Confirm market breadth has recovered.'],
        risk_flags: ['Live submission blocked.'],
        operator_questions: ['Should this stay watch-only?']
      },
      company_memory_review: {
        recommended_signal: 'WATCH',
        confidence: 0.62,
        conviction_score: 68,
        summary: 'XYZ memory review says watch only.',
        thesis: 'Event evidence is constructive but broad market context is weak.',
        risk_flags: ['Risk-off context'],
        evidence_used: ['latest consolidated action is MANUAL_REVIEW'],
        wait_for: ['breadth recovery'],
        authority_scope: 'review_input_only',
        review_status: 'completed'
      }
    }
    const payloads: Record<string, unknown> = {
      'market-context': {
        summary: {
          top_context_count: 50,
          breadth_above_dma50_pct: 62,
          breadth_trend_alignment_pct: 51,
          breadth_rs_positive_pct: 58,
          triggered_event_count_7d: 3,
          context_observed_event_count_7d: 11
        },
        top_universe: []
      },
      'technical-calibration-home': { summary: [] },
      'ts-forecast-promotion-check-home': {
        decision: 'hold_research_only',
        ready_for_operator_review: false,
        scorecard: {
          headline: 'TS forecast paper evidence has not passed promotion gates.',
          operator_action: 'Keep TS forecasts research-only until paper evidence beats momentum with enough breadth.',
          ready_group_count: 0,
          group_count: 1,
          best_group: {
            evaluated_trades: 7,
            avg_cost_adjusted_return: 0.024,
            lift_vs_momentum: 0.016,
            failed_gates: ['evaluated_trades', 'symbol_count']
          }
        }
      },
      'ts-forecast-review-rules-home': {
        status: 'ok',
        summary: {
          row_count: 1,
          active_review_count: 1,
          trusted_overlay_count: 0,
          issue_count: 0
        },
        rules: [
          {
            rule_id: 'timesfm_2p5_200m:10:0',
            model_name: 'timesfm_2p5_200m',
            horizon_days: 10,
            status: 'active_review',
            usable_for_live_policy: false
          }
        ],
        issues: [],
        operator_boundary: {
          broker_execution_enabled: false,
          policy_auto_promotion_allowed: false
        }
      },
      'signal-refresh-home': { signals: [], meta: { signals: { total: 0 } } },
      home: {
        generated_at: '2026-06-07T10:00:00+05:30',
        asof_date: '2026-06-07',
        summary: { action_count: 1, today_count: 0, watch_count: 0, alert_count: 0 },
        ts_forecast_paper_summary: [
          {
            paper_decision: 'PAPER_BUY',
            model_name: 'timesfm_2p5_200m',
            horizon_days: 10,
            from_date: '2026-05-01',
            to_date: '2026-05-31',
            row_count: 12,
            evaluated_trades: 7,
            win_rate_pct: 57.1,
            avg_cost_adjusted_return_pct: 2.4,
            baseline_avg_cost_adjusted_return_pct: 0.8,
            aligned_positive_count: 3,
            conflict_exit_count: 1,
            operator_note: 'Forecast paper results are evidence only.'
          }
        ]
      },
      'home-actions-paged': {
        top_action_recommendations: [actionRow],
        action_recommendations: [],
        alerts: [],
        meta: {
          top_action_recommendations: { returned: 1, total: 1 },
          action_recommendations: { returned: 0, total: 0 },
          alerts: { returned: 0, total: 0 }
        }
      },
      'home-portfolio-paged': {
        today_recommendations: [],
        current_recommendations: [],
        exited_recommendations: [],
        portfolio: [],
        lifecycle: [],
        meta: {}
      }
    }
    Object.assign(globalThis, {
      useRuntimeConfig: () => ({ public: { apiBase: '' } }),
      useOperatorApi: () => ({
        getMarketContext: vi.fn(async () => payloads['market-context']),
        getTechnicalCalibration: vi.fn(async () => payloads['technical-calibration-home']),
        getTsForecastPromotionCheck: vi.fn(async () => payloads['ts-forecast-promotion-check-home']),
        getTsForecastReviewRules: vi.fn(async () => payloads['ts-forecast-review-rules-home']),
        getSignalRefresh: vi.fn(async () => payloads['signal-refresh-home']),
        getHome: vi.fn(async () => payloads.home),
        getActions: vi.fn(async () => payloads['home-actions-paged']),
        getPortfolio: vi.fn(async () => payloads['home-portfolio-paged']),
        getSymbolTraceSummary: vi.fn()
      }),
      useAsyncData: vi.fn((key: string, _handler: () => Promise<unknown>) => ({
        data: ref(payloads[key]),
        refresh: vi.fn(async () => undefined),
        pending: ref(false),
        error: ref(null)
      }))
    })

    const wrapper = await mountAsync(HomePage)
    const text = wrapper.text()

    expect(text).toContain('Action Queue')
    expect(text).toContain('XYZ · Final action: MANUAL_REVIEW')
    expect(text).toContain('Company memory review')
    expect(text).toContain('XYZ memory review says watch only.')
    expect(text).toContain('review_input_only')
    expect(text).toContain('Execution approval gate')
    expect(text).toContain('Dry-run preview only. This panel does not approve, reconcile, or submit broker orders.')
    expect(text).toContain('LIVE BLOCKED')
    expect(text).toContain('Approval: missing')
    expect(text).toContain('Reconciliation: not_run')
    expect(text).toContain('Reason Contract')
    expect(text).toContain('Market gate')
    expect(text).toContain('Risk-off market context blocks positive action.')
    expect(text).toContain('Triggered')
    expect(text).toContain('Material top-context events, 7d')
    expect(text).toContain('Observed')
    expect(text).toContain('Context-only top events, 7d')
    expect(text).toContain('TS Forecast Paper Portfolio')
    expect(text).toContain('Forecast evidence must beat paper baselines first')
    expect(text).toContain('Promotion gate')
    expect(text).toContain('hold_research_only')
    expect(text).toContain('Failed gates: evaluated_trades, symbol_count')
    expect(text).toContain('Configured review rules')
    expect(text).toContain('Manual TS rules are visible, not live authority')
    expect(text).toContain('active_review')
    expect(text).toContain('live policy: blocked')
    expect(text).toContain('PAPER_BUY')
    expect(text).toContain('timesfm_2p5_200m · 10d')
    expect(text).toContain('Momentum:')
    expect(text).toContain('Aligned advisory: 3')
    expect(text).toContain('Conflict resolution: 1 losing candidate(s)')
    expect(text).toContain('Why manual review instead of BUY')
    expect(text).toContain('Live submission blocked.')
  })

  it('renders Operator Journey timeline and read-only safety boundary', async () => {
    const payload = {
      generated_at: '2026-06-11T10:00:00+05:30',
      status: 'ok',
      filters: { symbol: 'ABC' },
      summary: {
        stage_counts: {
          manual_decisions: 1,
          wait_signals: 1,
          wait_signal_matches: 1,
          signal_refresh: 1,
          actions: 1,
          portfolio: 1,
          execution: 1
        },
        timeline_count: 7,
        skipped_source_count: 1,
        has_wait_match: true,
        has_portfolio_or_execution_implication: true,
        operator_boundary: {
          read_only: true,
          mutates_portfolio: false,
          mutates_action_recommendation: false,
          submits_order: false
        }
      },
      stages: {
        manual_decisions: [
          {
            decided_at: '2026-06-10T09:00:00+05:30',
            item_id: 'manual-1',
            symbol: 'ABC',
            decision: 'watch_for_event',
            rationale: 'Wait for management clarification.'
          }
        ],
        wait_signals: [
          {
            created_at: '2026-06-10T09:01:00+05:30',
            signal_id: 'signal-1',
            symbol: 'ABC',
            expected_action: 'MANUAL_REVIEW',
            wait_question: 'Watch for order update.'
          }
        ],
        wait_signal_matches: [
          {
            matched_at: '2026-06-11T09:05:00+05:30',
            signal_id: 'signal-1',
            symbol: 'ABC',
            match_reason: 'Order update was announced.'
          }
        ],
        signal_refresh: [
          {
            refreshed_at: '2026-06-11T09:06:00+05:30',
            refresh_id: 'refresh-1',
            symbol: 'ABC',
            signal_action: 'MANUAL_REVIEW',
            action_reason: 'Matched wait signal requires review.'
          }
        ],
        actions: [
          {
            published_on: '2026-06-11T09:07:00+05:30',
            symbol: 'ABC',
            action_code: 'MANUAL_REVIEW',
            action_reason: 'Matched Manual Review wait signal.'
          }
        ],
        portfolio: [
          {
            published_on: '2026-06-11T09:08:00+05:30',
            symbol: 'ABC',
            portfolio_status: 'review_manual',
            portfolio_reason: 'Manual review required.'
          }
        ],
        execution: [
          {
            published_on: '2026-06-11T09:09:00+05:30',
            symbol: 'ABC',
            execution_status: 'submit_blocked',
            execution_reason: 'Review-only action is not broker-submittable.'
          }
        ]
      },
      timeline: [
        {
          stage: 'execution',
          title: 'Execution preview',
          timestamp: '2026-06-11T09:09:00+05:30',
          symbol: 'ABC',
          action: 'submit_blocked',
          reason: 'Review-only action is not broker-submittable.',
          source_table: 'advisory_execution_orders',
          source_key: 'exec-1'
        },
        {
          stage: 'manual_decisions',
          title: 'Manual-review decision',
          timestamp: '2026-06-10T09:00:00+05:30',
          symbol: 'ABC',
          action: 'watch_for_event',
          reason: 'Wait for management clarification.',
          source_table: 'advisory_manual_review_decisions',
          source_key: 'manual-1'
        }
      ],
      skipped_sources: [
        { stage: 'portfolio', source: 'advisory_portfolio_orders', reason: 'missing_table' }
      ]
    }
    Object.assign(globalThis, {
      useOperatorApi: () => ({
        getOperatorJourney: vi.fn(async () => payload)
      }),
      useAsyncData: vi.fn((_key: string, _handler: () => Promise<unknown>) => ({
        data: ref(payload),
        refresh: vi.fn(async () => undefined),
        pending: ref(false),
        error: ref(null)
      }))
    })

    const wrapper = await mountAsync(OperatorJourneyPage)
    const text = wrapper.text()

    expect(text).toContain('Operator Journey')
    expect(text).toContain('Trace a decision from issue to downstream implication.')
    expect(text).toContain('Read-only operator context')
    expect(text).toContain('portfolio')
    expect(text).toContain('unchanged')
    expect(text).toContain('Skipped sources')
    expect(text).toContain('advisory_portfolio_orders')
    expect(text).toContain('Journey Timeline')
    expect(text).toContain('Review-only action is not broker-submittable.')
    expect(text).toContain('Wait for management clarification.')
    expect(text).toContain('Manual Decisions')
    expect(text).toContain('Wait Signals')
    expect(text).toContain('Execution')
  })

  it('renders Health current blockers and guarded superseded cleanup apply controls', async () => {
    const summaryPayload = {
      generated_at: '2026-06-07T10:00:00+05:30',
      snapshot: {
        freshness: 'stale',
        generated_at: '2026-06-07T08:00:00+05:30',
        age_seconds: 7200,
        source: 'operator_snapshot'
      },
      snapshot_warning: {
        title: 'Stale health snapshot',
        message: 'Health summary is older than the current advisory state.'
      },
      summary: {},
      runtime_processes: [],
      cron_status: [],
      sync_state: []
    }
    const detailsPayload = {
      generated_at: '2026-06-07T10:02:00+05:30',
      status: 'warn',
      sections: {
        database: { status: 'ok', message: 'connected' },
        operator_api: { status: 'ok', latency_ms: 42, url: 'http://localhost:8000' },
        trace_summaries: { status: 'warn', row_count: 5, message: 'cache miss fallback observed' },
        redis: { status: 'ok', message: 'available' },
        dhan: { status: 'ok', message: 'disabled in tests' },
        dhan_cache: { status: 'warn', seconds_to_expiry: 300, message: 'token expires soon' },
        frontend: { status: 'ok', message: 'typechecked' },
        operator_snapshot: { status: 'warn', generated_at: '2026-06-07T08:00:00+05:30', age_seconds: 7200, message: 'stale' },
        slow_operations: {
          status: 'warn',
          issue_count: 1,
          returned_count: 1,
          state_file: 'logs/operator_api_slow.json',
          issues: [
            {
              fingerprint: 'slow-actions',
              kind: 'large_response',
              status: 'open',
              last_details: { route: '/api/actions' },
              last_elapsed_ms: 1500,
              max_elapsed_ms: 1800,
              count: 3
            }
          ]
        },
        degradation_feed: {
          status: 'warn',
          active_count: 1,
          recovered_count: 1,
          counts_by_kind: { parser_failure: 1 },
          rows: [
            {
              kind: 'parser_failure',
              source: 'announcements',
              symbol: 'ABC',
              status: 'warn',
              message: 'Announcement parser fallback used.',
              observed_at: '2026-06-07T09:45:00+05:30',
              suggested_fix: 'Review parser contract before trusting fresh event output.'
            }
          ],
          lifecycle: {
            groups: [
              {
                key: 'superseded_ready',
                label: 'Superseded Ready',
                status: 'warn',
                count: 3,
                description: 'Recovered failures can be marked superseded after dry-run review.',
                next_action: 'Review dry-run output; apply only after operator confirmation.'
              }
            ],
            superseded_preview: {
              event_processing_candidates: 2,
              announcement_document_candidates: 1,
              dry_run_command: 'python -m advisory.superseded_failures --limit 500',
              apply_command: 'python -m advisory.superseded_failures --apply --limit 500',
              event_processing_sample: [
                { unique_id: 'event-1', symbol: 'ABC', stage: 'parse', started_at: '2026-06-07T09:00:00+05:30', superseded_by_status: 'completed' }
              ],
              announcement_document_sample: [
                { unique_id: 'doc-1', symbol: 'XYZ', ocr_status: 'completed', parse_status: 'completed' }
              ]
            }
          }
        },
        table_freshness: [],
        cron_logs: [],
        optional_dependencies: [],
        sync_state_failures: []
      },
      fix_hints: [
        {
          title: 'Trace summaries stale',
          reason: 'Rebuild materialized summaries.',
          status: 'warn',
          commands: ['python -m advisory.trace_summary_store --symbol-limit 150']
        }
      ],
      current_blockers: {
        status: 'warn',
        count: 1,
        error_count: 0,
        warn_count: 1,
        counts_by_category: { trace_cache: 1 },
        rows: [
          {
            category: 'trace_cache',
            title: 'Trace cache fallback',
            reason: 'Symbol pages may fall back to live trace builds.',
            status: 'warn',
            source: 'trace_summaries',
            trust_impact: 'Trace drilldowns may be slow or stale.',
            commands: ['python -m advisory.trace_summary_store --symbol-limit 150']
          }
        ]
      }
    }
    Object.assign(globalThis, {
      useOperatorApi: () => ({
        getSummary: vi.fn(async () => summaryPayload),
        getHealthDetails: vi.fn(async () => detailsPayload),
        applySupersededCleanup: vi.fn(async () => ({
          status: 'ok',
          mode: 'apply',
          dry_run: false,
          counts: { total_updated: 3 },
          audit_run: { run_id: 'superseded-run-1' },
          result: {},
          operator_boundary: { mutates_portfolio: false, submits_order: false }
        })),
        updateSlowIssueStatus: vi.fn()
      }),
      useAsyncData: vi.fn((key: string, _handler: () => Promise<unknown>) => ({
        data: ref(key === 'summary-health' ? summaryPayload : detailsPayload),
        refresh: vi.fn(async () => undefined),
        pending: ref(false),
        error: ref(null)
      }))
    })

    const wrapper = await mountAsync(HealthPage)
    const text = wrapper.text()

    expect(text).toContain('Stale health snapshot')
    expect(text).toContain('Current Blockers')
    expect(text).toContain('Trace cache fallback')
    expect(text).toContain('Trace drilldowns may be slow or stale.')
    expect(text).toContain('Fallbacks & Degradation')
    expect(text).toContain('Superseded Cleanup Preview')
    expect(text).toContain('Applying this marks only superseded metadata')
    expect(text).toContain('It never changes portfolio rows, action recommendations, config, or broker orders.')
    expect(text).toContain('python -m advisory.superseded_failures --limit 500')
    expect(text).toContain('python -m advisory.superseded_failures --apply --limit 500')
    expect(text).toContain('Mark Superseded')
    expect(text).toContain('superseded_failure_cleanup_apply')
    expect(text).toContain('Announcement parser fallback used.')
  })

  it('renders Event Inbox actionability context for policy rows', async () => {
    const reviewEventPolicyGroup = vi.fn(async () => ({ status: 'ok' }))
    const decideEventPolicyPromotionReview = vi.fn(async () => ({ status: 'ok' }))
    const previewEventPolicyConfigChange = vi.fn(async () => ({
      status: 'ok',
      source_type: 'event_policy_review_rule',
      unified_diff: '--- a/config/advisory_setups.yaml\n+++ b/config/advisory_setups.yaml\n+event_policy_review_rules:',
      rollback_note: 'Rollback by not applying this preview.',
      applied: false
    }))
    const policyPayload = {
      generated_at: '2026-06-07T10:00:00+05:30',
      status: 'ok',
      summary: {
        action_counts: { MANUAL_REVIEW: 1 },
        policy_class_counts: { ORDER_WIN: 1 },
        compact: true
      },
      rows: [
        {
          unique_id: 'event-1',
          symbol: 'ABC',
          action_type: 'MANUAL_REVIEW',
          action_status: 'positive_but_incomplete',
          policy_class: 'ORDER_WIN',
          event_class: 'ORDER_WIN',
          policy_score: 0.18,
          confidence: 0.7,
          action_reason: 'Order win needs confirmation.',
          action_detail: 'Company announced a large order win.',
          checks: [{ check_type: 'evidence_quality', rationale: 'Check if priced in.' }],
          operator_notes: {
            operator_summary: 'Review order size against revenue.',
            possible_action: 'Keep watch until technical trigger confirms.',
            wait_for_events: ['Price/volume reaction'],
            operator_questions: ['Is this already priced in?']
          },
          actionability: {
            materiality: 'high',
            confidence: 0.7,
            review_priority: 'high',
            freshness: { bucket: 'fresh', age_days: 0 },
            source_quality: { quality: 'high', source_type: 'exchange_announcement' },
            market_scope: {
              scope_type: 'sector_and_peer_group',
              affected_sectors: ['capital goods'],
              affected_peers: ['PEER1'],
              affected_sector_count: 1,
              affected_peer_count: 1
            },
            current_exposure: { bucket: 'open_position' },
            price_reaction: { bucket: 'strong_positive', value: 0.061 },
            deterministic_boundary: { broker_executable: false, final_action_authority: 'action_consolidation' },
            suggested_next_evidence: ['Latest price/volume reaction versus the event day']
          }
        }
      ]
    }
    const policyEvaluationPayload = {
      status: 'ok',
      summary: [
        {
          evaluated_at: '2026-06-07T10:00:00+05:30',
          group_type: 'source_quality',
          group_value: 'high',
          horizon_days: 5,
          matured_count: 12,
          avg_forward_return_after_cost: 0.042,
          hit_rate_after_cost: 0.58,
          avg_confidence: 0.74,
          recommendation: 'candidate_policy_strengthen'
        },
        {
          evaluated_at: '2026-06-07T10:00:00+05:30',
          group_type: 'market_scope',
          group_value: 'sector_and_peer_group',
          horizon_days: 5,
          matured_count: 8,
          avg_forward_return_after_cost: -0.012,
          hit_rate_after_cost: 0.37,
          avg_confidence: 0.62,
          recommendation: 'candidate_policy_tighten_or_downgrade'
        }
      ]
    }
    const promotionReviewsPayload = {
      status: 'ok',
      reviews: [
        {
          reviewed_at: '2026-06-08T10:00:00+05:30',
          evaluated_at: '2026-06-07T10:00:00+05:30',
          horizon_days: 5,
          group_type: 'source_quality',
          group_value: 'high',
          recommendation: 'promote_review_rule',
          confidence: 0.6,
          patch: { mode: 'manual_review_only' },
          manual_patch_text: 'event_policy_review_rules:\n  - group_type: "source_quality"',
          manual_decision: 'approved'
        }
      ]
    }
    Object.assign(globalThis, {
      useOperatorApi: () => ({
        getEvents: vi.fn(async () => ({ events: [], pagination: { events: {} }, snapshot: {}, snapshot_warning: null })),
        getEventPolicy: vi.fn(async () => policyPayload),
        getEventPolicyEvaluation: vi.fn(async () => policyEvaluationPayload),
        getEventPolicyPromotionReviews: vi.fn(async () => promotionReviewsPayload),
        reviewEventPolicyGroup,
        decideEventPolicyPromotionReview,
        previewEventPolicyConfigChange,
        getEventTraceSummary: vi.fn(async () => ({ status: 'ok', rows: [] }))
      }),
      useAsyncData: vi.fn((key: string, _handler: () => Promise<unknown>) => ({
        data: ref(key === 'event-policy' ? policyPayload : key === 'event-policy-evaluation' ? policyEvaluationPayload : key === 'event-policy-promotion-reviews' ? promotionReviewsPayload : { events: [], pagination: { events: {} }, snapshot: {}, snapshot_warning: null }),
        refresh: vi.fn(async () => undefined),
        pending: ref(false),
        error: ref(null)
      }))
    })

    const wrapper = await mountAsync(EventsPage)
    const text = wrapper.text()

    expect(text).toContain('Actionability context')
    expect(text).toContain('Use this to decide whether Manual Review can lead to action')
    expect(text).toContain('HIGH')
    expect(text).toContain('FRESH')
    expect(text).toContain('EXCHANGE ANNOUNCEMENT')
    expect(text).toContain('SECTOR AND PEER GROUP')
    expect(text).toContain('capital goods')
    expect(text).toContain('PEER1')
    expect(text).toContain('OPEN POSITION')
    expect(text).toContain('STRONG POSITIVE')
    expect(text).toContain('REVIEW ONLY')
    expect(text).toContain('Latest price/volume reaction versus the event day')
    expect(text).toContain('Actionability calibration')
    expect(text).toContain('SOURCE QUALITY')
    expect(text).toContain('MARKET SCOPE')
    expect(text).toContain('SECTOR AND PEER GROUP')
    expect(text).toContain('CANDIDATE POLICY STRENGTHEN')
    expect(text).toContain('CANDIDATE POLICY TIGHTEN OR DOWNGRADE')
    expect(text).toContain('Create Review')
    expect(text).toContain('Promotion reviews')
    expect(text).toContain('Manual-only')
    expect(text).toContain('event_policy_review_rules')
    expect(text).toContain('Approve')
    expect(text).toContain('Needs More Data')
    expect(text).toContain('Reject')
    expect(text).toContain('Reviewed Diff')

    await wrapper.findAll('button').find(button => button.text() === 'Create Review')?.trigger('click')
    await flushPromises()
    expect(reviewEventPolicyGroup).toHaveBeenCalledWith({
      evaluated_at: '2026-06-07T10:00:00+05:30',
      horizon_days: 5,
      group_type: 'source_quality',
      group_value: 'high'
    })

    await wrapper.findAll('button').find(button => button.text() === 'Approve')?.trigger('click')
    await flushPromises()
    expect(decideEventPolicyPromotionReview).toHaveBeenCalledWith(expect.objectContaining({
      decision: 'approved',
      group_type: 'source_quality',
      group_value: 'high'
    }))

    await wrapper.findAll('button').find(button => button.text() === 'Reviewed Diff')?.trigger('click')
    await flushPromises()
    expect(previewEventPolicyConfigChange).toHaveBeenCalledWith({
      reviewed_at: '2026-06-08T10:00:00+05:30',
      evaluated_at: '2026-06-07T10:00:00+05:30',
      horizon_days: 5,
      group_type: 'source_quality',
      group_value: 'high',
      persist: true
    })
    expect(wrapper.text()).toContain('Reviewed diff preview')
    expect(wrapper.text()).toContain('Preview only. This did not edit config')
    expect(wrapper.text()).toContain('Rollback by not applying this preview.')
  })

  it('renders Symbol Detail stale snapshot and compact reason-contract evidence', async () => {
    const actionRow = {
      symbol: 'ABC',
      action_code: 'MANUAL_REVIEW',
      source_action: 'BUY',
      action_source: 'event_policy',
      action_reason: 'Positive setup blocked until the operator reviews market context.',
      reason_contract_status: 'complete_review_only',
      entry_price: 100,
      current_price: 115,
      recommended_stop_price: 92,
      attractive_price_high: 108,
      recommendation_reason: {
        status: 'complete_review_only',
        action_code: 'MANUAL_REVIEW',
        action_source: 'event_policy',
        original_action_code: 'BUY',
        primary_reason: 'Positive setup blocked until the operator reviews market context.',
        evidence: {
          macro_regime: {
            market_context_adjustment: 'manual_review',
            market_context_adjustment_reason: 'Risk-off market context blocks positive action.',
            regime_name: 'VOLATILE',
            breadth_trend_alignment_pct: 30,
            risk_off_score: 0.8
          }
        }
      },
      company_memory_review: {
        recommended_signal: 'WATCH',
        confidence: 0.61,
        conviction_score: 67,
        summary: 'ABC memory review says watch for breadth recovery.',
        thesis: 'Technical action is constructive but market gate blocks entry.',
        risk_flags: ['Risk-off context'],
        evidence_used: ['latest consolidated action is MANUAL_REVIEW'],
        wait_for: ['breadth recovery'],
        authority_scope: 'review_input_only',
        review_status: 'completed'
      }
    }
    const actionsPayload = {
      generated_at: '2026-06-07T10:00:00+05:30',
      snapshot: {
        freshness: 'stale',
        generated_at: '2026-06-07T08:30:00+05:30',
        age_seconds: 5400,
        source: 'operator_snapshot'
      },
      snapshot_warning: {
        title: 'Stale action snapshot',
        message: 'Actions are older than the current advisory state.'
      },
      top_action_recommendations: [actionRow],
      action_recommendations: [],
      alerts: []
    }
    const portfolioPayload = {
      today_recommendations: [],
      current_recommendations: [],
      exited_recommendations: [],
      portfolio: [],
      lifecycle: []
    }
    const eventsPayload = {
      events: [
        {
          symbol: 'ABC',
          unique_id: 'event-abc',
          subject: 'ABC result update',
          concise_summary_text: 'Fresh results need review.',
          source_type: 'announcement'
        }
      ],
      operator_feed: [],
      alerts: []
    }
    const tracePayload = {
      symbol: 'ABC',
      raw_counts: {},
      processing: [],
      decisions: [],
      manual_review_wait_signal_links: [],
      action_conflicts: []
    }
    Object.assign(globalThis, {
      useRoute: () => ({ params: { symbol: 'abc' }, query: {}, hash: '' }),
      useOperatorApi: () => ({
        getActions: vi.fn(async () => actionsPayload),
        getPortfolio: vi.fn(async () => portfolioPayload),
        getEvents: vi.fn(async () => eventsPayload),
        getSymbolTraceSummary: vi.fn(async () => tracePayload)
      }),
      useAsyncData: vi.fn((key: string, _handler: () => Promise<unknown>) => {
        const payload = key.startsWith('symbol-actions-')
          ? actionsPayload
          : key.startsWith('symbol-portfolio-')
            ? portfolioPayload
            : key.startsWith('symbol-events-')
              ? eventsPayload
              : tracePayload
        return {
          data: ref(payload),
          refresh: vi.fn(async () => undefined),
          pending: ref(false),
          error: ref(null)
        }
      })
    })

    const wrapper = await mountAsync(SymbolPage)
    const text = wrapper.text()

    expect(text).toContain('ABC')
    expect(text).toContain('Stale action snapshot')
    expect(text).toContain('Can I still participate?')
    expect(text).toContain('formal target missing')
    expect(text).toContain('Current price is 6.5% above the attractive zone high.')
    expect(text).toContain('Reason Contract')
    expect(text).toContain('Company Memory Review')
    expect(text).toContain('ABC memory review says watch for breadth recovery.')
    expect(text).toContain('review_input_only')
    expect(text).toContain('Original: BUY')
    expect(text).toContain('Market gate')
    expect(text).toContain('Risk-off market context blocks positive action.')
    expect(text).toContain('ABC result update')
  })

  it('renders Screener preview validation issues and safety boundary', async () => {
    const previewScreener = vi.fn(async () => ({
      generated_at: '2026-06-11T10:00:00+05:30',
      status: 'invalid',
      query_name: 'Bad DMA',
      query_slug: 'bad-dma',
      query_hash: 'abc123',
      screener_url: 'https://www.screener.in/screen/raw/?query=x',
      validation_issues: [
        {
          code: 'moving_average_alias',
          text: '50 Day Moving Average',
          suggestion: 'Use `DMA 50` instead of `50 Day Moving Average`.'
        }
      ],
      row_count: 0,
      rows: [],
      meta: { persisted: false, validation_only: true },
      operator_boundary: {
        read_only: true,
        broker_execution_enabled: false,
        registers_production_screener: false
      }
    }))
    const coveragePayload = {
      generated_at: '2026-06-11T10:00:00+05:30',
      status: 'ok',
      lookback_days: 30,
      window: { start_date: '2026-05-12', end_date: '2026-06-11' },
      summary: {
        screener_count: 1,
        returned_count: 1,
        constituent_rows: 10,
        candidate_rows: 2,
        action_rows: 1
      },
      screeners: [
        {
          screener_slug: 'breakouts',
          screener_name: 'Breakouts',
          latest_constituent_date: '2026-06-10',
          constituent_symbols: 10,
          constituent_rows: 10,
          candidate_symbols: 2,
          candidate_rows: 2,
          action_symbols: 1,
          action_rows: 1,
          positive_action_rows: 1,
          candidate_symbol_coverage_pct: 20,
          action_symbol_coverage_pct: 10,
          positive_action_rate_pct: 100,
          pass_now_rows: 1,
          watch_rows: 1,
          manual_review_rows: 0,
          exit_action_rows: 0
        }
      ],
      notes: []
    }
    Object.assign(globalThis, {
      useOperatorApi: () => ({
        previewScreener,
        getScreenerCoverage: vi.fn(async () => coveragePayload)
      }),
      useAsyncData: vi.fn((_key: string, _handler: () => Promise<unknown>) => ({
        data: ref(coveragePayload),
        refresh: vi.fn(async () => undefined),
        pending: ref(false),
        error: ref(null)
      }))
    })

    const wrapper = await mountAsync(ScreenersPage)
    await wrapper.get('form').trigger('submit.prevent')
    await flushPromises()
    await nextTick()
    const text = wrapper.text()

    expect(previewScreener).toHaveBeenCalledWith(expect.objectContaining({ fetch_rows: false }))
    expect(text).toContain('Screener workbench')
    expect(text).toContain('Safe preview only')
    expect(text).toContain('Coverage metrics')
    expect(text).toContain('Breakouts')
    expect(text).toContain('Candidate Coverage')
    expect(text).toContain('moving_average_alias')
    expect(text).toContain('Use `DMA 50` instead of `50 Day Moving Average`.')
    expect(text).toContain('Query results are never stored by preview')
  })
})
