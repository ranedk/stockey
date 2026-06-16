<script setup lang="ts">
import type { ConfigChangeApplicationResult, ConfigChangeApplicationsPayload, Dict } from '~/types/api'
import type { TraceSummary } from '~/types/api'

const api = useOperatorApi()
const eventLimit = ref(50)
const eventOffset = ref(0)
const eventSymbol = ref('')
const eventStatus = ref('all')
const eventSearch = ref('')
const { data, refresh: refreshEvents, error: eventsError } = await useAsyncData(
  'events',
  () => api.getEvents(eventLimit.value, {
    offset: eventOffset.value,
    symbol: eventSymbol.value.trim().toUpperCase(),
    status: eventStatus.value,
    search: eventSearch.value.trim(),
    compact: true
  }),
  { watch: [eventLimit, eventOffset, eventStatus] }
)
const selectedActionType = ref('ALL')
const { data: policyData, refresh: refreshPolicy } = await useAsyncData('event-policy', () => api.getEventPolicy(100, selectedActionType.value), {
  watch: [selectedActionType]
})
const { data: policyEvalData } = await useAsyncData('event-policy-evaluation', () => api.getEventPolicyEvaluation(80))
const { data: promotionData, refresh: refreshPromotionReviews } = await useAsyncData('event-policy-promotion-reviews', () => api.getEventPolicyPromotionReviews(25))
const { data: applicationsData, refresh: refreshApplications } = await useAsyncData<ConfigChangeApplicationsPayload>('config-change-applications-event-policy', () => api.getConfigChangeApplications(10))
const events = computed(() => data.value?.events || [])
const eventMeta = computed(() => asDict(data.value?.pagination?.events || data.value?.meta?.events))
const snapshotMeta = computed(() => asDict(data.value?.snapshot))
const snapshotWarning = computed(() => asDict(data.value?.snapshot_warning))
const policyRows = computed(() => policyData.value?.rows || [])
const policyEvalRows = computed(() => policyEvalData.value?.summary || [])
const actionabilityEvalGroupTypes = new Set([
  'source_quality',
  'source_family',
  'source_authority',
  'source_confirmation_required',
  'market_scope'
])
const actionabilityEvalRows = computed(() => policyEvalRows.value.filter((row: Dict) => actionabilityEvalGroupTypes.has(String(row.group_type || ''))))
const promotionReviews = computed<Dict[]>(() => (promotionData.value?.reviews || []) as Dict[])
const applicationRows = computed(() => applicationsData.value?.applications || [])
const policySummary = computed(() => policyData.value?.summary || {})
const policyCompact = computed(() => Boolean(policySummary.value.compact))
const actionCounts = computed(() => policySummary.value.action_counts as Record<string, number> || {})
const policyClassCounts = computed(() => policySummary.value.policy_class_counts as Record<string, number> || {})
const traces = reactive<Record<string, TraceSummary>>({})
const loadingTrace = reactive<Record<string, boolean>>({})
const promotionBusy = reactive<Record<string, boolean>>({})
const previewBusy = reactive<Record<string, boolean>>({})
const promotionMessage = ref('')
const previewMessage = ref('')
const previewDiffs = reactive<Record<string, Dict>>({})
const applicationDecision = ref<'approved_to_apply' | 'marked_applied' | 'rejected' | 'needs_more_data'>('approved_to_apply')
const applicationNote = ref('')
const applicationBusy = reactive<Record<string, boolean>>({})
const applicationResult = reactive<Record<string, ConfigChangeApplicationResult>>({})
const applicationMessage = ref('')
const promotionOperatorId = ref('operator')
const promotionDecisionReason = ref('')
const actionTypes = ['ALL', 'MANUAL_REVIEW', 'BUY_WATCH', 'REDUCE_EXPOSURE_REVIEW', 'NO_ACTION']

