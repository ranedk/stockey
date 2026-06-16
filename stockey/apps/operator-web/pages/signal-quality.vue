<script setup lang="ts">
import type { ConfigChangeApplicationResult, ConfigChangeApplicationsPayload, ConfigChangePreviewResult, Dict, SignalQualityPromotionReviewResult } from '~/types/api'

const api = useOperatorApi()
const { data, refresh, pending, error } = await useAsyncData('signal-quality', () => api.getSignalQuality(8))
const { data: healthData } = await useAsyncData('signal-quality-health', () => api.getHealthDetails())
const { data: reviewsData, refresh: refreshReviews } = await useAsyncData('signal-quality-promotion-reviews', () => api.getSignalQualityPromotionReviews(25))
const { data: applicationsData, refresh: refreshApplications } = await useAsyncData<ConfigChangeApplicationsPayload>('config-change-applications-signal-quality', () => api.getConfigChangeApplications(10))
const reviewingKey = ref('')
const reviewError = ref('')
const reviewResult = ref<SignalQualityPromotionReviewResult | null>(null)
const decisionReviewKey = ref('')
const decisionReason = ref('')
const decisionResult = ref<Dict | null>(null)
const decisionError = ref('')
const configPreviewKey = ref('')
const configPreviewResult = ref<ConfigChangePreviewResult | null>(null)
const configPreviewError = ref('')
const applicationDecision = ref<'approved_to_apply' | 'marked_applied' | 'rejected' | 'needs_more_data'>('approved_to_apply')
const applicationNote = ref('')
const applicationBusy = ref(false)
const applicationError = ref('')
const applicationResult = ref<ConfigChangeApplicationResult | null>(null)

const summaryRows = computed(() => data.value?.summary || [])
const coverageRows = computed(() => data.value?.coverage || [])
const exampleRows = computed(() => data.value?.examples || [])
const reviewRows = computed(() => reviewsData.value?.reviews || [])
const applicationRows = computed(() => applicationsData.value?.applications || [])
const trustGate = computed(() => {
  const sections = healthData.value?.sections
  if (!sections || typeof sections !== 'object') return {}
  return ((sections as Dict).trust_gate || {}) as Dict
})
const trustChecks = computed(() => Array.isArray(trustGate.value.checks) ? trustGate.value.checks as Dict[] : [])

function numberText(value: unknown, digits = 2) {
  const num = Number(value)
  if (Number.isNaN(num)) return '-'
  return Intl.NumberFormat('en-IN', { maximumFractionDigits: digits }).format(num)
}

function pct(value: unknown, digits = 1) {
  const num = Number(value)
  if (Number.isNaN(num)) return '-'
  return `${(num * 100).toFixed(digits)}%`
}

function dateText(value: unknown) {
  if (!value) return '-'
  const date = new Date(String(value))
  if (Number.isNaN(date.getTime())) return String(value)
  return date.toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' })
}

function variantLabel(value: unknown) {
  const text = String(value || '').replaceAll('_', ' ')
  return text ? text.replace(/\b\w/g, (char) => char.toUpperCase()) : '-'
}

function variantClass(row: Dict) {
  const variant = String(row.variant || '')
  const lift = Number(row.lift_vs_technical_only)
  if (variant === 'technical_only') return 'border-ink/15 bg-ink text-paper'
  if (!Number.isNaN(lift) && lift > 0.01) return 'border-moss/25 bg-moss/10 text-moss'
  if (!Number.isNaN(lift) && lift < -0.01) return 'border-rust/25 bg-rust/10 text-rust'
  return 'border-black/10 bg-white/80 text-ink'
}

function recommendationClass(row: Dict) {
  const text = String(row.recommendation || '')
  if (text.includes('improves')) return 'bg-moss/10 text-moss'
  if (text.includes('worse')) return 'bg-rust/10 text-rust'
  if (text.includes('insufficient')) return 'bg-sun/20 text-ink'
  return 'bg-white/70 text-ink/70'
}

