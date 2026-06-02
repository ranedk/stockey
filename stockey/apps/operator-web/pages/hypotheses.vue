<script setup lang="ts">
const api = useOperatorApi()
const { data, refresh } = await useAsyncData('hypotheses', () => api.getHypotheses(100))

const form = reactive({
  title: '',
  description: '',
  keywords: '',
  exclusions: '',
  expectedEffect: 'manual_review',
  triggerScope: 'market',
  status: 'active_review',
  source: 'operator',
  holdingWindowDays: 20,
  minTerms: 1,
  reliabilityRule: '',
  validationMethod: 'operator_review'
})
const scan = reactive({
  fromDate: '',
  toDate: '',
  sources: ['news', 'announcements', 'announcement_documents'],
  useLlm: true
})
const saving = ref(false)
const previewing = ref(false)
const running = ref(false)
const error = ref('')
const lastCreated = ref<Record<string, unknown> | null>(null)
const previewResult = ref<Awaited<ReturnType<ReturnType<typeof useOperatorApi>['previewHypothesis']>> | null>(null)
const runResult = ref<Awaited<ReturnType<ReturnType<typeof useOperatorApi>['runHypotheses']>> | null>(null)
const editingId = ref('')
const updatingStatusId = ref('')
const checkingId = ref('')
const checkResult = ref<Record<string, unknown> | null>(null)
const statusFilter = ref('all')
const reliabilityFilter = ref('all')
const scopeFilter = ref('all')
const checkSettings = reactive({
  lookbackDays: 365,
  minMatches: 3,
  operatorNotes: ''
})

const hypotheses = computed(() => data.value?.hypotheses || [])
const matches = computed(() => data.value?.matches || [])
const actionPlans = computed(() => data.value?.action_plans || [])
const reliabilityChecks = computed(() => data.value?.promotion_audits || [])
const statusOptions = [
  { key: 'all', label: 'All' },
  { key: 'draft', label: 'Draft' },
  { key: 'active_review', label: 'Active Review' },
  { key: 'trusted_overlay', label: 'Trusted Overlay' },
  { key: 'paused', label: 'Paused' },
  { key: 'retired', label: 'Retired' }
]
const reliabilityOptions = [
  { key: 'all', label: 'All' },
  { key: 'missing', label: 'No Check' },
  { key: 'sufficient_history', label: 'Sufficient History' },
  { key: 'insufficient_history', label: 'Insufficient History' }
]
const reliabilityByHypothesis = computed(() => {
  const out: Record<string, Record<string, unknown>> = {}
  for (const row of reliabilityChecks.value) {
    const id = String(row.hypothesis_id || '')
    if (id && !out[id]) out[id] = row
  }
  return out
})
const matchesByHypothesis = computed(() => groupRows(matches.value, 'hypothesis_id'))
const plansByHypothesis = computed(() => groupRows(actionPlans.value, 'hypothesis_id'))
const scopeOptions = computed(() => {
  const scopes = new Set(['all'])
  for (const row of hypotheses.value) scopes.add(String(row.trigger_scope || 'market'))
  return [...scopes].map((key) => ({ key, label: key === 'all' ? 'All Scopes' : titleCase(key) }))
})
const lifecycleCounts = computed(() => {
  const counts: Record<string, number> = {}
  for (const row of hypotheses.value) {
    const status = normalizedStatus(row.status)
    counts[status] = (counts[status] || 0) + 1
  }
  return counts
})
const filteredHypotheses = computed(() => hypotheses.value.filter((row) => {
  const status = normalizedStatus(row.status)
  const reliability = reliabilityStatus(row)
  const scope = String(row.trigger_scope || 'market')
  return (statusFilter.value === 'all' || status === statusFilter.value)
    && (reliabilityFilter.value === 'all' || reliability === reliabilityFilter.value)
    && (scopeFilter.value === 'all' || scope === scopeFilter.value)
}))

function parseKeywords(value: string) {
  return value
    .split(/[,;\n]+/)
    .map((item) => item.trim())
    .filter(Boolean)
}

function parseJsonish<T = Record<string, unknown>>(value: unknown, fallback: T = {} as T): T {
  if (!value) return fallback
  if (typeof value === 'object') return value as T
  try {
    return JSON.parse(String(value)) as T
  } catch {
    return fallback
  }
}

function listText(value: unknown) {
  if (!value) return ''
  if (Array.isArray(value)) return value.filter(Boolean).join(', ')
  return String(value)
}

function reliabilityEvidence(row: Record<string, unknown>) {
  return parseJsonish(row.evidence_json)
}

function reliabilityForwardSummary(row: Record<string, unknown>) {
  const evidence = reliabilityEvidence(row)
  return Array.isArray(evidence.forward_return_summary) ? evidence.forward_return_summary as Record<string, unknown>[] : []
}

