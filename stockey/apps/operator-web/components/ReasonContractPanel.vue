<script setup lang="ts">
import type { Dict } from '~/types/api'

const props = defineProps<{
  contract?: unknown
  status?: unknown
  compact?: boolean
}>()

type Section = {
  key: string
  title: string
  data: Dict
  entries: [string, unknown][]
}

function parseContract(value: unknown): Dict {
  if (!value) return {}
  if (typeof value === 'string') {
    try {
      const parsed = JSON.parse(value)
      return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed as Dict : {}
    } catch {
      return { primary_reason: value }
    }
  }
  if (typeof value === 'object' && !Array.isArray(value)) return value as Dict
  return {}
}

function objectValue(value: unknown): Dict {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Dict : {}
}

function arrayValue(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function humanKey(value: string) {
  return value.replace(/_/g, ' ').replace(/\b\w/g, (char) => char.toUpperCase())
}

function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : String(Math.round(value * 100) / 100)
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  if (Array.isArray(value)) return value.map((item) => displayValue(item)).join(', ')
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function hasValue(value: unknown) {
  if (value === null || value === undefined || value === '') return false
  if (Array.isArray(value)) return value.length > 0
  if (typeof value === 'object') return Object.keys(value as Dict).length > 0
  return true
}

function filteredEntries(value: unknown): [string, unknown][] {
  return Object.entries(objectValue(value)).filter(([, entryValue]) => hasValue(entryValue))
}

function candidateEntries(value: unknown): [string, unknown][] {
  return Object.entries(objectValue(value)).filter(([, entryValue]) => hasValue(entryValue))
}

const parsed = computed(() => parseContract(props.contract))
const evidence = computed(() => objectValue(parsed.value.evidence))
const effectiveStatus = computed(() => String(parsed.value.status || props.status || '').trim())
const isDowngraded = computed(() => effectiveStatus.value.toLowerCase().includes('incomplete') || effectiveStatus.value.toLowerCase().includes('downgrade'))
const missingFields = computed(() => arrayValue(parsed.value.missing_fields).map((item) => String(item)).filter(Boolean))
const competingCandidates = computed(() => arrayValue(parsed.value.competing_candidates).filter(hasValue))
const conflictResolution = computed(() => objectValue(evidence.value.conflict_resolution))
const losingCandidates = computed(() => arrayValue(conflictResolution.value.losing_candidates).filter(hasValue))
const conflictSummary = computed(() => String(conflictResolution.value.source_precedence_reason || '').trim())
const hasConflictResolution = computed(() => Object.keys(conflictResolution.value).length > 0)
const eventEvidence = computed(() => objectValue(evidence.value.event))
const adversarialReview = computed(() => {
  const reviewAction = String(eventEvidence.value.review_action || '').trim()
  const reviewReason = String(eventEvidence.value.review_reason || '').trim()
  const actionStatus = String(eventEvidence.value.action_status || '').trim()
  const hasVeto = eventEvidence.value.veto === true || String(eventEvidence.value.veto || '').toLowerCase() === 'true'
  return {
    hasData: Boolean(reviewAction || reviewReason || actionStatus || hasVeto),
    reviewAction,
    reviewReason,
    actionStatus,
    hasVeto
  }
})
const marketGate = computed(() => {
  const macro = objectValue(evidence.value.macro_regime)
  const adjustment = String(macro.market_context_adjustment || '').trim()
  const reason = String(macro.market_context_adjustment_reason || '').trim()
  const originalAction = String(parsed.value.original_action_code || parsed.value.original_action || '').trim()
  return {
    hasData: Boolean(adjustment || reason),
    adjustment,
    reason,
    originalAction,
    regime: String(macro.regime_name || '').trim(),
    macroRisk: String(macro.macro_risk_state || '').trim(),
    breadth: macro.breadth_trend_alignment_pct,
    riskOffScore: macro.risk_off_score
  }
})
const primaryReason = computed(() => String(parsed.value.primary_reason || parsed.value.reason || '').trim())
const reasonDetail = computed(() => String(parsed.value.reason_detail || parsed.value.action_reason || '').trim())

const sections = computed<Section[]>(() => {
  return [
    ['screener', 'Screener'],
    ['technical', 'Technical'],
    ['event', 'Event'],
    ['company_memory', 'Company Memory'],
    ['playbook', 'Playbook'],
    ['wait_signal', 'Wait Signal'],
    ['macro_regime', 'Macro / Regime'],
    ['lifecycle', 'Lifecycle'],
    ['risk', 'Risk']
  ].map(([key, title]) => {
    const data = objectValue(evidence.value[key])
    return { key, title, data, entries: filteredEntries(data) }
  }).filter((section) => section.entries.length > 0)
})

const hasContract = computed(() => Object.keys(parsed.value).length > 0 || Boolean(effectiveStatus.value))
</script>

<template>
  <section
    v-if="hasContract"
    class="rounded-3xl border p-4"
    :class="isDowngraded ? 'border-sun/60 bg-sun/10' : 'border-moss/25 bg-white/70'"
  >
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <p class="text-xs font-black uppercase tracking-[0.22em] text-ink/45">Reason Contract</p>
        <h4 class="mt-1 text-base font-black text-ink">
          {{ parsed.action_code || parsed.final_action || parsed.new_action || 'Action rationale' }}
          <span v-if="parsed.action_source" class="text-sm font-semibold text-ink/50">· {{ parsed.action_source }}</span>
        </h4>
      </div>
      <span
        v-if="effectiveStatus"
        class="rounded-full px-3 py-1 text-xs font-black"
        :class="isDowngraded ? 'bg-sun text-ink' : 'bg-moss text-white'"
      >
        {{ effectiveStatus }}
      </span>
    </div>

      <div class="mt-3 grid gap-2 text-sm md:grid-cols-3">
      <p v-if="(parsed.original_action_code || parsed.original_action) && (parsed.original_action_code || parsed.original_action) !== parsed.action_code" class="rounded-2xl bg-white/80 px-3 py-2">
        <b>Original:</b> {{ parsed.original_action_code || parsed.original_action }}
      </p>
      <p v-if="parsed.setup_id" class="rounded-2xl bg-white/80 px-3 py-2"><b>Setup:</b> {{ parsed.setup_id }}</p>
      <p v-if="parsed.execution_action" class="rounded-2xl bg-white/80 px-3 py-2"><b>Execution:</b> {{ parsed.execution_action }}</p>
    </div>

    <p v-if="primaryReason" class="mt-3 rounded-2xl bg-white/80 p-3 text-sm font-semibold leading-6 text-ink/75">
      {{ primaryReason }}
    </p>
    <p v-if="reasonDetail && reasonDetail !== primaryReason" class="mt-2 rounded-2xl bg-white/60 p-3 text-sm leading-6 text-ink/65">
      {{ reasonDetail }}
    </p>

    <section v-if="adversarialReview.hasData" class="mt-3 rounded-2xl border border-rust/25 bg-rust/10 p-3">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p class="text-xs font-black uppercase tracking-[0.2em] text-rust">Adversarial review</p>
          <p v-if="adversarialReview.reviewReason" class="mt-2 text-sm font-semibold leading-6 text-ink/75">
            {{ adversarialReview.reviewReason }}
          </p>
        </div>
        <span
          class="rounded-full px-3 py-1 text-xs font-black"
          :class="adversarialReview.hasVeto ? 'bg-rust text-white' : 'bg-white text-ink/70'"
        >
          {{ adversarialReview.hasVeto ? 'veto' : (adversarialReview.reviewAction || adversarialReview.actionStatus || 'review') }}
        </span>
      </div>
      <div class="mt-3 grid gap-2 text-sm md:grid-cols-3">
        <p v-if="adversarialReview.reviewAction" class="rounded-xl bg-white/80 px-3 py-2"><b>Action:</b> {{ adversarialReview.reviewAction }}</p>
        <p v-if="adversarialReview.actionStatus" class="rounded-xl bg-white/80 px-3 py-2"><b>Status:</b> {{ adversarialReview.actionStatus }}</p>
        <p class="rounded-xl bg-white/80 px-3 py-2"><b>Veto:</b> {{ adversarialReview.hasVeto ? 'yes' : 'no' }}</p>
      </div>
    </section>

    <section v-if="marketGate.hasData" class="mt-3 rounded-2xl border border-sun/50 bg-sun/15 p-3">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p class="text-xs font-black uppercase tracking-[0.2em] text-rust">Market gate</p>
          <p v-if="marketGate.reason" class="mt-2 text-sm font-semibold leading-6 text-ink/75">
            {{ marketGate.reason }}
          </p>
        </div>
        <span class="rounded-full bg-sun px-3 py-1 text-xs font-black text-ink">
          {{ marketGate.adjustment || 'market context' }}
        </span>
      </div>
      <div class="mt-3 grid gap-2 text-sm md:grid-cols-3">
        <p v-if="marketGate.originalAction && marketGate.originalAction !== parsed.action_code" class="rounded-xl bg-white/80 px-3 py-2">
          <b>Original:</b> {{ marketGate.originalAction }}
        </p>
        <p v-if="marketGate.regime" class="rounded-xl bg-white/80 px-3 py-2"><b>Regime:</b> {{ marketGate.regime }}</p>
        <p v-if="marketGate.macroRisk" class="rounded-xl bg-white/80 px-3 py-2"><b>Macro risk:</b> {{ marketGate.macroRisk }}</p>
        <p v-if="hasValue(marketGate.breadth)" class="rounded-xl bg-white/80 px-3 py-2"><b>Breadth:</b> {{ displayValue(marketGate.breadth) }}%</p>
        <p v-if="hasValue(marketGate.riskOffScore)" class="rounded-xl bg-white/80 px-3 py-2"><b>Risk-off score:</b> {{ displayValue(marketGate.riskOffScore) }}</p>
      </div>
    </section>

    <div v-if="missingFields.length" class="mt-3 rounded-2xl bg-sun/20 p-3">
      <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/45">Missing Evidence</p>
      <div class="mt-2 flex flex-wrap gap-2">
        <span v-for="field in missingFields" :key="field" class="rounded-full bg-white px-3 py-1 text-xs font-bold text-ink/70">
          {{ field }}
        </span>
      </div>
    </div>

    <div v-if="sections.length" class="mt-3 grid gap-3" :class="compact ? 'lg:grid-cols-2' : 'lg:grid-cols-3'">
      <article v-for="section in sections" :key="section.key" class="rounded-2xl bg-white/75 p-3">
        <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/45">{{ section.title }}</p>
        <dl class="mt-2 space-y-1 text-sm">
          <div v-for="[key, value] in section.entries" :key="key" class="grid grid-cols-[0.9fr_1.1fr] gap-2">
            <dt class="font-semibold text-ink/50">{{ humanKey(key) }}</dt>
            <dd class="break-words text-ink/75">{{ displayValue(value) }}</dd>
          </div>
        </dl>
      </article>
    </div>

    <details v-if="hasConflictResolution" class="mt-3 rounded-2xl bg-white/65 p-3">
      <summary class="cursor-pointer text-sm font-black text-moss">
        Conflict resolution: {{ conflictResolution.same_symbol_conflict_count || losingCandidates.length || 0 }} losing candidate(s)
      </summary>
      <p v-if="conflictSummary" class="mt-3 rounded-2xl bg-paper/80 p-3 text-sm font-semibold leading-6 text-ink/75">
        {{ conflictSummary }}
      </p>
      <div class="mt-3 grid gap-2 text-sm md:grid-cols-3">
        <p v-if="conflictResolution.winning_action_code" class="rounded-2xl bg-paper/80 px-3 py-2">
          <b>Winner:</b> {{ conflictResolution.winning_action_code }}
        </p>
        <p v-if="conflictResolution.winning_action_source" class="rounded-2xl bg-paper/80 px-3 py-2">
          <b>Source:</b> {{ conflictResolution.winning_action_source }}
        </p>
        <p v-if="conflictResolution.same_symbol_candidate_count" class="rounded-2xl bg-paper/80 px-3 py-2">
          <b>Candidates:</b> {{ conflictResolution.same_symbol_candidate_count }}
        </p>
      </div>
      <div v-if="losingCandidates.length" class="mt-3 space-y-2">
        <article v-for="(candidate, idx) in losingCandidates" :key="idx" class="rounded-2xl bg-paper/80 p-3 text-sm text-ink/70">
          <template v-if="typeof candidate === 'object' && candidate !== null">
            <p v-for="[key, value] in candidateEntries(candidate)" :key="key">
              <b>{{ humanKey(key) }}:</b> {{ displayValue(value) }}
            </p>
          </template>
          <p v-else>{{ displayValue(candidate) }}</p>
        </article>
      </div>
    </details>

    <details v-if="competingCandidates.length" class="mt-3 rounded-2xl bg-white/65 p-3">
      <summary class="cursor-pointer text-sm font-black text-moss">Competing candidates: {{ competingCandidates.length }}</summary>
      <div class="mt-3 space-y-2">
        <article v-for="(candidate, idx) in competingCandidates" :key="idx" class="rounded-2xl bg-paper/80 p-3 text-sm text-ink/70">
          <template v-if="typeof candidate === 'object' && candidate !== null">
            <p v-for="[key, value] in candidateEntries(candidate)" :key="key">
              <b>{{ humanKey(key) }}:</b> {{ displayValue(value) }}
            </p>
          </template>
          <p v-else>{{ displayValue(candidate) }}</p>
        </article>
      </div>
    </details>
  </section>
</template>
