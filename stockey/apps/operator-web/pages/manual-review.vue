<script setup lang="ts">
import type { Dict } from '~/types/api'

const api = useOperatorApi()
const { data, refresh, pending, error: loadError } = await useAsyncData('manual-review', () => api.getManualReview(150))

const selectedLane = ref('investment_review')
const selectedType = ref('all')
const selectedSeverity = ref('all')
const decisionByItem = ref<Record<string, string>>({})
const rationaleByItem = ref<Record<string, string>>({})
const followUpByItem = ref<Record<string, string>>({})
const operatorId = ref('operator')
const savingItemId = ref('')
const saveError = ref('')
const saveSuccess = ref('')
const decisionOptions = [
  {
    key: 'needs_more_data',
    label: 'Needs more data',
    closes: false,
    effect: 'Keeps this item in manual review with your note. Use when the system or you need more evidence before a decision.',
    useFor: 'Unclear investment evidence, incomplete context, stale price/news, or unanswered operator questions.'
  },
  {
    key: 'watch_for_event',
    label: 'Watch for event',
    closes: false,
    effect: 'Keeps this item active, records the future evidence to wait for, and creates an active wait signal for watchers/signal refresh. It does not add the stock to portfolio by itself.',
    useFor: 'You know the exact trigger needed: management clarification, price reaction, support break, result update, or follow-up announcement.'
  },
  {
    key: 'add_operator_note',
    label: 'Add note',
    closes: false,
    effect: 'Adds context only. It does not close the item and does not change portfolio, model, screener, or execution behavior.',
    useFor: 'Your research notes, links, or interpretation that should help a later decision.'
  },
  {
    key: 'approve_for_manual_config',
    label: 'Approve manual config',
    closes: true,
    effect: 'Closes this review as approved for a later manual config/code change. It does not put the stock into portfolio and does not trade.',
    useFor: 'Research/config/threshold items where you approve the proposed change, but implementation remains separate.'
  },
  {
    key: 'ignore',
    label: 'Ignore',
    closes: true,
    effect: 'Closes this item as not worth further review. It does not create a no-action signal beyond this manual-review queue.',
    useFor: 'Noise, low-materiality news, duplicate rows, or stale context.'
  },
  {
    key: 'downgrade_to_no_action',
    label: 'Downgrade to no action',
    closes: true,
    effect: 'Closes this item and records that your decision is explicitly no action. It removes the item from the active Manual Review queue but does not sell/buy anything.',
    useFor: 'Investment review where the evidence is weak, already priced in, contradictory, or not actionable.'
  },
  {
    key: 'mark_fixed',
    label: 'Mark fixed',
    closes: true,
    effect: 'Closes a technical/operational issue after you have fixed it or confirmed a rerun succeeded.',
    useFor: 'Parser failures, OCR/ingest errors, broken URLs, DB/API issues, and other non-investment problems.'
  }
]

const items = computed(() => asList(data.value?.items))
const summary = computed(() => asDict(data.value?.summary))
const byType = computed(() => asDict(summary.value.by_type))
const bySeverity = computed(() => asDict(summary.value.by_severity))
const skippedSources = computed(() => asList(summary.value.skipped_sources))
const sourceWarnings = computed(() => asList(data.value?.source_warnings || summary.value.source_warnings))
const technicalItems = computed(() => items.value.filter((item) => Boolean(item.is_technical_issue) || String(item.review_lane || '') === 'technical_issue'))
const investmentItems = computed(() => items.value.filter((item) => String(item.review_lane || '') === 'investment_review'))
const researchItems = computed(() => items.value.filter((item) => String(item.review_lane || '') === 'research_config'))
const laneFilters = computed(() => [
  { key: 'investment_review', label: 'Investment review', count: investmentItems.value.length },
  { key: 'technical_issue', label: 'Technical issues', count: technicalItems.value.length },
  { key: 'research_config', label: 'Research/config', count: researchItems.value.length },
  { key: 'all', label: 'All lanes', count: items.value.length }
])
const typeFilters = computed(() => [
  { key: 'all', label: 'All', count: items.value.length },
  ...Object.entries(byType.value).map(([key, count]) => ({ key, label: typeLabel(key), count: Number(count || 0) }))
])
const severityFilters = computed(() => [
  { key: 'all', label: 'All', count: items.value.length },
  ...Object.entries(bySeverity.value).map(([key, count]) => ({ key, label: String(key).toUpperCase(), count: Number(count || 0) }))
])
const filteredItems = computed(() => items.value.filter((item) => {
  const laneOk = selectedLane.value === 'all' || String(item.review_lane || '') === selectedLane.value
  const typeOk = selectedType.value === 'all' || String(item.item_type || '') === selectedType.value
  const severityOk = selectedSeverity.value === 'all' || String(item.severity || '') === selectedSeverity.value
  return laneOk && typeOk && severityOk
}))