function asList(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function asDict(value: unknown): Dict {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Dict : {}
}

function notes(row: Dict): Dict {
  return typeof row.operator_notes === 'object' && row.operator_notes ? row.operator_notes as Dict : {}
}

function checks(row: Dict): unknown[] {
  return asList(row.checks)
}

function actionability(row: Dict): Dict {
  return asDict(row.actionability)
}

function marketScope(row: Dict): Dict {
  return asDict(actionability(row).market_scope)
}

function tone(actionType: unknown) {
  const value = String(actionType || '').toUpperCase()
  if (value === 'REDUCE_EXPOSURE_REVIEW') return 'bg-rust text-paper'
  if (value === 'BUY_WATCH') return 'bg-moss text-paper'
  if (value === 'MANUAL_REVIEW') return 'bg-sun text-ink'
  return 'bg-white text-ink'
}

function pct(value: unknown) {
  const num = Number(value)
  if (Number.isNaN(num)) return '-'
  return `${Math.round(num * 1000) / 10}%`
}

function numberText(value: unknown) {
  const num = Number(value)
  if (Number.isNaN(num)) return String(value || '-')
  return Intl.NumberFormat('en-IN', { maximumFractionDigits: 3 }).format(num)
}

function signedPct(value: unknown) {
  const num = Number(value)
  if (Number.isNaN(num)) return '-'
  const formatted = `${Math.round(num * 1000) / 10}%`
  return num > 0 ? `+${formatted}` : formatted
}

function humanLabel(value: unknown) {
  return String(value || '-').replaceAll('_', ' ').toUpperCase()
}

function titleLabel(value: unknown) {
  const text = String(value || '').replaceAll('_', ' ')
  return text ? text.replace(/\b\w/g, (char) => char.toUpperCase()) : '-'
}

function boolLabel(value: unknown) {
  return value ? 'yes' : 'no'
}

function promotionKey(row: Dict) {
  return [row.evaluated_at, row.horizon_days, row.group_type, row.group_value, row.reviewed_at].map(value => String(value || '')).join('|')
}

async function createPromotionReview(row: Dict) {
  const key = promotionKey(row)
  promotionBusy[key] = true
  promotionMessage.value = ''
  try {
    await api.reviewEventPolicyGroup({
      evaluated_at: row.evaluated_at,
      horizon_days: row.horizon_days,
      group_type: row.group_type,
      group_value: row.group_value
    })
    promotionMessage.value = `Created event-policy promotion review for ${humanLabel(row.group_type)} = ${humanLabel(row.group_value)}.`
    await refreshPromotionReviews()
  } catch (error) {
    promotionMessage.value = `Promotion review failed: ${error instanceof Error ? error.message : String(error)}`
  } finally {
    promotionBusy[key] = false
  }
}

async function decidePromotionReview(row: Dict, decision: string) {
  const key = promotionKey(row)
  promotionBusy[key] = true
  promotionMessage.value = ''
  try {
    await api.decideEventPolicyPromotionReview({
      reviewed_at: row.reviewed_at,
      evaluated_at: row.evaluated_at,
      horizon_days: row.horizon_days,
      group_type: row.group_type,
      group_value: row.group_value,
      decision,
      operator_id: promotionOperatorId.value,
      decision_reason: promotionDecisionReason.value
    })
    promotionMessage.value = `Recorded ${humanLabel(decision)} for ${humanLabel(row.group_type)} = ${humanLabel(row.group_value)}. No config or broker behavior changed.`
    await refreshPromotionReviews()
  } catch (error) {
    promotionMessage.value = `Promotion decision failed: ${error instanceof Error ? error.message : String(error)}`
  } finally {
    promotionBusy[key] = false
  }
}

async function previewEventPolicyConfigChange(row: Dict) {
  const key = promotionKey(row)
  previewBusy[key] = true
  previewMessage.value = ''
  try {
    const result = await api.previewEventPolicyConfigChange({
      reviewed_at: row.reviewed_at,
      evaluated_at: row.evaluated_at,
      horizon_days: row.horizon_days,
      group_type: row.group_type,
      group_value: row.group_value,
      persist: true
    })
    previewDiffs[key] = result as unknown as Dict
    previewMessage.value = `Generated reviewed diff preview for ${humanLabel(row.group_type)} = ${humanLabel(row.group_value)}. No config file was changed.`
  } catch (error) {
    previewMessage.value = `Reviewed diff preview failed: ${error instanceof Error ? error.message : String(error)}`
  } finally {
    previewBusy[key] = false
  }
}

async function recordEventPolicyConfigApplication(row: Dict) {
  const key = promotionKey(row)
  const previewId = String(previewDiffs[key]?.preview_id || '').trim()
  applicationMessage.value = ''
  if (!previewId) {
    applicationMessage.value = 'Generate and persist a reviewed diff preview before recording an application decision.'
    return
  }
  applicationBusy[key] = true
  try {
    applicationResult[key] = await api.decideConfigChangeApplication({
      preview_id: previewId,
      application_decision: applicationDecision.value,
      operator_note: applicationNote.value,
      verify_config: true
    })
    applicationNote.value = ''
    applicationMessage.value = 'Recorded event-policy config application audit. No config, policy, portfolio, or broker state was changed.'
    await refreshApplications()
  } catch (error) {
    applicationMessage.value = `Application audit failed: ${error instanceof Error ? error.message : String(error)}`
  } finally {
    applicationBusy[key] = false
  }
}

async function loadTrace(row: Record<string, unknown>) {
  const uniqueId = String(row.unique_id || '')
  if (!uniqueId || traces[uniqueId] || loadingTrace[uniqueId]) return
  loadingTrace[uniqueId] = true
  try {
    traces[uniqueId] = await api.getEventTraceSummary(uniqueId)
  } finally {
    loadingTrace[uniqueId] = false
  }
}

async function applyEventFilters() {
  eventOffset.value = 0
  await refreshEvents()
}

async function loadNextEvents() {
  const next = Number(eventMeta.value.next_offset)
  if (!Number.isFinite(next)) return
  eventOffset.value = next
}

function eventDetailPath(row: Record<string, unknown>) {
  const uniqueId = String(row.unique_id || '')
  return uniqueId ? `/api/events/${encodeURIComponent(uniqueId)}/detail` : ''
}
</script>

<template>
  <section>
    <p class="text-sm font-semibold uppercase tracking-[0.3em] text-ink/45">Event Inbox</p>
    <h1 class="mt-3 text-4xl font-black">Event policy decisions</h1>
    <p class="mt-3 max-w-3xl text-ink/60">
      Structured news and announcements are mapped into bounded event-policy actions. Manual review rows include operator notes, wait-for events, and questions; low-value rows are downgraded to no action.
    </p>
  </section>

  <SnapshotWarning class="mt-4" :snapshot="snapshotMeta" :warning="snapshotWarning" :generated-at="data?.generated_at" />

  <section class="mt-8 grid gap-4 md:grid-cols-4">
    <MetricTile label="Manual Review" :value="String(actionCounts.MANUAL_REVIEW || 0)" note="Needs operator judgement" />
    <MetricTile label="Buy Watch" :value="String(actionCounts.BUY_WATCH || 0)" note="Positive evidence only" />
    <MetricTile label="Reduce Review" :value="String(actionCounts.REDUCE_EXPOSURE_REVIEW || 0)" note="Risk overlay" />
    <MetricTile label="No Action" :value="String(actionCounts.NO_ACTION || 0)" note="Ignored unless follow-up appears" />
  </section>

  <section class="mt-6 glass-panel rounded-3xl p-5">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Policy filters</p>
        <p class="mt-1 text-sm text-ink/60">Generated {{ policyData?.generated_at || '-' }}</p>
      </div>
      <button class="rounded-full bg-ink px-4 py-2 text-sm font-bold text-paper" type="button" @click="refreshPolicy()">
        Refresh
      </button>
    </div>
    <div class="mt-4 flex flex-wrap gap-2">
      <button
        v-for="item in actionTypes"
        :key="item"
        class="rounded-full px-4 py-2 text-sm font-black"
        :class="selectedActionType === item ? 'bg-ink text-paper' : 'bg-white text-ink'"
        type="button"
        @click="selectedActionType = item"
      >
        {{ item }}
      </button>
    </div>
    <details class="mt-4">
      <summary class="cursor-pointer text-sm font-black text-moss">Policy class counts</summary>
      <div class="mt-3 flex flex-wrap gap-2">
        <span v-for="(count, key) in policyClassCounts" :key="key" class="rounded-full bg-white px-3 py-1 text-xs font-bold text-ink/70">
          {{ key }}: {{ count }}
        </span>
      </div>
    </details>
    <p v-if="policyCompact" class="mt-4 rounded-2xl border border-sun/30 bg-sun/10 p-3 text-sm font-semibold leading-6 text-ink/65">
      Event-policy rows use compact payloads by default. Parsed checks, operator notes, and LLM review are shown; bulky raw JSON source columns are omitted from this list response.
    </p>
  </section>

  <section class="mt-6 glass-panel rounded-3xl p-5">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Policy evaluation</p>
        <h2 class="mt-2 text-2xl font-black">Realized forward-return evidence</h2>
        <p class="mt-2 max-w-4xl text-sm leading-6 text-ink/60">
          Research-only metrics from matured event-policy rows joined to future Dhan daily closes. Use this to tighten or monitor policy classes; it does not auto-change live thresholds.
        </p>
      </div>
      <span class="rounded-full bg-white px-3 py-1 text-xs font-black text-ink/60">
        {{ policyEvalData?.status || 'missing_table' }}
      </span>
    </div>
    <div v-if="policyEvalRows.length" class="mt-5 grid gap-3 lg:grid-cols-2">
      <article v-for="(row, idx) in policyEvalRows.slice(0, 8)" :key="idx" class="rounded-2xl bg-white/70 p-4">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/45">{{ row.group_type }} · {{ row.horizon_days }}d</p>
            <p class="mt-1 text-lg font-black text-ink">{{ row.group_value }}</p>
          </div>
          <span class="rounded-full bg-ink px-3 py-1 text-xs font-bold text-paper">{{ row.recommendation || 'monitor' }}</span>
        </div>
        <div class="mt-3 grid gap-2 text-sm md:grid-cols-4">
          <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Matured:</b> {{ row.matured_count || 0 }}</p>
          <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Avg:</b> {{ pct(row.avg_forward_return_after_cost) }}</p>
          <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Hit:</b> {{ pct(row.hit_rate_after_cost) }}</p>
          <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Score:</b> {{ numberText(row.avg_policy_score) }}</p>
        </div>
      </article>
    </div>
    <div v-if="actionabilityEvalRows.length" class="mt-5 rounded-3xl border border-moss/20 bg-moss/5 p-4">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p class="text-xs font-black uppercase tracking-[0.24em] text-moss">Actionability calibration</p>
          <p class="mt-2 text-sm leading-6 text-ink/65">
            These rows test whether source quality, source authority, confirmation requirements, and affected-market scope are helping or hurting realized outcomes.
          </p>
        </div>
        <span class="rounded-full bg-white px-3 py-1 text-xs font-black text-ink/55">{{ actionabilityEvalRows.length }} groups</span>
      </div>
      <div class="mt-4 grid gap-3 lg:grid-cols-2">
        <article v-for="(row, idx) in actionabilityEvalRows.slice(0, 8)" :key="`actionability-${idx}`" class="rounded-2xl bg-white/80 p-4">
          <div class="flex flex-wrap items-start justify-between gap-3">
            <div>
              <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/45">{{ humanLabel(row.group_type) }} · {{ row.horizon_days }}d</p>
              <p class="mt-1 text-lg font-black text-ink">{{ humanLabel(row.group_value) }}</p>
            </div>
            <span class="rounded-full px-3 py-1 text-xs font-black" :class="String(row.recommendation || '').includes('strengthen') ? 'bg-moss text-paper' : String(row.recommendation || '').includes('tighten') ? 'bg-rust text-paper' : 'bg-sun text-ink'">
              {{ humanLabel(row.recommendation || 'monitor') }}
            </span>
          </div>
          <div class="mt-3 grid gap-2 text-sm md:grid-cols-4">
            <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Matured:</b> {{ row.matured_count || 0 }}</p>
            <p class="rounded-xl bg-paper/70 px-3 py-2"><b>After cost:</b> {{ pct(row.avg_forward_return_after_cost) }}</p>
            <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Hit:</b> {{ pct(row.hit_rate_after_cost) }}</p>
            <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Confidence:</b> {{ pct(row.avg_confidence) }}</p>
          </div>
          <div class="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-black/5 bg-paper/70 p-3">
            <p class="text-xs font-bold leading-5 text-ink/55">
              Create a manual-only promotion review for this group. This does not change config, portfolio, or broker behavior.
            </p>
            <button
              class="rounded-full bg-ink px-4 py-2 text-xs font-black text-paper disabled:cursor-not-allowed disabled:bg-ink/30"
              type="button"
              :disabled="promotionBusy[promotionKey(row)] || !row.evaluated_at"
              @click="createPromotionReview(row)"
            >
              {{ promotionBusy[promotionKey(row)] ? 'Creating...' : 'Create Review' }}
            </button>
          </div>
        </article>
      </div>
    </div>
    <section v-if="promotionReviews.length || promotionMessage" class="mt-5 rounded-3xl border border-sun/30 bg-sun/10 p-4">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p class="text-xs font-black uppercase tracking-[0.24em] text-ink/45">Promotion reviews</p>
          <h3 class="mt-2 text-xl font-black text-ink">Manual policy-calibration queue</h3>
          <p class="mt-2 max-w-4xl text-sm leading-6 text-ink/65">
            These reviews turn realized event-policy evidence into operator notes. Decisions recorded here are audit notes only; they do not apply policy patches, portfolio changes, or broker orders.
          </p>
        </div>
        <span class="rounded-full bg-white px-3 py-1 text-xs font-black text-ink/60">Manual-only</span>
      </div>
      <p v-if="promotionMessage" class="mt-4 rounded-2xl bg-white/80 p-3 text-sm font-bold text-ink/70">{{ promotionMessage }}</p>
      <p v-if="previewMessage" class="mt-3 rounded-2xl bg-white/80 p-3 text-sm font-bold text-ink/70">{{ previewMessage }}</p>
      <p v-if="applicationMessage" class="mt-3 rounded-2xl bg-white/80 p-3 text-sm font-bold text-ink/70">{{ applicationMessage }}</p>
      <div class="mt-4 grid gap-3 md:grid-cols-2">
        <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
          Operator ID
          <input v-model="promotionOperatorId" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss" placeholder="operator" />
        </label>
        <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
          Decision reason
          <input v-model="promotionDecisionReason" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss" placeholder="Why this review was approved, rejected, or deferred" />
        </label>
      </div>
      <section class="mt-4 rounded-2xl border border-black/10 bg-white/70 p-4">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.24em] text-ink/45">Recent application decisions</p>
            <p class="mt-2 text-sm leading-6 text-ink/60">
              Audit trail for reviewed event-policy/config previews. These rows confirm Stockey did not apply config, policy, portfolio, or broker changes.
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
                <p class="text-xs font-black uppercase tracking-[0.18em] text-ink/45">{{ titleLabel(row.application_decision) }}</p>
                <p class="mt-1 text-sm font-black text-ink">{{ row.preview_id || row.application_id }}</p>
              </div>
              <span class="rounded-full bg-ink px-3 py-1 text-xs font-black text-paper">{{ titleLabel(row.verification_status) }}</span>
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
      <div v-if="promotionReviews.length" class="mt-4 grid gap-3 lg:grid-cols-2">
        <article v-for="(review, idx) in promotionReviews" :key="`${promotionKey(review)}-${idx}`" class="rounded-2xl bg-white/85 p-4">
          <div class="flex flex-wrap items-start justify-between gap-3">
            <div>
              <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/45">{{ humanLabel(review.group_type) }} · {{ review.horizon_days }}d</p>
              <p class="mt-1 text-lg font-black text-ink">{{ humanLabel(review.group_value) }}</p>
              <p class="mt-1 text-xs font-bold text-ink/45">Reviewed {{ review.reviewed_at || '-' }}</p>
            </div>
            <span class="rounded-full bg-ink px-3 py-1 text-xs font-black text-paper">{{ humanLabel(review.recommendation || 'review') }}</span>
          </div>
          <div class="mt-3 grid gap-2 text-sm md:grid-cols-3">
            <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Confidence:</b> {{ pct(review.confidence) }}</p>
            <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Patch:</b> {{ humanLabel(asDict(review.patch).mode || 'manual_review_only') }}</p>
            <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Decision:</b> {{ humanLabel(asDict(review.manual_decision).decision || 'pending') }}</p>
          </div>
          <details v-if="review.manual_patch_text" class="mt-3 rounded-2xl bg-ink p-3">
            <summary class="cursor-pointer text-xs font-black uppercase tracking-[0.18em] text-paper/60">Manual patch text</summary>
            <pre class="mt-3 overflow-auto text-xs leading-5 text-paper">{{ review.manual_patch_text }}</pre>
          </details>
          <div class="mt-4 flex flex-wrap gap-2">
            <button class="rounded-full bg-moss px-4 py-2 text-xs font-black text-paper disabled:bg-moss/30" type="button" :disabled="promotionBusy[promotionKey(review)]" @click="decidePromotionReview(review, 'approved')">
              Approve
            </button>
            <button class="rounded-full bg-sun px-4 py-2 text-xs font-black text-ink disabled:bg-sun/30" type="button" :disabled="promotionBusy[promotionKey(review)]" @click="decidePromotionReview(review, 'needs_more_data')">
              Needs More Data
            </button>
            <button class="rounded-full bg-rust px-4 py-2 text-xs font-black text-paper disabled:bg-rust/30" type="button" :disabled="promotionBusy[promotionKey(review)]" @click="decidePromotionReview(review, 'rejected')">
              Reject
            </button>
            <button
              v-if="String(review.manual_decision || '').toLowerCase() === 'approved'"
              class="rounded-full bg-ink px-4 py-2 text-xs font-black text-paper disabled:bg-ink/30"
              type="button"
              :disabled="previewBusy[promotionKey(review)]"
              @click="previewEventPolicyConfigChange(review)"
            >
              {{ previewBusy[promotionKey(review)] ? 'Generating Diff...' : 'Reviewed Diff' }}
            </button>
          </div>
          <details v-if="previewDiffs[promotionKey(review)]" class="mt-3 rounded-2xl border border-black/10 bg-paper/80 p-3" open>
            <summary class="cursor-pointer text-xs font-black uppercase tracking-[0.18em] text-ink/45">Reviewed diff preview</summary>
            <p class="mt-2 text-xs font-bold leading-5 text-ink/60">
              Preview only. This did not edit config, action rules, portfolio rows, or broker state.
            </p>
            <pre class="mt-3 max-h-96 overflow-auto rounded-2xl bg-ink p-4 text-xs leading-5 text-paper">{{ previewDiffs[promotionKey(review)].unified_diff || '-' }}</pre>
            <p class="mt-3 text-xs font-bold text-ink/55">{{ previewDiffs[promotionKey(review)].rollback_note || 'Rollback by not applying this preview.' }}</p>
            <div class="mt-4 rounded-2xl border border-black/10 bg-white/80 p-4">
              <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/45">Application audit</p>
              <p class="mt-2 text-sm leading-6 text-ink/65">
                Record whether this reviewed event-policy diff was manually applied, rejected, or needs more data. This writes only audit state.
              </p>
              <div class="mt-3 grid gap-3 md:grid-cols-[220px_1fr_auto]">
                <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
                  Decision
                  <select v-model="applicationDecision" class="rounded-xl border border-black/10 bg-paper px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss">
                    <option value="approved_to_apply">Approved to apply manually</option>
                    <option value="marked_applied">Marked applied manually</option>
                    <option value="needs_more_data">Needs more data</option>
                    <option value="rejected">Rejected</option>
                  </select>
                </label>
                <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
                  Operator note
                  <input v-model="applicationNote" class="rounded-xl border border-black/10 bg-paper px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss" placeholder="What did you verify or decide?" />
                </label>
                <button class="self-end rounded-full bg-sun px-4 py-2 text-xs font-black text-ink disabled:opacity-50" type="button" :disabled="applicationBusy[promotionKey(review)]" @click="recordEventPolicyConfigApplication(review)">
                  {{ applicationBusy[promotionKey(review)] ? 'Recording...' : 'Record Audit' }}
                </button>
              </div>
            </div>
            <div v-if="applicationResult[promotionKey(review)]" class="mt-3 rounded-2xl border border-moss/20 bg-moss/10 p-4">
              <p class="text-xs font-black uppercase tracking-[0.2em] text-moss">Saved application decision</p>
              <p class="mt-2 text-sm font-bold text-ink">{{ titleLabel(asDict(applicationResult[promotionKey(review)].decision_effect).state) }}</p>
              <p class="mt-2 text-sm leading-6 text-ink/65">{{ asDict(applicationResult[promotionKey(review)].decision_effect).next_step || applicationResult[promotionKey(review)].note }}</p>
              <div class="mt-3 grid gap-2 text-xs font-bold text-ink/60 md:grid-cols-4">
                <span class="rounded-xl bg-white px-3 py-2">Config mutated: {{ boolLabel(asDict(applicationResult[promotionKey(review)].decision_effect).mutates_config) }}</span>
                <span class="rounded-xl bg-white px-3 py-2">Policy mutated: {{ boolLabel(asDict(applicationResult[promotionKey(review)].decision_effect).mutates_policy) }}</span>
                <span class="rounded-xl bg-white px-3 py-2">Portfolio mutated: {{ boolLabel(asDict(applicationResult[promotionKey(review)].decision_effect).mutates_portfolio) }}</span>
                <span class="rounded-xl bg-white px-3 py-2">Broker allowed: {{ boolLabel(asDict(applicationResult[promotionKey(review)].operator_boundary).broker_execution_allowed) }}</span>
              </div>
            </div>
          </details>
        </article>
      </div>
    </section>
    <p v-if="!policyEvalRows.length" class="mt-4 rounded-2xl bg-white/70 p-4 text-sm text-ink/60">
      No event-policy evaluation summary yet. Run `python -m advisory.event_policy_evaluator --dry-run` first, then without `--dry-run` to persist research rows.
    </p>
  </section>

  <section class="mt-8 grid gap-4">
    <RecordCard
      v-for="(row, idx) in policyRows"
      :key="`${row.unique_id}-${row.setup_id}-${idx}`"
      :title="String(row.unique_id || 'Policy event')"
      :subtitle="String(row.action_reason || row.action_detail || '')"
      :record="row"
    >
      <template #badge>
        <div class="flex flex-wrap gap-2">
          <SymbolLink :symbol="row.symbol" subtle />
          <span class="rounded-full px-3 py-1 text-xs font-bold" :class="tone(row.action_type)">{{ row.action_type || 'POLICY' }}</span>
        </div>
      </template>

      <div class="mt-4 grid gap-3 md:grid-cols-4">
        <MetricTile label="Class" :value="String(row.policy_class || row.event_class || '-')" note="Policy class" />
        <MetricTile label="Status" :value="String(row.action_status || '-')" note="Policy status" />
        <MetricTile label="Score" :value="String(row.policy_score ?? '-')" note="Policy score" />
        <MetricTile label="LLM" :value="String(row.llm_review_status || '-')" note="Manual review refinement" />
      </div>

      <div v-if="Object.keys(actionability(row)).length" class="mt-4 rounded-2xl border border-moss/20 bg-white/75 p-4">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.24em] text-ink/45">Actionability context</p>
            <p class="mt-2 text-sm leading-6 text-ink/65">
              Use this to decide whether Manual Review can lead to action, a wait signal, or no action.
            </p>
          </div>
          <span class="rounded-full px-3 py-1 text-xs font-black" :class="String(actionability(row).review_priority) === 'high' ? 'bg-rust text-paper' : String(actionability(row).review_priority) === 'medium' ? 'bg-sun text-ink' : 'bg-moss text-paper'">
            {{ String(actionability(row).review_priority || 'unknown').replaceAll('_', ' ').toUpperCase() }}
          </span>
        </div>
        <div class="mt-4 grid gap-3 md:grid-cols-3 xl:grid-cols-7">
          <MetricTile label="Materiality" :value="String(actionability(row).materiality || '-').toUpperCase()" :note="`confidence ${pct(actionability(row).confidence)}`" />
          <MetricTile label="Freshness" :value="humanLabel(asDict(actionability(row).freshness).bucket)" :note="`${numberText(asDict(actionability(row).freshness).age_days)} day(s) old`" />
          <MetricTile label="Source" :value="humanLabel(asDict(actionability(row).source_quality).quality)" :note="humanLabel(asDict(actionability(row).source_quality).source_type)" />
          <MetricTile label="Scope" :value="humanLabel(marketScope(row).scope_type)" :note="`${numberText(marketScope(row).affected_sector_count)} sectors / ${numberText(marketScope(row).affected_peer_count)} peers`" />
          <MetricTile label="Exposure" :value="humanLabel(asDict(actionability(row).current_exposure).bucket)" note="Current holding context" />
          <MetricTile label="Reaction" :value="humanLabel(asDict(actionability(row).price_reaction).bucket)" :note="signedPct(asDict(actionability(row).price_reaction).value)" />
          <MetricTile label="Authority" :value="asDict(actionability(row).deterministic_boundary).broker_executable ? 'BROKER' : 'REVIEW ONLY'" :note="String(asDict(actionability(row).deterministic_boundary).final_action_authority || '-')" />
        </div>
        <div v-if="asList(marketScope(row).affected_sectors).length || asList(marketScope(row).affected_peers).length" class="mt-4 grid gap-3 md:grid-cols-2">
          <div v-if="asList(marketScope(row).affected_sectors).length" class="rounded-2xl bg-paper/70 p-3">
            <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/45">Affected sectors</p>
            <div class="mt-2 flex flex-wrap gap-2">
              <span v-for="sector in asList(marketScope(row).affected_sectors)" :key="String(sector)" class="rounded-full bg-moss/10 px-3 py-1 text-xs font-black text-moss">
                {{ sector }}
              </span>
            </div>
          </div>
          <div v-if="asList(marketScope(row).affected_peers).length" class="rounded-2xl bg-paper/70 p-3">
            <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/45">Affected peers</p>
            <div class="mt-2 flex flex-wrap gap-2">
              <span v-for="peer in asList(marketScope(row).affected_peers)" :key="String(peer)" class="rounded-full bg-sun/20 px-3 py-1 text-xs font-black text-ink">
                {{ peer }}
              </span>
            </div>
          </div>
        </div>
        <div v-if="asList(actionability(row).suggested_next_evidence).length" class="mt-4">
          <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/45">Suggested next evidence</p>
          <ul class="mt-2 grid gap-2 text-sm text-ink/70 md:grid-cols-2">
            <li v-for="item in asList(actionability(row).suggested_next_evidence)" :key="String(item)" class="rounded-xl bg-paper/70 px-3 py-2">- {{ item }}</li>
          </ul>
        </div>
      </div>

      <div v-if="Object.keys(notes(row)).length" class="mt-4 rounded-2xl bg-paper/70 p-4">
        <p class="text-xs font-black uppercase tracking-[0.24em] text-ink/45">Operator notes</p>
        <p class="mt-2 text-sm leading-6 text-ink/70">{{ notes(row).operator_summary || notes(row).rationale || '-' }}</p>
        <p v-if="notes(row).possible_action" class="mt-3 text-sm leading-6 text-ink/70"><b>Possible action:</b> {{ notes(row).possible_action }}</p>
        <p v-if="notes(row).downgrade_reason" class="mt-3 rounded-xl bg-white px-3 py-2 text-sm font-bold text-rust">
          Downgrade: {{ notes(row).downgrade_reason }}
        </p>
        <div class="mt-3 grid gap-3 md:grid-cols-2">
          <div v-if="asList(notes(row).wait_for_events).length">
            <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/45">Wait for</p>
            <ul class="mt-2 space-y-1 text-sm text-ink/70">
              <li v-for="item in asList(notes(row).wait_for_events)" :key="String(item)">- {{ item }}</li>
            </ul>
          </div>
          <div v-if="asList(notes(row).operator_questions).length">
            <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/45">Questions</p>
            <ul class="mt-2 space-y-1 text-sm text-ink/70">
              <li v-for="item in asList(notes(row).operator_questions)" :key="String(item)">- {{ item }}</li>
            </ul>
          </div>
        </div>
      </div>

      <details v-if="checks(row).length" class="mt-4 rounded-2xl bg-white/70 p-4">
        <summary class="cursor-pointer text-sm font-black text-moss">Policy checks</summary>
        <pre class="mt-3 overflow-auto rounded-2xl bg-ink p-4 text-xs leading-5 text-paper">{{ JSON.stringify(row.checks, null, 2) }}</pre>
      </details>

      <button
        class="mt-4 rounded-full bg-ink px-4 py-2 text-sm font-bold text-paper"
        type="button"
        @click="loadTrace(row)"
      >
        {{ loadingTrace[String(row.unique_id || '')] ? 'Loading trace...' : 'Load decision trace' }}
      </button>
      <NuxtLink
        v-if="row.symbol"
        class="ml-2 inline-flex rounded-full bg-white px-4 py-2 text-sm font-bold text-ink"
        :to="`/symbols/${encodeURIComponent(String(row.symbol || '').toUpperCase())}`"
      >
        Open symbol page
      </NuxtLink>
      <NuxtLink
        v-if="row.unique_id"
        class="ml-2 inline-flex rounded-full bg-white px-4 py-2 text-sm font-bold text-ink"
        :to="`/decision-trace?unique_id=${encodeURIComponent(String(row.unique_id || ''))}`"
      >
        Open trace page
      </NuxtLink>
      <TraceTimeline
        v-if="traces[String(row.unique_id || '')]"
        class="mt-4"
        :trace="traces[String(row.unique_id || '')]"
        title="Event trace"
      />
    </RecordCard>
    <p v-if="!policyRows.length" class="glass-panel rounded-3xl p-6 text-ink/60">No event-policy rows found. Run `python -m advisory.event_policy` or the advisory pipeline through the event_policy stage.</p>
  </section>

  <section class="mt-10">
    <p class="text-sm font-semibold uppercase tracking-[0.3em] text-ink/45">Raw Event Feed</p>
    <h2 class="mt-3 text-3xl font-black">Latest news and announcements</h2>
  </section>

  <ApiErrorBanner v-if="eventsError" class="mt-5" title="Raw event feed failed" :error="eventsError" />

  <section class="mt-5 glass-panel rounded-3xl p-5">
    <div class="grid gap-3 md:grid-cols-[0.8fr_0.7fr_1.4fr_auto]">
      <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
        Symbol
        <input v-model="eventSymbol" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss" placeholder="Optional" @keyup.enter="applyEventFilters" />
      </label>
      <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
        Status
        <select v-model="eventStatus" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss">
          <option value="all">All</option>
          <option value="error">Error</option>
          <option value="warn">Warn</option>
          <option value="parsed">Parsed</option>
          <option value="review">Review</option>
        </select>
      </label>
      <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
        Search
        <input v-model="eventSearch" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss" placeholder="subject, id, event class..." @keyup.enter="applyEventFilters" />
      </label>
      <button class="self-end rounded-full bg-ink px-5 py-2 text-sm font-black text-paper" type="button" @click="applyEventFilters">
        Apply
      </button>
    </div>
    <p class="mt-3 text-sm font-semibold text-ink/55">
      Showing {{ eventMeta.returned_count ?? eventMeta.returned ?? events.length }} of {{ eventMeta.total_count ?? eventMeta.total ?? events.length }} rows. Offset {{ eventMeta.offset || 0 }}.
    </p>
  </section>

  <section class="mt-5 grid gap-4">
    <RecordCard
      v-for="(row, idx) in events"
      :key="idx"
      :title="String(row.unique_id || 'Event')"
      :subtitle="String(row.subject || row.concise_summary_text || '')"
      :record="row"
      :detail-path="eventDetailPath(row)"
    >
      <template #badge>
        <div class="flex flex-wrap gap-2">
          <SymbolLink :symbol="row.symbol || row.ticker" subtle />
          <span class="rounded-full bg-ember px-3 py-1 text-xs font-bold text-white">{{ row.event_status || row.parse_status || 'EVENT' }}</span>
        </div>
      </template>
      <button
        class="mt-4 rounded-full bg-ink px-4 py-2 text-sm font-bold text-paper"
        type="button"
        @click="loadTrace(row)"
      >
        {{ loadingTrace[String(row.unique_id || '')] ? 'Loading trace...' : 'Load decision trace' }}
      </button>
      <NuxtLink
        v-if="row.symbol || row.ticker"
        class="ml-2 inline-flex rounded-full bg-white px-4 py-2 text-sm font-bold text-ink"
        :to="`/symbols/${encodeURIComponent(String(row.symbol || row.ticker || '').toUpperCase())}`"
      >
        Open symbol page
      </NuxtLink>
      <NuxtLink
        v-if="row.unique_id"
        class="ml-2 inline-flex rounded-full bg-white px-4 py-2 text-sm font-bold text-ink"
        :to="`/decision-trace?unique_id=${encodeURIComponent(String(row.unique_id || ''))}`"
      >
        Open trace page
      </NuxtLink>
      <TraceTimeline
        v-if="traces[String(row.unique_id || '')]"
        class="mt-4"
        :trace="traces[String(row.unique_id || '')]"
        title="Event trace"
      />
    </RecordCard>
    <p v-if="!events.length" class="glass-panel rounded-3xl p-6 text-ink/60">No event rows found.</p>
  </section>

  <div v-if="eventMeta.has_more" class="mt-5 flex justify-center">
    <button class="rounded-full bg-moss px-6 py-3 text-sm font-black text-paper" type="button" @click="loadNextEvents">
      Load next {{ eventLimit }} events
    </button>
  </div>
</template>