function pct(value: unknown) {
  const num = Number(value)
  if (!Number.isFinite(num)) return '-'
  return `${Math.round(num * 10000) / 100}%`
}

function groupRows(rows: Record<string, unknown>[], key: string) {
  const out: Record<string, Record<string, unknown>[]> = {}
  for (const row of rows) {
    const id = String(row[key] || '')
    if (!id) continue
    out[id] ||= []
    out[id].push(row)
  }
  return out
}

function titleCase(value: unknown) {
  return String(value || '').replaceAll('_', ' ').replace(/\b\w/g, (char) => char.toUpperCase())
}

function normalizedStatus(value: unknown) {
  const status = String(value || 'active_review').toLowerCase()
  if (status === 'production') return 'trusted_overlay'
  if (status === 'testing' || status === 'validated') return 'active_review'
  if (status === 'disabled') return 'paused'
  if (status === 'inactive' || status === 'rejected') return 'retired'
  return status
}

function statusClass(value: unknown) {
  const status = normalizedStatus(value)
  if (status === 'trusted_overlay') return 'bg-ember text-white'
  if (status === 'active_review') return 'bg-moss text-white'
  if (status === 'draft') return 'bg-sun text-ink'
  if (status === 'paused') return 'bg-ink/20 text-ink'
  return 'bg-ink text-paper'
}

function reliabilityStatus(row: Record<string, unknown>) {
  const check = reliabilityByHypothesis.value[String(row.hypothesis_id || '')]
  return String(check?.audit_status || 'missing').toLowerCase()
}

function reliabilityClass(value: unknown) {
  const status = String(value || '').toLowerCase()
  if (status === 'sufficient_history') return 'bg-moss text-white'
  if (status === 'missing') return 'bg-ink/20 text-ink'
  return 'bg-ember text-white'
}

function actionBoundary(row: Record<string, unknown>) {
  const status = normalizedStatus(row.status)
  if (status === 'trusted_overlay') return 'review-only overlay can influence consolidated actions'
  if (status === 'active_review') return 'research/review only until trusted'
  if (status === 'draft') return 'not scanned by default'
  if (status === 'paused') return 'temporarily excluded from active scans'
  return 'retired and excluded'
}

function expectedEffect(row: Record<string, unknown>) {
  const effect = parseJsonish(row.expected_effect_json)
  return String(effect.effect || effect.action_bias || 'manual_review')
}

function triggerKeywords(row: Record<string, unknown>) {
  const triggerPatterns = parseJsonish(row.trigger_patterns_json)
  return listText(triggerPatterns.keywords || triggerPatterns.required_terms || [])
}

function latestPlans(row: Record<string, unknown>) {
  return (plansByHypothesis.value[String(row.hypothesis_id || '')] || []).slice(0, 3)
}

function latestMatches(row: Record<string, unknown>) {
  return (matchesByHypothesis.value[String(row.hypothesis_id || '')] || []).slice(0, 3)
}

function actionPlanChecks(row: Record<string, unknown>) {
  const checks = parseJsonish(row.checks_json, [])
  return Array.isArray(checks) ? checks as Record<string, unknown>[] : []
}

function buildHypothesisPayload() {
  return {
    hypothesis_id: editingId.value || undefined,
    title: form.title,
    description: form.description,
    source: form.source,
    status: form.status,
    trigger_scope: form.triggerScope,
    trigger_patterns: {
      keywords: {
        include: parseKeywords(form.keywords),
        exclude: parseKeywords(form.exclusions)
      },
      required_terms: []
    },
    expected_effect: {
      effect: form.expectedEffect,
      operator_note: form.description
    },
    holding_window_days: Number(form.holdingWindowDays || 20),
    decision_policy: {
      min_terms: Number(form.minTerms || 1),
      reliability_rule: form.reliabilityRule || undefined,
      validation_protocol: {
        method: form.validationMethod || 'operator_review'
      }
    },
    allow_unaudited_production: false
  }
}

function resetForm() {
  editingId.value = ''
  form.title = ''
  form.description = ''
  form.keywords = ''
  form.exclusions = ''
  form.expectedEffect = 'manual_review'
  form.triggerScope = 'market'
  form.status = 'active_review'
  form.source = 'operator'
  form.holdingWindowDays = 20
  form.minTerms = 1
  form.reliabilityRule = ''
  form.validationMethod = 'operator_review'
  previewResult.value = null
}