function asDict(value: unknown): Dict {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Dict : {}
}

function asList(value: unknown): Dict[] {
  return Array.isArray(value) ? value.filter((item) => typeof item === 'object' && item !== null) as Dict[] : []
}

function asStringList(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item || '').trim()).filter(Boolean) : []
}

function display(value: unknown) {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  if (typeof value === 'number') return Intl.NumberFormat('en-IN', { maximumFractionDigits: 3 }).format(value)
  return String(value)
}

function typeLabel(value: unknown) {
  const text = String(value || 'unknown').replaceAll('_', ' ')
  return text.replace(/\b\w/g, (char) => char.toUpperCase())
}

function laneLabel(value: unknown) {
  const lane = String(value || '').toLowerCase()
  if (lane === 'technical_issue') return 'Technical issue'
  if (lane === 'research_config') return 'Research/config'
  return 'Investment review'
}

function laneClass(value: unknown) {
  const lane = String(value || '').toLowerCase()
  if (lane === 'technical_issue') return 'bg-rust text-paper'
  if (lane === 'research_config') return 'bg-ink text-paper'
  return 'bg-moss text-paper'
}

function impactMeta(item: Dict) {
  const itemType = String(item.item_type || '').toLowerCase()
  const lane = String(item.review_lane || '').toLowerCase()
  const raw = asDict(item.raw)
  const actionCode = String(raw.action_code || raw.action_type || raw.source_action || item.status || '').toUpperCase()
  if (itemType === 'execution_blocker') {
    return {
      label: 'Execution-blocking',
      tone: 'bg-rust text-paper',
      boundary: 'Blocks order planning or submission until fixed. Manual Review still does not submit orders.',
      next: 'Fix the execution blocker, verify reconciliation/dry-run status, then close it as fixed.'
    }
  }
  if (lane === 'technical_issue' || Boolean(item.is_technical_issue)) {
    return {
      label: 'Operational',
      tone: 'bg-rust text-paper',
      boundary: 'Affects pipeline/data trust, not investment intent or broker state.',
      next: 'Fix the upstream issue, rerun the source if needed, then mark fixed after the row is no longer current.'
    }
  }
  if (lane === 'research_config') {
    return {
      label: 'Research-only',
      tone: 'bg-ink text-paper',
      boundary: 'Can approve a later manual config/code change; it does not apply that change here.',
      next: 'Record the review decision, then make any config/code change through the normal manual workflow.'
    }
  }
  if (itemType === 'wait_signal_followup') {
    return {
      label: 'Investment follow-up',
      tone: 'bg-blue-700 text-paper',
      boundary: 'A watched condition matched. This is review-only until a later refresh/advisory pass changes action state.',
      next: 'Decide whether the match is actionable, needs more evidence, or should be closed as noise.'
    }
  }
  if (actionCode.includes('REDUCE') || actionCode.includes('SELL') || actionCode.includes('EXIT')) {
    return {
      label: 'Investment-impacting',
      tone: 'bg-sun text-ink',
      boundary: 'May affect future risk/action review, but this click does not change portfolio or submit orders.',
      next: 'Answer the operator questions and record whether to wait, close as no action, or keep reviewing.'
    }
  }
  return {
    label: 'Investment-impacting',
    tone: 'bg-moss text-paper',
    boundary: 'May affect future advisory state, but this page only records review state.',
    next: 'Record the review decision; future action changes require the normal advisory or signal-refresh flow.'
  }
}

