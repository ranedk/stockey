<script setup lang="ts">
import type { ConfigChangeApplicationResult, ConfigChangeApplicationsPayload, ConfigChangePreviewResult, Dict, TechnicalPromotionReviewResult } from '~/types/api'

const api = useOperatorApi()
const { data, pending, refresh, error: loadError } = await useAsyncData('technical-calibration', () => api.getTechnicalCalibration(12))
const { data: reviewsData, refresh: refreshReviews } = await useAsyncData('technical-promotion-reviews', () => api.getTechnicalPromotionReviews(25))
const { data: applicationsData, refresh: refreshApplications } = await useAsyncData<ConfigChangeApplicationsPayload>('config-change-applications-technical', () => api.getConfigChangeApplications(10))
const reviewSetupId = ref('EVENT_OPPORTUNITY_V1')
const reviewingConfigId = ref('')
const reviewError = ref('')
const reviewResult = ref<TechnicalPromotionReviewResult | null>(null)
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
const configRows = computed(() => data.value?.top_configs || [])
const reviewRows = computed(() => reviewsData.value?.reviews || [])
const applicationRows = computed(() => applicationsData.value?.applications || [])

function numberText(value: unknown, digits = 2) {
  const num = Number(value)
  if (Number.isNaN(num)) return '-'
  return Intl.NumberFormat('en-IN', { maximumFractionDigits: digits }).format(num)
}

function pct(value: unknown) {
  const num = Number(value)
  if (Number.isNaN(num)) return '-'
  return `${Math.round(num * 1000) / 10}%`
}

function configObject(row: Dict) {
  const raw = row.best_threshold_config_json || row.threshold_config_json
  if (!raw) return {}
  if (typeof raw === 'object') return raw as Dict
  try {
    return JSON.parse(String(raw)) as Dict
  } catch {
    return {}
  }
}

function configText(row: Dict) {
  const config = configObject(row)
  return JSON.stringify(config, null, 2)
}

function copyConfig(row: Dict) {
  if (!import.meta.client) return
  navigator.clipboard?.writeText(configText(row))
}

function copyJson(value: unknown) {
  if (!import.meta.client) return
  navigator.clipboard?.writeText(JSON.stringify(value || {}, null, 2))
}

