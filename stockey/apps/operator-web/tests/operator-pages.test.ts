import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import { defineComponent, h, nextTick, ref, Suspense } from 'vue'

import ManualReviewPage from '../pages/manual-review.vue'
import HomePage from '../pages/index.vue'
import EventsPage from '../pages/events.vue'
import HealthPage from '../pages/health.vue'
import OperationsPage from '../pages/operations.vue'
import OperatorJourneyPage from '../pages/operator-journey.vue'
import ExecutionApprovalsPage from '../pages/execution-approvals.vue'
import IdentityIssuesPage from '../pages/identity-issues.vue'
import ScreenersPage from '../pages/screeners.vue'
import SignalQualityPage from '../pages/signal-quality.vue'
import SymbolPage from '../pages/symbols/[symbol].vue'
import TechnicalCalibrationPage from '../pages/technical-calibration.vue'
import DefaultLayout from '../layouts/default.vue'
import LinkButton from '../components/LinkButton.vue'
import PayloadFreshnessStrip from '../components/PayloadFreshnessStrip.vue'
import ReasonContractPanel from '../components/ReasonContractPanel.vue'
import SnapshotWarning from '../components/SnapshotWarning.vue'
import SourceWarnings from '../components/SourceWarnings.vue'
import SymbolLink from '../components/SymbolLink.vue'
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
  props: ['to', 'variant', 'title', 'ariaLabel'],
  template: '<a :href="to" :title="title || ariaLabel" :aria-label="ariaLabel || title"><slot /></a>'
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
        PayloadFreshnessStrip,
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
  it('links the execution approval safety workbench from the global nav', async () => {
    Object.assign(globalThis, {
      useOperatorApi: () => ({
        getRuntime: vi.fn(async () => ({
          live_trading_enabled: false,
          live_trading_operator_note: 'Broker submission is disabled.'
        }))
      }),
      useAsyncData: vi.fn((_key: string, handler: () => Promise<unknown>) => ({
        data: ref(null),
        refresh: vi.fn(async () => undefined),
        pending: ref(false),
        error: ref(null),
        execute: handler
      }))
    })

    const wrapper = await mountAsync(DefaultLayout)

    const executionLink = wrapper.find('a[href="/execution-approvals"]')
    expect(executionLink.exists()).toBe(true)
    expect(executionLink.text()).toContain('Execution Approvals')
    expect(wrapper.text()).toContain('Live trading status unknown')
  })

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
    const decideManualReview = vi.fn(async () => ({
      status: 'ok',
      item_id: 'manual:ABC:1',
      decision: 'needs_more_data',
      note: 'Decision recorded only. No config, strategy, broker, or trading behavior was changed.',
      manual_review_state: {
        state: 'open_needs_more_data',
        active: true,
        closes_item: false,
        reopened_by_wait_signal: false
      },
      decision_effect: {
        decision: 'needs_more_data',
        closes_item: false,
        creates_wait_signal: false,
        mutates_portfolio: false,
        mutates_action_recommendation: false,
        submits_order: false,
        description: 'Annotates the review item and keeps it open until more evidence is available.'
      }
    }))
    const manualReviewPayload = {
      generated_at: '2026-06-07T10:00:00+05:30',
      status: 'ok',
      summary: {
        total_items: 1,
        untrimmed_items: 1,
        duplicate_suppressed: 2,
        stale_operator_decision: 1,
        closed_by_operator: 1,
        annotated_by_operator: 1,
        queue_contract: {
          active_displayed_items: 1,
          active_untrimmed_items: 1,
          payload_bytes: 2048,
          max_row_bytes: 1536,
          avg_row_bytes: 1024,
          closed_by_operator_suppressed: 1,
          suppressed_rows_are_audit_only: true,
          manual_review_decisions_mutate_portfolio: false,
          manual_review_decisions_mutate_action_recommendation: false,
          manual_review_decisions_submit_order: false,
          operator_note: 'Manual Review shows only active displayed items. Closed, duplicate, and matched-wait-suppressed rows remain audit/debug history and should not be treated as active investment work.'
        },
        by_type: { action_review: 1 },
        by_severity: { warning: 1 },
        by_review_lane: { investment_review: 1, technical_issue: 0, research_config: 0 },
        by_review_category: { investment_judgment_review: 1, technical_or_operational_issue: 0, research_or_config_review: 0 },
        by_item_impact: { investment_followup: 1, investment_entry_or_watch: 0, technical_or_operational: 0 },
        plain_english: {
          investment_judgment_review: 'Operator judgment is required; saving a Manual Review decision does not trade or mutate portfolio rows.',
          technical_or_operational_issue: 'Operational repair work; mark fixed only after the upstream issue is corrected or a rerun confirms recovery.',
          research_or_config_review: 'Research or policy review; approval records intent but does not apply a config/code change.'
        },
        source_warnings: [
          {
            source: 'manual_review',
            reason: 'stale_visible_rows',
            message: 'Manual review rows are stale.',
            affected_rows: 1
          }
        ],
        payload: {
          payload_bytes: 2048,
          max_row_bytes: 1536,
          avg_row_bytes: 1024
        },
        compact: true,
        raw_included: false
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
          source_evidence: {
            source_kind: 'wait_signal_followup',
            headline: 'Result filing matched requested clarification.',
            read_only: true,
            facts: [
              { label: 'Original review item', value: 'manual:ABC:0' },
              { label: 'Condition type', value: 'result_update' },
              { label: 'Waited for', value: 'Wait for result clarification.' }
            ]
          },
          latest_operator_decision: {
            decision: 'ignore',
            operator_id: 'operator',
            decided_at: '2026-06-07T08:45:00+05:30',
            rationale: 'Original item looked noisy before the clarification arrived.'
          },
          manual_review_state: {
            state: 'source_updated_after_operator_decision',
            active: true,
            closes_item: false,
            decision_stale: true,
            source_updated_at: '2026-06-07T09:30:00+05:30',
            decided_at: '2026-06-07T08:45:00+05:30',
            reason: 'Source row changed after the operator decision; the item is active again for review.'
          },
          visibility_lifecycle: {
            active: true,
            visibility_state: 'source_updated_after_operator_decision',
            active_reason: 'A previous operator decision exists, but newer source evidence arrived after that decision, so this item is active again.',
            is_reopened: true,
            reopened_reason: 'Source row changed after the operator decision; the item is active again for review.',
            latest_decision: 'ignore',
            latest_decision_at: '2026-06-07T08:45:00+05:30',
            source_updated_at: '2026-06-07T09:30:00+05:30',
            closing_decisions: ['downgrade_to_no_action', 'ignore', 'mark_fixed'],
            non_closing_decisions: ['add_operator_note', 'needs_more_data', 'watch_for_event'],
            decision_boundary: 'Closing decisions remove only this Manual Review item from the active queue. Non-closing decisions annotate or create a wait signal. Manual Review decisions do not mutate portfolio rows, action recommendations, or broker orders.',
            mutates_portfolio: false,
            mutates_action_recommendation: false,
            submits_order: false
          },
          operator_boundary: {
            review_category: 'investment_judgment_review',
            primary_operator_task: 'Decide whether the matched evidence is actionable or should be closed as no action.',
            manual_review_decision_mutates_portfolio: false,
            manual_review_decision_mutates_action_recommendation: false,
            manual_review_decision_submits_order: false,
            manual_review_decision_can_create_wait_signal: true
          },
          decision_effects: {
            watch_for_event: {
              closes_item: false,
              creates_wait_signal: true,
              description: 'Backend contract: creates a wait signal and keeps this item open.'
            }
          },
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
    expect(text).toContain('Active Queue Contract')
    expect(text).toContain('Only active rows need operator action.')
    expect(text).toContain('Closed hidden: 1')
    expect(text).toContain('Payload: 2 KB')
    expect(text).toContain('Largest row: 1.5 KB')
    expect(text).toContain('Raw mode: compact')
    expect(text).toContain('Portfolio/action/broker mutation from this page:')
    expect(text).toContain('blocked / blocked / blocked')
    expect(text).toContain('What Work Is This?')
    expect(text).toContain('Manual Review category breakdown')
    expect(text).toContain('Investment judgment')
    expect(text).toContain('Operator judgment is required')
    expect(text).toContain('Technical/data issue')
    expect(text).toContain('Research/config')
    expect(text).toContain('Follow-up · 1')
    expect(text).toContain('Decision Guide')
    expect(text).toContain('What each Manual Review choice does')
    expect(text).toContain('Approve manual config')
    expect(text).toContain('Closes this review as approved for a later manual config/code change.')
    expect(text).toContain('Downgrade to no action')
    expect(text).toContain('Mark fixed')
    expect(text).toContain('NO BROKER SUBMIT')
    expect(text).toContain('Deduped')
    expect(text).toContain('2 repeated conflict rows hidden')
    expect(text).toContain('Reopened')
    expect(text).toContain('Reopened After Source Update')
    expect(text).toContain('newer source evidence arrived after that decision')
    expect(text).toContain('Reopened After New Evidence')
    expect(text).toContain('Previously Ignore')
    expect(text).toContain('Source row changed after the operator decision')
    expect(text).toContain('Investment follow-up')
    expect(text).toContain('Matched Wait Signal')
    expect(text).toContain('Original review item: manual:ABC:0')
    expect(text).toContain('Operator boundary')
    expect(text).toContain('Investment Judgment Review')
    expect(text).toContain('Decide whether the matched evidence is actionable or should be closed as no action.')
    expect(text).toContain('Source Evidence')
    expect(text).toContain('Wait Signal Followup')
    expect(text).toContain('Result filing matched requested clarification.')
    expect(text).toContain('Why This Is Active')
    expect(text).toContain('Source Updated After Operator Decision')
    expect(text).toContain('ACTIVE REVIEW')
    expect(text).toContain('A previous operator decision exists, but newer source evidence arrived after that decision')
    expect(text).toContain('Closes active item')
    expect(text).toContain('Keeps active / annotates')
    expect(text).toContain('Manual Review decisions do not mutate portfolio rows, action recommendations, or broker orders.')
    expect(text).toContain('Original review item: manual:ABC:0')
    expect(text).toContain('READ ONLY')
    expect(text).toContain('Backend contract: creates a wait signal and keeps this item open.')
    expect(text).toContain('Creates wait signal')
    expect(text).toContain('Portfolio unchanged · Action row unchanged · Broker unchanged')
    expect(text).toContain('Active queue:')
    expect(text).toContain('stays active / annotated')
    expect(text).toContain('Action row:')
    expect(text).toContain('unchanged')
    expect(text).toContain('Creates wait signal')
    expect(wrapper.find('a[title="Open ABC symbol detail"]').exists()).toBe(true)
    expect(wrapper.find('a[title^="Open decision trace for"]').exists()).toBe(true)
    expect(decideManualReview).not.toHaveBeenCalled()

    await wrapper.find('select').setValue('needs_more_data')
    await wrapper.find('input[placeholder="Why this operator decision is safe"]').setValue('Need one more management clarification before action.')
    await wrapper.findAll('button').find(button => button.text() === 'Save')?.trigger('click')
    await flushPromises()
    await nextTick()

    expect(decideManualReview).toHaveBeenCalledWith(expect.objectContaining({
      item_id: 'manual:ABC:1',
      decision: 'needs_more_data',
      rationale: 'Need one more management clarification before action.',
      operator_id: 'operator'
    }))
    expect(refreshManualReview).toHaveBeenCalled()
    expect(wrapper.text()).toContain('Saved Decision Effect')
    expect(wrapper.text()).toContain('Next state: Open Needs More Data')
    expect(wrapper.text()).toContain('Queue effect: keeps active / annotates')
    expect(wrapper.text()).toContain('Wait signal: not created')
    expect(wrapper.text()).toContain('Portfolio: unchanged')
    expect(wrapper.text()).toContain('Action row: unchanged')
    expect(wrapper.text()).toContain('Broker: no broker submit')
    expect(wrapper.text()).toContain('No config, strategy, broker, or trading behavior was changed')
  })

  it('renders shared link buttons as explicit navigation controls', () => {
    const wrapper = mount(LinkButton, {
      props: {
        to: '/operations',
        title: 'Open Operations'
      },
      slots: {
        default: 'Open operations'
      },
      global: {
        components: {
          NuxtLink: nuxtLinkStub
        }
      }
    })

    const link = wrapper.find('a')
    expect(link.exists()).toBe(true)
    expect(link.attributes('href')).toBe('/operations')
    expect(link.attributes('title')).toBe('Open Operations')
    expect(link.attributes('aria-label')).toBe('Open Operations')
    expect(link.classes()).toContain('underline')
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
      action_queue_contract: {
        display_status: 'review_only',
        approved_filter_match: false,
        manual_filter_match: true,
        broker_order_candidate: false,
        operator_note: 'Approved filter means broker-candidate final action only. Review-only/manual rows remain active operator work even if an upstream portfolio/source row was approved.'
      },
      final_state_trust: {
        status: 'manual_review',
        final_action: 'MANUAL_REVIEW',
        display_status: 'review_only',
        broker_order_candidate: false,
        approved_filter_match: false,
        live_submission_allowed: false,
        operator_note: 'Final State Trust explains why the row is visible and what must happen before execution. It is read-only and does not submit broker orders.',
        checks: [
          {
            key: 'reason_contract',
            label: 'Reason contract',
            status: 'warning',
            detail: 'Reason contract status is complete_review_only. Complete explanations are required before an action can be trusted.'
          },
          {
            key: 'action_queue',
            label: 'Queue classification',
            status: 'warning',
            detail: 'Final action is MANUAL_REVIEW; queue status is review_only. Approved filter means broker-candidate final action only.'
          },
          {
            key: 'execution_boundary',
            label: 'Execution boundary',
            status: 'passed',
            detail: 'This final action is not broker-candidate, so it remains read-only operator work unless a later pipeline run changes the final action.'
          }
        ],
        blockers: [],
        warnings: ['Reason contract status is complete_review_only. Complete explanations are required before an action can be trusted.']
      },
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
            conflict_precedence_rule_id: 'EVENT_POLICY_REVIEW_BEATS_POSITIVE_ENTRY',
            conflict_precedence_reason: 'Event-policy manual review must block positive entry until operator review.',
            winning_action_code: 'MANUAL_REVIEW',
            winning_action_source: 'event_policy',
            same_symbol_candidate_count: 2,
            same_symbol_conflict_count: 1,
            losing_candidates: [
              { action_code: 'WATCH', action_source: 'watchlist', setup_id: 'technical_watch', source_action: 'READY' }
            ]
          },
          action_transition: {
            action_code: 'MANUAL_REVIEW',
            state_effect: 'review_only',
            broker_order_candidate: false,
            broker_execution_allowed: false,
            broker_boundary: 'This action is review-only/monitoring/policy context and does not create a broker order.',
            next_required_stage: 'operator_manual_review',
            allowed_after_preview: false,
            required_preconditions: ['operator_question_present', 'no_broker_order'],
            missing_preconditions: [],
            precondition_status: 'complete',
            entry_evidence_required: false,
            exit_evidence_required: false,
            policy_audit_required: false
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
        review_status: 'completed',
        evidence_source_contract: {
          coverage_status: 'partial',
          missing_required_sources: ['announcement_evidence']
        }
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
      'ts-forecast-promotion-reviews-home': {
        status: 'ok',
        reviews: [
          {
            reviewed_at: '2026-06-07T09:00:00+05:30',
            model_name: 'timesfm_2p5_200m',
            horizon_days: 10,
            recommendation: 'review_candidate',
            manual_decision: 'approved'
          }
        ]
      },
      'config-change-applications-home': {
        status: 'ok',
        applications: [
          {
            application_id: 'app-ts-1',
            preview_id: 'preview-ts-1',
            application_decision: 'marked_applied',
            verification_status: 'verified',
            operator_boundary: {
              broker_execution_allowed: false
            }
          }
        ]
      },
      'signal-refresh-home': {
        generated_at: '2026-06-07T10:16:00+05:30',
        status: 'ok',
        signals: [
          {
            refresh_id: 'refresh-1',
            symbol: 'ABC',
            refreshed_at: '2026-06-07T10:15:00+05:30',
            signal_action: 'WATCH',
            signal_status: 'watch_or_review',
            signal_source: 'wait_signal',
            reason: 'router:announcement',
            effect_type: 'wait_match_created',
            effect_summary: 'Fast refresh stayed review-only until operator/advisory reconciliation.',
            operator_next_step_kind: 'watch_for_confirmation',
            operator_next_step: 'Inspect the trigger evidence and keep watching for confirmation. Run full advisory for authoritative entry, stop, target, and sizing.',
            authority_scope: 'review_input_only',
            portfolio_authority: 'none',
            broker_execution_allowed: false,
            full_advisory_required: true,
            router_context: {
              source_types: ['price_alert'],
              action_type: 'refresh_symbol_price',
              reasons: ['ENTRY_ZONE_HIT'],
              priority_score: 3,
              best_rank: 4
            }
          }
        ],
        meta: { signals: { total: 1 } }
      },
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
        generated_at: '2026-06-07T10:02:00+05:30',
        snapshot: {
          freshness: 'stale',
          generated_at: '2026-06-07T08:30:00+05:30',
          age_seconds: 5520,
          source: 'operator_snapshot'
        },
        snapshot_warning: {
          title: 'Stale action snapshot',
          message: 'Action Queue snapshot is older than the current advisory state.',
          source: 'operator_snapshot',
          age_seconds: 5520
        },
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
        generated_at: '2026-06-07T10:03:00+05:30',
        today_recommendations: [],
        current_recommendations: [],
        exited_recommendations: [],
        portfolio: [],
        lifecycle: [],
        meta: {}
      },
      'home-identity-issues': {
        generated_at: '2026-06-07T10:04:00+05:30',
        status: 'ok',
        summary: { total_open: 1 },
        issues: [
          {
            issue_key: 'dhan_security_id_missing:stock:NSE:XYZ',
            issue_type: 'dhan_security_id_missing',
            symbol: 'XYZ',
            requested_exchange: 'NSE',
            source: 'resolve_dhan_identity',
            error_text: 'No Dhan security id mapped for NSE:XYZ',
            suggested_action: 'Refresh Dhan scrip master and company master, then verify NSE:XYZ mapping.',
            last_seen_at: '2026-06-07T09:35:00+05:30'
          }
        ],
        skipped: []
      }
    }
    Object.assign(globalThis, {
      useRuntimeConfig: () => ({ public: { apiBase: '' } }),
      useOperatorApi: () => ({
        getMarketContext: vi.fn(async () => payloads['market-context']),
        getTechnicalCalibration: vi.fn(async () => payloads['technical-calibration-home']),
        getTsForecastPromotionCheck: vi.fn(async () => payloads['ts-forecast-promotion-check-home']),
        getTsForecastReviewRules: vi.fn(async () => payloads['ts-forecast-review-rules-home']),
        getTsForecastPromotionReviews: vi.fn(async () => payloads['ts-forecast-promotion-reviews-home']),
        getConfigChangeApplications: vi.fn(async () => payloads['config-change-applications-home']),
        getSignalRefresh: vi.fn(async () => payloads['signal-refresh-home']),
        getHome: vi.fn(async () => payloads.home),
        getActions: vi.fn(async () => payloads['home-actions-paged']),
        getPortfolio: vi.fn(async () => payloads['home-portfolio-paged']),
        getIdentityIssues: vi.fn(async () => payloads['home-identity-issues']),
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
    expect(text).toContain('Payload freshness')
    expect(text).toContain('Actions')
    expect(text).toContain('Action Queue snapshot is older than the current advisory state.')
    expect(text).toContain('authority: review_input_only')
    expect(text).toContain('portfolio: none')
    expect(text).toContain('broker: blocked')
    expect(text).toContain('full advisory required')
    expect(text).toContain('Operator next step')
    expect(text).toContain('Run full advisory for authoritative entry, stop, target, and sizing.')
    expect(text).toContain('Router trigger')
    expect(text).toContain('Refresh Symbol Price')
    expect(text).toContain('Entry Zone Hit')
    expect(text).toContain('priority 3')
    expect(text).toContain('rank 4')
    expect(text).toContain('XYZ · Final action: MANUAL_REVIEW')
    expect(text).toContain('queue status')
    expect(text).toContain('Review Only')
    expect(text).toContain('Portfolio eligibility')
    expect(text).toContain('Not portfolio-eligible until manual review is resolved.')
    expect(text).toContain('Upstream source event_policy suggested BUY, but the final consolidated action is MANUAL_REVIEW.')
    expect(text).toContain('Resolve the manual review item or wait for a later advisory run to emit a broker-candidate action.')
    expect(text).toContain('Identity / source blocker')
    expect(text).toContain('1 open source issue for XYZ.')
    expect(text).toContain('This is an operational data problem, not an investment thesis.')
    expect(text).toContain('Dhan Security Id Missing (NSE): No Dhan security id mapped for NSE:XYZ')
    expect(text).toContain('SOURCE BLOCKED')
    expect(text).toContain('Repair: Refresh Dhan scrip master and company master, then verify NSE:XYZ mapping.')
    expect(text).toContain('Open identity issues')
    expect(text).toContain('Final State Trust')
    expect(text).toContain('Final action is MANUAL_REVIEW in review only.')
    expect(text).toContain('This is read-only operator work unless a later pipeline run changes the final action.')
    expect(text).toContain('Reason contract status is complete_review_only. Complete explanations are required before an action can be trusted.')
    expect(text).toContain('Execution boundary')
    expect(text).toContain('Company memory review')
    expect(text).toContain('XYZ memory review says watch only.')
    expect(text).toContain('review_input_only')
    expect(text).toContain('Evidence coverage:')
    expect(text).toContain('Missing compact evidence: announcement_evidence')
    expect(text).toContain('Execution approval gate')
    expect(text).toContain('Dry-run preview only. This panel does not approve, reconcile, or submit broker orders.')
    expect(text).toContain('Why this is not approved/executable')
    expect(text).toContain('Final action is MANUAL_REVIEW, after starting from BUY.')
    expect(text).toContain('Consolidated decision')
    expect(text).toContain('Winner is MANUAL_REVIEW from event_policy.')
    expect(text).toContain('2 candidates were considered; 1 losing conflict was recorded.')
    expect(text).toContain('Rule EVENT_POLICY_REVIEW_BEATS_POSITIVE_ENTRY:')
    expect(text).toContain('Event review beats watch candidate.')
    expect(text).toContain('Lost: WATCH from watchlist · technical_watch · source action READY')
    expect(text).toContain('Execution preconditions')
    expect(text).toContain('This action is monitoring, policy, or review context and does not create a broker order.')
    expect(text).toContain('State effect: review_only')
    expect(text).toContain('Next stage: operator_manual_review')
    expect(text).toContain('Operator question')
    expect(text).toContain('No broker order')
    expect(text).toContain('Broker boundary: This action is review-only/monitoring/policy context and does not create a broker order.')
    expect(text).toContain('NOT BROKER READY')
    expect(text).toContain('Operator approval is missing.')
    expect(text).toContain('Broker reconciliation is not_run.')
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
    expect(text).toContain('Reviewed config workflow')
    expect(text).toContain('TS forecast promotion reviews and application audits')
    expect(text).toContain('visibility only')
    expect(text).toContain('Latest TS reviews')
    expect(text).toContain('Approved')
    expect(text).toContain('Latest config application audits')
    expect(text).toContain('Marked Applied')
    expect(text).toContain('broker: blocked')
    expect(text).toContain('PAPER_BUY')
    expect(text).toContain('timesfm_2p5_200m · 10d')
    expect(text).toContain('Momentum:')
    expect(text).toContain('Aligned advisory: 3')
    expect(text).toContain('Conflict resolution: 1 losing candidate(s)')
    expect(text).toContain('Why manual review instead of BUY')
    expect(text).toContain('Live submission blocked.')
  })

  it('renders Execution Approvals as read-only with blockers', async () => {
    const payload = {
      generated_at: '2026-06-11T10:00:00+05:30',
      status: 'ok',
      summary: {
        row_count: 1,
        blocked_count: 1,
        missing_approval_count: 1,
        missing_reconciliation_count: 1,
        missing_evidence_count: 1,
        live_allowed_count: 1,
        missing_post_live_allowance_approval_count: 1
      },
      source_warnings: [
        {
          reason: 'source_query_skipped',
          source: 'advisory_execution_orders',
          message: 'A source query failed while building this page, so visible rows may be incomplete.',
          error: 'missing_table',
          affected_rows: 1
        }
      ],
      operator_boundary: {
        approves_execution: false,
        submits_broker_orders: false,
        mutates_execution_orders: false,
        requires_post_live_allowance_approval: true,
        note: 'This endpoint is an execution approval workbench preview. Live allowance still requires a fresh approval decision before live preflight or CLI submission can proceed.'
      },
      rows: [
        {
          symbol: 'ABC',
          setup_id: 'SETUP',
          unique_id: 'u1',
          transaction_type: 'BUY',
          quantity: 10,
          reference_price: 100,
          estimated_order_value_inr: 1000,
          asof_date: '2026-06-11T00:00:00+05:30',
          execution_status: 'planned',
          execution_reason: 'Dry-run order preview.',
          published_on: '2026-06-11T09:30:00+05:30',
          safety_contract: {
            operator_approval_status: 'missing',
            broker_reconciliation_status: 'not_run',
            live_evidence_status: 'missing',
            live_evidence_successful_runs: 0,
            live_evidence_min_successful_runs: 3,
            live_submission_allowed: true,
            live_allowance_allowed_at: '2026-06-11T10:00:00+05:30',
            post_live_allowance_approval_required: true,
            post_live_allowance_approval_status: 'missing'
          },
          blockers: [
            'Operator approval is missing.',
            'Broker reconciliation is not_run.',
            'Live evidence checklist is missing.',
            'Fresh operator approval after live allowance is required before live submission; live allowance was recorded at 2026-06-11T10:00:00+05:30.'
          ],
          readiness_checks: [
            { key: 'dry_run_row', label: 'Dry-run execution row', status: 'passed', required: true, passed: true, detail: 'Execution status is planned.' },
            { key: 'operator_approval', label: 'Operator approval', status: 'blocked', required: true, passed: false, detail: 'Approval status is missing.' },
            { key: 'broker_reconciliation', label: 'Broker reconciliation', status: 'blocked', required: true, passed: false, detail: 'Reconciliation status is not_run.' },
            { key: 'live_evidence', label: 'Repeated dry-run evidence', status: 'blocked', required: true, passed: false, detail: 'Evidence status is missing with 0/3 successful run(s).' },
            { key: 'live_allowance', label: 'Final live allowance', status: 'passed', required: true, passed: true, detail: 'Live allowance is true.' },
            { key: 'post_live_allowance_approval', label: 'Fresh approval after live allowance', status: 'blocked', required: true, passed: false, detail: 'Post-allowance approval status is missing.' }
          ],
          readiness_summary: { required_count: 6, passed_count: 2, blocked_count: 4 },
          latest_operator_decision: {
            decision: 'approve_dry_run',
            decided_at: '2026-06-11T10:00:00+05:30',
            rationale: 'Dry-run reviewed.'
          }
        }
      ],
      skipped: []
    }
    const decideExecutionApproval = vi.fn(async () => ({
      status: 'ok',
      note: 'Execution approval decision recorded for audit only.',
      operator_boundary: {
        records_audit_only: true,
        mutates_execution_orders: false,
        submits_broker_orders: false
      },
      decision: { decision: 'approve_dry_run', symbol: 'ABC' }
    }))
    const applyExecutionApprovalContract = vi.fn(async () => ({
      status: 'ok',
      note: 'Execution safety contract marked operator-approved only.',
      operator_boundary: {
        updates_operator_approval_status: true,
        updates_reconciliation_status: false,
        live_submission_allowed: false,
        submits_broker_orders: false
      },
      updated_row: { symbol: 'ABC', updated_rows: 1 },
      safety_contract: {
        operator_approval_status: 'approved',
        broker_reconciliation_status: 'not_run',
        live_submission_allowed: false
      }
    }))
    const runExecutionReconciliation = vi.fn(async (body) => ({
      status: 'ok',
      mode: body.apply ? 'apply' : 'dry_run',
      dry_run: !body.apply,
      note: body.apply
        ? 'Broker reconciliation persisted from broker order-state reads only.'
        : 'Dry-run broker reconciliation preview only.',
      summary: {
        target_count: 1,
        reconciled_count: 1,
        fill_count: 0
      },
      orders: [{ symbol: 'ABC', execution_status: 'filled' }],
      fills: [],
      targets: [{ symbol: 'ABC', execution_status: 'planned' }],
      operator_boundary: {
        reads_broker_order_state: true,
        persists_reconciliation: Boolean(body.apply),
        updates_reconciliation_status: Boolean(body.apply),
        updates_operator_approval_status: false,
        approves_live_submission: false,
        live_submission_allowed: false,
        submits_broker_orders: false
      }
    }))
    const reviewExecutionEvidence = vi.fn(async (body) => ({
      status: 'ok',
      mode: body.apply ? 'apply' : 'preview',
      dry_run: !body.apply,
      decision: 'passed',
      note: body.apply
        ? 'Execution evidence marked passed for the current safety contract only.'
        : 'Execution evidence preview only.',
      review: { decision: 'passed', successful_runs: 3, required_runs: 3 },
      evidence: { successful_runs: 3, required_runs: 3, failed_runs: 0, blockers: [] },
      updated_row: body.apply ? { symbol: 'ABC', updated_rows: 1 } : {},
      safety_contract: body.apply ? { live_evidence_status: 'passed', live_submission_allowed: false } : {},
      operator_boundary: {
        records_audit: Boolean(body.apply),
        mutates_execution_orders: Boolean(body.apply),
        updates_live_evidence_status: Boolean(body.apply),
        updates_operator_approval_status: false,
        updates_reconciliation_status: false,
        live_submission_allowed: false,
        submits_broker_orders: false
      }
    }))
    const applyExecutionLiveAllowance = vi.fn(async (body) => ({
      status: 'ok',
      mode: body.apply ? 'apply' : 'preview',
      dry_run: !body.apply,
      decision: 'ready',
      note: body.apply
        ? 'Live allowance applied to the execution safety contract only.'
        : 'Live allowance preview only.',
      allowance: { confirmation_phrase: 'ALLOW LIVE ABC SETUP u1' },
      blockers: [],
      updated_row: body.apply ? { symbol: 'ABC', updated_rows: 1 } : {},
      safety_contract: body.apply ? { live_submission_allowed: true } : {},
      operator_boundary: {
        records_audit: Boolean(body.apply),
        mutates_execution_orders: Boolean(body.apply),
        updates_live_submission_allowed: Boolean(body.apply),
        submits_broker_orders: false,
        expected_confirmation_phrase: 'ALLOW LIVE ABC SETUP u1'
      }
    }))
    const previewExecutionLiveSubmit = vi.fn(async () => ({
      status: 'ok',
      decision: 'ready',
      summary: { matched_rows: 1, eligible_planned_rows: 1, skipped_rows: 0 },
      expected_live_token: 'STOCKEY-LIVE-2026-06-11-1-abcdef123456',
      cli_command_preview: 'STOCKEY_LIVE_TRADING_ENABLED=true python -m advisory.execution_engine --live --include-existing --date 2026-06-11 --live-confirmation STOCKEY-LIVE-2026-06-11-1-abcdef123456',
      env_required: { STOCKEY_LIVE_TRADING_ENABLED: 'true' },
      blockers: [],
      planned_orders: [{ symbol: 'ABC', execution_status: 'planned' }],
      skipped_orders: [],
      operator_boundary: {
        read_only: true,
        mutates_execution_orders: false,
        submits_broker_orders: false,
        requires_manual_cli_execution: true
      },
      note: 'Live submit preflight is ready.'
    }))

    Object.assign(globalThis, {
      useOperatorApi: () => ({
        getExecutionApprovals: vi.fn(async () => payload),
        decideExecutionApproval,
        applyExecutionApprovalContract,
        runExecutionReconciliation,
        reviewExecutionEvidence,
        applyExecutionLiveAllowance,
        previewExecutionLiveSubmit
      }),
      useAsyncData: vi.fn((key: string) => ({
        data: ref(key === 'execution-approvals' ? payload : {}),
        refresh: vi.fn(async () => undefined),
        pending: ref(false),
        error: ref(null)
      }))
    })

    const wrapper = await mountAsync(ExecutionApprovalsPage)
    const text = wrapper.text()

    expect(text).toContain('Execution Approvals')
    expect(text).toContain('This page does not approve, reconcile, or submit broker orders.')
    expect(text).toContain('Source Freshness')
    expect(text).toContain('source_query_skipped')
    expect(text).toContain('advisory_execution_orders')
    expect(text).toContain('approves execution: no')
    expect(text).toContain('submits orders: no')
    expect(text).toContain('post-allowance approval required: yes')
    expect(text).toContain('Post-Allow Approval')
    expect(text).toContain('ABC')
    expect(text).toContain('Why this cannot be submitted')
    expect(text).toContain('Live Readiness Checklist')
    expect(text).toContain('2/6 passed')
    expect(text).toContain('Dry-run execution row')
    expect(text).toContain('Operator approval')
    expect(text).toContain('Fresh approval after live allowance')
    expect(text).toContain('Operator approval is missing.')
    expect(text).toContain('Broker reconciliation is not_run.')
    expect(text).toContain('Live evidence checklist is missing.')
    expect(text).toContain('Evidence: missing')
    expect(text).toContain('Post-live-allowance approval')
    expect(text).toContain('Live allowance alone is not enough.')
    expect(text).toContain('fresh approval after allowance')
    expect(text).toContain('Audit-only operator decision')
    expect(text).toContain('It does not set `operator_approval_status=approved`')
    expect(text).toContain('Reviewed safety-contract update')
    expect(text).toContain('reconciliation stays unchanged and live submission remains blocked')
    expect(text).toContain('Broker reconciliation')
    expect(text).toContain('NO ORDER SUBMISSION')
    expect(text).toContain('Live evidence review')
    expect(text).toContain('EVIDENCE ONLY')
    expect(text).toContain('Final live allowance')
    expect(text).toContain('NO BROKER SUBMIT')
    expect(text).toContain('resets post-allowance approval to missing')
    expect(text).toContain('Live submit preflight')
    expect(text).toContain('fresh post-allowance approval')
    expect(text).toContain('The UI never submits broker orders.')

    const liveSubmitButton = wrapper.findAll('button').find((button) => button.text().includes('Preview Live Submit'))
    expect(liveSubmitButton).toBeTruthy()
    await liveSubmitButton!.trigger('click')
    await flushPromises()
    expect(previewExecutionLiveSubmit).toHaveBeenCalledWith(expect.objectContaining({
      limit: 100
    }))
    expect(wrapper.text()).toContain('STOCKEY-LIVE-2026-06-11-1-abcdef123456')
    expect(wrapper.text()).toContain('STOCKEY_LIVE_TRADING_ENABLED=true python -m advisory.execution_engine --live --include-existing')
    expect(wrapper.text()).toContain('Run this manually only after reviewing the exact order set.')

    const input = wrapper.find('input[placeholder="Why are you recording this decision?"]')
    await input.setValue('Dry-run reviewed; keep blocked until reconciliation exists.')
    await wrapper.findAll('select')[1].setValue('approve_dry_run')
    const recordButton = wrapper.findAll('button').find((button) => button.text().includes('Record Audit'))
    expect(recordButton).toBeTruthy()
    await recordButton!.trigger('click')
    await flushPromises()

    expect(decideExecutionApproval).toHaveBeenCalledWith(expect.objectContaining({
      decision: 'approve_dry_run',
      rationale: 'Dry-run reviewed; keep blocked until reconciliation exists.'
    }))

    const contractInput = wrapper.find('input[placeholder="Why should operator approval status be marked approved?"]')
    await contractInput.setValue('Approved after dry-run audit; reconciliation still required.')
    const applyButton = wrapper.findAll('button').find((button) => button.text().includes('Apply Approval Status'))
    expect(applyButton).toBeTruthy()
    await applyButton!.trigger('click')
    await flushPromises()

    expect(applyExecutionApprovalContract).toHaveBeenCalledWith(expect.objectContaining({
      confirm: true,
      rationale: 'Approved after dry-run audit; reconciliation still required.'
    }))

    const previewButton = wrapper.findAll('button').find((button) => button.text().includes('Preview Reconciliation'))
    expect(previewButton).toBeTruthy()
    await previewButton!.trigger('click')
    await flushPromises()
    expect(runExecutionReconciliation).toHaveBeenCalledWith(expect.objectContaining({
      row: expect.objectContaining({ symbol: 'ABC' }),
      apply: false,
      dry_run: true
    }))

    const persistButton = wrapper.findAll('button').find((button) => button.text().includes('Persist Reconciliation'))
    expect(persistButton).toBeTruthy()
    await persistButton!.trigger('click')
    await flushPromises()
    expect(runExecutionReconciliation).toHaveBeenCalledWith(expect.objectContaining({
      row: expect.objectContaining({ symbol: 'ABC' }),
      apply: true,
      dry_run: false,
      confirm: true
    }))

    const evidencePreviewButton = wrapper.findAll('button').find((button) => button.text().includes('Preview Evidence'))
    expect(evidencePreviewButton).toBeTruthy()
    await evidencePreviewButton!.trigger('click')
    await flushPromises()
    expect(reviewExecutionEvidence).toHaveBeenCalledWith(expect.objectContaining({
      row: expect.objectContaining({ symbol: 'ABC' }),
      apply: false,
      dry_run: true
    }))

    const evidenceInput = wrapper.find('input[placeholder="Why should this evidence gate be marked passed?"]')
    await evidenceInput.setValue('Three clean dry-run and reconciliation cycles reviewed.')
    const evidenceApplyButton = wrapper.findAll('button').find((button) => button.text().includes('Apply Evidence Passed'))
    expect(evidenceApplyButton).toBeTruthy()
    await evidenceApplyButton!.trigger('click')
    await flushPromises()
    expect(reviewExecutionEvidence).toHaveBeenCalledWith(expect.objectContaining({
      row: expect.objectContaining({ symbol: 'ABC' }),
      apply: true,
      dry_run: false,
      confirm: true,
      rationale: 'Three clean dry-run and reconciliation cycles reviewed.'
    }))

    const allowancePreviewButton = wrapper.findAll('button').find((button) => button.text().includes('Preview Allowance'))
    expect(allowancePreviewButton).toBeTruthy()
    await allowancePreviewButton!.trigger('click')
    await flushPromises()
    expect(applyExecutionLiveAllowance).toHaveBeenCalledWith(expect.objectContaining({
      row: expect.objectContaining({ symbol: 'ABC' }),
      apply: false,
      dry_run: true
    }))

    const allowanceRationale = wrapper.find('input[placeholder="Why should this order be allowed for live submission?"]')
    await allowanceRationale.setValue('All safety gates reviewed.')
    const allowancePhrase = wrapper.find('input[placeholder="Type exact phrase from preview"]')
    await allowancePhrase.setValue('ALLOW LIVE ABC SETUP u1')
    const allowanceApplyButton = wrapper.findAll('button').find((button) => button.text().includes('Apply Live Allowance'))
    expect(allowanceApplyButton).toBeTruthy()
    await allowanceApplyButton!.trigger('click')
    await flushPromises()
    expect(applyExecutionLiveAllowance).toHaveBeenCalledWith(expect.objectContaining({
      row: expect.objectContaining({ symbol: 'ABC' }),
      apply: true,
      dry_run: false,
      confirm: true,
      rationale: 'All safety gates reviewed.',
      confirmation_phrase: 'ALLOW LIVE ABC SETUP u1'
    }))
  })

  it('renders shared symbol links as explicit navigation controls', () => {
    const wrapper = mount(SymbolLink, {
      props: {
        symbol: 'abc'
      },
      global: {
        components: {
          NuxtLink: nuxtLinkStub
        }
      }
    })

    const link = wrapper.find('a')
    expect(link.exists()).toBe(true)
    expect(link.attributes('href')).toBe('/symbols/ABC')
    expect(link.text()).toContain('ABC')
    expect(link.attributes('title')).toBe('Open ABC symbol detail')
    expect(link.attributes('aria-label')).toBe('Open ABC symbol detail')
    expect(link.classes()).toContain('underline')
  })

  it('renders Identity Issues repair boundaries for preview and apply results', async () => {
    const payload = {
      generated_at: '2026-06-11T10:00:00+05:30',
      status: 'ok',
      summary: {
        total_open: 1,
        read_only: true,
        broker_execution_enabled: false,
        source_warnings: []
      },
      source_warnings: [],
      issues: [
        {
          issue_key: 'dhan_security_id_missing:stock:NSE:HUIL',
          issue_type: 'dhan_security_id_missing',
          status: 'open',
          symbol: 'HUIL',
          requested_exchange: 'NSE',
          asset_type: 'stock',
          source: 'advisory_execution_orders',
          error_text: 'No Dhan security id mapped for NSE:HUIL',
          repair_hint: 'Refresh Dhan scrip master and company master.',
          attempt_count: 2,
          exchanges_tried: ['NSE', 'BSE'],
          fallback_tried: [{ exchange: 'BSE', status: 'missing' }],
          first_seen_at: '2026-06-10T09:00:00+05:30',
          last_seen_at: '2026-06-11T09:00:00+05:30',
          manual_review_item_id: 'identity_issue:advisory_identity_issues:dhan_security_id_missing:stock:NSE:HUIL'
        }
      ],
      skipped: []
    }
    const previewIdentityIssueResolution = vi.fn(async () => ({
      status: 'ok',
      mode: 'dry_run',
      checked_rows: 1,
      counts: { would_resolve: 1, still_open: 0 },
      results: [
        {
          issue_key: 'dhan_security_id_missing:stock:NSE:HUIL',
          symbol: 'HUIL',
          requested_exchange: 'NSE',
          status: 'would_resolve'
        }
      ],
      operator_boundary: {
        mutates_identity_issue_status: false,
        mutates_identity_mapping: false,
        mutates_broker_execution: false,
        requires_preview_issue_keys: false
      },
      note: 'Preview only. Use issue_keys from would_resolve rows if you want to close them.'
    }))
    const applyIdentityIssueResolution = vi.fn(async () => ({
      status: 'ok',
      mode: 'apply',
      checked_rows: 1,
      counts: { resolved: 1 },
      results: [
        {
          issue_key: 'dhan_security_id_missing:stock:NSE:HUIL',
          symbol: 'HUIL',
          requested_exchange: 'NSE',
          status: 'resolved'
        }
      ],
      operator_boundary: {
        mutates_identity_issue_status: true,
        mutates_identity_mapping: false,
        mutates_broker_execution: false,
        requires_preview_issue_keys: true
      },
      note: 'Resolved identity issue rows were closed. Re-run Health and advisory/data source if needed.'
    }))
    const refresh = vi.fn(async () => undefined)

    Object.assign(globalThis, {
      useOperatorApi: () => ({
        getIdentityIssues: vi.fn(async () => payload),
        previewIdentityIssueResolution,
        applyIdentityIssueResolution
      }),
      useAsyncData: vi.fn((_key: string, _handler: () => Promise<unknown>) => ({
        data: ref(payload),
        refresh,
        pending: ref(false),
        error: ref(null)
      }))
    })

    const wrapper = await mountAsync(IdentityIssuesPage)
    expect(wrapper.text()).toContain('Identity Issues')
    expect(wrapper.text()).toContain('Preview is read-only. Apply only closes identity issue rows')
    expect(wrapper.text()).toContain('No Dhan security id mapped for NSE:HUIL')

    const recheckButton = wrapper.findAll('button').find((button) => button.text().includes('Recheck mappings'))
    expect(recheckButton).toBeTruthy()
    await recheckButton!.trigger('click')
    await flushPromises()
    expect(previewIdentityIssueResolution).toHaveBeenCalled()
    expect(wrapper.text()).toContain('Preview Result')
    expect(wrapper.text()).toContain('Mutates issue status: no')
    expect(wrapper.text()).toContain('Mutates mappings: no')
    expect(wrapper.text()).toContain('Broker execution: no')

    const closeButton = wrapper.findAll('button').find((button) => button.text().includes('Close 1 resolved'))
    expect(closeButton).toBeTruthy()
    await closeButton!.trigger('click')
    await flushPromises()
    expect(applyIdentityIssueResolution).toHaveBeenCalledWith(expect.objectContaining({
      issue_keys: ['dhan_security_id_missing:stock:NSE:HUIL']
    }))
    expect(refresh).toHaveBeenCalled()
    expect(wrapper.text()).toContain('Apply Result')
    expect(wrapper.text()).toContain('Mutates issue status: yes')
    expect(wrapper.text()).toContain('Mutates mappings: no')
    expect(wrapper.text()).toContain('Broker execution: no')
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

  it('renders Operations artifact inspection boundaries and S3 evidence', async () => {
    const payloads: Record<string, unknown> = {
      'operations-smoke': {
        status: 'ok',
        generated_at: '2026-06-12T09:00:00+05:30',
        trust_level: 'usable',
        trust_status: 'ok',
        fix_hints: [],
        operator_smoke: {
          counts: { current_blockers: 0 },
          next_commands: []
        }
      },
      'operations-cron-status': {
        status: 'ok',
        counts: {},
        jobs: []
      },
      'operations-cron-logs': {
        status: 'ok',
        logs: [],
        pagination: { logs: { returned_count: 0, total_count: 0 } }
      },
      'operations-event-model-promotion-check': {
        status: 'ok',
        decision: 'hold_research_only',
        ready_for_operator_review: false,
        scorecard: {
          status: 'not_usable',
          operator_action: 'Keep research-only.',
          broker_execution_allowed: false,
          policy_auto_promotion_allowed: false,
          research_safety: {
            leakage_control: {
              passed: false,
              source: 'artifact metadata',
              reason: 'missing point-in-time validation evidence'
            },
            false_discovery_control: {
              passed: true,
              source: 'research ledger',
              reason: 'single fixed-config run recorded'
            },
            cost_adjusted_baseline: {
              passed: false,
              source: 'holdout evaluation',
              reason: 'model did not beat passive after-cost baseline'
            }
          }
        },
        gates: [
          {
            gate: 'leakage_control_evidence',
            passed: false,
            value: { source: 'artifact metadata', reason: 'missing point-in-time validation evidence' },
            threshold: 'explicit pass marker or recognized validation evidence'
          },
          {
            gate: 'false_discovery_control_evidence',
            passed: true,
            value: { source: 'research ledger', reason: 'single fixed-config run recorded' },
            threshold: 'explicit pass marker or recognized validation evidence'
          },
          {
            gate: 'cost_adjusted_baseline_evidence',
            passed: false,
            value: { source: 'holdout evaluation', reason: 'model did not beat passive after-cost baseline' },
            threshold: 'explicit pass marker or recognized validation evidence'
          }
        ],
        failed_gates: ['leakage_control_evidence', 'cost_adjusted_baseline_evidence']
      },
      'operations-event-model-artifacts': {
        status: 'ok',
        read_only: true,
        api_schema: {
          read_only: true,
          broker_execution_enabled: false
        },
        artifact: {
          model_version: 'event_meta_model_h10',
          latest_prefix: 'models/event/latest',
          version_prefix: 'models/event/event_meta_model_h10/20260612T090000Z',
          files: [
            {
              role: 'model',
              filename: 'event_meta_model.json',
              local_path: '.cache/advisory_event_meta_model/event_meta_model.json',
              bytes: 1234,
              sha256: 'abc123',
              latest_key: 'models/event/latest/event_meta_model.json'
            }
          ]
        },
        latest_s3_heads: [
          {
            key: 'models/event/latest/event_meta_model.json',
            status: 'ok',
            content_length: 1234,
            last_modified: '2026-06-12T09:05:00+05:30'
          }
        ],
        pagination: {
          artifact_files: { returned_count: 1, total_count: 1 }
        },
        operator_boundary: {
          authority_scope: 'read_only_artifact_inspection',
          system_applies_model: false,
          policy_auto_promotion_allowed: false,
          s3_write_allowed: false,
          broker_execution_allowed: false,
          operator_action: 'Use this page to verify local artifact hashes and S3 HEAD status only.'
        }
      },
      'operations-research-ledger': {
        status: 'ok',
        runs: [
          {
            research_run_id: 'run-1',
            run_type: 'ts_forecast',
            entrypoint: 'advisory.ts_forecast_paper_portfolio',
            label: 'TS paper portfolio',
            objective: 'Compare TS forecast to momentum baseline.',
            status: 'completed',
            started_ts: '2026-06-12T08:00:00+05:30',
            completed_ts: '2026-06-12T08:05:00+05:30',
            asof_date: '2026-06-01T00:00:00+05:30',
            config_hash: 'hash-1',
            config: { horizon_days: 10 },
            validation_protocol: { baseline: 'momentum' },
            result_metrics: { win_rate: 0.6 },
            notes: { known_failure_cases: ['thin breadth'] }
          }
        ],
        summary: {
          total_count: 1,
          returned_count: 1,
          failed_count: 0,
          running_count: 0,
          missing_validation_protocol_count: 0
        },
        pagination: {
          runs: { returned_count: 1, total_count: 1 }
        },
        operator_boundary: {
          authority_scope: 'read_only_research_audit',
          system_starts_research_runs: false,
          system_finishes_research_runs: false,
          policy_auto_promotion_allowed: false,
          broker_execution_allowed: false,
          operator_action: 'Use this view to audit experiment provenance, validation protocol, results, and missing ledger evidence.'
        }
      },
      'operations-commands': {
        status: 'ok',
        commands: [
          {
            key: 'operator_smoke',
            label: 'Operator Smoke',
            description: 'Read-only smoke',
            risk: 'safe_read_only',
            dry_run: true,
            timeout_seconds: 60,
            args: ['python', '-m', 'advisory.operator_smoke']
          }
        ],
        recent_runs: []
      },
      'operations-api-errors': {
        status: 'ok',
        errors: [],
        summary: {}
      }
    }
    Object.assign(globalThis, {
      useOperatorApi: () => ({
        runOperationsSmoke: vi.fn(async () => payloads['operations-smoke']),
        getCronStatus: vi.fn(async () => payloads['operations-cron-status']),
        getCronLogs: vi.fn(async () => payloads['operations-cron-logs']),
        getEventModelPromotionCheck: vi.fn(async () => payloads['operations-event-model-promotion-check']),
        getEventModelArtifacts: vi.fn(async () => payloads['operations-event-model-artifacts']),
        getResearchLedger: vi.fn(async () => payloads['operations-research-ledger']),
        getOperatorCommands: vi.fn(async () => payloads['operations-commands']),
        getOperatorApiErrors: vi.fn(async () => payloads['operations-api-errors']),
        runOperatorCommand: vi.fn(async () => ({ status: 'ok', run: {} }))
      }),
      useAsyncData: vi.fn((key: string, _handler: () => Promise<unknown>) => ({
        data: ref(payloads[key]),
        refresh: vi.fn(async () => undefined),
        pending: ref(false),
        error: ref(null)
      }))
    })

    const wrapper = await mountAsync(OperationsPage)
    const text = wrapper.text()

    expect(text).toContain('Event-model S3 backup')
    expect(text).toContain('Research Safety Controls')
    expect(text).toContain('Leakage control')
    expect(text).toContain('missing point-in-time validation evidence')
    expect(text).toContain('False-discovery control')
    expect(text).toContain('single fixed-config run recorded')
    expect(text).toContain('Cost-adjusted baseline')
    expect(text).toContain('model did not beat passive after-cost baseline')
    expect(text).toContain('Research-only authority')
    expect(text).toContain('Model stays research-only/manual-review even when evidence gates pass.')
    expect(text).toContain('Read-only artifact inspection')
    expect(text).toContain('Use this page to verify local artifact hashes and S3 HEAD status only.')
    expect(text).toContain('Applies model: no')
    expect(text).toContain('Policy promotion: no')
    expect(text).toContain('S3 writes: no')
    expect(text).toContain('Broker allowed: no')
    expect(text).toContain('Local artifact files')
    expect(text).toContain('event_meta_model.json')
    expect(text).toContain('sha256: abc123')
    expect(text).toContain('S3 object HEAD checks')
    expect(text).toContain('models/event/latest/event_meta_model.json')
    expect(text).toContain('Experiment audit trail')
    expect(text).toContain('Read-only research review')
    expect(text).toContain('Starts runs: no')
    expect(text).toContain('Finishes runs: no')
    expect(text).toContain('TS paper portfolio')
    expect(text).toContain('Compare TS forecast to momentum baseline.')
    expect(text).toContain('"baseline": "momentum"')
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
        api_latency_probe: {
          status: 'warn',
          path: 'logs/performance/latest_api_latency_probe.json',
          age_seconds: 7200,
          slow_count: 1,
          error_count: 0,
          message: 'API latency probe found slow endpoints or stale probe data.',
          operator_action: 'Run the probe, then run the ranked performance report.',
          command: 'python scripts/api_latency_probe.py --output-path logs/performance/latest_api_latency_probe.json',
          performance_report_command: 'python scripts/api_performance_report.py --limit 20'
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
        sync_state_failures: [],
        downloader_run_state: {
          status: 'warn',
          message: 'Latest standardized downloader/parser run-state loaded.',
          rows: [
            {
              module: 'data.nseindia.recent_events',
              source_name: 'download_runner:data.nseindia.recent_events',
              purpose: 'events',
              phase: 'downloader',
              status: 'warn',
              classification: 'source_unavailable',
              classification_label: 'Source unavailable',
              classification_meaning: 'The upstream website/API was unavailable, blocked, or timed out. This is not valid no-data.',
              classification_operator_action: 'Retry later or through the serialized source queue; do not treat the empty output as complete data.',
              rows: 0,
              state_advanced: false,
              updated_at: '2026-06-07T09:30:00+05:30'
            },
            {
              module: 'data.dhanlive.ohlcv',
              source_name: 'download_runner:data.dhanlive.ohlcv',
              purpose: 'dhan_ohlcv_precheck',
              phase: 'downloader',
              status: 'warn',
              classification: 'reference_mapping_missing',
              classification_label: 'Reference mapping missing',
              classification_meaning: 'A symbol could not be mapped to the required broker/security identifier.',
              classification_operator_action: 'Refresh Dhan scrip master, check NSE/BSE fallback mapping, then rerun the failed OHLCV source.',
              rows: 0,
              reference_mapping_missing_count: 2,
              no_data_count: 1,
              source_unavailable_count: 0,
              failed_symbols: ['HUIL', { symbol: 'CPPLUS', exchange: 'NSE' }],
              state_advanced: false,
              updated_at: '2026-06-07T09:35:00+05:30',
              error: 'No Dhan security id mapped for NSE:HUIL'
            }
          ]
        },
        watcher_source_counters: {
          status: 'warn',
          message: 'Latest OHLCV/news/announcement watcher counters loaded.',
          returned_count: 3,
          stale_count: 1,
          error_count: 0,
          missing_sources: [],
          rows: [
            {
              source_name: 'continuous_watch:ohlcv',
              watcher_source: 'ohlcv',
              status: 'ok',
              updated_at: '2026-06-07T09:55:00+05:30',
              age_minutes: 7,
              produced_data: true,
              counters: {
                symbol_count: 12,
                latest_price_count: 12,
                alert_persisted_count: 1,
                alert_suppressed_count: 2
              }
            },
            {
              source_name: 'continuous_watch:news',
              watcher_source: 'news',
              status: 'warn',
              updated_at: '2026-06-07T08:00:00+05:30',
              age_minutes: 122,
              produced_data: false,
              counters: {
                watch_count: 8,
                news_item_count: 0,
                matched_event_count: 0,
                persisted_event_count: 0
              }
            },
            {
              source_name: 'continuous_watch:announcements',
              watcher_source: 'announcements',
              status: 'ok',
              updated_at: '2026-06-07T09:50:00+05:30',
              age_minutes: 12,
              produced_data: true,
              counters: {
                unique_ingest_targets: 3,
                ingest_run_count: 3,
                discovered_count: 4,
                parsed_count: 3,
                failed_count: 1,
                match_count: 2,
                persisted_event_count: 2
              }
            }
          ]
        }
      },
      deferred_diagnostics: {
        status: 'warn',
        count: 2,
        operator_action: 'Fast Health intentionally skipped deep diagnostics. Run full Health only when you need the listed evidence.',
        full_diagnostics_command: 'python -m advisory.operator_health --full --skip-dhan',
        api_full_payload_hint: '/api/health/details?mode=full&compact=false',
        rows: [
          {
            section: 'identity_issues',
            status: 'ok',
            message: 'Dhan/security identity issues deferred in fast health mode.',
            reason: 'Identity coverage checks join action rows, company master, and broker ids; use full health for source-blocker details.',
            command: 'python -m advisory.operator_health --full --skip-dhan'
          },
          {
            section: 'feature_stage_gates',
            status: 'ok',
            message: 'Feature freshness stage gates deferred in fast health mode.',
            reason: 'Stage-gate checks evaluate required feature inputs for recent symbols and are available in full health mode.',
            command: 'python -m advisory.operator_health --full --skip-dhan'
          }
        ]
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
    const ingestionPayload = {
      generated_at: '2026-06-07T10:03:00+05:30',
      status: 'ok',
      summary: {
        count: 2,
        status_counts: { failed: 1, empty_valid_source: 1 },
        source_counts: { bhavcopy: 1, indices: 1 },
        classification_counts: { schema_changed: 1 },
        classification_details: {
          schema_changed: {
            classification: 'schema_changed',
            label: 'Schema changed',
            meaning: 'The source file parsed but expected columns or layout changed.',
            operator_action: 'Inspect a fresh source sample, update the parser/schema mapping, rerun the parser, then refresh Health.',
            trust_impact: 'active_recent_failures_block_trust'
          }
        },
        enriched_sample_rows: [
          {
            source_prefix: 'bhavcopy',
            object_key: 'bhavcopy/bhavcopy_2026-06-01.zip',
            status: 'failed',
            error_message: 'classification=schema_changed; missing columns',
            classification: 'schema_changed',
            classification_detail: {
              label: 'Schema changed',
              meaning: 'The source file parsed but expected columns or layout changed.',
              operator_action: 'Inspect a fresh source sample, update the parser/schema mapping, rerun the parser, then refresh Health.'
            },
            processed_at: '2026-06-07T09:00:00+05:30'
          }
        ]
      },
      operator_boundary: {
        read_only: true,
        clear_command: 'python scripts/ingestion_state_runner.py clear --source <source> --key <object_key>',
        note: 'This endpoint summarizes file-level ingestion state only.'
      }
    }
    Object.assign(globalThis, {
      useOperatorApi: () => ({
        getSummary: vi.fn(async () => summaryPayload),
        getHealthDetails: vi.fn(async () => detailsPayload),
        getIngestionState: vi.fn(async () => ingestionPayload),
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
        data: ref(key === 'summary-health' ? summaryPayload : key === 'operator-ingestion-state' ? ingestionPayload : detailsPayload),
        refresh: vi.fn(async () => undefined),
        pending: ref(false),
        error: ref(null)
      }))
    })

    const wrapper = await mountAsync(HealthPage)
    const text = wrapper.text()

    expect(text).toContain('Stale health snapshot')
    expect(text).toContain('Deferred diagnostics')
    expect(text).toContain('2 deep check(s) skipped in fast Health')
    expect(text).toContain('Identity coverage checks join action rows, company master, and broker ids')
    expect(text).toContain('Feature freshness stage gates')
    expect(text).toContain('python -m advisory.operator_health --full --skip-dhan')
    expect(text).toContain('Current Blockers')
    expect(text).toContain('Trace cache fallback')
    expect(text).toContain('Trace drilldowns may be slow or stale.')
    expect(text).toContain('Fallbacks & Degradation')
    expect(text).toContain('Watcher Source Counters')
    expect(text).toContain('Show latest downloader/parser runs')
    expect(text).toContain('Source unavailable')
    expect(text).toContain('This is not valid no-data.')
    expect(text).toContain('Retry later or through the serialized source queue')
    expect(text).toContain('Failed Symbols / Source Skips')
    expect(text).toContain('data.dhanlive.ohlcv')
    expect(text).toContain('Mapping misses: 2')
    expect(text).toContain('No-data/source: 1 / 0')
    expect(text).toContain('Symbols: HUIL, CPPLUS')
    expect(text).toContain('Refresh Dhan scrip master, check NSE/BSE fallback mapping, then rerun the failed OHLCV source.')
    expect(text).toContain('No Dhan security id mapped for NSE:HUIL')
    expect(text).toContain('Latest OHLCV/news/announcement watcher counters')
    expect(text).toContain('Alerts persisted/suppressed: 1 / 2')
    expect(text).toContain('RSS items: 0')
    expect(text).toContain('Discovered/parsed/failed: 4 / 3 / 1')
    expect(text).toContain('Superseded Cleanup Preview')
    expect(text).toContain('API latency probe')
    expect(text).toContain('Run the probe, then run the ranked performance report.')
    expect(text).toContain('python scripts/api_latency_probe.py --output-path logs/performance/latest_api_latency_probe.json')
    expect(text).toContain('python scripts/api_performance_report.py --limit 20')
    expect(text).toContain('File-Level Ingestion State')
    expect(text).toContain('Schema changed')
    expect(text).toContain('The source file parsed but expected columns or layout changed.')
    expect(text).toContain('Inspect a fresh source sample, update the parser/schema mapping, rerun the parser, then refresh Health.')
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
      preview_id: 'preview-event-policy-1',
      source_type: 'event_policy_review_rule',
      unified_diff: '--- a/config/advisory_setups.yaml\n+++ b/config/advisory_setups.yaml\n+event_policy_review_rules:',
      rollback_note: 'Rollback by not applying this preview.',
      applied: false
    }))
    const decideConfigChangeApplication = vi.fn(async () => ({
      status: 'ok',
      application_id: 'app-event-1',
      preview_id: 'preview-event-policy-1',
      application_decision: 'approved_to_apply',
      verification_status: 'not_supported',
      applied_by_system: false,
      applied: false,
      note: 'Decision recorded only.',
      decision_effect: {
        state: 'approved_for_manual_application',
        next_step: 'Operator may manually apply the reviewed patch outside Stockey.',
        mutates_config: false,
        mutates_policy: false,
        mutates_portfolio: false
      },
      operator_boundary: {
        system_applies_config: false,
        system_promotes_policy: false,
        broker_execution_allowed: false
      }
    }))
    const refreshApplications = vi.fn(async () => undefined)
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
    const applicationPayload = {
      status: 'ok',
      applications: [
        {
          application_id: 'app-event-old',
          preview_id: 'preview-event-old',
          application_decision: 'marked_applied',
          verification_status: 'not_supported',
          decision_effect: {
            next_step: 'Rerun the relevant dry-run checks.',
            mutates_config: false
          },
          operator_boundary: {
            broker_execution_allowed: false
          }
        }
      ]
    }
    Object.assign(globalThis, {
      useOperatorApi: () => ({
        getEvents: vi.fn(async () => ({ events: [], pagination: { events: {} }, snapshot: {}, snapshot_warning: null })),
        getEventPolicy: vi.fn(async () => policyPayload),
        getEventPolicyEvaluation: vi.fn(async () => policyEvaluationPayload),
        getEventPolicyPromotionReviews: vi.fn(async () => promotionReviewsPayload),
        getConfigChangeApplications: vi.fn(async () => applicationPayload),
        reviewEventPolicyGroup,
        decideEventPolicyPromotionReview,
        previewEventPolicyConfigChange,
        decideConfigChangeApplication,
        getEventTraceSummary: vi.fn(async () => ({ status: 'ok', rows: [] }))
      }),
      useAsyncData: vi.fn((key: string, _handler: () => Promise<unknown>) => ({
        data: ref(key === 'event-policy' ? policyPayload : key === 'event-policy-evaluation' ? policyEvaluationPayload : key === 'event-policy-promotion-reviews' ? promotionReviewsPayload : key === 'config-change-applications-event-policy' ? applicationPayload : { events: [], pagination: { events: {} }, snapshot: {}, snapshot_warning: null }),
        refresh: key === 'config-change-applications-event-policy' ? refreshApplications : vi.fn(async () => undefined),
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
    expect(text).toContain('Recent application decisions')
    expect(text).toContain('Audit trail for reviewed event-policy/config previews')
    expect(text).toContain('Config mutated: no')
    expect(text).toContain('Broker allowed: no')

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
    expect(wrapper.text()).toContain('Application audit')

    await wrapper.findAll('button').find(button => button.text() === 'Record Audit')?.trigger('click')
    await flushPromises()
    expect(decideConfigChangeApplication).toHaveBeenCalledWith({
      preview_id: 'preview-event-policy-1',
      application_decision: 'approved_to_apply',
      operator_note: '',
      verify_config: true
    })
    expect(refreshApplications).toHaveBeenCalled()
    expect(wrapper.text()).toContain('Saved application decision')
    expect(wrapper.text()).toContain('Approved For Manual Application')
    expect(wrapper.text()).toContain('Policy mutated: no')
  })

  it('renders technical config application audit boundaries', async () => {
    const previewTechnicalThresholdConfigChange = vi.fn(async () => ({
      status: 'ok',
      preview_id: 'preview-technical-1',
      source_type: 'technical_threshold',
      config_path: 'config/advisory_setups.yaml',
      unified_diff: '--- a/config/advisory_setups.yaml\n+++ b/config/advisory_setups.yaml\n+threshold: 72',
      rollback_note: 'Rollback by not applying this preview.',
      applied: false
    }))
    const decideConfigChangeApplication = vi.fn(async () => ({
      status: 'ok',
      application_id: 'app-1',
      preview_id: 'preview-technical-1',
      application_decision: 'approved_to_apply',
      verification_status: 'not_requested',
      applied_by_system: false,
      applied: false,
      note: 'Decision recorded only.',
      decision_effect: {
        state: 'approved_for_manual_application',
        next_step: 'Operator may manually apply the reviewed patch outside Stockey.',
        mutates_config: false,
        mutates_policy: false,
        mutates_portfolio: false
      },
      operator_boundary: {
        system_applies_config: false,
        system_promotes_policy: false,
        broker_execution_allowed: false
      }
    }))
    const refreshApplications = vi.fn(async () => undefined)
    const payloads: Record<string, unknown> = {
      'technical-calibration': {
        status: 'ok',
        generated_at: '2026-06-12T09:00:00+05:30',
        summary: [],
        top_configs: []
      },
      'technical-promotion-reviews': {
        status: 'ok',
        reviews: [
          {
            reviewed_at: '2026-06-12T08:30:00+05:30',
            setup_id: 'EVENT_OPPORTUNITY_V1',
            config_id: 'cfg-1',
            recommendation: 'approved_for_manual_review',
            confidence: 0.82,
            manual_decision: 'approved',
            manual_patch_text: 'threshold: 72'
          }
        ]
      },
      'config-change-applications-technical': {
        status: 'ok',
        applications: [
          {
            application_id: 'app-old',
            preview_id: 'preview-old',
            application_decision: 'marked_applied',
            verification_status: 'verified',
            decision_effect: {
              next_step: 'Rerun the relevant dry-run checks.',
              mutates_config: false
            },
            operator_boundary: {
              broker_execution_allowed: false
            }
          }
        ]
      }
    }
    Object.assign(globalThis, {
      useOperatorApi: () => ({
        getTechnicalCalibration: vi.fn(async () => payloads['technical-calibration']),
        getTechnicalPromotionReviews: vi.fn(async () => payloads['technical-promotion-reviews']),
        getConfigChangeApplications: vi.fn(async () => payloads['config-change-applications-technical']),
        previewTechnicalThresholdConfigChange,
        decideConfigChangeApplication
      }),
      useAsyncData: vi.fn((key: string, _handler: () => Promise<unknown>) => ({
        data: ref(payloads[key]),
        refresh: key === 'config-change-applications-technical' ? refreshApplications : vi.fn(async () => undefined),
        pending: ref(false),
        error: ref(null)
      }))
    })

    const wrapper = await mountAsync(TechnicalCalibrationPage)
    expect(wrapper.text()).toContain('Recent application decisions')
    expect(wrapper.text()).toContain('Config mutated: no')
    expect(wrapper.text()).toContain('Broker allowed: no')

    await wrapper.findAll('button').find(button => button.text() === 'Reviewed Diff')?.trigger('click')
    await flushPromises()
    expect(previewTechnicalThresholdConfigChange).toHaveBeenCalledWith({
      reviewed_at: '2026-06-12T08:30:00+05:30',
      setup_id: 'EVENT_OPPORTUNITY_V1',
      config_id: 'cfg-1'
    })
    expect(wrapper.text()).toContain('Application audit')
    expect(wrapper.text()).toContain('Record what happened after reviewing this diff')

    await wrapper.findAll('button').find(button => button.text() === 'Record Audit')?.trigger('click')
    await flushPromises()
    expect(decideConfigChangeApplication).toHaveBeenCalledWith({
      preview_id: 'preview-technical-1',
      application_decision: 'approved_to_apply',
      operator_note: '',
      verify_config: true
    })
    expect(refreshApplications).toHaveBeenCalled()
    expect(wrapper.text()).toContain('Saved application decision')
    expect(wrapper.text()).toContain('approved for manual application')
    expect(wrapper.text()).toContain('Policy mutated: no')
  })

  it('renders signal quality config application audit boundaries', async () => {
    const previewSignalQualityConfigChange = vi.fn(async () => ({
      status: 'ok',
      preview_id: 'preview-signal-1',
      source_type: 'signal_quality_overlay',
      config_path: 'config/advisory_setups.yaml',
      unified_diff: '--- a/config/advisory_setups.yaml\n+++ b/config/advisory_setups.yaml\n+signal_quality_rules:',
      rollback_note: 'Rollback by not applying this preview.',
      applied: false
    }))
    const decideConfigChangeApplication = vi.fn(async () => ({
      status: 'ok',
      application_id: 'app-signal-1',
      preview_id: 'preview-signal-1',
      application_decision: 'approved_to_apply',
      verification_status: 'not_requested',
      applied_by_system: false,
      applied: false,
      note: 'Decision recorded only.',
      decision_effect: {
        state: 'approved_for_manual_application',
        next_step: 'Operator may manually apply the reviewed patch outside Stockey.',
        mutates_config: false,
        mutates_policy: false,
        mutates_portfolio: false
      },
      operator_boundary: {
        system_applies_config: false,
        system_promotes_policy: false,
        broker_execution_allowed: false
      }
    }))
    const refreshApplications = vi.fn(async () => undefined)
    const payloads: Record<string, unknown> = {
      'signal-quality': {
        status: 'ok',
        latest_evaluated_at: '2026-06-12T09:00:00+05:30',
        summary: [
          {
            evaluated_at: '2026-06-12T09:00:00+05:30',
            horizon_days: 10,
            variant: 'technical_plus_all',
            avg_forward_return_after_cost: 0.04,
            lift_vs_technical_only: 0.02,
            hit_rate_after_cost: 0.6,
            selected_count: 12,
            matured_count: 10,
            recommendation: 'review_candidate'
          }
        ],
        coverage: [
          {
            horizon_days: 10,
            candidate_rows: 25,
            event_policy_rows: 10,
            bhavcopy_rows: 20,
            company_memory_rows: 8
          }
        ],
        examples: [],
        meta: {}
      },
      'signal-quality-health': {
        sections: {
          trust_gate: {
            status: 'ok',
            trust_level: 'usable',
            recommendation: 'Review-only signal quality evidence.',
            checks: []
          }
        }
      },
      'signal-quality-promotion-reviews': {
        status: 'ok',
        reviews: [
          {
            reviewed_at: '2026-06-12T08:30:00+05:30',
            evaluated_at: '2026-06-12T08:00:00+05:30',
            horizon_days: 10,
            variant: 'technical_plus_all',
            recommendation: 'approved_for_manual_review',
            confidence: 0.82,
            manual_decision: 'approved',
            manual_patch_text: 'signal_quality_rules: []'
          }
        ]
      },
      'config-change-applications-signal-quality': {
        status: 'ok',
        applications: [
          {
            application_id: 'app-signal-old',
            preview_id: 'preview-signal-old',
            application_decision: 'marked_applied',
            verification_status: 'verified',
            decision_effect: {
              next_step: 'Rerun the relevant dry-run checks.',
              mutates_config: false
            },
            operator_boundary: {
              broker_execution_allowed: false
            }
          }
        ]
      }
    }
    Object.assign(globalThis, {
      useOperatorApi: () => ({
        getSignalQuality: vi.fn(async () => payloads['signal-quality']),
        getHealthDetails: vi.fn(async () => payloads['signal-quality-health']),
        getSignalQualityPromotionReviews: vi.fn(async () => payloads['signal-quality-promotion-reviews']),
        getConfigChangeApplications: vi.fn(async () => payloads['config-change-applications-signal-quality']),
        previewSignalQualityConfigChange,
        decideConfigChangeApplication
      }),
      useAsyncData: vi.fn((key: string, _handler: () => Promise<unknown>) => ({
        data: ref(payloads[key]),
        refresh: key === 'config-change-applications-signal-quality' ? refreshApplications : vi.fn(async () => undefined),
        pending: ref(false),
        error: ref(null)
      }))
    })

    const wrapper = await mountAsync(SignalQualityPage)
    expect(wrapper.text()).toContain('Recent application decisions')
    expect(wrapper.text()).toContain('Config mutated: no')
    expect(wrapper.text()).toContain('Broker allowed: no')

    await wrapper.findAll('button').find(button => button.text() === 'Reviewed Diff')?.trigger('click')
    await flushPromises()
    expect(previewSignalQualityConfigChange).toHaveBeenCalledWith({
      reviewed_at: '2026-06-12T08:30:00+05:30',
      evaluated_at: '2026-06-12T08:00:00+05:30',
      horizon_days: 10,
      variant: 'technical_plus_all'
    })
    expect(wrapper.text()).toContain('Application audit')
    expect(wrapper.text()).toContain('Record whether this reviewed overlay diff was manually applied')

    await wrapper.findAll('button').find(button => button.text() === 'Record Audit')?.trigger('click')
    await flushPromises()
    expect(decideConfigChangeApplication).toHaveBeenCalledWith({
      preview_id: 'preview-signal-1',
      application_decision: 'approved_to_apply',
      operator_note: '',
      verify_config: true
    })
    expect(refreshApplications).toHaveBeenCalled()
    expect(wrapper.text()).toContain('Saved application decision')
    expect(wrapper.text()).toContain('Approved For Manual Application')
    expect(wrapper.text()).toContain('Policy mutated: no')
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
          action_transition: {
            action_code: 'MANUAL_REVIEW',
            state_effect: 'review_only',
            broker_order_candidate: false,
            broker_execution_allowed: false,
            broker_boundary: 'This action is review-only/monitoring/policy context and does not create a broker order.',
            next_required_stage: 'operator_manual_review',
            allowed_after_preview: false,
            required_preconditions: ['operator_question_present', 'no_broker_order'],
            missing_preconditions: [],
            precondition_status: 'complete',
            entry_evidence_required: false,
            exit_evidence_required: false,
            policy_audit_required: false
          },
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
        review_status: 'completed',
        evidence_source_contract: {
          coverage_status: 'partial',
          missing_required_sources: ['bhavcopy_evidence']
        }
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
      generated_at: '2026-06-07T10:01:00+05:30',
      today_recommendations: [],
      current_recommendations: [],
      exited_recommendations: [],
      portfolio: [],
      lifecycle: []
    }
    const eventsPayload = {
      generated_at: '2026-06-07T10:02:00+05:30',
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
      generated_at: '2026-06-07T10:03:00+05:30',
      status: 'ok',
      symbol: 'ABC',
      raw_counts: {},
      processing: [],
      decisions: [],
      manual_review_wait_signal_links: [],
      action_conflicts: []
    }
    const featureFreshnessPayload = {
      generated_at: '2026-06-07T10:04:00+05:30',
      symbol: 'ABC',
      status: 'blocked',
      counts: { fresh: 2, stale: 1, missing: 0 },
      blockers: [{ input_key: 'technical_daily', label: 'Technical Features', status: 'stale', reason: 'older_than_freshness_window' }],
      inputs: [],
      stage_gates: [
        {
          stage: 'actions',
          status: 'blocked',
          symbols_checked: 1,
          blocked_count: 1,
          required_input_keys: ['technical_daily'],
          gate_effect: 'Blocked positive action until technical features are fresh.'
        },
        {
          stage: 'lifecycle',
          status: 'ok',
          symbols_checked: 1,
          blocked_count: 0,
          required_input_keys: ['daily_ohlcv'],
          gate_effect: 'Lifecycle may proceed.'
        }
      ],
      stage_gate_summary: {
        stage_count: 2,
        blocked_stage_count: 1,
        error_stage_count: 0,
        blocked_inputs: [{ stage: 'actions', input_key: 'technical_daily', label: 'Technical Features', status: 'stale', reason: 'older_than_freshness_window' }],
        operator_boundary: {
          read_only: true,
          mutates_recommendations: false,
          mutates_portfolio: false,
          broker_execution_allowed: false
        }
      }
    }
    const identityIssuesPayload = {
      generated_at: '2026-06-07T10:05:00+05:30',
      status: 'ok',
      summary: {
        total_open: 1,
        by_type: { dhan_security_id_missing: 1 },
        broker_execution_enabled: false
      },
      source_warnings: [
        {
          title: 'Stale source rows',
          message: 'Identity issue rows are stale.',
          source: 'resolve_dhan_identity',
          reason: 'source_rows_stale',
          operator_action: 'rerun_source_or_refresh_advisory'
        }
      ],
      issues: [
        {
          issue_key: 'dhan_security_id_missing:stock:NSE:ABC',
          issue_type: 'dhan_security_id_missing',
          status: 'open',
          symbol: 'ABC',
          requested_exchange: 'NSE',
          asset_type: 'stock',
          source: 'resolve_dhan_identity',
          error_text: 'No Dhan security id mapped for NSE:ABC',
          suggested_action: 'Refresh Dhan scrip master and company master.',
          repair_hint: 'Refresh Dhan scrip master and company master.',
          attempt_count: 2,
          last_seen_at: '2026-06-07T09:45:00+05:30'
        }
      ]
    }
    Object.assign(globalThis, {
      useRoute: () => ({ params: { symbol: 'abc' }, query: {}, hash: '' }),
      useOperatorApi: () => ({
        getActions: vi.fn(async () => actionsPayload),
        getPortfolio: vi.fn(async () => portfolioPayload),
        getPortfolioDetail: vi.fn(async () => ({ policy_changes: [] })),
        getEvents: vi.fn(async () => eventsPayload),
        getSymbolTraceSummary: vi.fn(async () => tracePayload),
        getFeatureFreshness: vi.fn(async () => featureFreshnessPayload),
        getIdentityIssues: vi.fn(async () => identityIssuesPayload)
      }),
      useAsyncData: vi.fn((key: string, _handler: () => Promise<unknown>) => {
        const payload = key.startsWith('symbol-actions-')
          ? actionsPayload
          : key.startsWith('symbol-portfolio-')
            ? portfolioPayload
            : key.startsWith('symbol-events-')
              ? eventsPayload
              : key.startsWith('symbol-feature-freshness-')
                ? featureFreshnessPayload
                : key.startsWith('symbol-identity-issues-')
                  ? identityIssuesPayload
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
    expect(text).toContain('Payload freshness')
    expect(text).toContain('Data inputs')
    expect(text).toContain('Identity issue rows are stale.')
    expect(text).toContain('Can I still participate?')
    expect(text).toContain('formal target missing')
    expect(text).toContain('Current price is 6.5% above the attractive zone high.')
    expect(text).toContain('Reason Contract')
    expect(text).toContain('Evidence coverage:')
    expect(text).toContain('Missing compact evidence: bhavcopy_evidence')
    expect(text).toContain('Execution preconditions')
    expect(text).toContain('State effect: review_only')
    expect(text).toContain('Next stage: operator_manual_review')
    expect(text).toContain('Operator question')
    expect(text).toContain('No broker order')
    expect(text).toContain('does not create a broker order')
    expect(text).toContain('Company Memory Review')
    expect(text).toContain('ABC memory review says watch for breadth recovery.')
    expect(text).toContain('review_input_only')
    expect(text).toContain('Original: BUY')
    expect(text).toContain('Market gate')
    expect(text).toContain('Risk-off market context blocks positive action.')
    expect(text).toContain('Current Stage Gates')
    expect(text).toContain('What would be blocked if the pipeline ran now')
    expect(text).toContain('Blocked positive action until technical features are fresh.')
    expect(text).toContain('Broker execution: no')
    expect(text).toContain('actions: Technical Features')
    expect(text).toContain('Identity / Source Blockers')
    expect(text).toContain('No Dhan security id mapped for NSE:ABC')
    expect(text).toContain('Refresh Dhan scrip master and company master.')
    expect(text).toContain('Open identity workbench')
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
    const failuresPayload = {
      generated_at: '2026-06-11T10:00:00+05:30',
      status: 'warn',
      message: 'Recent Screener.in query/fetch/parse failures found.',
      window_hours: 24,
      active_count: 1,
      validation_count: 0,
      fetch_count: 0,
      parse_count: 1,
      counts_by_stage: { registered_parse: 1 },
      rows: [
        {
          failure_id: 'failure-1',
          observed_at: '2026-06-11T09:45:00+05:30',
          failure_stage: 'registered_parse',
          query_name: 'Breakout screener',
          query_text: 'Current price > DMA 50',
          screener_url: 'https://www.screener.in/screens/1/demo/',
          error_type: 'ValueError',
          error_message: 'Could not find Screener.in results table',
          body_excerpt: 'Login required'
        }
      ],
      operator_boundary: {
        read_only: true,
        manual_review_item_source: false,
        broker_execution_enabled: false,
        registers_production_screener: false,
        clears_failure_rows: false,
        side_effects: 'Read-only failure audit view. It does not clear failures, retry Screener.in, register screeners, change recommendations, or submit broker orders.',
        operator_action: 'Fix query syntax, login/session, or parser issue; then rerun the affected Screener workflow and refresh Health/Screeners.'
      }
    }
    Object.assign(globalThis, {
      useOperatorApi: () => ({
        previewScreener,
        getScreenerCoverage: vi.fn(async () => coveragePayload),
        getScreenerFailures: vi.fn(async () => failuresPayload)
      }),
      useAsyncData: vi.fn((key: string, _handler: () => Promise<unknown>) => {
        const payload = key === 'screener-failures' ? failuresPayload : coveragePayload
        return {
          data: ref(payload),
          refresh: vi.fn(async () => undefined),
          pending: ref(false),
          error: ref(null)
        }
      })
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
    expect(text).toContain('Recent Screener.in failures')
    expect(text).toContain('operational repair signals, not investment Manual Review items')
    expect(text).toContain('Breakout screener')
    expect(text).toContain('Could not find Screener.in results table')
    expect(text).toContain('Fix query syntax, login/session, or parser issue')
    expect(text).toContain('does not clear failures, retry Screener.in, register screeners, change recommendations, or submit broker orders')
    expect(text).toContain('moving_average_alias')
    expect(text).toContain('Use `DMA 50` instead of `50 Day Moving Average`.')
    expect(text).toContain('Query results are never stored by preview')
  })
})