function statusClass(value: unknown) {
  const status = String(value || '').toLowerCase()
  if (status === 'error' || status === 'failed' || status.includes('blocked')) return 'bg-rust text-paper'
  if (status === 'warning' || status === 'review' || status.includes('review')) return 'bg-sun text-ink'
  return 'bg-moss text-paper'
}

function itemAccent(value: unknown) {
  const severity = String(value || '').toLowerCase()
  if (severity === 'error') return 'border-rust/60 bg-rust/5'
  if (severity === 'warning') return 'border-sun/80 bg-sun/10'
  return 'border-moss/40 bg-white/75'
}

function tracePath(item: Dict) {
  if (item.unique_id) return `/decision-trace?unique_id=${encodeURIComponent(String(item.unique_id))}`
  if (item.symbol) return `/decision-trace?symbol=${encodeURIComponent(String(item.symbol))}`
  return ''
}

function itemId(item: Dict) {
  return String(item.item_id || '')
}

function latestDecision(item: Dict): Dict {
  return asDict(item.latest_operator_decision)
}

function selectedDecisionMeta(item: Dict) {
  const selected = decisionByItem.value[itemId(item)] || String(item.suggested_decision || 'needs_more_data')
  return decisionOptions.find((option) => option.key === selected) || decisionOptions[0]
}

function decisionBoundaryMeta(item: Dict) {
  const decision = selectedDecisionMeta(item)
  if (decision.key === 'watch_for_event') {
    return {
      label: 'Creates wait signal',
      tone: 'border-blue-500/25 bg-blue-500/10 text-blue-800',
      summary: 'Keeps this review open and creates a watcher condition from the follow-up event.'
    }
  }
  if (decision.closes) {
    return {
      label: 'Closing decision',
      tone: 'border-rust/25 bg-rust/10 text-rust',
      summary: 'Removes this item from the active Manual Review queue after the decision is saved.'
    }
  }
  return {
    label: 'Annotating decision',
    tone: 'border-sun/40 bg-sun/10 text-ink',
    summary: 'Keeps this item active and adds operator context for the next review pass.'
  }
}

function selectedDecision(item: Dict) {
  return decisionByItem.value[itemId(item)] || String(item.suggested_decision || 'needs_more_data')
}

function operatorQuestions(item: Dict) {
  return asStringList(item.operator_questions)
}

function waitForEvents(item: Dict) {
  return asStringList(item.wait_for_events)
}

function waitSignalFollowup(item: Dict): Dict {
  const raw = asDict(item.raw)
  return asDict(raw.wait_signal_followup)
}

async function submitDecision(item: Dict) {
  const id = itemId(item)
  if (!id) return
  saveError.value = ''
  saveSuccess.value = ''
  const decision = selectedDecision(item)
  const rationale = (rationaleByItem.value[id] || '').trim()
  if (decision !== 'add_operator_note' && !rationale) {
    saveError.value = 'Rationale is required before recording this decision.'
    return
  }
  if (decision === 'watch_for_event' && !(followUpByItem.value[id] || '').trim()) {
    saveError.value = 'Event to wait for is required when choosing Watch for event.'
    return
  }
  savingItemId.value = id
  try {
    const result = await api.decideManualReview({
      item_id: id,
      item,
      decision,
      rationale,
      follow_up_event: followUpByItem.value[id] || '',
      operator_id: operatorId.value || 'operator'
    })
    saveSuccess.value = `${result.decision} recorded for ${id}.`
    decisionByItem.value[id] = String(item.suggested_decision || 'needs_more_data')
    rationaleByItem.value[id] = ''
    followUpByItem.value[id] = ''
    await refresh()
  } catch (err) {
    saveError.value = err instanceof Error ? err.message : String(err)
  } finally {
    savingItemId.value = ''
  }
}
</script>