function reviewKey(row: Dict) {
  return `${String(row.reviewed_at || '')}:${String(row.setup_id || '')}:${String(row.config_id || '')}`
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

function humanLabel(value: unknown) {
  return String(value || '-').replaceAll('_', ' ')
}

function boolLabel(value: unknown) {
  return value ? 'yes' : 'no'
}

function configsForHorizon(horizon: unknown) {
  return configRows.value.filter((row) => Number(row.horizon_days) === Number(horizon))
}

async function requestPromotionReview(configId: unknown) {
  const normalizedConfigId = String(configId || '').trim()
  reviewError.value = ''
  reviewResult.value = null
  if (!reviewSetupId.value.trim()) {
    reviewError.value = 'Enter a setup_id before requesting review.'
    return
  }
  if (!normalizedConfigId) {
    reviewError.value = 'No calibration config_id available for review.'
    return
  }
  reviewingConfigId.value = normalizedConfigId
  try {
    reviewResult.value = await api.reviewTechnicalCalibration({
      setup_id: reviewSetupId.value.trim(),
      config_id: normalizedConfigId,
      use_llm: true
    })
    await refreshReviews()
  } catch (error) {
    reviewError.value = error instanceof Error ? error.message : String(error)
  } finally {
    reviewingConfigId.value = ''
  }
}

async function recordDecision(row: Dict, decision: 'approved' | 'rejected' | 'needs_more_data') {
  const key = reviewKey(row)
  decisionReviewKey.value = key
  decisionError.value = ''
  decisionResult.value = null
  try {
    decisionResult.value = await api.decideTechnicalPromotionReview({
      reviewed_at: row.reviewed_at,
      setup_id: row.setup_id,
      config_id: row.config_id,
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
    configPreviewResult.value = await api.previewTechnicalThresholdConfigChange({
      reviewed_at: row.reviewed_at,
      setup_id: row.setup_id,
      config_id: row.config_id
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
        <h1 class="max-w-4xl text-5xl font-black tracking-tight">Technical Threshold Calibration</h1>
        <p class="mt-5 max-w-3xl text-lg leading-8 text-paper/70">
          Latest calibration run from realized forward returns after costs. These configs are evidence for manual review, not live threshold promotion.
        </p>
      </div>
      <button class="rounded-full bg-paper px-5 py-3 text-sm font-black text-ink" type="button" @click="refresh()">
        Refresh
      </button>
      <LinkButton to="/signal-quality">
        Open signal quality
      </LinkButton>
    </div>
  </section>

  <ApiErrorBanner v-if="loadError" :error="loadError" title="Could not load the calibration run" />
  <p v-if="pending" class="mt-6 text-sm text-ink/40">loading…</p>

  <section class="mt-6 glass-panel rounded-3xl p-6">
    <div class="flex flex-wrap items-end gap-4">
      <div class="min-w-[280px] flex-1">
        <label class="text-xs font-black uppercase tracking-[0.28em] text-ink/45" for="review-setup-id">Manual review setup</label>
        <input
          id="review-setup-id"
          v-model="reviewSetupId"
          class="mt-2 w-full rounded-2xl border border-black/10 bg-white px-4 py-3 text-sm font-bold text-ink outline-none focus:border-moss"
          placeholder="EVENT_OPPORTUNITY_V1"
        >
      </div>
      <p class="max-w-3xl text-sm leading-6 text-ink/60">
        Ask the LLM to review a candidate config against this setup. The output is advisory only: it writes a review record and a pending patch payload, but it does not edit live setup thresholds.
      </p>
    </div>
    <p v-if="reviewError" class="mt-4 rounded-2xl bg-rust/15 p-4 text-sm font-bold text-rust">{{ reviewError }}</p>
  </section>

  <section class="mt-6 grid gap-4 md:grid-cols-4">
    <MetricTile label="Status" :value="data?.status || 'unknown'" note="API/table availability" />
    <MetricTile label="Horizons" :value="String(summaryRows.length)" note="Matured return windows" />
    <MetricTile label="Top Configs" :value="String(configRows.length)" note="Ranked threshold rows" />
    <MetricTile label="Generated" :value="String(data?.generated_at || '-')" note="API read time" />
  </section>

  <section class="mt-8 space-y-6">
    <article v-for="row in summaryRows" :key="String(row.horizon_days)" class="glass-panel rounded-3xl p-6">
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p class="text-xs font-black uppercase tracking-[0.28em] text-ink/45">Horizon {{ row.horizon_days }} trading days</p>
          <h2 class="mt-2 text-2xl font-black">{{ row.best_config_id || 'No eligible config yet' }}</h2>
          <p class="mt-2 max-w-3xl text-sm leading-6 text-ink/60">
            Recommendation: <b>{{ row.recommendation || '-' }}</b>. Promote only after checking sample size, objective stability, and whether the config makes investing sense.
          </p>
        </div>
        <button class="rounded-full bg-ink px-4 py-2 text-sm font-bold text-paper" type="button" @click="copyConfig(row)">
          Copy Best Config
        </button>
        <button
          class="rounded-full bg-moss px-4 py-2 text-sm font-bold text-paper disabled:opacity-50"
          type="button"
          :disabled="reviewingConfigId === String(row.best_config_id || '')"
          @click="requestPromotionReview(row.best_config_id)"
        >
          {{ reviewingConfigId === String(row.best_config_id || '') ? 'Reviewing...' : 'LLM Review' }}
        </button>
      </div>

      <div class="mt-5 grid gap-3 md:grid-cols-5">
        <MetricTile label="Eligible" :value="numberText(row.best_eligible_count, 0)" note="Rows selected by best config" />
        <MetricTile label="Hit Rate" :value="pct(row.best_hit_rate_after_cost)" note="After costs and target threshold" />
        <MetricTile label="Avg Return" :value="pct(row.best_avg_forward_return_after_cost)" note="After costs" />
        <MetricTile label="Objective" :value="numberText(row.best_objective_score, 4)" note="Ranking metric" />
        <MetricTile label="Baseline Avg" :value="pct(row.baseline_avg_forward_return_after_cost)" note="Broad eligible baseline" />
      </div>

      <div v-if="Number(row.best_eligible_count || 0) < 30" class="mt-5 rounded-2xl bg-sun/20 p-4 text-sm font-semibold text-ink/75">
        Low sample warning: fewer than 30 eligible rows. Treat this as exploratory only.
      </div>

      <details class="mt-5 rounded-2xl bg-white/70 p-4">
        <summary class="cursor-pointer text-sm font-black text-moss">Best threshold JSON</summary>
        <pre class="mt-3 overflow-auto rounded-2xl bg-ink p-4 text-xs leading-5 text-paper">{{ configText(row) }}</pre>
      </details>

      <div class="mt-5">
        <h3 class="text-sm font-black uppercase tracking-[0.24em] text-ink/45">Top configs for this horizon</h3>
        <div class="mt-3 overflow-x-auto rounded-2xl bg-white/70">
          <table class="w-full min-w-[900px] text-left text-sm">
            <thead class="bg-ink text-paper">
              <tr>
                <th class="px-4 py-3">Rank</th>
                <th class="px-4 py-3">Config</th>
                <th class="px-4 py-3">Eligible</th>
                <th class="px-4 py-3">Hit</th>
                <th class="px-4 py-3">Avg Ret</th>
                <th class="px-4 py-3">Spread</th>
                <th class="px-4 py-3">Objective</th>
                <th class="px-4 py-3">Review</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="cfg in configsForHorizon(row.horizon_days)" :key="String(cfg.config_id)" class="border-b border-black/5">
                <td class="px-4 py-3 font-bold">{{ cfg.rn || '-' }}</td>
                <td class="px-4 py-3 font-semibold">{{ cfg.config_id }}</td>
                <td class="px-4 py-3">{{ numberText(cfg.eligible_count, 0) }}</td>
                <td class="px-4 py-3">{{ pct(cfg.hit_rate_after_cost) }}</td>
                <td class="px-4 py-3">{{ pct(cfg.avg_forward_return_after_cost) }}</td>
                <td class="px-4 py-3">{{ pct(cfg.spread_vs_rejected) }}</td>
                <td class="px-4 py-3">{{ numberText(cfg.objective_score, 4) }}</td>
                <td class="px-4 py-3">
                  <button
                    class="rounded-full bg-ink px-3 py-2 text-xs font-black text-paper disabled:opacity-50"
                    type="button"
                    :disabled="reviewingConfigId === String(cfg.config_id || '')"
                    @click="requestPromotionReview(cfg.config_id)"
                  >
                    {{ reviewingConfigId === String(cfg.config_id || '') ? 'Running' : 'Ask LLM' }}
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </article>

    <article v-if="reviewResult" class="glass-panel rounded-3xl p-6">
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p class="text-xs font-black uppercase tracking-[0.28em] text-ink/45">LLM-assisted manual review</p>
          <h2 class="mt-2 text-2xl font-black">{{ reviewResult.llm_review?.recommendation || '-' }}</h2>
          <p class="mt-2 max-w-4xl text-sm leading-6 text-ink/60">{{ reviewResult.llm_review?.summary || '-' }}</p>
        </div>
        <button class="rounded-full bg-ink px-4 py-2 text-sm font-bold text-paper" type="button" @click="copyJson(reviewResult.pending_patch)">
          Copy Pending Patch
        </button>
      </div>

      <div class="mt-5 grid gap-3 md:grid-cols-4">
        <MetricTile label="Setup" :value="reviewResult.setup_id" note="Target setup" />
        <MetricTile label="Config" :value="reviewResult.config_id" note="Candidate threshold row" />
        <MetricTile label="Status" :value="reviewResult.review_status || '-'" note="LLM/codex status" />
        <MetricTile label="Confidence" :value="pct(reviewResult.llm_review?.confidence)" note="Reviewer confidence" />
      </div>

      <div class="mt-5 grid gap-4 lg:grid-cols-2">
        <div class="rounded-2xl bg-white/70 p-4">
          <h3 class="text-sm font-black uppercase tracking-[0.24em] text-ink/45">Reasons</h3>
          <ul class="mt-3 space-y-2 text-sm leading-6 text-ink/70">
            <li v-for="reason in (reviewResult.llm_review?.reasons as unknown[] || [])" :key="String(reason)">- {{ reason }}</li>
          </ul>
        </div>
        <div class="rounded-2xl bg-white/70 p-4">
          <h3 class="text-sm font-black uppercase tracking-[0.24em] text-ink/45">Overfit / manual checks</h3>
          <ul class="mt-3 space-y-2 text-sm leading-6 text-ink/70">
            <li v-for="risk in ([...(reviewResult.llm_review?.overfit_risks as unknown[] || []), ...(reviewResult.llm_review?.suggested_manual_checks as unknown[] || [])])" :key="String(risk)">- {{ risk }}</li>
          </ul>
        </div>
      </div>

      <details class="mt-5 rounded-2xl bg-white/70 p-4">
        <summary class="cursor-pointer text-sm font-black text-moss">Pending patch payload</summary>
        <pre class="mt-3 overflow-auto rounded-2xl bg-ink p-4 text-xs leading-5 text-paper">{{ JSON.stringify(reviewResult.pending_patch, null, 2) }}</pre>
      </details>
    </article>

    <article class="glass-panel rounded-3xl p-6">
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p class="text-xs font-black uppercase tracking-[0.28em] text-ink/45">Manual approval audit</p>
          <h2 class="mt-2 text-2xl font-black">Recent threshold reviews</h2>
          <p class="mt-2 max-w-4xl text-sm leading-6 text-ink/60">
            Decisions are audit records only. `Approved` means you accept the patch for manual editing; it does not edit `config/advisory_setups.yaml`.
          </p>
        </div>
        <button class="rounded-full bg-ink px-4 py-2 text-sm font-bold text-paper" type="button" @click="refreshReviews()">
          Refresh Reviews
        </button>
      </div>

      <div class="mt-5">
        <label class="text-xs font-black uppercase tracking-[0.28em] text-ink/45" for="decision-reason">Decision reason</label>
        <input
          id="decision-reason"
          v-model="decisionReason"
          class="mt-2 w-full rounded-2xl border border-black/10 bg-white px-4 py-3 text-sm font-semibold text-ink outline-none focus:border-moss"
          placeholder="Why are you approving/rejecting this review?"
        >
      </div>

      <p v-if="decisionError" class="mt-4 rounded-2xl bg-rust/15 p-4 text-sm font-bold text-rust">{{ decisionError }}</p>
      <p v-if="configPreviewError" class="mt-4 rounded-2xl bg-rust/15 p-4 text-sm font-bold text-rust">{{ configPreviewError }}</p>
      <p v-if="applicationError" class="mt-4 rounded-2xl bg-rust/15 p-4 text-sm font-bold text-rust">{{ applicationError }}</p>
      <p v-if="decisionResult" class="mt-4 rounded-2xl bg-moss/15 p-4 text-sm font-bold text-moss">
        Decision recorded: {{ decisionResult.decision }}. No config change was applied.
      </p>
      <article v-if="configPreviewResult" class="mt-4 rounded-2xl bg-ink p-4 text-paper">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.24em] text-paper/45">Reviewed config diff</p>
            <h3 class="mt-1 text-xl font-black">{{ configPreviewResult.config_path }}</h3>
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
            Record what happened after reviewing this diff. This API writes only an audit record; it does not edit config, promote policy, mutate portfolio rows, or submit broker orders.
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
          <p class="mt-2 text-sm font-bold text-paper">{{ humanLabel(asDict(applicationResult.decision_effect).state) }}</p>
          <p class="mt-2 text-sm leading-6 text-paper/70">{{ asDict(applicationResult.decision_effect).next_step || applicationResult.note }}</p>
          <div class="mt-3 grid gap-2 text-xs font-bold text-paper/70 md:grid-cols-4">
            <span class="rounded-xl bg-black/20 px-3 py-2">Config mutated: {{ boolLabel(asDict(applicationResult.decision_effect).mutates_config) }}</span>
            <span class="rounded-xl bg-black/20 px-3 py-2">Policy mutated: {{ boolLabel(asDict(applicationResult.decision_effect).mutates_policy) }}</span>
            <span class="rounded-xl bg-black/20 px-3 py-2">Portfolio mutated: {{ boolLabel(asDict(applicationResult.decision_effect).mutates_portfolio) }}</span>
            <span class="rounded-xl bg-black/20 px-3 py-2">Broker allowed: {{ boolLabel(asDict(applicationResult.operator_boundary).broker_execution_allowed) }}</span>
          </div>
        </div>
      </article>

      <section class="mt-4 rounded-2xl border border-black/10 bg-white/70 p-4">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.24em] text-ink/45">Recent application decisions</p>
            <p class="mt-2 text-sm leading-6 text-ink/60">
              Audit trail for reviewed config previews. These rows explain what happens next and confirm Stockey did not apply config or broker changes.
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
                <p class="text-xs font-black uppercase tracking-[0.18em] text-ink/45">{{ humanLabel(row.application_decision) }}</p>
                <p class="mt-1 text-sm font-black text-ink">{{ row.preview_id || row.application_id }}</p>
              </div>
              <span class="rounded-full bg-ink px-3 py-1 text-xs font-black text-paper">{{ humanLabel(row.verification_status) }}</span>
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

      <div class="mt-5 space-y-4">
        <div v-for="row in reviewRows" :key="reviewKey(row)" class="rounded-3xl bg-white/70 p-5">
          <div class="flex flex-wrap items-start justify-between gap-4">
            <div>
              <p class="text-xs font-black uppercase tracking-[0.22em] text-ink/45">{{ row.setup_id }} / {{ row.config_id }}</p>
              <h3 class="mt-2 text-xl font-black">{{ row.recommendation || '-' }} <span class="text-sm font-bold text-ink/45">confidence {{ pct(row.confidence) }}</span></h3>
              <p class="mt-2 text-sm leading-6 text-ink/60">
                Reviewed {{ row.reviewed_at || '-' }}. Manual decision:
                <b>{{ row.manual_decision || 'pending' }}</b>
                <span v-if="row.manual_decided_at"> at {{ row.manual_decided_at }}</span>
              </p>
              <p v-if="row.manual_decision_reason" class="mt-2 text-sm leading-6 text-ink/60">Reason: {{ row.manual_decision_reason }}</p>
            </div>
            <div class="flex flex-wrap gap-2">
              <button class="rounded-full bg-moss px-3 py-2 text-xs font-black text-paper disabled:opacity-50" type="button" :disabled="decisionReviewKey === reviewKey(row)" @click="recordDecision(row, 'approved')">
                Approve
              </button>
              <button class="rounded-full bg-sun px-3 py-2 text-xs font-black text-ink disabled:opacity-50" type="button" :disabled="decisionReviewKey === reviewKey(row)" @click="recordDecision(row, 'needs_more_data')">
                Needs Data
              </button>
              <button class="rounded-full bg-rust px-3 py-2 text-xs font-black text-paper disabled:opacity-50" type="button" :disabled="decisionReviewKey === reviewKey(row)" @click="recordDecision(row, 'rejected')">
                Reject
              </button>
              <button class="rounded-full bg-ink px-3 py-2 text-xs font-black text-paper" type="button" @click="copyPatchText(row)">
                Copy Patch
              </button>
              <button
                class="rounded-full bg-ink/80 px-3 py-2 text-xs font-black text-paper disabled:opacity-50"
                type="button"
                :disabled="row.manual_decision !== 'approved' || configPreviewKey === reviewKey(row)"
                @click="previewConfigChange(row)"
              >
                {{ configPreviewKey === reviewKey(row) ? 'Generating' : 'Reviewed Diff' }}
              </button>
            </div>
          </div>

          <details class="mt-4 rounded-2xl bg-ink p-4 text-paper">
            <summary class="cursor-pointer text-sm font-black">Patch guidance</summary>
            <pre class="mt-3 overflow-auto text-xs leading-5">{{ patchText(row) || JSON.stringify(row.patch || {}, null, 2) }}</pre>
          </details>
        </div>

        <p v-if="!reviewRows.length" class="rounded-2xl bg-white/70 p-4 text-sm text-ink/60">
          No promotion reviews have been recorded yet. Run LLM Review on a candidate config first.
        </p>
      </div>
    </article>

    <p v-if="!summaryRows.length" class="glass-panel rounded-3xl p-6 text-ink/60">
      No calibration summary rows found. Run `python -m advisory.technical_threshold_calibration --from-date YYYY-MM-DD --to-date YYYY-MM-DD --horizons 5 10 20`.
    </p>
  </section>
</template>