function rowsForHorizon(horizon: unknown) {
  return summaryRows.value.filter((row) => Number(row.horizon_days) === Number(horizon))
}

const horizons = computed(() => {
  const values = new Set(summaryRows.value.map((row) => Number(row.horizon_days)).filter((value) => !Number.isNaN(value)))
  return Array.from(values).sort((a, b) => a - b)
})

function coverageForHorizon(horizon: unknown) {
  return coverageRows.value.find((row) => Number(row.horizon_days) === Number(horizon)) || {}
}

function examplesFor(row: Dict) {
  return exampleRows.value
    .filter((example) => Number(example.horizon_days) === Number(row.horizon_days) && String(example.variant) === String(row.variant))
    .slice(0, 5)
}

function rawContext(row: Dict) {
  const raw = row.raw_context_json
  if (!raw) return {}
  if (typeof raw === 'object') return raw as Dict
  try {
    return JSON.parse(String(raw)) as Dict
  } catch {
    return { raw }
  }
}

function bestOverlayForHorizon(horizon: unknown) {
  const meta = data.value?.meta || {}
  const best = meta.best_overlay_by_horizon
  if (!best || typeof best !== 'object') return null
  return (best as Dict)[String(horizon)] as Dict | undefined
}

function rowKey(row: Dict) {
  return `${String(row.evaluated_at || data.value?.latest_evaluated_at || '')}:${String(row.horizon_days || '')}:${String(row.variant || '')}`
}

function reviewKey(row: Dict) {
  return `${String(row.reviewed_at || '')}:${String(row.evaluated_at || '')}:${String(row.horizon_days || '')}:${String(row.variant || '')}`
}

function canReview(row: Dict) {
  return String(row.variant || '') !== 'technical_only' && Boolean(row.evaluated_at || data.value?.latest_evaluated_at)
}

function copyJson(value: unknown) {
  if (!import.meta.client) return
  navigator.clipboard?.writeText(JSON.stringify(value || {}, null, 2))
}

function patchText(row: Dict) {
  const finalPatch = typeof row.final_patch === 'object' && row.final_patch ? row.final_patch as Dict : {}
  return String(row.manual_patch_text || finalPatch.manual_patch_text || '')
}

function copyPatchText(row: Dict) {
  if (!import.meta.client) return
  navigator.clipboard?.writeText(patchText(row) || JSON.stringify(row.patch || row.pending_patch || {}, null, 2))
}

function copyDiff(value: unknown) {
  if (!import.meta.client) return
  navigator.clipboard?.writeText(String(value || ''))
}

function asDict(value: unknown): Dict {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Dict : {}
}

function boolLabel(value: unknown) {
  return value ? 'yes' : 'no'
}

async function requestPromotionReview(row: Dict) {
  const key = rowKey(row)
  reviewingKey.value = key
  reviewError.value = ''
  reviewResult.value = null
  try {
    reviewResult.value = await api.reviewSignalQualityOverlay({
      evaluated_at: row.evaluated_at || data.value?.latest_evaluated_at,
      horizon_days: row.horizon_days,
      variant: row.variant
    })
    await refreshReviews()
  } catch (error) {
    reviewError.value = error instanceof Error ? error.message : String(error)
  } finally {
    reviewingKey.value = ''
  }
}

async function recordDecision(row: Dict, decision: 'approved' | 'rejected' | 'needs_more_data') {
  const key = reviewKey(row)
  decisionReviewKey.value = key
  decisionError.value = ''
  decisionResult.value = null
  try {
    decisionResult.value = await api.decideSignalQualityPromotionReview({
      reviewed_at: row.reviewed_at,
      evaluated_at: row.evaluated_at,
      horizon_days: row.horizon_days,
      variant: row.variant,
      decision,
      decision_reason: decisionReason.value
    }) as unknown as Dict
    decisionReason.value = ''
    await refreshReviews()
  } catch (error) {
    decisionError.value = error instanceof Error ? error.message : String(error)
  } finally {
    decisionReviewKey.value = ''
  }
}

