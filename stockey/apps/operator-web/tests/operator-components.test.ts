import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ReasonContractPanel from '../components/ReasonContractPanel.vue'
import SnapshotWarning from '../components/SnapshotWarning.vue'
import SourceWarnings from '../components/SourceWarnings.vue'
import TraceTimeline from '../components/TraceTimeline.vue'

const statusPillStub = {
  template: '<span class="status-pill"><slot /></span>'
}

const nuxtLinkStub = {
  props: ['to'],
  template: '<a :href="to"><slot /></a>'
}

describe('operator safety UI components', () => {
  it('renders stale snapshot metadata and refresh guidance', () => {
    const wrapper = mount(SnapshotWarning, {
      props: {
        snapshot: {
          freshness: 'stale',
          generated_at: '2026-06-07T09:00:00+05:30',
          age_seconds: 7200,
          source: 'operator_snapshot'
        },
        warning: {
          title: 'Stale action snapshot',
          message: 'Actions are older than the current advisory state.'
        }
      }
    })

    const text = wrapper.text()
    expect(text).toContain('Stale action snapshot')
    expect(text).toContain('2h old')
    expect(text).toContain('operator_snapshot')
    expect(text).toContain('python -m advisory.operator_snapshot')
  })

  it('renders source freshness warning details', () => {
    const wrapper = mount(SourceWarnings, {
      props: {
        warnings: [
          {
            source: 'manual_review',
            reason: 'stale_visible_rows',
            message: 'Open review rows are older than the freshness window.',
            latest_at: '2026-06-06T18:00:00+05:30',
            age_seconds: 90000,
            affected_rows: 1200
          }
        ]
      },
      global: {
        components: {
          StatusPill: statusPillStub
        }
      }
    })

    const text = wrapper.text()
    expect(text).toContain('Source Freshness')
    expect(text).toContain('stale_visible_rows')
    expect(text).toContain('manual_review')
    expect(text).toContain('Open review rows are older than the freshness window.')
    expect(text).toContain('age 1d')
    expect(text).toContain('rows 1,200')
  })

  it('renders reason-contract safety evidence without trace drilldown', () => {
    const wrapper = mount(ReasonContractPanel, {
      props: {
        status: 'incomplete_contract_downgraded',
        contract: {
          action_code: 'MANUAL_REVIEW',
          action_source: 'event_policy',
          original_action_code: 'BUY',
          primary_reason: 'Broker-capable action downgraded for review.',
          missing_fields: ['risk.stop_loss', 'execution.order_preview'],
          evidence: {
            event: {
              review_action: 'veto',
              review_reason: 'Announcement contradicts the BUY thesis.',
              action_status: 'blocked_by_adversarial_review',
              veto: true
            },
            macro_regime: {
              market_context_adjustment: 'manual_review',
              market_context_adjustment_reason: 'Risk-off market context blocks positive action.',
              regime_name: 'VOLATILE',
              breadth_trend_alignment_pct: 34.2,
              risk_off_score: 0.81
            },
            conflict_resolution: {
              source_precedence_reason: 'Lifecycle exit beats fresh BUY setup.',
              winning_action_code: 'SELL',
              winning_action_source: 'lifecycle',
              same_symbol_conflict_count: 1,
              losing_candidates: [
                { action_code: 'BUY', action_source: 'screener' }
              ]
            }
          }
        }
      }
    })

    const text = wrapper.text()
    expect(text).toContain('Reason Contract')
    expect(text).toContain('incomplete_contract_downgraded')
    expect(text).toContain('Original: BUY')
    expect(text).toContain('Adversarial review')
    expect(text).toContain('Announcement contradicts the BUY thesis.')
    expect(text).toContain('Market gate')
    expect(text).toContain('Risk-off market context blocks positive action.')
    expect(text).toContain('risk.stop_loss')
    expect(text).toContain('Conflict resolution: 1 losing candidate(s)')
    expect(text).toContain('Lifecycle exit beats fresh BUY setup.')
  })

  it('renders trace manual-review wait links and embedded reason contracts', () => {
    const wrapper = mount(TraceTimeline, {
      props: {
        syncUrl: false,
        trace: {
          symbol: 'ABC',
          raw_counts: {
            processing: 1,
            traces: 1,
            steps: 1,
            action_conflicts: 0,
            manual_review_wait_signal_links: 1
          },
          _trace_summary_cache: {
            source: 'materialized',
            generated_at: '2026-06-07T10:00:00+05:30',
            entity_type: 'symbol',
            entity_key: 'ABC'
          },
          processing: [
            {
              domain: 'event',
              stage: 'event_evaluation',
              status: 'completed',
              completed_at: '2026-06-07T10:01:00+05:30',
              payload: { event_class: 'RESULT_UPDATE', confidence: 0.88 }
            }
          ],
          decisions: [
            {
              trace_id: 'trace-abc',
              symbol: 'ABC',
              domain: 'action',
              trigger_type: 'signal_refresh',
              final_action: 'MANUAL_REVIEW',
              final_reason: 'Matched wait signal requires operator review.',
              updated_at: '2026-06-07T10:05:00+05:30',
              payload: {
                reason_contract_status: 'complete_review_only',
                recommendation_reason: {
                  action_code: 'MANUAL_REVIEW',
                  evidence: {
                    wait_signal: {
                      signal_id: 42,
                      match_reason: 'Result filing matched requested clarification.'
                    }
                  }
                }
              },
              steps: []
            }
          ],
          manual_review_wait_signal_links: [
            {
              manual_review_item_id: 'manual:ABC:1',
              signal_id: '42',
              decision: 'watch_for_event',
              match_status: 'matched',
              expected_action: 'MANUAL_REVIEW',
              wait_question: 'Wait for clarification filing.',
              match_reason: 'Result filing matched requested clarification.',
              match_source_table: 'exchange_events',
              match_source_key: 'event-42',
              condition: { condition_type: 'result_update' }
            }
          ],
          action_conflicts: []
        }
      },
      global: {
        components: {
          NuxtLink: nuxtLinkStub,
          ReasonContractPanel
        }
      }
    })

    const text = wrapper.text()
    expect(text).toContain('ABC')
    expect(text).toContain('Decision to Wait Signal Links')
    expect(text).toContain('manual:ABC:1')
    expect(text).toContain('Result filing matched requested clarification.')
    expect(text).toContain('Reason Contract')
    expect(text).toContain('Wait Signal')
  })
})
