import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import { defineComponent, h, nextTick, ref, Suspense } from 'vue'

import ManualReviewPage from '../pages/manual-review.vue'
import HomePage from '../pages/index.vue'
import HealthPage from '../pages/health.vue'
import SymbolPage from '../pages/symbols/[symbol].vue'
import ReasonContractPanel from '../components/ReasonContractPanel.vue'
import SnapshotWarning from '../components/SnapshotWarning.vue'
import SourceWarnings from '../components/SourceWarnings.vue'

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
      'market-context': { summary: {}, top_universe: [] },
      'technical-calibration-home': { summary: [] },
      'signal-refresh-home': { signals: [], meta: { signals: { total: 0 } } },
      home: {
        generated_at: '2026-06-07T10:00:00+05:30',
        asof_date: '2026-06-07',
        summary: { action_count: 1, today_count: 0, watch_count: 0, alert_count: 0 }
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
    expect(text).toContain('Conflict resolution: 1 losing candidate(s)')
    expect(text).toContain('Why manual review instead of BUY')
    expect(text).toContain('Live submission blocked.')
  })

  it('renders Health current blockers and read-only superseded cleanup preview', async () => {
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
                next_action: 'Review dry-run output; do not apply from the UI.'
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
    expect(text).toContain('Candidate rows are read-only here.')
    expect(text).toContain('python -m advisory.superseded_failures --limit 500')
    expect(text).toContain('python -m advisory.superseded_failures --apply --limit 500')
    expect(text).toContain('Announcement parser fallback used.')
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
})