async function previewConfigChange(row: Dict) {
  const key = reviewKey(row)
  configPreviewKey.value = key
  configPreviewError.value = ''
  configPreviewResult.value = null
  try {
    configPreviewResult.value = await api.previewSignalQualityConfigChange({
      reviewed_at: row.reviewed_at,
      evaluated_at: row.evaluated_at,
      horizon_days: row.horizon_days,
      variant: row.variant
    })
  } catch (error) {
    configPreviewError.value = error instanceof Error ? error.message : String(error)
  } finally {
    configPreviewKey.value = ''
  }
}

async function recordConfigApplication() {
  const previewId = String(configPreviewResult.value?.preview_id || '').trim()
  applicationError.value = ''
  applicationResult.value = null
  if (!previewId) {
    applicationError.value = 'Generate and persist a reviewed diff preview before recording an application decision.'
    return
  }
  applicationBusy.value = true
  try {
    applicationResult.value = await api.decideConfigChangeApplication({
      preview_id: previewId,
      application_decision: applicationDecision.value,
      operator_note: applicationNote.value,
      verify_config: true
    })
    applicationNote.value = ''
    await refreshApplications()
  } catch (error) {
    applicationError.value = error instanceof Error ? error.message : String(error)
  } finally {
    applicationBusy.value = false
  }
}
</script>

