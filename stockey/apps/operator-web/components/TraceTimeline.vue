<script setup lang="ts">
import type { Dict, ManualReviewWaitSignalLink, TraceDecision, TraceStage, TraceSummary } from '~/types/api'

const props = defineProps<{
  trace?: TraceSummary | null
  title?: string
  syncUrl?: boolean
}>()

const route = useRoute()
const router = useRouter()

const queryKeys = {
  domain: 'trace_domain',
  status: 'trace_status',
  quick: 'trace_quick',
  search: 'trace_q',
  focus: 'trace_focus'
}

const domainOptions = ['event', 'review', 'playbook', 'technical', 'risk', 'macro', 'exchange', 'portfolio', 'lifecycle', 'action', 'execution', 'processing']
const statusOptions = ['error', 'blocked', 'review', 'veto', 'completed', 'planned', 'allocated', 'approved']
const quickOptions = ['problems', 'execution_blockers', 'action_changes', 'event_driven']

const selectedDomain = ref('')
const selectedStatus = ref('')
const quickFilter = ref('')
const searchText = ref('')

let syncingFiltersFromRoute = false

function routeQueryValue(key: string) {
  const value = route.query[key]
  return Array.isArray(value) ? String(value[0] || '') : String(value || '')
}

function readRouteFilters() {
  if (!props.syncUrl) return
  syncingFiltersFromRoute = true
  const domain = routeQueryValue(queryKeys.domain)
  const status = routeQueryValue(queryKeys.status)
  const quick = routeQueryValue(queryKeys.quick)
  selectedDomain.value = domainOptions.includes(domain) ? domain : ''
  selectedStatus.value = statusOptions.includes(status) ? status : ''
  quickFilter.value = quickOptions.includes(quick) ? quick : ''
  searchText.value = routeQueryValue(queryKeys.search)
  syncingFiltersFromRoute = false
}

function setQueryValue(query: Record<string, string | string[] | undefined>, key: string, value: string) {
  if (value) {
    query[key] = value
  } else {
    delete query[key]
  }
}

function syncFiltersToUrl() {
  if (!props.syncUrl || syncingFiltersFromRoute) return
  const query = { ...route.query } as Record<string, string | string[] | undefined>
  setQueryValue(query, queryKeys.domain, selectedDomain.value)
  setQueryValue(query, queryKeys.status, selectedStatus.value)
  setQueryValue(query, queryKeys.quick, quickFilter.value)
  setQueryValue(query, queryKeys.search, searchText.value.trim())
  router.replace({ query, hash: route.hash })
}