<template>
  <section class="rounded-[2rem] bg-ink p-8 text-paper shadow-soft">
    <p class="text-sm font-semibold uppercase tracking-[0.35em] text-paper/55">Manual Review</p>
    <div class="mt-4 flex flex-wrap items-end justify-between gap-5">
      <div>
        <h1 class="max-w-4xl text-5xl font-black tracking-tight">One queue for decisions that need human judgment.</h1>
        <p class="mt-4 max-w-3xl text-lg leading-8 text-paper/70">
          Consolidates manual action recommendations, event-policy reviews, conflicts, execution blockers, promotion reviews, and extraction failures.
        </p>
      </div>
      <button class="rounded-full bg-paper px-5 py-3 text-sm font-black text-ink disabled:opacity-50" :disabled="pending" type="button" @click="refresh()">
        {{ pending ? 'Refreshing...' : 'Refresh queue' }}
      </button>
    </div>
  </section>

  <section v-if="loadError" class="mt-6">
    <ApiErrorBanner title="Manual review queue failed" :error="loadError" />
  </section>

  <SourceWarnings :warnings="sourceWarnings" />

  <section class="mt-6 grid gap-4 md:grid-cols-5">
    <MetricTile label="Open Items" :value="String(summary.total_items || 0)" :note="`${summary.untrimmed_items || 0} before limit`" />
    <MetricTile label="Errors" :value="String(bySeverity.error || 0)" note="Execution or processing blockers" />
    <MetricTile label="Warnings" :value="String(bySeverity.warning || 0)" note="Conflicts and extraction issues" />
    <MetricTile label="Investment" :value="String(investmentItems.length)" note="Judgment or action review" />
    <MetricTile label="Technical" :value="String(technicalItems.length)" note="Fix pipeline/data issue first" />
    <MetricTile label="Closed" :value="String(summary.closed_by_operator || 0)" :note="`${summary.annotated_by_operator || 0} annotated`" />
  </section>

  <section class="mt-6 grid gap-4 lg:grid-cols-2">
    <div class="rounded-3xl border border-moss/20 bg-moss/10 p-5">
      <p class="text-xs font-black uppercase tracking-[0.25em] text-moss">Investment Review</p>
      <p class="mt-2 text-sm leading-6 text-ink/65">
        Use this lane to decide whether evidence is actionable, whether to wait for a specific trigger, or whether to downgrade to no action. Saving a decision here only annotates/closes the review item; it does not buy, sell, or add portfolio rows.
      </p>
    </div>
    <div class="rounded-3xl border border-rust/20 bg-rust/10 p-5">
      <p class="text-xs font-black uppercase tracking-[0.25em] text-rust">Technical Issue</p>
      <p class="mt-2 text-sm leading-6 text-ink/65">
        Parser failures, request timeouts, bad URLs, DB/API issues, and extraction failures are operational work. Mark fixed only after correcting the issue or confirming a rerun succeeded.
      </p>
    </div>
  </section>

  <section class="mt-8 glass-panel rounded-3xl p-5">
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div>
        <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Filters</p>
        <h2 class="mt-2 text-2xl font-black">Review queue</h2>
      </div>
      <div class="grid gap-2">
        <p class="text-sm font-semibold text-ink/55">Generated {{ data?.generated_at || '-' }}</p>
        <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
          Operator id
          <input v-model="operatorId" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss" placeholder="operator" />
        </label>
      </div>
    </div>
    <p v-if="saveError" class="mt-4 rounded-2xl bg-rust/10 p-3 text-sm font-bold text-rust">{{ saveError }}</p>
    <p v-if="saveSuccess" class="mt-4 rounded-2xl bg-moss/10 p-3 text-sm font-bold text-moss">{{ saveSuccess }}</p>
    <div class="mt-5 flex flex-wrap gap-2">
      <button
        v-for="filter in laneFilters"
        :key="filter.key"
        class="rounded-full px-4 py-2 text-sm font-black transition"
        :class="selectedLane === filter.key ? 'bg-moss text-paper' : 'bg-white/80 text-ink/60 hover:bg-white'"
        type="button"
        @click="selectedLane = filter.key"
      >
        {{ filter.label }} · {{ filter.count }}
      </button>
    </div>
    <div class="mt-5 flex flex-wrap gap-2">
      <button
        v-for="filter in typeFilters"
        :key="filter.key"
        class="rounded-full px-4 py-2 text-sm font-black transition"
        :class="selectedType === filter.key ? 'bg-ink text-paper' : 'bg-white/80 text-ink/60 hover:bg-white'"
        type="button"
        @click="selectedType = filter.key"
      >
        {{ filter.label }} · {{ filter.count }}
      </button>
    </div>
    <div class="mt-3 flex flex-wrap gap-2">
      <button
        v-for="filter in severityFilters"
        :key="filter.key"
        class="rounded-full px-4 py-2 text-sm font-black transition"
        :class="selectedSeverity === filter.key ? 'bg-moss text-paper' : 'bg-white/80 text-ink/60 hover:bg-white'"
        type="button"
        @click="selectedSeverity = filter.key"
      >
        {{ filter.label }} · {{ filter.count }}
      </button>
    </div>

    <div class="mt-6 space-y-4">
      <article v-for="item in filteredItems" :key="String(item.item_id)" class="rounded-3xl border p-5" :class="itemAccent(item.severity)">
        <div class="flex flex-wrap items-start justify-between gap-4">
          <div class="max-w-4xl">
            <div class="flex flex-wrap gap-2">
              <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(item.severity)">{{ String(item.severity || 'review').toUpperCase() }}</span>
              <span class="rounded-full px-3 py-1 text-xs font-black" :class="laneClass(item.review_lane)">{{ laneLabel(item.review_lane) }}</span>
              <span class="rounded-full px-3 py-1 text-xs font-black" :class="impactMeta(item).tone">{{ impactMeta(item).label }}</span>
              <span class="rounded-full bg-ink/10 px-3 py-1 text-xs font-black text-ink">{{ typeLabel(item.item_type) }}</span>
              <span class="rounded-full bg-white/80 px-3 py-1 text-xs font-black text-ink/65">{{ display(item.status) }}</span>
            </div>
            <h3 class="mt-3 text-2xl font-black text-ink">{{ item.title || 'Manual review item' }}</h3>
            <p class="mt-2 text-sm leading-6 text-ink/65">{{ item.reason || 'No reason text was provided by the source row.' }}</p>
            <p v-if="item.decision_hint" class="mt-2 text-sm font-bold leading-6 text-ink/65">{{ item.decision_hint }}</p>
          </div>
          <NuxtLink v-if="item.symbol" class="rounded-full bg-white px-4 py-2 text-sm font-black text-ink" :to="`/symbols/${encodeURIComponent(String(item.symbol).toUpperCase())}`">
            Open symbol
          </NuxtLink>
          <NuxtLink v-if="tracePath(item)" class="rounded-full bg-ink px-4 py-2 text-sm font-black text-paper" :to="tracePath(item)">
            Open trace
          </NuxtLink>
        </div>
        <div class="mt-4 grid gap-2 text-sm md:grid-cols-4">
          <p class="rounded-xl bg-white/75 px-3 py-2"><b>Symbol:</b> <SymbolLink :symbol="item.symbol" subtle /></p>
          <p class="rounded-xl bg-white/75 px-3 py-2"><b>Setup:</b> {{ display(item.setup_id) }}</p>
          <p class="rounded-xl bg-white/75 px-3 py-2"><b>As of:</b> {{ display(item.asof_date) }}</p>
          <p class="rounded-xl bg-white/75 px-3 py-2"><b>Updated:</b> {{ display(item.updated_at) }}</p>
        </div>
        <div class="mt-4 grid gap-3 lg:grid-cols-3">
          <div class="rounded-2xl bg-white/75 p-4">
            <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/40">Why This Is Here</p>
            <p class="mt-2 text-sm leading-6 text-ink/65">{{ item.operator_summary || item.reason || 'No operator summary was available.' }}</p>
          </div>
          <div class="rounded-2xl bg-white/75 p-4">
            <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/40">Questions To Answer</p>
            <ul v-if="operatorQuestions(item).length" class="mt-2 space-y-1 text-sm leading-6 text-ink/65">
              <li v-for="question in operatorQuestions(item)" :key="question">- {{ question }}</li>
            </ul>
            <p v-else class="mt-2 text-sm leading-6 text-ink/55">No structured questions were generated for this item.</p>
          </div>
          <div class="rounded-2xl bg-white/75 p-4">
            <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/40">Wait Signals</p>
            <ul v-if="waitForEvents(item).length" class="mt-2 space-y-1 text-sm leading-6 text-ink/65">
              <li v-for="event in waitForEvents(item)" :key="event">- {{ event }}</li>
            </ul>
            <p v-else class="mt-2 text-sm leading-6 text-ink/55">{{ item.possible_action || 'No specific wait signal was generated.' }}</p>
          </div>
        </div>
        <div class="mt-4 grid gap-3 lg:grid-cols-2">
          <div class="rounded-2xl border border-black/10 bg-white/75 p-4">
            <div class="flex flex-wrap items-center gap-2">
              <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/40">Item Impact</p>
              <span class="rounded-full px-3 py-1 text-xs font-black" :class="impactMeta(item).tone">{{ impactMeta(item).label }}</span>
            </div>
            <p class="mt-2 text-sm leading-6 text-ink/65">{{ impactMeta(item).boundary }}</p>
          </div>
          <div class="rounded-2xl border border-black/10 bg-white/75 p-4">
            <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/40">Likely Next Step</p>
            <p class="mt-2 text-sm leading-6 text-ink/65">{{ impactMeta(item).next }}</p>
          </div>
        </div>
        <div v-if="waitSignalFollowup(item).signal_id" class="mt-4 rounded-3xl border border-blue-500/20 bg-blue-500/10 p-4">
          <p class="text-xs font-black uppercase tracking-[0.2em] text-blue-700">Matched Wait Signal</p>
          <p class="mt-2 text-sm leading-6 text-ink/70">
            This item exists because a wait condition previously created from Manual Review has matched fresh evidence. It is a follow-up review only; it does not buy, sell, alter portfolio rows, or change action recommendations by itself.
          </p>
          <div class="mt-3 grid gap-2 text-sm md:grid-cols-2">
            <p class="rounded-2xl bg-white/75 px-4 py-3"><b>Original review item:</b> {{ display(waitSignalFollowup(item).manual_review_item_id) }}</p>
            <p class="rounded-2xl bg-white/75 px-4 py-3"><b>Condition type:</b> {{ typeLabel(waitSignalFollowup(item).condition_type) }}</p>
            <p class="rounded-2xl bg-white/75 px-4 py-3"><b>Waited for:</b> {{ display(waitSignalFollowup(item).wait_question) }}</p>
            <p class="rounded-2xl bg-white/75 px-4 py-3"><b>Matched at:</b> {{ display(waitSignalFollowup(item).matched_at) }}</p>
          </div>
          <p class="mt-3 rounded-2xl bg-white/75 px-4 py-3 text-sm leading-6 text-ink/70">
            <b>Evidence:</b> {{ waitSignalFollowup(item).evidence_summary || waitSignalFollowup(item).match_reason || 'Matched evidence was recorded.' }}
          </p>
        </div>
        <p class="mt-3 break-all text-xs font-semibold text-ink/45">{{ item.source_table }} · {{ item.source_key }}</p>
        <div v-if="latestDecision(item).decision" class="mt-4 rounded-2xl bg-ink/5 p-4">
          <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/40">Latest operator annotation</p>
          <p class="mt-2 text-sm font-bold text-ink">{{ latestDecision(item).decision }} · {{ display(latestDecision(item).operator_id) }} · {{ display(latestDecision(item).decided_at) }}</p>
          <p class="mt-1 text-sm leading-6 text-ink/60">{{ latestDecision(item).rationale || 'No rationale recorded.' }}</p>
          <p v-if="latestDecision(item).follow_up_event" class="mt-1 text-sm text-ink/55"><b>Wait for:</b> {{ latestDecision(item).follow_up_event }}</p>
        </div>
        <div class="mt-4 rounded-2xl bg-white/70 p-4">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/40">Record operator decision</p>
              <p class="mt-1 text-sm text-ink/60">
                Closing decisions remove the item from the active queue. Notes and watch decisions keep it visible.
              </p>
            </div>
            <span class="rounded-full px-3 py-1 text-xs font-black" :class="selectedDecisionMeta(item).closes ? 'bg-rust text-paper' : 'bg-sun text-ink'">
              {{ selectedDecisionMeta(item).closes ? 'CLOSES ITEM' : 'ANNOTATES ITEM' }}
            </span>
          </div>
          <div class="mt-4 rounded-2xl border p-4" :class="decisionBoundaryMeta(item).tone">
            <div class="flex flex-wrap items-center justify-between gap-3">
              <p class="text-sm font-black">{{ decisionBoundaryMeta(item).label }}</p>
              <p class="text-xs font-black uppercase tracking-[0.18em]">Portfolio unchanged · Broker unchanged</p>
            </div>
            <p class="mt-1 text-sm leading-6">{{ decisionBoundaryMeta(item).summary }}</p>
          </div>
          <div class="mt-4 grid gap-3 lg:grid-cols-[0.8fr_1.2fr_1fr_auto]">
            <label class="grid gap-2 text-sm font-bold text-ink/70">
              Decision
              <select :value="selectedDecision(item)" class="rounded-2xl border border-black/10 bg-white px-4 py-3 text-ink outline-none focus:border-moss" @change="decisionByItem[itemId(item)] = String(($event.target as HTMLSelectElement).value)">
                <option v-for="option in decisionOptions" :key="option.key" :value="option.key">{{ option.label }}</option>
              </select>
              <span class="rounded-2xl border border-black/10 bg-paper/80 p-3 text-xs font-semibold normal-case leading-5 tracking-normal text-ink/65">
                <b class="text-ink">What happens:</b> {{ selectedDecisionMeta(item).effect }}
              </span>
            </label>
            <label class="grid gap-2 text-sm font-bold text-ink/70">
              Rationale
              <input v-model="rationaleByItem[itemId(item)]" class="rounded-2xl border border-black/10 bg-white px-4 py-3 text-ink outline-none focus:border-moss" placeholder="Why this operator decision is safe" />
            </label>
            <label class="grid gap-2 text-sm font-bold text-ink/70">
              Event to wait for
              <input v-model="followUpByItem[itemId(item)]" class="rounded-2xl border border-black/10 bg-white px-4 py-3 text-ink outline-none focus:border-moss" placeholder="Optional future evidence" />
            </label>
            <button class="self-end rounded-full bg-moss px-5 py-3 text-sm font-black text-paper disabled:opacity-50" :disabled="savingItemId === itemId(item)" type="button" @click="submitDecision(item)">
              {{ savingItemId === itemId(item) ? 'Saving...' : 'Save' }}
            </button>
          </div>
          <div class="mt-4 rounded-2xl border border-black/10 bg-paper/70 p-4">
            <p class="text-sm font-black text-ink">{{ selectedDecisionMeta(item).label }}</p>
            <p class="mt-1 text-sm leading-6 text-ink/65">{{ selectedDecisionMeta(item).effect }}</p>
            <p class="mt-1 text-xs font-semibold uppercase tracking-[0.18em] text-ink/40">Best for</p>
            <p class="mt-1 text-sm leading-6 text-ink/60">{{ selectedDecisionMeta(item).useFor }}</p>
          </div>
        </div>
        <details class="mt-3">
          <summary class="cursor-pointer text-sm font-black text-moss">Show source row</summary>
          <pre class="mt-3 max-h-80 overflow-auto rounded-2xl bg-ink p-4 text-xs leading-5 text-paper">{{ JSON.stringify(item.raw || {}, null, 2) }}</pre>
        </details>
      </article>
      <p v-if="!filteredItems.length" class="rounded-2xl bg-white/75 p-4 text-sm text-ink/60">No manual review items match the current filters.</p>
    </div>
  </section>

  <section v-if="skippedSources.length" class="mt-8 glass-panel rounded-3xl p-5">
    <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Skipped Sources</p>
    <h2 class="mt-2 text-2xl font-black">Tables or queries not included</h2>
    <p class="mt-2 max-w-3xl text-sm leading-6 text-ink/60">
      These are visible so missing optional data sources do not silently hide review work.
    </p>
    <div class="mt-4 grid gap-3 md:grid-cols-2">
      <article v-for="source in skippedSources" :key="`${source.source}:${source.error}`" class="rounded-2xl bg-white/75 p-4">
        <p class="font-black text-ink">{{ source.source }}</p>
        <p class="mt-1 text-sm text-ink/60">{{ source.error }}</p>
      </article>
    </div>
  </section>
</template>