<template>
  <section class="rounded-[2rem] bg-ink p-8 text-paper shadow-soft">
    <p class="text-sm font-semibold uppercase tracking-[0.35em] text-paper/55">Research-only</p>
    <div class="mt-4 flex flex-wrap items-end justify-between gap-4">
      <div>
        <h1 class="max-w-4xl text-5xl font-black tracking-tight">Signal Quality Review</h1>
        <p class="mt-5 max-w-3xl text-lg leading-8 text-paper/70">
          Compare technical-only outcomes against event-policy, bhavcopy, and company-memory overlays. This page is evidence for manual rule changes, not live execution authority.
        </p>
      </div>
      <button class="rounded-full bg-paper px-5 py-3 text-sm font-black text-ink disabled:opacity-50" type="button" :disabled="pending" @click="refresh()">
        {{ pending ? 'Refreshing...' : 'Refresh' }}
      </button>
    </div>
  </section>

  <ApiErrorBanner v-if="error" class="mt-6" :error="error" title="Signal quality API failed" />
  <p v-if="reviewError" class="mt-6 rounded-2xl bg-rust/15 p-4 text-sm font-bold text-rust">{{ reviewError }}</p>
  <p v-if="decisionError" class="mt-6 rounded-2xl bg-rust/15 p-4 text-sm font-bold text-rust">{{ decisionError }}</p>
  <p v-if="configPreviewError" class="mt-6 rounded-2xl bg-rust/15 p-4 text-sm font-bold text-rust">{{ configPreviewError }}</p>
  <p v-if="applicationError" class="mt-6 rounded-2xl bg-rust/15 p-4 text-sm font-bold text-rust">{{ applicationError }}</p>

  <section class="mt-6 grid gap-4 md:grid-cols-4">
    <MetricTile label="Status" :value="data?.status || 'unknown'" note="API/table availability" />
    <MetricTile label="Latest Run" :value="dateText(data?.latest_evaluated_at)" note="Evaluation timestamp" />
    <MetricTile label="Horizons" :value="String(horizons.length)" note="Forward-return windows" />
    <MetricTile label="Examples" :value="String(exampleRows.length)" note="Selected matured examples" />
  </section>

  <section class="mt-6 glass-panel rounded-3xl p-6">
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div>
        <p class="text-xs font-black uppercase tracking-[0.28em] text-ink/45">Promotion safety gate</p>
        <h2 class="mt-2 text-2xl font-black">{{ trustGate.trust_level || 'unknown' }}</h2>
        <p class="mt-2 max-w-4xl text-sm leading-6 text-ink/60">
          {{ trustGate.recommendation || 'Fetch Health to see whether recommendations are usable, review-only, or blocked.' }}
        </p>
      </div>
      <p class="rounded-full px-4 py-2 text-sm font-black" :class="trustGate.status === 'ok' ? 'bg-moss/10 text-moss' : trustGate.status === 'error' ? 'bg-rust/10 text-rust' : 'bg-sun/20 text-ink'">
        {{ trustGate.status || 'unknown' }}
      </p>
    </div>
    <div v-if="trustChecks.length" class="mt-4 grid gap-3 md:grid-cols-3">
      <article v-for="check in trustChecks" :key="String(check.key)" class="rounded-2xl bg-white/75 p-4 text-sm">
        <p class="font-black">{{ check.label || check.key }}</p>
        <p class="mt-1 text-ink/60">{{ check.message || check.status || '-' }}</p>
      </article>
    </div>
    <p class="mt-4 rounded-2xl bg-paper/80 p-4 text-sm font-semibold text-ink/65">
      Review and approval here are audit records only. They do not edit `config/advisory_setups.yaml`, action rules, or broker execution.
    </p>
  </section>

  <section v-if="data?.status === 'missing_table' || data?.status === 'empty'" class="mt-6 rounded-3xl bg-sun/20 p-6 text-ink">
    <h2 class="text-xl font-black">No signal-quality run found</h2>
    <p class="mt-2 text-sm leading-6 text-ink/70">
      Run `python -m advisory.signal_quality_evaluator --from-date YYYY-MM-DD --to-date YYYY-MM-DD --horizons 5 10 20` after advisory candidates and Dhan OHLCV are available.
    </p>
  </section>

  <section class="mt-6 rounded-3xl border border-black/10 bg-white/70 p-5">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <p class="text-xs font-black uppercase tracking-[0.24em] text-ink/45">Recent application decisions</p>
        <p class="mt-2 text-sm leading-6 text-ink/60">
          Audit trail for reviewed config previews. These rows confirm Stockey did not apply config, policy, portfolio, or broker changes.
        </p>
      </div>
      <button class="rounded-full bg-white px-4 py-2 text-xs font-black text-ink shadow-sm" type="button" @click="refreshApplications()">
        Refresh applications
      </button>
    </div>
    <div v-if="applicationRows.length" class="mt-4 grid gap-3 md:grid-cols-2">
      <article v-for="row in applicationRows.slice(0, 6)" :key="String(row.application_id)" class="rounded-2xl bg-paper/80 p-4">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.18em] text-ink/45">{{ variantLabel(row.application_decision) }}</p>
            <p class="mt-1 text-sm font-black text-ink">{{ row.preview_id || row.application_id }}</p>
          </div>
          <span class="rounded-full bg-ink px-3 py-1 text-xs font-black text-paper">{{ variantLabel(row.verification_status) }}</span>
        </div>
        <p class="mt-3 text-sm leading-6 text-ink/65">{{ asDict(row.decision_effect).next_step || row.note || 'Audit decision recorded.' }}</p>
        <div class="mt-3 grid gap-2 text-xs font-bold text-ink/60 md:grid-cols-2">
          <span class="rounded-xl bg-white px-3 py-2">Config mutated: {{ boolLabel(asDict(row.decision_effect).mutates_config) }}</span>
          <span class="rounded-xl bg-white px-3 py-2">Broker allowed: {{ boolLabel(asDict(row.operator_boundary).broker_execution_allowed) }}</span>
        </div>
      </article>
    </div>
    <p v-else class="mt-4 rounded-2xl bg-paper/80 p-4 text-sm text-ink/60">No config application audit rows yet.</p>
  </section>

  <section v-for="horizon in horizons" :key="horizon" class="mt-8 glass-panel rounded-3xl p-6">
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div>
        <p class="text-xs font-black uppercase tracking-[0.28em] text-ink/45">{{ horizon }} trading-day horizon</p>
        <h2 class="mt-2 text-3xl font-black">Overlay lift versus technical-only</h2>
        <p class="mt-2 max-w-3xl text-sm leading-6 text-ink/60">
          Best overlay: <b>{{ variantLabel(bestOverlayForHorizon(horizon)?.variant) }}</b>,
          lift: <b>{{ pct(bestOverlayForHorizon(horizon)?.lift_vs_technical_only) }}</b>.
          Require stable sample size before changing production rules.
        </p>
      </div>
      <div class="rounded-2xl bg-white/75 p-4 text-sm leading-6 text-ink/70">
        <p><b>Candidates:</b> {{ numberText(coverageForHorizon(horizon).candidate_rows, 0) }}</p>
        <p><b>Event rows:</b> {{ numberText(coverageForHorizon(horizon).event_policy_rows, 0) }}</p>
        <p><b>Bhavcopy rows:</b> {{ numberText(coverageForHorizon(horizon).bhavcopy_rows, 0) }}</p>
        <p><b>Company memory rows:</b> {{ numberText(coverageForHorizon(horizon).company_memory_rows, 0) }}</p>
      </div>
    </div>

    <div class="mt-6 grid gap-4 lg:grid-cols-5">
      <article
        v-for="row in rowsForHorizon(horizon)"
        :key="`${row.horizon_days}-${row.variant}`"
        class="rounded-3xl border p-5 shadow-soft"
        :class="variantClass(row)"
      >
        <p class="text-xs font-black uppercase tracking-[0.22em] opacity-65">{{ variantLabel(row.variant) }}</p>
        <p class="mt-3 text-3xl font-black">{{ pct(row.avg_forward_return_after_cost) }}</p>
        <p class="text-xs font-bold opacity-65">avg return after costs</p>
        <div class="mt-4 grid gap-2 text-sm">
          <p class="rounded-2xl bg-white/50 px-3 py-2"><b>Lift:</b> {{ pct(row.lift_vs_technical_only) }}</p>
          <p class="rounded-2xl bg-white/50 px-3 py-2"><b>Hit:</b> {{ pct(row.hit_rate_after_cost) }}</p>
          <p class="rounded-2xl bg-white/50 px-3 py-2"><b>Selected:</b> {{ numberText(row.selected_count, 0) }}</p>
          <p class="rounded-2xl bg-white/50 px-3 py-2"><b>Matured:</b> {{ numberText(row.matured_count, 0) }}</p>
        </div>
        <p class="mt-4 inline-flex rounded-full px-3 py-1 text-xs font-black" :class="recommendationClass(row)">
          {{ String(row.recommendation || 'monitor').replaceAll('_', ' ') }}
        </p>
        <button
          v-if="canReview(row)"
          class="mt-4 w-full rounded-full bg-ink px-4 py-2 text-xs font-black text-paper disabled:opacity-50"
          type="button"
          :disabled="reviewingKey === rowKey(row)"
          @click="requestPromotionReview(row)"
        >
          {{ reviewingKey === rowKey(row) ? 'Creating review...' : 'Create manual review' }}
        </button>
      </article>
    </div>

    <article v-if="reviewResult" class="mt-8 rounded-3xl bg-white/80 p-5">
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p class="text-xs font-black uppercase tracking-[0.24em] text-ink/45">Latest generated review</p>
          <h3 class="mt-1 text-2xl font-black">{{ reviewResult.llm_review?.recommendation || '-' }}</h3>
          <p class="mt-2 max-w-4xl text-sm leading-6 text-ink/60">{{ reviewResult.llm_review?.summary || '-' }}</p>
        </div>
        <button class="rounded-full bg-ink px-4 py-2 text-sm font-bold text-paper" type="button" @click="copyJson(reviewResult.pending_patch)">
          Copy patch JSON
        </button>
      </div>
      <div class="mt-4 grid gap-4 md:grid-cols-3">
        <div class="rounded-2xl bg-paper/80 p-4">
          <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/45">Reasons</p>
          <ul class="mt-3 space-y-2 text-sm text-ink/70">
            <li v-for="item in (reviewResult.llm_review?.reasons as string[] || [])" :key="item">{{ item }}</li>
          </ul>
        </div>
        <div class="rounded-2xl bg-paper/80 p-4">
          <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/45">Risks</p>
          <ul class="mt-3 space-y-2 text-sm text-ink/70">
            <li v-for="item in (reviewResult.llm_review?.promotion_risks as string[] || [])" :key="item">{{ item }}</li>
          </ul>
        </div>
        <div class="rounded-2xl bg-paper/80 p-4">
          <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/45">Manual checks</p>
          <ul class="mt-3 space-y-2 text-sm text-ink/70">
            <li v-for="item in (reviewResult.llm_review?.suggested_manual_checks as string[] || [])" :key="item">{{ item }}</li>
          </ul>
        </div>
      </div>
    </article>

    <section class="mt-8 rounded-3xl bg-white/80 p-5">
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p class="text-xs font-black uppercase tracking-[0.24em] text-ink/45">Manual overlay reviews</p>
          <h3 class="mt-1 text-2xl font-black">Review decisions</h3>
          <p class="mt-2 max-w-4xl text-sm leading-6 text-ink/60">
            Use approval only as a recorded operator decision. Apply any suggested rule manually in a separate reviewed change.
          </p>
        </div>
        <button class="rounded-full bg-paper px-4 py-2 text-sm font-black text-ink" type="button" @click="refreshReviews()">Refresh reviews</button>
      </div>
      <div class="mt-4">
        <label class="text-xs font-black uppercase tracking-[0.22em] text-ink/45" for="signal-quality-decision-reason">Decision reason</label>
        <textarea id="signal-quality-decision-reason" v-model="decisionReason" class="mt-2 min-h-24 w-full rounded-2xl border border-black/10 bg-paper px-4 py-3 text-sm text-ink outline-none focus:border-moss" placeholder="Why are you approving, rejecting, or asking for more data?"></textarea>
      </div>
      <p v-if="decisionResult" class="mt-4 rounded-2xl bg-moss/10 p-4 text-sm font-bold text-moss">
        Recorded {{ decisionResult.decision }}. Applied to production: {{ decisionResult.applied ? 'yes' : 'no' }}.
      </p>
      <article v-if="configPreviewResult" class="mt-4 rounded-2xl bg-ink p-4 text-paper">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.24em] text-paper/45">Reviewed config diff</p>
            <h4 class="mt-1 text-xl font-black">{{ configPreviewResult.config_path }}</h4>
            <p class="mt-2 text-sm text-paper/65">{{ configPreviewResult.rollback_note }}</p>
          </div>
          <button class="rounded-full bg-paper px-4 py-2 text-xs font-black text-ink" type="button" @click="copyDiff(configPreviewResult.unified_diff)">
            Copy Diff
          </button>
        </div>
        <pre class="mt-4 max-h-96 overflow-auto rounded-2xl bg-black/40 p-4 text-xs leading-5">{{ configPreviewResult.unified_diff }}</pre>
        <div class="mt-4 rounded-2xl border border-paper/15 bg-paper/10 p-4">
          <p class="text-xs font-black uppercase tracking-[0.24em] text-paper/45">Application audit</p>
          <p class="mt-2 text-sm leading-6 text-paper/70">
            Record whether this reviewed overlay diff was manually applied, rejected, or still needs data. This writes only audit state and never changes config, policy, portfolio, or broker orders.
          </p>
          <div class="mt-4 grid gap-3 md:grid-cols-[220px_1fr_auto]">
            <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-paper/45">
              Decision
              <select v-model="applicationDecision" class="rounded-xl border border-paper/20 bg-ink px-3 py-2 text-sm normal-case tracking-normal text-paper outline-none focus:border-sun">
                <option value="approved_to_apply">Approved to apply manually</option>
                <option value="marked_applied">Marked applied manually</option>
                <option value="needs_more_data">Needs more data</option>
                <option value="rejected">Rejected</option>
              </select>
            </label>
            <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-paper/45">
              Operator note
              <input v-model="applicationNote" class="rounded-xl border border-paper/20 bg-ink px-3 py-2 text-sm normal-case tracking-normal text-paper outline-none focus:border-sun" placeholder="What did you verify or decide?" />
            </label>
            <button class="self-end rounded-full bg-sun px-4 py-2 text-xs font-black text-ink disabled:opacity-50" type="button" :disabled="applicationBusy" @click="recordConfigApplication">
              {{ applicationBusy ? 'Recording...' : 'Record Audit' }}
            </button>
          </div>
        </div>
        <div v-if="applicationResult" class="mt-4 rounded-2xl border border-moss/30 bg-moss/10 p-4">
          <p class="text-xs font-black uppercase tracking-[0.24em] text-moss">Saved application decision</p>
          <p class="mt-2 text-sm font-bold text-paper">{{ variantLabel(asDict(applicationResult.decision_effect).state) }}</p>
          <p class="mt-2 text-sm leading-6 text-paper/70">{{ asDict(applicationResult.decision_effect).next_step || applicationResult.note }}</p>
          <div class="mt-3 grid gap-2 text-xs font-bold text-paper/70 md:grid-cols-4">
            <span class="rounded-xl bg-black/20 px-3 py-2">Config mutated: {{ boolLabel(asDict(applicationResult.decision_effect).mutates_config) }}</span>
            <span class="rounded-xl bg-black/20 px-3 py-2">Policy mutated: {{ boolLabel(asDict(applicationResult.decision_effect).mutates_policy) }}</span>
            <span class="rounded-xl bg-black/20 px-3 py-2">Portfolio mutated: {{ boolLabel(asDict(applicationResult.decision_effect).mutates_portfolio) }}</span>
            <span class="rounded-xl bg-black/20 px-3 py-2">Broker allowed: {{ boolLabel(asDict(applicationResult.operator_boundary).broker_execution_allowed) }}</span>
          </div>
        </div>
      </article>
      <div v-if="reviewRows.length" class="mt-5 grid gap-4">
        <article v-for="review in reviewRows" :key="reviewKey(review)" class="rounded-2xl border border-black/10 bg-paper/80 p-4">
          <div class="flex flex-wrap items-start justify-between gap-4">
            <div>
              <p class="text-xs font-black uppercase tracking-[0.22em] text-ink/45">{{ variantLabel(review.variant) }}</p>
              <h4 class="mt-1 text-xl font-black">{{ review.recommendation || '-' }}</h4>
              <p class="mt-1 text-sm text-ink/60">
                Horizon {{ review.horizon_days }} days, reviewed {{ dateText(review.reviewed_at) }}.
                Manual decision: <b>{{ review.manual_decision || 'pending' }}</b>
              </p>
            </div>
            <div class="flex flex-wrap gap-2">
              <button class="rounded-full bg-moss px-3 py-2 text-xs font-black text-paper disabled:opacity-50" type="button" :disabled="decisionReviewKey === reviewKey(review)" @click="recordDecision(review, 'approved')">Approve</button>
              <button class="rounded-full bg-rust px-3 py-2 text-xs font-black text-paper disabled:opacity-50" type="button" :disabled="decisionReviewKey === reviewKey(review)" @click="recordDecision(review, 'rejected')">Reject</button>
              <button class="rounded-full bg-sun px-3 py-2 text-xs font-black text-ink disabled:opacity-50" type="button" :disabled="decisionReviewKey === reviewKey(review)" @click="recordDecision(review, 'needs_more_data')">Needs data</button>
              <button class="rounded-full bg-ink px-3 py-2 text-xs font-black text-paper" type="button" @click="copyPatchText(review)">Copy patch</button>
              <button
                class="rounded-full bg-ink/80 px-3 py-2 text-xs font-black text-paper disabled:opacity-50"
                type="button"
                :disabled="review.manual_decision !== 'approved' || configPreviewKey === reviewKey(review)"
                @click="previewConfigChange(review)"
              >
                {{ configPreviewKey === reviewKey(review) ? 'Generating' : 'Reviewed Diff' }}
              </button>
            </div>
          </div>
          <details class="mt-4 rounded-2xl bg-white/70 p-3">
            <summary class="cursor-pointer text-sm font-black text-moss">Review evidence and patch guidance</summary>
            <pre class="mt-3 max-h-72 overflow-auto rounded-2xl bg-ink p-4 text-xs leading-5 text-paper">{{ JSON.stringify({ evidence: review.signal_quality_evidence, llm_review: review.llm_review, patch: review.patch, final_patch: review.final_patch }, null, 2) }}</pre>
          </details>
        </article>
      </div>
      <p v-else class="mt-4 rounded-2xl bg-paper/80 p-4 text-sm text-ink/55">No manual overlay reviews yet.</p>
    </section>

    <div class="mt-8 space-y-5">
      <article v-for="row in rowsForHorizon(horizon)" :key="`examples-${row.horizon_days}-${row.variant}`" class="rounded-3xl bg-white/75 p-5">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.24em] text-ink/45">Examples</p>
            <h3 class="mt-1 text-xl font-black">{{ variantLabel(row.variant) }}</h3>
          </div>
          <p class="rounded-full bg-paper px-3 py-1 text-xs font-black text-ink/65">
            {{ examplesFor(row).length }} shown
          </p>
        </div>

        <div v-if="examplesFor(row).length" class="mt-4 grid gap-3 lg:grid-cols-2">
          <article v-for="example in examplesFor(row)" :key="`${example.variant}-${example.symbol}-${example.setup_id}`" class="rounded-2xl border border-black/10 bg-paper/80 p-4">
            <div class="flex flex-wrap items-start justify-between gap-3">
              <div>
                <SymbolLink v-if="example.symbol" :symbol="String(example.symbol)" class="text-lg font-black" />
                <p class="mt-1 text-xs font-bold uppercase tracking-[0.2em] text-ink/45">{{ example.setup_id || '-' }}</p>
              </div>
              <p class="rounded-full bg-moss/10 px-3 py-1 text-sm font-black text-moss">{{ pct(example.forward_return_after_cost) }}</p>
            </div>
            <div class="mt-3 grid gap-2 text-sm text-ink/70 md:grid-cols-2">
              <p><b>Signal date:</b> {{ dateText(example.asof_date) }}</p>
              <p><b>Exit date:</b> {{ dateText(example.exit_date) }}</p>
              <p><b>Score:</b> {{ numberText(example.technical_total_score) }}</p>
              <p><b>Entry/Exit:</b> {{ numberText(example.entry_close) }} -> {{ numberText(example.exit_close) }}</p>
            </div>
            <details class="mt-3 rounded-2xl bg-white/70 p-3">
              <summary class="cursor-pointer text-sm font-black text-moss">Evidence context</summary>
              <pre class="mt-3 max-h-64 overflow-auto rounded-2xl bg-ink p-4 text-xs leading-5 text-paper">{{ JSON.stringify(rawContext(example), null, 2) }}</pre>
            </details>
          </article>
        </div>
        <p v-else class="mt-4 rounded-2xl bg-paper/80 p-4 text-sm text-ink/55">
          No selected matured examples for this variant in the latest run.
        </p>
      </article>
    </div>
  </section>
</template>