function safeAnchor(value: unknown) {
  return String(value || 'trace')
    .trim()
    .replace(/[^a-zA-Z0-9_-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 120) || 'trace'
}

function processingAnchor(stage: TraceStage, idx: number) {
  return `trace-processing-${safeAnchor(`${stage.stage}-${stage.started_at || stage.completed_at || idx}`)}`
}

function decisionAnchor(decision: TraceDecision) {
  return `trace-decision-${safeAnchor(decision.trace_id || `${decision.symbol}-${decision.trigger_type}-${decision.updated_at || decision.asof_date}`)}`
}

function stepAnchor(decision: TraceDecision, step: TraceStage) {
  return `trace-step-${safeAnchor(`${decision.trace_id}-${step.step_idx}-${step.stage}`)}`
}

async function focusAnchor(anchor: string) {
  if (!props.syncUrl) return
  const query = { ...route.query, [queryKeys.focus]: anchor }
  await router.replace({ query, hash: `#${anchor}` })
  await nextTick()
  document.getElementById(anchor)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

async function copyAnchor(anchor: string) {
  await focusAnchor(anchor)
  if (typeof window === 'undefined' || !navigator?.clipboard) return
  try {
    await navigator.clipboard.writeText(window.location.href)
  } catch {
    // Clipboard permissions vary by browser; the URL still updates for manual copy.
  }
}

readRouteFilters()

watch([selectedDomain, selectedStatus, quickFilter, searchText], syncFiltersToUrl)
watch(() => route.query, readRouteFilters, { deep: true })

onMounted(async () => {
  if (!props.syncUrl) return
  await nextTick()
  const anchor = routeQueryValue(queryKeys.focus) || route.hash.replace(/^#/, '')
  if (anchor) {
    document.getElementById(anchor)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
})

function formatWhen(value?: string) {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('en-IN', {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone: 'Asia/Kolkata'
  })
}

function statusClass(status?: string) {
  const normalized = String(status || '').toLowerCase()
  if (['ok', 'success', 'done', 'clear', 'completed'].some((item) => normalized.includes(item))) {
    return 'bg-moss text-white'
  }
  if (['error', 'failed', 'veto', 'reject'].some((item) => normalized.includes(item))) {
    return 'bg-ember text-white'
  }
  if (['manual', 'review', 'warning', 'penalize'].some((item) => normalized.includes(item))) {
    return 'bg-sun text-ink'
  }
  return 'bg-ink/10 text-ink'
}

function domainTitle(domain?: string) {
  const titles: Record<string, string> = {
    event: 'Event Evaluation',
    review: 'Adversarial Review',
    playbook: 'Investor Playbook',
    technical: 'Technical State',
    lifecycle: 'Lifecycle / Exit Policy',
    risk: 'Risk Sizing',
    portfolio: 'Portfolio Allocation',
    macro: 'Macro / Regime Context',
    exchange: 'Exchange-Event Context',
    action: 'Action Consolidation',
    execution: 'Execution Eligibility',
    processing: 'Processing'
  }
  return titles[String(domain || '')] || 'Trace Step'
}

function domainClass(domain?: string) {
  const normalized = String(domain || '')
  if (normalized === 'event') return 'border-sky/30 bg-sky/10'
  if (normalized === 'review') return 'border-ember/30 bg-ember/10'
  if (normalized === 'playbook') return 'border-sun/50 bg-sun/10'
  if (normalized === 'technical') return 'border-moss/30 bg-moss/10'
  if (normalized === 'lifecycle') return 'border-sun/40 bg-sun/10'
  if (normalized === 'risk') return 'border-danger/30 bg-danger/10'
  if (normalized === 'portfolio') return 'border-moss/30 bg-white'
  if (normalized === 'macro') return 'border-sky/30 bg-white'
  if (normalized === 'exchange') return 'border-ember/30 bg-white'
  if (normalized === 'action') return 'border-ink/20 bg-ink/5'
  if (normalized === 'execution') return 'border-ember/30 bg-white'
  return 'border-black/10 bg-white/75'
}

function textValue(value: unknown) {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : String(Math.round(value * 100) / 100)
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  return String(value)
}

function pctValue(value: unknown) {
  if (value === null || value === undefined || value === '') return '-'
  const num = Number(value)
  if (Number.isNaN(num)) return String(value)
  return `${Math.round(num * 100) / 100}%`
}

function payloadOf(item: TraceStage | TraceDecision) {
  return (item.payload || {}) as Dict
}

function hasAny(payload: Dict, keys: string[]) {
  return keys.some((key) => payload[key] !== undefined && payload[key] !== null && payload[key] !== '')
}

function objectEntries(value: unknown) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return []
  return Object.entries(value as Record<string, unknown>)
}

function lower(value: unknown) {
  return String(value || '').toLowerCase()
}

function itemText(item: TraceStage | TraceDecision) {
  return JSON.stringify(item || {}).toLowerCase()
}

function isProblem(item: TraceStage | TraceDecision) {
  const text = itemText(item)
  return ['error', 'fail', 'blocked', 'veto', 'manual', 'reject', 'stale', 'missing'].some((token) => text.includes(token))
}

function isExecutionBlocker(item: TraceStage | TraceDecision) {
  const text = itemText(item)
  return lower(item.domain) === 'execution' && ['submit_blocked', 'blocked', 'submit_error', 'reconcile_error'].some((token) => text.includes(token))
}

function isActionChange(item: TraceDecision) {
  return Boolean(item.action_changed) || Boolean(item.previous_action && item.new_action && item.previous_action !== item.new_action)
}

function isEventDriven(item: TraceStage | TraceDecision) {
  return lower(item.domain) === 'event' || itemText(item).includes('event_evaluation')
}

function matchesFilters(item: TraceStage | TraceDecision) {
  if (selectedDomain.value && lower(item.domain) !== selectedDomain.value) return false
  if (selectedStatus.value && !itemText(item).includes(selectedStatus.value)) return false
  if (searchText.value.trim() && !itemText(item).includes(searchText.value.trim().toLowerCase())) return false
  if (quickFilter.value === 'problems' && !isProblem(item)) return false
  if (quickFilter.value === 'execution_blockers' && !isExecutionBlocker(item)) return false
  if (quickFilter.value === 'action_changes' && !isActionChange(item as TraceDecision)) return false
  if (quickFilter.value === 'event_driven' && !isEventDriven(item)) return false
  return true
}

const filteredProcessing = computed(() => (props.trace?.processing || []).filter(matchesFilters))
const filteredDecisions = computed(() => {
  return (props.trace?.decisions || [])
    .map((decision) => {
      const steps = (decision.steps || []).filter(matchesFilters)
      const keepDecision = matchesFilters(decision) || steps.length > 0
      return keepDecision ? { ...decision, steps } : null
    })
    .filter(Boolean) as TraceDecision[]
})
const filteredConflicts = computed(() => {
  const search = searchText.value.trim().toLowerCase()
  return (props.trace?.action_conflicts || []).filter((conflict) => {
    if (selectedDomain.value && selectedDomain.value !== 'action') return false
    if (quickFilter.value && !['problems', 'action_changes'].includes(quickFilter.value)) return false
    if (search && !JSON.stringify(conflict).toLowerCase().includes(search)) return false
    return true
  })
})
const filteredManualWaitLinks = computed(() => {
  const search = searchText.value.trim().toLowerCase()
  return (props.trace?.manual_review_wait_signal_links || []).filter((link) => {
    if (selectedDomain.value && !['review', 'playbook', 'action'].includes(selectedDomain.value)) return false
    if (selectedStatus.value && !JSON.stringify(link).toLowerCase().includes(selectedStatus.value)) return false
    if (quickFilter.value && !['problems', 'action_changes', 'event_driven'].includes(quickFilter.value)) return false
    if (search && !JSON.stringify(link).toLowerCase().includes(search)) return false
    return true
  })
})
const allTraceItems = computed(() => {
  const decisions = props.trace?.decisions || []
  return [
    ...(props.trace?.processing || []),
    ...decisions,
    ...decisions.flatMap((decision) => decision.steps || [])
  ]
})
const counters = computed(() => {
  const items = allTraceItems.value
  return {
    problems: items.filter(isProblem).length,
    executionBlockers: items.filter(isExecutionBlocker).length,
    actionChanges: (props.trace?.decisions || []).filter(isActionChange).length,
    eventDriven: items.filter(isEventDriven).length,
    conflicts: props.trace?.action_conflicts?.length || 0,
    manualWaitLinks: props.trace?.manual_review_wait_signal_links?.length || 0,
    visibleDecisions: filteredDecisions.value.length,
    visibleProcessing: filteredProcessing.value.length
  }
})
const cacheMeta = computed(() => (props.trace?._trace_summary_cache || {}) as Dict)
const decisionPageMeta = computed(() => ((props.trace?.pagination as Dict | undefined)?.decisions || {}) as Dict)
const cacheSource = computed(() => String(cacheMeta.value.source || 'unknown'))
const isLiveFallback = computed(() => cacheSource.value === 'live_fallback')

function cacheLabel() {
  if (cacheSource.value === 'materialized') return 'Materialized cache'
  if (cacheSource.value === 'live_fallback') return 'Live fallback'
  return 'Cache unknown'
}

function cacheClass() {
  if (cacheSource.value === 'materialized') return 'bg-moss text-paper'
  if (cacheSource.value === 'live_fallback') return 'bg-rust text-paper'
  return 'bg-sun text-ink'
}

function setQuickFilter(value: string) {
  quickFilter.value = quickFilter.value === value ? '' : value
}

function clearFilters() {
  selectedDomain.value = ''
  selectedStatus.value = ''
  quickFilter.value = ''
  searchText.value = ''
}

function waitLinkStatus(link: ManualReviewWaitSignalLink) {
  return link.match_status || link.wait_signal_status || link.decision || 'recorded'
}
</script>

<template>
  <section v-if="trace" class="space-y-5">
    <div class="glass-panel rounded-3xl p-5">
      <p class="text-xs font-bold uppercase tracking-[0.3em] text-ink/45">{{ title || 'Decision Trace' }}</p>
      <div class="mt-3 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 class="text-2xl font-black text-ink">{{ trace.symbol || trace.unique_id || 'Trace' }}</h2>
          <p class="mt-1 text-sm text-ink/60">
            {{ trace.raw_counts.processing || 0 }} processing rows,
            {{ trace.raw_counts.traces || 0 }} decisions,
            {{ trace.raw_counts.steps || 0 }} steps,
            {{ trace.raw_counts.action_conflicts || 0 }} conflicts,
            {{ trace.raw_counts.manual_review_wait_signal_links || 0 }} manual waits
          </p>
        </div>
        <div class="flex flex-wrap justify-end gap-2">
          <span class="rounded-full px-3 py-1 text-xs font-black" :class="cacheClass()">{{ cacheLabel() }}</span>
          <span v-if="trace.unique_id" class="rounded-full bg-white px-3 py-1 text-xs font-bold text-ink/65">{{ trace.unique_id }}</span>
        </div>
      </div>
      <div class="mt-4 grid gap-2 text-xs md:grid-cols-5">
        <p class="rounded-xl bg-white/70 px-3 py-2"><b>Cache generated:</b> {{ formatWhen(String(cacheMeta.generated_at || '')) }}</p>
        <p class="rounded-xl bg-white/70 px-3 py-2"><b>Source max:</b> {{ formatWhen(String(cacheMeta.source_max_ts || '')) }}</p>
        <p class="rounded-xl bg-white/70 px-3 py-2"><b>Rows limit:</b> {{ textValue(cacheMeta.limit_rows) }}</p>
        <p class="rounded-xl bg-white/70 px-3 py-2"><b>Shown:</b> {{ textValue(decisionPageMeta.returned_count) }} / {{ textValue(decisionPageMeta.total_count) }}</p>
        <p class="rounded-xl bg-white/70 px-3 py-2"><b>Entity:</b> {{ textValue(cacheMeta.entity_type) }} / {{ textValue(cacheMeta.entity_key) }}</p>
      </div>
      <div v-if="isLiveFallback" class="mt-4 rounded-2xl bg-rust/10 p-4">
        <p class="text-sm font-black text-rust">This trace was built live because the materialized summary cache was missing.</p>
        <p class="mt-1 text-sm leading-6 text-ink/65">Run “Rebuild Trace Summaries” from Operations to make this trace load through the fast path.</p>
        <NuxtLink class="mt-3 inline-flex rounded-full bg-ink px-4 py-2 text-sm font-black text-paper" to="/operations">
          Open Operations
        </NuxtLink>
      </div>
    </div>

    <div class="glass-panel rounded-3xl p-5">
      <div class="grid gap-3 md:grid-cols-5">
        <div class="rounded-2xl bg-white/70 p-3">
          <p class="text-xs font-bold uppercase tracking-[0.2em] text-ink/45">Problems</p>
          <p class="mt-1 text-2xl font-black">{{ counters.problems }}</p>
        </div>
        <div class="rounded-2xl bg-white/70 p-3">
          <p class="text-xs font-bold uppercase tracking-[0.2em] text-ink/45">Exec Blockers</p>
          <p class="mt-1 text-2xl font-black">{{ counters.executionBlockers }}</p>
        </div>
        <div class="rounded-2xl bg-white/70 p-3">
          <p class="text-xs font-bold uppercase tracking-[0.2em] text-ink/45">Action Changes</p>
          <p class="mt-1 text-2xl font-black">{{ counters.actionChanges }}</p>
        </div>
        <div class="rounded-2xl bg-white/70 p-3">
          <p class="text-xs font-bold uppercase tracking-[0.2em] text-ink/45">Event Driven</p>
          <p class="mt-1 text-2xl font-black">{{ counters.eventDriven }}</p>
        </div>
        <div class="rounded-2xl bg-white/70 p-3">
          <p class="text-xs font-bold uppercase tracking-[0.2em] text-ink/45">Conflicts</p>
          <p class="mt-1 text-2xl font-black">{{ counters.conflicts }}</p>
        </div>
      </div>

      <div class="mt-5 grid gap-3 lg:grid-cols-[1fr_0.7fr_0.7fr_auto]">
        <input v-model="searchText" class="rounded-2xl border border-black/10 bg-white/80 px-4 py-3 text-sm outline-none focus:border-moss" placeholder="Search symbol, reason, action, status..." />
        <select v-model="selectedDomain" class="rounded-2xl border border-black/10 bg-white/80 px-4 py-3 text-sm outline-none focus:border-moss">
          <option value="">All domains</option>
          <option v-for="domain in domainOptions" :key="domain" :value="domain">{{ domainTitle(domain) }}</option>
        </select>
        <select v-model="selectedStatus" class="rounded-2xl border border-black/10 bg-white/80 px-4 py-3 text-sm outline-none focus:border-moss">
          <option value="">All statuses</option>
          <option v-for="status in statusOptions" :key="status" :value="status">{{ status }}</option>
        </select>
        <button class="rounded-full bg-white px-5 py-3 text-sm font-black text-ink" type="button" @click="clearFilters">Clear</button>
      </div>

      <div class="mt-4 flex flex-wrap gap-2">
        <button class="rounded-full px-4 py-2 text-sm font-bold" :class="quickFilter === 'problems' ? 'bg-ember text-white' : 'bg-white text-ink'" type="button" @click="setQuickFilter('problems')">Show problems only</button>
        <button class="rounded-full px-4 py-2 text-sm font-bold" :class="quickFilter === 'execution_blockers' ? 'bg-ember text-white' : 'bg-white text-ink'" type="button" @click="setQuickFilter('execution_blockers')">Execution blockers</button>
        <button class="rounded-full px-4 py-2 text-sm font-bold" :class="quickFilter === 'action_changes' ? 'bg-moss text-white' : 'bg-white text-ink'" type="button" @click="setQuickFilter('action_changes')">Action changes</button>
        <button class="rounded-full px-4 py-2 text-sm font-bold" :class="quickFilter === 'event_driven' ? 'bg-sky text-white' : 'bg-white text-ink'" type="button" @click="setQuickFilter('event_driven')">Event-driven changes</button>
      </div>
      <p v-if="syncUrl" class="mt-3 text-xs font-semibold text-ink/45">Filters and copied trace links are stored in the URL for reloads and sharing.</p>
    </div>

    <div v-if="trace.manual_review_wait_signal_links?.length" class="glass-panel rounded-3xl p-5">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p class="text-xs font-bold uppercase tracking-[0.24em] text-ink/45">Manual Review Waits</p>
          <h3 class="mt-1 text-lg font-black">Decision to Wait Signal Links</h3>
        </div>
        <span class="rounded-full bg-sun px-3 py-1 text-xs font-black text-ink">{{ counters.manualWaitLinks }}</span>
      </div>
      <div class="mt-4 grid gap-3 md:grid-cols-2">
        <article v-for="link in filteredManualWaitLinks" :key="`${link.manual_review_item_id || 'manual'}-${link.signal_id || 'signal'}-${link.match_source_key || 'pending'}`" class="rounded-2xl border border-sun/40 bg-white/75 p-4">
          <div class="flex flex-wrap items-center gap-2">
            <span class="rounded-full bg-ink px-3 py-1 text-xs font-black text-paper">{{ link.decision || 'manual decision' }}</span>
            <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(waitLinkStatus(link))">{{ waitLinkStatus(link) }}</span>
            <span v-if="link.expected_action" class="rounded-full bg-sun/20 px-3 py-1 text-xs font-black text-ink">{{ link.expected_action }}</span>
          </div>
          <p v-if="link.rationale" class="mt-3 text-sm leading-6 text-ink/70">{{ link.rationale }}</p>
          <p v-if="link.wait_question || link.follow_up_event" class="mt-2 rounded-2xl bg-paper/70 p-3 text-sm font-semibold text-ink/70">
            {{ link.wait_question || link.follow_up_event }}
          </p>
          <div class="mt-3 grid gap-2 text-xs md:grid-cols-2">
            <p class="rounded-xl bg-white px-3 py-2"><b>Manual item:</b> {{ textValue(link.manual_review_item_id) }}</p>
            <p class="rounded-xl bg-white px-3 py-2"><b>Wait signal:</b> {{ textValue(link.signal_id) }}</p>
            <p class="rounded-xl bg-white px-3 py-2"><b>Decided:</b> {{ formatWhen(link.decided_at) }}</p>
            <p class="rounded-xl bg-white px-3 py-2"><b>Matched:</b> {{ formatWhen(link.matched_at) }}</p>
            <p class="rounded-xl bg-white px-3 py-2"><b>Condition:</b> {{ textValue(link.signal_type || link.condition?.condition_type) }}</p>
            <p class="rounded-xl bg-white px-3 py-2"><b>Evidence:</b> {{ textValue(link.match_source_table) }} / {{ textValue(link.match_source_key) }}</p>
          </div>
          <p v-if="link.match_reason" class="mt-3 rounded-2xl bg-moss/10 p-3 text-sm font-semibold text-moss">{{ link.match_reason }}</p>
        </article>
        <p v-if="!filteredManualWaitLinks.length" class="rounded-2xl bg-white/60 p-4 text-sm text-ink/55">No manual-review wait links match the current filters.</p>
      </div>
    </div>

    <div class="grid gap-5 lg:grid-cols-[0.9fr_1.1fr]">
      <div class="glass-panel rounded-3xl p-5">
        <h3 class="text-lg font-black">Processing Stages</h3>
        <div class="mt-4 space-y-3">
          <article v-for="(stage, idx) in filteredProcessing" :id="processingAnchor(stage, idx)" :key="`${stage.stage}-${idx}`" class="scroll-mt-6 rounded-2xl border p-4" :class="domainClass(stage.domain)">
            <div class="flex items-start justify-between gap-3">
              <div>
                <p class="text-xs font-bold uppercase tracking-[0.22em] text-ink/45">{{ domainTitle(stage.domain) }}</p>
                <p class="mt-1 font-black text-ink">{{ stage.stage }}</p>
                <p class="mt-1 text-xs text-ink/55">{{ formatWhen(stage.completed_at || stage.started_at) }}</p>
              </div>
              <div class="flex flex-wrap justify-end gap-2">
                <button v-if="syncUrl" class="rounded-full bg-white px-3 py-1 text-xs font-black text-ink/60" type="button" @click="copyAnchor(processingAnchor(stage, idx))">Copy link</button>
                <span class="rounded-full px-3 py-1 text-xs font-bold" :class="statusClass(stage.status)">{{ stage.status }}</span>
              </div>
            </div>
            <p v-if="stage.error" class="mt-3 rounded-2xl bg-ember/10 p-3 text-sm font-semibold text-ember">{{ stage.error }}</p>
            <p v-if="stage.source_type" class="mt-2 text-sm text-ink/60">Source: {{ stage.source_type }}</p>
            <div v-if="stage.domain === 'event' && hasAny(payloadOf(stage), ['event_class', 'direction', 'materiality', 'confidence'])" class="mt-3 grid gap-2 text-sm md:grid-cols-2">
              <p class="rounded-2xl bg-white/75 px-3 py-2"><b>Class:</b> {{ textValue(payloadOf(stage).event_class) }}</p>
              <p class="rounded-2xl bg-white/75 px-3 py-2"><b>Direction:</b> {{ textValue(payloadOf(stage).direction) }}</p>
              <p class="rounded-2xl bg-white/75 px-3 py-2"><b>Materiality:</b> {{ textValue(payloadOf(stage).materiality) }}</p>
              <p class="rounded-2xl bg-white/75 px-3 py-2"><b>Confidence:</b> {{ textValue(payloadOf(stage).confidence) }}</p>
            </div>
            <details v-if="stage.payload && Object.keys(stage.payload).length" class="mt-3">
              <summary class="cursor-pointer text-xs font-bold text-moss">Stage details</summary>
              <pre class="mt-2 max-h-56 overflow-auto rounded-2xl bg-ink p-3 text-xs leading-5 text-paper">{{ JSON.stringify(stage.payload, null, 2) }}</pre>
            </details>
          </article>
          <p v-if="!filteredProcessing.length" class="rounded-2xl bg-white/60 p-4 text-sm text-ink/55">No processing stages match the current filters.</p>
        </div>
      </div>

      <div class="glass-panel rounded-3xl p-5">
        <h3 class="text-lg font-black">Decision Timeline</h3>
        <div class="mt-4 space-y-4">
          <article v-for="decision in filteredDecisions" :id="decisionAnchor(decision)" :key="decision.trace_id" class="scroll-mt-6 rounded-2xl border p-4" :class="domainClass(decision.domain)">
            <div class="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p class="text-xs font-bold uppercase tracking-[0.22em] text-ink/45">{{ domainTitle(decision.domain) }}</p>
                <p class="mt-1 font-black text-ink">{{ decision.trigger_type }}</p>
                <p class="mt-1 text-xs text-ink/55">{{ formatWhen(decision.updated_at || decision.asof_date) }}</p>
              </div>
              <div class="flex flex-wrap justify-end gap-2">
                <button v-if="syncUrl" class="rounded-full bg-white px-3 py-1 text-xs font-black text-ink/60" type="button" @click="copyAnchor(decisionAnchor(decision))">Copy link</button>
                <span class="rounded-full px-3 py-1 text-xs font-bold" :class="statusClass(decision.final_action)">{{ decision.final_action || 'TRACE' }}</span>
              </div>
            </div>
            <p v-if="decision.final_reason" class="mt-3 text-sm leading-6 text-ink/70">{{ decision.final_reason }}</p>
            <p v-if="payloadOf(decision).manual_revision_summary" class="mt-3 rounded-2xl bg-white/75 p-3 text-sm text-ink/70">
              <b>Manual revision:</b> {{ payloadOf(decision).manual_revision_summary }}
            </p>
            <p v-if="payloadOf(decision).reason_contract_status" class="mt-3 rounded-2xl bg-white/75 p-3 text-sm text-ink/70">
              <b>Reason contract:</b> {{ payloadOf(decision).reason_contract_status }}
            </p>
            <ReasonContractPanel
              v-if="payloadOf(decision).recommendation_reason || payloadOf(decision).reason_contract_status"
              class="mt-3"
              :contract="payloadOf(decision).recommendation_reason"
              :status="payloadOf(decision).reason_contract_status"
              compact
            />
            <div class="mt-3 grid gap-2 text-sm md:grid-cols-3">
              <p v-if="decision.previous_action" class="rounded-2xl bg-white px-3 py-2"><b>Previous:</b> {{ decision.previous_action }}</p>
              <p v-if="decision.new_action" class="rounded-2xl bg-white px-3 py-2"><b>New:</b> {{ decision.new_action }}</p>
              <p v-if="decision.setup_id" class="rounded-2xl bg-white px-3 py-2"><b>Setup:</b> {{ decision.setup_id }}</p>
            </div>
            <div v-if="decision.steps.length" class="mt-4 border-l-2 border-moss/30 pl-4">
              <div v-for="step in decision.steps" :id="stepAnchor(decision, step)" :key="`${decision.trace_id}-${step.step_idx}-${step.stage}`" class="mb-3 scroll-mt-6 rounded-2xl border p-4" :class="domainClass(step.domain)">
                <div class="flex flex-wrap items-center justify-between gap-2">
                  <div class="flex flex-wrap items-center gap-2">
                  <span class="text-xs font-bold uppercase tracking-[0.22em] text-ink/45">{{ domainTitle(step.domain) }}</span>
                  <span class="text-sm font-black text-ink">{{ step.stage }}</span>
                  <span class="rounded-full px-2 py-0.5 text-[11px] font-bold" :class="statusClass(step.status)">{{ step.status }}</span>
                  </div>
                  <button v-if="syncUrl" class="rounded-full bg-white px-2.5 py-1 text-xs font-black text-ink/50" type="button" @click="copyAnchor(stepAnchor(decision, step))">#</button>
                </div>
                <p v-if="step.reason" class="mt-1 text-sm text-ink/65">{{ step.reason }}</p>

                <div v-if="step.domain === 'technical'" class="mt-3 grid gap-2 text-sm md:grid-cols-3">
                  <p v-if="payloadOf(step).technical_state" class="rounded-2xl bg-white/75 px-3 py-2"><b>State:</b> {{ payloadOf(step).technical_state }}</p>
                  <p v-if="payloadOf(step).technical_score || payloadOf(step).technical_total_score" class="rounded-2xl bg-white/75 px-3 py-2"><b>Score:</b> {{ textValue(payloadOf(step).technical_score || payloadOf(step).technical_total_score) }}</p>
                  <p v-if="payloadOf(step).technical_trigger_type" class="rounded-2xl bg-white/75 px-3 py-2"><b>Trigger:</b> {{ payloadOf(step).technical_trigger_type }}</p>
                  <p v-if="payloadOf(step).stop_price" class="rounded-2xl bg-white/75 px-3 py-2"><b>Stop:</b> {{ textValue(payloadOf(step).stop_price) }}</p>
                  <p v-if="payloadOf(step).invalidation_price" class="rounded-2xl bg-white/75 px-3 py-2"><b>Invalidation:</b> {{ textValue(payloadOf(step).invalidation_price) }}</p>
                </div>

                <div v-else-if="step.domain === 'event'" class="mt-3 grid gap-2 text-sm md:grid-cols-3">
                  <p v-if="payloadOf(step).event_class" class="rounded-2xl bg-white/75 px-3 py-2"><b>Class:</b> {{ payloadOf(step).event_class }}</p>
                  <p v-if="payloadOf(step).direction" class="rounded-2xl bg-white/75 px-3 py-2"><b>Direction:</b> {{ payloadOf(step).direction }}</p>
                  <p v-if="payloadOf(step).materiality" class="rounded-2xl bg-white/75 px-3 py-2"><b>Materiality:</b> {{ payloadOf(step).materiality }}</p>
                  <p v-if="payloadOf(step).surprise !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Surprise:</b> {{ textValue(payloadOf(step).surprise) }}</p>
                  <p v-if="payloadOf(step).novelty !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Novelty:</b> {{ textValue(payloadOf(step).novelty) }}</p>
                  <p v-if="payloadOf(step).confidence !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Confidence:</b> {{ textValue(payloadOf(step).confidence) }}</p>
                  <p v-if="payloadOf(step).expected_decay_days" class="rounded-2xl bg-white/75 px-3 py-2"><b>Decay:</b> {{ payloadOf(step).expected_decay_days }} days</p>
                  <p v-if="payloadOf(step).state_transition_hint" class="rounded-2xl bg-white/75 px-3 py-2"><b>Hint:</b> {{ payloadOf(step).state_transition_hint }}</p>
                </div>

                <div v-else-if="step.domain === 'review'" class="mt-3 grid gap-2 text-sm md:grid-cols-3">
                  <p v-if="payloadOf(step).review_action" class="rounded-2xl bg-white/75 px-3 py-2"><b>Action:</b> {{ payloadOf(step).review_action }}</p>
                  <p v-if="payloadOf(step).review_score !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Score:</b> {{ textValue(payloadOf(step).review_score) }}</p>
                  <p v-if="payloadOf(step).veto !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Veto:</b> {{ textValue(payloadOf(step).veto) }}</p>
                </div>

                <div v-else-if="step.domain === 'playbook'" class="mt-3 grid gap-2 text-sm md:grid-cols-3">
                  <p v-if="payloadOf(step).playbook_id" class="rounded-2xl bg-white/75 px-3 py-2"><b>Playbook:</b> {{ payloadOf(step).playbook_id }}</p>
                  <p v-if="payloadOf(step).source_action" class="rounded-2xl bg-white/75 px-3 py-2"><b>Plan:</b> {{ payloadOf(step).source_action }}</p>
                  <p v-if="payloadOf(step).mapped_action" class="rounded-2xl bg-white/75 px-3 py-2"><b>Overlay:</b> {{ payloadOf(step).mapped_action }}</p>
                  <p v-if="payloadOf(step).confidence !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Confidence:</b> {{ pctValue(payloadOf(step).confidence) }}</p>
                  <p v-if="payloadOf(step).execution_mode" class="rounded-2xl bg-white/75 px-3 py-2"><b>Execution:</b> {{ payloadOf(step).execution_mode }}</p>
                  <p v-if="payloadOf(step).production_allowed !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Production:</b> {{ textValue(payloadOf(step).production_allowed) }}</p>
                </div>
                <p v-if="step.domain === 'playbook' && payloadOf(step).operator_summary" class="mt-3 rounded-2xl bg-white/75 p-3 text-sm text-ink/70">
                  {{ payloadOf(step).operator_summary }}
                </p>
                <p v-if="step.domain === 'playbook' && payloadOf(step).review_boundary" class="mt-3 rounded-2xl bg-sun/15 p-3 text-sm font-semibold text-ink/70">
                  {{ payloadOf(step).review_boundary }}
                </p>

                <div v-if="step.domain === 'action'" class="mt-3 grid gap-2 text-sm md:grid-cols-3">
                  <p v-if="payloadOf(step).winner_action" class="rounded-2xl bg-white/75 px-3 py-2"><b>Winner:</b> {{ payloadOf(step).winner_action }}</p>
                  <p v-if="payloadOf(step).winner_source" class="rounded-2xl bg-white/75 px-3 py-2"><b>Source:</b> {{ payloadOf(step).winner_source }}</p>
                  <p v-if="payloadOf(step).conflict_count !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Conflicts:</b> {{ payloadOf(step).conflict_count }}</p>
                  <p v-if="payloadOf(step).reason_contract_status" class="rounded-2xl bg-white/75 px-3 py-2"><b>Reason:</b> {{ payloadOf(step).reason_contract_status }}</p>
                </div>
                <p v-if="step.domain === 'action' && payloadOf(step).manual_revision_summary" class="mt-3 rounded-2xl bg-white/75 p-3 text-sm text-ink/70">
                  {{ payloadOf(step).manual_revision_summary }}
                </p>
                <ReasonContractPanel
                  v-if="step.domain === 'action' && (payloadOf(step).recommendation_reason || payloadOf(step).reason_contract_status)"
                  class="mt-3"
                  :contract="payloadOf(step).recommendation_reason"
                  :status="payloadOf(step).reason_contract_status"
                  compact
                />

                <div v-if="step.domain === 'lifecycle'" class="mt-3 grid gap-2 text-sm md:grid-cols-3">
                  <p v-if="payloadOf(step).position_status" class="rounded-2xl bg-white/75 px-3 py-2"><b>Position:</b> {{ payloadOf(step).position_status }}</p>
                  <p v-if="payloadOf(step).next_action || payloadOf(step).suggested_action" class="rounded-2xl bg-white/75 px-3 py-2"><b>Next:</b> {{ payloadOf(step).next_action || payloadOf(step).suggested_action }}</p>
                  <p v-if="payloadOf(step).pnl_pct !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>P/L:</b> {{ pctValue(payloadOf(step).pnl_pct) }}</p>
                  <p v-if="payloadOf(step).days_held !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Days held:</b> {{ payloadOf(step).days_held }}</p>
                  <p v-if="payloadOf(step).recommended_stop_price" class="rounded-2xl bg-white/75 px-3 py-2"><b>Stop:</b> {{ textValue(payloadOf(step).recommended_stop_price) }}</p>
                  <p v-if="payloadOf(step).recommended_target_price" class="rounded-2xl bg-white/75 px-3 py-2"><b>Target:</b> {{ textValue(payloadOf(step).recommended_target_price) }}</p>
                  <p v-if="payloadOf(step).action_fraction" class="rounded-2xl bg-white/75 px-3 py-2"><b>Fraction:</b> {{ pctValue(Number(payloadOf(step).action_fraction) * 100) }}</p>
                </div>

                <div v-else-if="step.domain === 'risk'" class="mt-3 grid gap-2 text-sm md:grid-cols-3">
                  <p v-if="payloadOf(step).allocation_status" class="rounded-2xl bg-white/75 px-3 py-2"><b>Status:</b> {{ payloadOf(step).allocation_status }}</p>
                  <p v-if="payloadOf(step).risk_bucket" class="rounded-2xl bg-white/75 px-3 py-2"><b>Risk:</b> {{ payloadOf(step).risk_bucket }}</p>
                  <p v-if="payloadOf(step).conviction_bucket" class="rounded-2xl bg-white/75 px-3 py-2"><b>Conviction:</b> {{ payloadOf(step).conviction_bucket }}</p>
                  <p v-if="payloadOf(step).suggested_allocation_inr !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Suggested:</b> INR {{ textValue(payloadOf(step).suggested_allocation_inr) }}</p>
                  <p v-if="payloadOf(step).allocation_pct_of_adv20d !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>ADV use:</b> {{ pctValue(Number(payloadOf(step).allocation_pct_of_adv20d) * 100) }}</p>
                  <p v-if="payloadOf(step).stop_price" class="rounded-2xl bg-white/75 px-3 py-2"><b>Stop:</b> {{ textValue(payloadOf(step).stop_price) }}</p>
                  <p v-if="payloadOf(step).invalidation_price" class="rounded-2xl bg-white/75 px-3 py-2"><b>Invalidation:</b> {{ textValue(payloadOf(step).invalidation_price) }}</p>
                  <p v-if="payloadOf(step).review_action" class="rounded-2xl bg-white/75 px-3 py-2"><b>Review:</b> {{ payloadOf(step).review_action }}</p>
                </div>

                <div v-else-if="step.domain === 'portfolio'" class="mt-3 grid gap-2 text-sm md:grid-cols-3">
                  <p v-if="payloadOf(step).portfolio_status" class="rounded-2xl bg-white/75 px-3 py-2"><b>Status:</b> {{ payloadOf(step).portfolio_status }}</p>
                  <p v-if="payloadOf(step).plan_rank !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Rank:</b> {{ payloadOf(step).plan_rank }}</p>
                  <p v-if="payloadOf(step).invest_score_pct !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Invest score:</b> {{ pctValue(payloadOf(step).invest_score_pct) }}</p>
                  <p v-if="payloadOf(step).requested_allocation_inr !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Requested:</b> INR {{ textValue(payloadOf(step).requested_allocation_inr) }}</p>
                  <p v-if="payloadOf(step).approved_allocation_inr !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Approved:</b> INR {{ textValue(payloadOf(step).approved_allocation_inr) }}</p>
                  <p v-if="payloadOf(step).remaining_capital_after_inr !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Remaining:</b> INR {{ textValue(payloadOf(step).remaining_capital_after_inr) }}</p>
                  <p v-if="payloadOf(step).overlap_reason" class="rounded-2xl bg-white/75 px-3 py-2"><b>Overlap:</b> {{ payloadOf(step).overlap_reason }}</p>
                  <p v-if="payloadOf(step).thesis_bucket" class="rounded-2xl bg-white/75 px-3 py-2"><b>Bucket:</b> {{ payloadOf(step).thesis_bucket }}</p>
                  <p v-if="payloadOf(step).expected_horizon_days" class="rounded-2xl bg-white/75 px-3 py-2"><b>Horizon:</b> {{ payloadOf(step).expected_horizon_days }} days</p>
                </div>

                <div v-else-if="step.domain === 'macro'" class="mt-3 grid gap-2 text-sm md:grid-cols-3">
                  <p v-if="payloadOf(step).macro_risk_state" class="rounded-2xl bg-white/75 px-3 py-2"><b>Risk state:</b> {{ payloadOf(step).macro_risk_state }}</p>
                  <p v-if="payloadOf(step).macro_stress_score !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Stress:</b> {{ textValue(payloadOf(step).macro_stress_score) }}</p>
                  <p v-if="payloadOf(step).macro_sizing_multiplier !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Sizing multiplier:</b> {{ textValue(payloadOf(step).macro_sizing_multiplier) }}</p>
                  <p v-if="payloadOf(step).macro_asof_date" class="rounded-2xl bg-white/75 px-3 py-2"><b>As of:</b> {{ formatWhen(String(payloadOf(step).macro_asof_date)) }}</p>
                </div>

                <div v-else-if="step.domain === 'exchange'" class="mt-3 grid gap-2 text-sm md:grid-cols-3">
                  <p v-if="payloadOf(step).exchange_event_score !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Event score:</b> {{ textValue(payloadOf(step).exchange_event_score) }}</p>
                  <p v-if="payloadOf(step).exchange_accumulation_score !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Accumulation:</b> {{ textValue(payloadOf(step).exchange_accumulation_score) }}</p>
                  <p v-if="payloadOf(step).exchange_distribution_score !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Distribution:</b> {{ textValue(payloadOf(step).exchange_distribution_score) }}</p>
                  <p v-if="payloadOf(step).deal_cluster_count_20d !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Deal clusters:</b> {{ payloadOf(step).deal_cluster_count_20d }}</p>
                  <p v-if="payloadOf(step).insider_net_value_90d !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Insider net:</b> {{ textValue(payloadOf(step).insider_net_value_90d) }}</p>
                  <p v-if="payloadOf(step).short_selling_event_count_20d !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Short events:</b> {{ payloadOf(step).short_selling_event_count_20d }}</p>
                  <p v-if="payloadOf(step).upcoming_earnings_14d !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Earnings soon:</b> {{ textValue(payloadOf(step).upcoming_earnings_14d) }}</p>
                  <p v-if="payloadOf(step).exchange_asof_date" class="rounded-2xl bg-white/75 px-3 py-2"><b>As of:</b> {{ formatWhen(String(payloadOf(step).exchange_asof_date)) }}</p>
                </div>

                <div v-else-if="step.domain === 'execution'" class="mt-3 grid gap-2 text-sm md:grid-cols-3">
                  <p v-if="payloadOf(step).execution_status" class="rounded-2xl bg-white/75 px-3 py-2"><b>Status:</b> {{ payloadOf(step).execution_status }}</p>
                  <p v-if="payloadOf(step).transaction_type" class="rounded-2xl bg-white/75 px-3 py-2"><b>Side:</b> {{ payloadOf(step).transaction_type }}</p>
                  <p v-if="payloadOf(step).quantity !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Qty:</b> {{ payloadOf(step).quantity }}</p>
                  <p v-if="payloadOf(step).filled_quantity !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Filled:</b> {{ payloadOf(step).filled_quantity }}</p>
                  <p v-if="payloadOf(step).estimated_order_value_inr !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Value:</b> INR {{ textValue(payloadOf(step).estimated_order_value_inr) }}</p>
                  <p v-if="payloadOf(step).reference_price !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Reference:</b> {{ textValue(payloadOf(step).reference_price) }}</p>
                  <p v-if="payloadOf(step).reference_price_source" class="rounded-2xl bg-white/75 px-3 py-2"><b>Price source:</b> {{ payloadOf(step).reference_price_source }}</p>
                  <p v-if="payloadOf(step).order_type" class="rounded-2xl bg-white/75 px-3 py-2"><b>Order:</b> {{ payloadOf(step).product_type || '-' }} / {{ payloadOf(step).order_type }}</p>
                  <p v-if="payloadOf(step).security_id" class="rounded-2xl bg-white/75 px-3 py-2"><b>Dhan id:</b> {{ payloadOf(step).security_id }}</p>
                  <p v-if="payloadOf(step).live_mode !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Live mode:</b> {{ textValue(payloadOf(step).live_mode) }}</p>
                  <p v-if="payloadOf(step).broker_order_id" class="rounded-2xl bg-white/75 px-3 py-2"><b>Broker order:</b> {{ payloadOf(step).broker_order_id }}</p>
                  <p v-if="payloadOf(step).broker_order_status" class="rounded-2xl bg-white/75 px-3 py-2"><b>Broker status:</b> {{ payloadOf(step).broker_order_status }}</p>
                </div>
                <div v-if="step.domain === 'execution' && payloadOf(step).safety_checks && typeof payloadOf(step).safety_checks === 'object'" class="mt-3 rounded-2xl bg-white/75 p-3 text-sm">
                  <p class="font-black text-ink">Safety checks</p>
                  <div class="mt-2 grid gap-2 md:grid-cols-3">
                    <p v-for="[key, value] in objectEntries(payloadOf(step).safety_checks)" :key="String(key)" class="rounded-xl bg-paper/70 px-3 py-2">
                      <b>{{ key }}:</b> {{ textValue(value) }}
                    </p>
                  </div>
                </div>
                <p v-if="step.domain === 'execution' && payloadOf(step).execution_reason" class="mt-3 rounded-2xl bg-ember/10 p-3 text-sm font-semibold text-ember">
                  {{ payloadOf(step).execution_reason }}
                </p>

                <div v-if="step.domain === 'action'" class="mt-3 grid gap-2 text-sm md:grid-cols-3">
                  <p v-if="payloadOf(step).winner_action" class="rounded-2xl bg-white/75 px-3 py-2"><b>Winner:</b> {{ payloadOf(step).winner_action }}</p>
                  <p v-if="payloadOf(step).winner_source" class="rounded-2xl bg-white/75 px-3 py-2"><b>Source:</b> {{ payloadOf(step).winner_source }}</p>
                  <p v-if="payloadOf(step).candidate_count !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Candidates:</b> {{ payloadOf(step).candidate_count }}</p>
                  <p v-if="payloadOf(step).conflict_count !== undefined" class="rounded-2xl bg-white/75 px-3 py-2"><b>Conflicts:</b> {{ payloadOf(step).conflict_count }}</p>
                </div>
                <details v-if="step.payload && Object.keys(step.payload).length" class="mt-2">
                  <summary class="cursor-pointer text-xs font-bold text-moss">Evidence values</summary>
                  <pre class="mt-2 max-h-56 overflow-auto rounded-2xl bg-ink p-3 text-xs leading-5 text-paper">{{ JSON.stringify(step.payload, null, 2) }}</pre>
                </details>
              </div>
            </div>
            <details v-if="decision.payload && Object.keys(decision.payload).length" class="mt-3">
              <summary class="cursor-pointer text-xs font-bold text-moss">Decision payload</summary>
              <pre class="mt-2 max-h-56 overflow-auto rounded-2xl bg-ink p-3 text-xs leading-5 text-paper">{{ JSON.stringify(decision.payload, null, 2) }}</pre>
            </details>
          </article>
          <p v-if="!filteredDecisions.length" class="rounded-2xl bg-white/60 p-4 text-sm text-ink/55">No decision rows match the current filters.</p>
        </div>
      </div>
    </div>

    <div class="glass-panel rounded-3xl p-5">
      <h3 class="text-lg font-black">Action Conflicts</h3>
      <div class="mt-4 grid gap-3 md:grid-cols-2">
        <article v-for="(conflict, idx) in filteredConflicts" :key="idx" class="rounded-2xl border border-black/10 bg-white/75 p-4">
          <div class="flex flex-wrap items-center gap-2">
            <span class="rounded-full bg-moss px-3 py-1 text-xs font-bold text-white">{{ conflict.winning_action_code }}</span>
            <span class="text-xs font-bold text-ink/45">beat</span>
            <span class="rounded-full bg-ember px-3 py-1 text-xs font-bold text-white">{{ conflict.losing_action_code }}</span>
          </div>
          <p class="mt-3 text-sm text-ink/65">{{ conflict.lost_reason }}</p>
          <div class="mt-3 flex flex-wrap gap-2">
            <span
              v-if="conflict.resolution_status"
              class="rounded-full px-3 py-1 text-xs font-black"
              :class="conflict.requires_manual_resolution ? 'bg-sun text-ink' : 'bg-moss/10 text-moss'"
            >
              {{ conflict.resolution_status }}
            </span>
            <span v-if="conflict.resolution_rule_id" class="rounded-full bg-white px-3 py-1 text-xs font-black text-ink/55">
              {{ conflict.resolution_rule_id }}
            </span>
          </div>
          <p v-if="conflict.resolution_reason" class="mt-2 rounded-2xl bg-paper/70 p-3 text-xs leading-5 text-ink/60">
            {{ conflict.resolution_reason }}
          </p>
          <p class="mt-2 text-xs text-ink/45">{{ formatWhen(conflict.asof_date) }} · {{ conflict.losing_source }}</p>
        </article>
        <p v-if="!filteredConflicts.length" class="rounded-2xl bg-white/60 p-4 text-sm text-ink/55">No action conflicts match the current filters.</p>
      </div>
    </div>
  </section>
</template>