function editHypothesis(row: Record<string, unknown>) {
  const triggerPatterns = parseJsonish(row.trigger_patterns_json)
  const expectedEffect = parseJsonish(row.expected_effect_json)
  const decisionPolicy = parseJsonish(row.decision_policy_json)
  const validation = parseJsonish(decisionPolicy.validation_protocol)
  editingId.value = String(row.hypothesis_id || '')
  form.title = String(row.title || '')
  form.description = String(row.description || '')
  form.keywords = listText(triggerPatterns.keywords)
  form.exclusions = listText(triggerPatterns.exclude_keywords)
  form.expectedEffect = String(expectedEffect.effect || expectedEffect.action_bias || 'manual_review')
  form.triggerScope = String(row.trigger_scope || 'market')
  form.status = String(row.status || 'active_review').replace('production', 'trusted_overlay').replace('validated', 'active_review').replace('testing', 'active_review')
  form.source = String(row.source || 'operator')
  form.holdingWindowDays = Number(row.holding_window_days || 20)
  form.minTerms = Number(decisionPolicy.min_terms || 1)
  form.reliabilityRule = String(decisionPolicy.reliability_rule || decisionPolicy.promotion_rule || '')
  form.validationMethod = String(validation.method || 'operator_review')
  previewResult.value = null
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

async function previewHypothesis() {
  error.value = ''
  previewing.value = true
  try {
    previewResult.value = await api.previewHypothesis(buildHypothesisPayload())
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    previewing.value = false
  }
}

async function createHypothesis() {
  error.value = ''
  saving.value = true
  try {
    const payload = buildHypothesisPayload()
    const result = editingId.value
      ? await api.updateHypothesis(editingId.value, payload)
      : await api.createHypothesis(payload)
    lastCreated.value = result.hypothesis
    resetForm()
    await refresh()
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    saving.value = false
  }
}

async function updateStatus(row: Record<string, unknown>, status: string) {
  const hypothesisId = String(row.hypothesis_id || '')
  if (!hypothesisId) return
  if (status === 'trusted_overlay') {
    const confirmed = window.confirm(`Mark ${hypothesisId} as a trusted review overlay? This can affect action consolidation only as review_only, not as a broker-executable order.`)
    if (!confirmed) return
  }
  error.value = ''
  updatingStatusId.value = hypothesisId
  try {
    const triggerPatterns = parseJsonish(row.trigger_patterns_json)
    const expectedEffect = parseJsonish(row.expected_effect_json)
    const decisionPolicy = parseJsonish(row.decision_policy_json)
    await api.updateHypothesis(hypothesisId, {
      hypothesis_id: hypothesisId,
      title: row.title,
      description: row.description,
      source: row.source,
      status,
      trigger_scope: row.trigger_scope,
      trigger_patterns: triggerPatterns,
      expected_effect: expectedEffect,
      holding_window_days: row.holding_window_days,
      decision_policy: decisionPolicy,
      allow_unaudited_production: false
    })
    await refresh()
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    updatingStatusId.value = ''
  }
}

async function runReliabilityCheck(row: Record<string, unknown>) {
  const hypothesisId = String(row.hypothesis_id || '')
  if (!hypothesisId) return null
  error.value = ''
  checkingId.value = hypothesisId
  try {
    const result = await api.runPromotionAudit(hypothesisId, {
      lookback_days: Number(checkSettings.lookbackDays || 365),
      min_matches: Number(checkSettings.minMatches || 3),
      operator_notes: checkSettings.operatorNotes || undefined,
      persist: true
    })
    checkResult.value = result.audit
    await refresh()
    return result
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
    return null
  } finally {
    checkingId.value = ''
  }
}

async function setLifecycle(row: Record<string, unknown>, status: string) {
  if (status === 'trusted_overlay') {
    const check = reliabilityByHypothesis.value[String(row.hypothesis_id || '')]
    const checkStatus = String(check?.audit_status || '').toLowerCase()
    const confirmed = window.confirm(
      checkStatus === 'sufficient_history'
        ? `Mark ${row.hypothesis_id} as a trusted review overlay? It remains review-only and cannot create broker orders.`
        : `This playbook does not have sufficient reliability history. Mark it trusted overlay anyway? It remains review-only and cannot create broker orders.`
    )
    if (!confirmed) return
  }
  await updateStatus(row, status)
}

async function runScan(hypothesisId?: unknown, persist = true) {
  error.value = ''
  running.value = true
  try {
    runResult.value = await api.runHypotheses({
      hypothesis_id: hypothesisId || undefined,
      from_date: scan.fromDate || undefined,
      to_date: scan.toDate || undefined,
      sources: scan.sources,
      build_actions: true,
      use_llm: scan.useLlm,
      persist
    })
    await refresh()
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    running.value = false
  }
}
</script>

<template>
  <section class="rounded-[2rem] bg-ink p-8 text-paper shadow-soft">
    <p class="text-sm font-semibold uppercase tracking-[0.35em] text-paper/55">Investor Playbooks</p>
    <h1 class="mt-4 max-w-4xl text-5xl font-black tracking-tight">Track investor playbooks as reliability overlays.</h1>
    <p class="mt-5 max-w-3xl text-lg leading-8 text-paper/70">
      Add a playbook, scan news and announcements, then let the action planner decide what else to check and what safe operator action to consider. Trusted overlays can affect consolidated actions only as review-only signals.
    </p>
  </section>

  <p v-if="error" class="mt-6 rounded-3xl border border-ember/30 bg-ember/10 p-4 text-sm font-semibold text-ember">{{ error }}</p>

  <section class="mt-8 grid gap-6 lg:grid-cols-[0.95fr_1.05fr]">
    <form class="glass-panel rounded-3xl p-6" @submit.prevent="createHypothesis">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 class="text-xl font-black">{{ editingId ? 'Edit Playbook' : 'Create Playbook' }}</h2>
          <p class="mt-1 text-sm text-ink/55">
            {{ editingId ? `Editing ${editingId}. Preview before saving changes.` : 'Preview first to see exactly what will be stored and scanned.' }}
          </p>
        </div>
        <span v-if="form.status === 'trusted_overlay'" class="rounded-full bg-ember px-3 py-1 text-xs font-black text-white">Trusted overlay</span>
      </div>
      <div class="mt-5 grid gap-4">
        <label class="grid gap-2 text-sm font-bold text-ink/70">
          Title
          <input v-model="form.title" required class="rounded-2xl border border-black/10 bg-white/80 px-4 py-3 text-ink outline-none focus:border-moss" placeholder="PM austerity call increases market risk" />
        </label>
        <label class="grid gap-2 text-sm font-bold text-ink/70">
          Description
          <textarea v-model="form.description" rows="4" class="rounded-2xl border border-black/10 bg-white/80 px-4 py-3 text-ink outline-none focus:border-moss" placeholder="If a top authority calls for austerity, liquidity/risk appetite can fall."></textarea>
        </label>
        <label class="grid gap-2 text-sm font-bold text-ink/70">
          Trigger keywords or phrases
          <textarea v-model="form.keywords" rows="3" class="rounded-2xl border border-black/10 bg-white/80 px-4 py-3 text-ink outline-none focus:border-moss" placeholder="austerity, fiscal tightening, spending cuts, prime minister"></textarea>
        </label>
        <label class="grid gap-2 text-sm font-bold text-ink/70">
          Exclusion keywords
          <textarea v-model="form.exclusions" rows="2" class="rounded-2xl border border-black/10 bg-white/80 px-4 py-3 text-ink outline-none focus:border-moss" placeholder="routine cost cutting, unrelated restructuring"></textarea>
        </label>
        <div class="grid gap-4 md:grid-cols-2">
          <label class="grid gap-2 text-sm font-bold text-ink/70">
            Source
            <input v-model="form.source" class="rounded-2xl border border-black/10 bg-white/80 px-4 py-3 text-ink outline-none focus:border-moss" placeholder="investor_interview" />
          </label>
          <label class="grid gap-2 text-sm font-bold text-ink/70">
            Expected effect
            <select v-model="form.expectedEffect" class="rounded-2xl border border-black/10 bg-white/80 px-4 py-3 text-ink outline-none focus:border-moss">
              <option value="manual_review">Manual review</option>
              <option value="reduce_exposure">Reduce exposure review</option>
              <option value="go_cash">Go cash review</option>
              <option value="sector_review">Sector review</option>
              <option value="short_research">Short research only</option>
            </select>
          </label>
          <label class="grid gap-2 text-sm font-bold text-ink/70">
            Scope
            <select v-model="form.triggerScope" class="rounded-2xl border border-black/10 bg-white/80 px-4 py-3 text-ink outline-none focus:border-moss">
              <option value="market">Market</option>
              <option value="sector">Sector</option>
              <option value="symbol">Symbol</option>
              <option value="portfolio">Portfolio</option>
            </select>
          </label>
          <label class="grid gap-2 text-sm font-bold text-ink/70">
            Status
            <select v-model="form.status" class="rounded-2xl border border-black/10 bg-white/80 px-4 py-3 text-ink outline-none focus:border-moss">
              <option value="draft">Draft</option>
              <option value="active_review">Active review</option>
              <option value="trusted_overlay">Trusted overlay</option>
              <option value="paused">Paused</option>
              <option value="retired">Retired</option>
            </select>
          </label>
          <label class="grid gap-2 text-sm font-bold text-ink/70">
            Min matched terms
            <input v-model.number="form.minTerms" min="1" type="number" class="rounded-2xl border border-black/10 bg-white/80 px-4 py-3 text-ink outline-none focus:border-moss" />
          </label>
          <label class="grid gap-2 text-sm font-bold text-ink/70">
            Holding window days
            <input v-model.number="form.holdingWindowDays" min="1" type="number" class="rounded-2xl border border-black/10 bg-white/80 px-4 py-3 text-ink outline-none focus:border-moss" />
          </label>
          <label class="grid gap-2 text-sm font-bold text-ink/70">
            Validation method
            <input v-model="form.validationMethod" class="rounded-2xl border border-black/10 bg-white/80 px-4 py-3 text-ink outline-none focus:border-moss" placeholder="point_in_time_forward_return" />
          </label>
        </div>
        <label class="grid gap-2 text-sm font-bold text-ink/70">
          Reliability rule
          <textarea v-model="form.reliabilityRule" rows="2" class="rounded-2xl border border-black/10 bg-white/80 px-4 py-3 text-ink outline-none focus:border-moss" placeholder="Trust only after enough historical matches, source diversity, price-linked examples, and manual review."></textarea>
        </label>
        <p v-if="form.status === 'trusted_overlay'" class="rounded-2xl bg-ember/10 p-3 text-sm font-semibold text-ember">
          Trusted overlays can affect consolidated actions as review-only overlays. They still cannot create direct broker orders.
        </p>
        <div class="flex flex-wrap gap-3">
          <button class="rounded-full bg-white px-5 py-3 text-sm font-black text-ink disabled:opacity-50" :disabled="previewing || !form.title" type="button" @click="previewHypothesis">
            {{ previewing ? 'Previewing...' : 'Preview normalized playbook' }}
          </button>
          <button class="rounded-full bg-moss px-5 py-3 text-sm font-black text-white disabled:opacity-50" :disabled="saving" type="submit">
            {{ saving ? 'Saving...' : editingId ? 'Save changes' : 'Save playbook' }}
          </button>
          <button v-if="editingId" class="rounded-full bg-ink/10 px-5 py-3 text-sm font-black text-ink" type="button" @click="resetForm">
            Cancel edit
          </button>
        </div>
      </div>
      <div v-if="previewResult" class="mt-4 rounded-3xl border border-moss/20 bg-white/75 p-4">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <h3 class="text-sm font-black uppercase tracking-[0.2em] text-ink/55">Generated Playbook Preview</h3>
          <span class="rounded-full bg-ink/10 px-3 py-1 text-xs font-bold text-ink/60">{{ previewResult.db_row_preview?.status || form.status }}</span>
        </div>
        <p class="mt-2 text-sm leading-6 text-ink/60">{{ previewResult.production_note }}</p>
        <details class="mt-3" open>
          <summary class="cursor-pointer text-sm font-black text-moss">Normalized payload</summary>
          <pre class="mt-2 max-h-72 overflow-auto rounded-2xl bg-ink p-4 text-xs leading-5 text-paper">{{ JSON.stringify(previewResult.normalized_payload, null, 2) }}</pre>
        </details>
        <details class="mt-3">
          <summary class="cursor-pointer text-sm font-black text-moss">Database row preview</summary>
          <pre class="mt-2 max-h-64 overflow-auto rounded-2xl bg-ink p-4 text-xs leading-5 text-paper">{{ JSON.stringify(previewResult.db_row_preview, null, 2) }}</pre>
        </details>
      </div>
      <pre v-if="lastCreated" class="mt-4 max-h-64 overflow-auto rounded-2xl bg-ink p-4 text-xs leading-5 text-paper">{{ JSON.stringify(lastCreated, null, 2) }}</pre>
    </form>

    <div class="glass-panel rounded-3xl p-6">
      <h2 class="text-xl font-black">Run Scan</h2>
      <p class="mt-2 text-sm leading-6 text-ink/60">
        Scan persisted ET/news, advisory announcement events, and announcement documents. Matching playbooks also generate action plans. Leave dates blank for the default recent window.
      </p>
      <div class="mt-5 grid gap-4 md:grid-cols-2">
        <label class="grid gap-2 text-sm font-bold text-ink/70">
          From
          <input v-model="scan.fromDate" type="date" class="rounded-2xl border border-black/10 bg-white/80 px-4 py-3 text-ink outline-none focus:border-moss" />
        </label>
        <label class="grid gap-2 text-sm font-bold text-ink/70">
          To
          <input v-model="scan.toDate" type="date" class="rounded-2xl border border-black/10 bg-white/80 px-4 py-3 text-ink outline-none focus:border-moss" />
        </label>
      </div>
      <label class="mt-4 flex items-center gap-3 rounded-2xl bg-white/70 p-3 text-sm font-bold text-ink/70">
        <input v-model="scan.useLlm" type="checkbox" class="h-4 w-4" />
        Use Codex action planner to decide checks and safe action boundaries
      </label>
      <div class="mt-4 rounded-3xl border border-black/10 bg-white/65 p-4">
        <h3 class="text-sm font-black uppercase tracking-[0.2em] text-ink/45">Reliability Check</h3>
        <p class="mt-2 text-sm leading-6 text-ink/60">Checks historical coverage, source diversity, and price-linked examples. This is evidence for operator judgement, not a statistical production gate.</p>
        <div class="mt-3 grid gap-3 md:grid-cols-2">
          <label class="grid gap-2 text-sm font-bold text-ink/70">
            Lookback days
            <input v-model.number="checkSettings.lookbackDays" min="1" type="number" class="rounded-2xl border border-black/10 bg-white/80 px-4 py-3 text-ink outline-none focus:border-moss" />
          </label>
          <label class="grid gap-2 text-sm font-bold text-ink/70">
            Minimum matches
            <input v-model.number="checkSettings.minMatches" min="1" type="number" class="rounded-2xl border border-black/10 bg-white/80 px-4 py-3 text-ink outline-none focus:border-moss" />
          </label>
        </div>
        <label class="mt-3 grid gap-2 text-sm font-bold text-ink/70">
          Operator notes
          <textarea v-model="checkSettings.operatorNotes" rows="2" class="rounded-2xl border border-black/10 bg-white/80 px-4 py-3 text-ink outline-none focus:border-moss" placeholder="Why this playbook should be trusted or kept under active review."></textarea>
        </label>
        <pre v-if="checkResult" class="mt-3 max-h-64 overflow-auto rounded-2xl bg-ink p-4 text-xs leading-5 text-paper">{{ JSON.stringify(checkResult, null, 2) }}</pre>
      </div>
      <button class="mt-5 rounded-full bg-ink px-5 py-3 text-sm font-black text-paper disabled:opacity-50" :disabled="running" type="button" @click="runScan()">
        {{ running ? 'Scanning...' : 'Run active playbooks' }}
      </button>
      <pre v-if="runResult" class="mt-4 max-h-80 overflow-auto rounded-2xl bg-white/80 p-4 text-xs leading-5 text-ink">{{ JSON.stringify(runResult, null, 2) }}</pre>
    </div>
  </section>

  <section class="mt-8 grid gap-4 md:grid-cols-5">
    <MetricTile label="Draft" :value="String(lifecycleCounts.draft || 0)" note="Saved but not scanned by default" />
    <MetricTile label="Active Review" :value="String(lifecycleCounts.active_review || 0)" note="Research and review queue" />
    <MetricTile label="Trusted" :value="String(lifecycleCounts.trusted_overlay || 0)" note="Review-only action overlay" />
    <MetricTile label="Paused" :value="String(lifecycleCounts.paused || 0)" note="Temporarily excluded" />
    <MetricTile label="Retired" :value="String(lifecycleCounts.retired || 0)" note="Historical only" />
  </section>

  <section class="mt-8 grid gap-6 lg:grid-cols-[1.15fr_0.85fr]">
    <div class="glass-panel rounded-3xl p-6">
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Lifecycle</p>
          <h2 class="mt-2 text-2xl font-black">Saved Playbooks</h2>
          <p class="mt-2 max-w-3xl text-sm leading-6 text-ink/60">
            Promotion audit is now operator-facing reliability check. Trusted overlays can influence consolidated actions only as review-only evidence.
          </p>
        </div>
        <span class="rounded-full bg-ink px-3 py-1 text-xs font-black text-paper">{{ filteredHypotheses.length }} shown</span>
      </div>
      <div class="mt-5 flex flex-wrap gap-2">
        <button
          v-for="option in statusOptions"
          :key="option.key"
          class="rounded-full px-4 py-2 text-sm font-black transition"
          :class="statusFilter === option.key ? 'bg-ink text-paper' : 'bg-white/80 text-ink/60 hover:bg-white'"
          type="button"
          @click="statusFilter = option.key"
        >
          {{ option.label }} · {{ option.key === 'all' ? hypotheses.length : lifecycleCounts[option.key] || 0 }}
        </button>
      </div>
      <div class="mt-3 flex flex-wrap gap-2">
        <button
          v-for="option in reliabilityOptions"
          :key="option.key"
          class="rounded-full px-4 py-2 text-sm font-black transition"
          :class="reliabilityFilter === option.key ? 'bg-moss text-paper' : 'bg-white/80 text-ink/60 hover:bg-white'"
          type="button"
          @click="reliabilityFilter = option.key"
        >
          {{ option.label }}
        </button>
        <button
          v-for="option in scopeOptions"
          :key="option.key"
          class="rounded-full px-4 py-2 text-sm font-black transition"
          :class="scopeFilter === option.key ? 'bg-sun text-ink' : 'bg-white/80 text-ink/60 hover:bg-white'"
          type="button"
          @click="scopeFilter = option.key"
        >
          {{ option.label }}
        </button>
      </div>
      <div class="mt-6 space-y-4">
        <article v-for="row in filteredHypotheses" :key="String(row.hypothesis_id)" class="rounded-3xl bg-white/75 p-5">
          <div class="flex flex-wrap items-start justify-between gap-4">
            <div class="max-w-3xl">
              <div class="flex flex-wrap gap-2">
                <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(row.status)">{{ titleCase(normalizedStatus(row.status)) }}</span>
                <span class="rounded-full px-3 py-1 text-xs font-black" :class="reliabilityClass(reliabilityStatus(row))">{{ titleCase(reliabilityStatus(row)) }}</span>
                <span class="rounded-full bg-ink/10 px-3 py-1 text-xs font-black text-ink/60">{{ titleCase(row.trigger_scope || 'market') }}</span>
              </div>
              <h3 class="mt-3 text-2xl font-black text-ink">{{ row.title || row.hypothesis_id }}</h3>
              <p class="mt-2 text-sm leading-6 text-ink/65">{{ row.description || 'No thesis description recorded.' }}</p>
            </div>
            <button class="rounded-full bg-white px-4 py-2 text-sm font-bold text-ink" type="button" @click="editHypothesis(row)">
              Edit
            </button>
          </div>

          <div class="mt-4 grid gap-2 text-sm md:grid-cols-4">
            <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Effect:</b> {{ titleCase(expectedEffect(row)) }}</p>
            <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Matches:</b> {{ matchesByHypothesis[String(row.hypothesis_id || '')]?.length || 0 }}</p>
            <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Plans:</b> {{ plansByHypothesis[String(row.hypothesis_id || '')]?.length || 0 }}</p>
            <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Boundary:</b> {{ actionBoundary(row) }}</p>
          </div>
          <p class="mt-3 text-sm text-ink/55"><b>Triggers:</b> {{ triggerKeywords(row) || 'No trigger terms recorded.' }}</p>

          <div class="mt-4 flex flex-wrap gap-2">
            <button class="rounded-full bg-ink px-4 py-2 text-sm font-bold text-paper" type="button" @click="runScan(row.hypothesis_id, false)">
              Dry-run this playbook
            </button>
            <button
              class="rounded-full bg-white px-4 py-2 text-sm font-bold text-ink disabled:opacity-50"
              type="button"
              :disabled="checkingId === row.hypothesis_id"
              @click="runReliabilityCheck(row)"
            >
              {{ checkingId === row.hypothesis_id ? 'Checking...' : 'Run reliability check' }}
            </button>
            <button v-if="normalizedStatus(row.status) !== 'active_review'" class="rounded-full bg-moss px-4 py-2 text-sm font-bold text-white disabled:opacity-50" type="button" :disabled="updatingStatusId === row.hypothesis_id" @click="setLifecycle(row, 'active_review')">
              Activate review
            </button>
            <button v-if="normalizedStatus(row.status) !== 'trusted_overlay'" class="rounded-full bg-ember px-4 py-2 text-sm font-bold text-white disabled:opacity-50" type="button" :disabled="updatingStatusId === row.hypothesis_id" @click="setLifecycle(row, 'trusted_overlay')">
              Mark trusted overlay
            </button>
            <button v-if="normalizedStatus(row.status) !== 'paused'" class="rounded-full bg-sun px-4 py-2 text-sm font-bold text-ink disabled:opacity-50" type="button" :disabled="updatingStatusId === row.hypothesis_id" @click="setLifecycle(row, 'paused')">
              Pause
            </button>
            <button v-if="normalizedStatus(row.status) !== 'retired'" class="rounded-full bg-ink/10 px-4 py-2 text-sm font-bold text-ink disabled:opacity-50" type="button" :disabled="updatingStatusId === row.hypothesis_id" @click="setLifecycle(row, 'retired')">
              Retire
            </button>
          </div>

          <div v-if="reliabilityByHypothesis[String(row.hypothesis_id || '')]" class="mt-4 rounded-2xl border border-black/10 bg-paper/70 p-4 text-sm">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <b>Latest reliability check</b>
              <span class="rounded-full px-3 py-1 text-xs font-black" :class="reliabilityClass(reliabilityStatus(row))">
                {{ titleCase(reliabilityStatus(row)) }}
              </span>
            </div>
            <p class="mt-2 text-ink/65">{{ reliabilityByHypothesis[String(row.hypothesis_id || '')].audit_reason }}</p>
            <p class="mt-2 text-xs text-ink/45">
              Matches: {{ reliabilityByHypothesis[String(row.hypothesis_id || '')].match_count || 0 }} ·
              Sources: {{ reliabilityByHypothesis[String(row.hypothesis_id || '')].source_type_count || 0 }} ·
              Symbols: {{ reliabilityByHypothesis[String(row.hypothesis_id || '')].symbol_count || 0 }} ·
              Checked: {{ reliabilityByHypothesis[String(row.hypothesis_id || '')].audited_at || '-' }}
            </p>
            <div v-if="reliabilityForwardSummary(reliabilityByHypothesis[String(row.hypothesis_id || '')]).length" class="mt-3 grid gap-2 md:grid-cols-2">
              <div v-for="summary in reliabilityForwardSummary(reliabilityByHypothesis[String(row.hypothesis_id || '')])" :key="String(summary.horizon_days)" class="rounded-xl bg-white/70 px-3 py-2 text-xs">
                <b>{{ summary.horizon_days }}d:</b>
                {{ summary.evaluated_count }} eval · mean {{ pct(summary.mean_forward_return) }} · hit {{ pct(summary.positive_hit_rate) }} · NIFTY excess {{ pct(summary.mean_excess_return_vs_benchmark) }} · sector excess {{ pct(summary.mean_excess_return_vs_sector_proxy) }}
              </div>
            </div>
          </div>

          <details v-if="latestPlans(row).length" class="mt-4">
            <summary class="cursor-pointer text-sm font-black text-moss">Latest action plans and operator questions</summary>
            <div class="mt-3 space-y-3">
              <div v-for="plan in latestPlans(row)" :key="`${plan.source_table}-${plan.source_key}`" class="rounded-2xl bg-paper/70 p-4">
                <div class="flex flex-wrap items-center justify-between gap-2">
                  <p class="font-black text-ink">{{ plan.action_type || plan.suggested_action }}</p>
                  <span class="rounded-full bg-ink/10 px-3 py-1 text-xs font-black text-ink/60">{{ plan.llm_status || 'planner' }}</span>
                </div>
                <p class="mt-2 text-sm text-ink/65">{{ plan.operator_summary || plan.decision_reason }}</p>
                <div v-if="actionPlanChecks(plan).length" class="mt-3 grid gap-2">
                  <p v-for="check in actionPlanChecks(plan)" :key="String(check.question)" class="rounded-xl bg-white/70 px-3 py-2 text-xs text-ink/70">
                    <b>{{ titleCase(check.check_type) }}:</b> {{ check.question }}
                  </p>
                </div>
              </div>
            </div>
          </details>

          <details v-if="latestMatches(row).length" class="mt-4">
            <summary class="cursor-pointer text-sm font-black text-moss">Latest matched evidence</summary>
            <div class="mt-3 grid gap-2">
              <p v-for="match in latestMatches(row)" :key="`${match.source_table}-${match.source_key}`" class="rounded-xl bg-paper/70 px-3 py-2 text-xs text-ink/70">
                <b>{{ match.source_type }}:</b> {{ match.subject || match.evidence_text || match.action_reason }}
              </p>
            </div>
          </details>

          <details class="mt-4">
            <summary class="cursor-pointer text-sm font-black text-moss">Show raw playbook row</summary>
            <pre class="mt-3 max-h-64 overflow-auto rounded-2xl bg-ink p-4 text-xs leading-5 text-paper">{{ JSON.stringify(row, null, 2) }}</pre>
          </details>
        </article>
        <p v-if="!filteredHypotheses.length" class="rounded-3xl bg-white/75 p-6 text-ink/60">No playbooks match the current filters.</p>
      </div>
    </div>
    <div>
      <h2 class="mb-3 text-xl font-black">Latest Action Plans</h2>
      <div class="space-y-3">
        <RecordCard
          v-for="row in actionPlans"
          :key="`${row.hypothesis_id}-${row.source_table}-${row.source_key}`"
          :title="String(row.operator_summary || row.hypothesis_id)"
          :subtitle="String(row.decision_reason || '')"
          :record="row"
        >
          <template #badge>
            <span class="rounded-full bg-ember px-3 py-1 text-xs font-bold text-white">{{ row.action_type || 'ACTION_PLAN' }}</span>
          </template>
        </RecordCard>
        <p v-if="!actionPlans.length" class="glass-panel rounded-3xl p-6 text-ink/60">No action plans yet.</p>
      </div>
    </div>
  </section>

  <section class="mt-8">
    <h2 class="mb-3 text-xl font-black">Latest Playbook Matches</h2>
    <div class="grid gap-3 lg:grid-cols-2">
      <RecordCard
        v-for="row in matches"
        :key="`${row.hypothesis_id}-${row.source_table}-${row.source_key}`"
        :title="String(row.hypothesis_title || row.hypothesis_id)"
        :subtitle="String(row.action_reason || row.subject || '')"
        :record="row"
      >
        <template #badge>
          <span class="rounded-full bg-ink px-3 py-1 text-xs font-bold text-paper">{{ row.suggested_action || 'MATCH' }}</span>
        </template>
      </RecordCard>
      <p v-if="!matches.length" class="glass-panel rounded-3xl p-6 text-ink/60">No playbook matches yet.</p>
    </div>
  </section>
</template>
