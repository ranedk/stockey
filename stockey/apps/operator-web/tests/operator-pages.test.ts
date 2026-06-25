import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import { defineComponent, h, nextTick, ref, Suspense } from 'vue'

import OperationsPage from '../pages/operations.vue'
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
  it('exposes the primary operator pages and live-trading status in the global nav', async () => {
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

    for (const href of ['/workbench', '/recommendations-unified', '/positions', '/llm-decisions', '/health-hub']) {
      expect(wrapper.find(`a[href="${href}"]`).exists()).toBe(true)
    }
    // The retired live-execution page is gone from the nav.
    expect(wrapper.find('a[href="/execution-approvals"]').exists()).toBe(false)
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
