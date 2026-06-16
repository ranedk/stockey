<script setup lang="ts">
import type { Dict } from '~/types/api'

const api = useOperatorApi()
const statusFilter = ref('all')
const limitFilter = ref(100)
const decisionByKey = reactive<Record<string, string>>({})
const rationaleByKey = reactive<Record<string, string>>({})
const decisionResultByKey = reactive<Record<string, string>>({})
const decisionErrorByKey = reactive<Record<string, string>>({})
const decisionPendingByKey = reactive<Record<string, boolean>>({})
const contractRationaleByKey = reactive<Record<string, string>>({})
const contractResultByKey = reactive<Record<string, string>>({})
const contractErrorByKey = reactive<Record<string, string>>({})
const contractPendingByKey = reactive<Record<string, boolean>>({})
const reconcileResultByKey = reactive<Record<string, string>>({})
const reconcileErrorByKey = reactive<Record<string, string>>({})
const reconcilePendingByKey = reactive<Record<string, boolean>>({})
const evidenceRationaleByKey = reactive<Record<string, string>>({})
const evidenceResultByKey = reactive<Record<string, string>>({})
const evidenceErrorByKey = reactive<Record<string, string>>({})
const evidencePendingByKey = reactive<Record<string, boolean>>({})
const liveAllowanceRationaleByKey = reactive<Record<string, string>>({})
const liveAllowancePhraseByKey = reactive<Record<string, string>>({})
const liveAllowanceExpectedByKey = reactive<Record<string, string>>({})
const liveAllowanceResultByKey = reactive<Record<string, string>>({})
const liveAllowanceErrorByKey = reactive<Record<string, string>>({})
const liveAllowancePendingByKey = reactive<Record<string, boolean>>({})
const liveSubmitPreflight = ref<Dict | null>(null)
const liveSubmitPreflightError = ref('')
const liveSubmitPreflightPending = ref(false)

const queryParams = computed(() => ({
  status: statusFilter.value === 'all' ? undefined : statusFilter.value,
  limit: limitFilter.value
}))

const { data, refresh, pending, error: loadError } = await useAsyncData(
  'execution-approvals',
  () => api.getExecutionApprovals(queryParams.value),
  { watch: [queryParams] }
)

const summary = computed(() => asDict(data.value?.summary))
const rows = computed(() => asList(data.value?.rows))
const boundary = computed(() => asDict(data.value?.operator_boundary))
const skipped = computed(() => asList(data.value?.skipped))
const sourceWarnings = computed(() => asList(data.value?.source_warnings))

function asDict(value: unknown): Dict {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Dict : {}
}

function asList(value: unknown): Dict[] {
  return Array.isArray(value) ? value.filter((item) => typeof item === 'object' && item !== null) as Dict[] : []
}

function display(value: unknown) {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'number') return Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 }).format(value)
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  return String(value)
}

function money(value: unknown) {
  const num = Number(value)
  if (!Number.isFinite(num)) return '-'
  return `₹${Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 }).format(num)}`
}

function formatTime(value: unknown) {
  const text = String(value || '')
  if (!text) return '-'
  const date = new Date(text)
  if (Number.isNaN(date.getTime())) return text
  return new Intl.DateTimeFormat('en-IN', {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone: 'Asia/Kolkata'
  }).format(date)
}

function contract(row: Dict) {
  return asDict(row.safety_contract)
}

function blockers(row: Dict) {
  return Array.isArray(row.blockers) ? row.blockers.map((item) => String(item || '').trim()).filter(Boolean) : []
}

function readinessChecks(row: Dict) {
  return asList(row.readiness_checks)
}

function readinessTone(check: Dict): 'success' | 'warning' | 'danger' | 'info' | 'dark' | 'neutral' {
  const status = String(check.status || '').toLowerCase()
  if (status === 'passed') return 'success'
  if (status === 'not_required') return 'neutral'
  return 'danger'
}

function rowTone(row: Dict): 'success' | 'warning' | 'danger' | 'info' | 'dark' | 'neutral' {
  const status = String(row.execution_status || '').toLowerCase()
  if (status.includes('error') || status.includes('invalid')) return 'danger'
  if (blockers(row).length) return 'warning'
  return 'info'
}

function rowKey(row: Dict) {
  return String(row.approval_key || `${row.asof_date}:${row.published_on}:${row.symbol}:${row.setup_id}:${row.unique_id}`)
}

function latestDecision(row: Dict) {
  return asDict(row.latest_operator_decision)
}

async function recordDecision(row: Dict) {
  const key = rowKey(row)
  decisionErrorByKey[key] = ''
  decisionResultByKey[key] = ''
  const decision = decisionByKey[key] || 'needs_more_data'
  const rationale = String(rationaleByKey[key] || '').trim()
  if (!rationale) {
    decisionErrorByKey[key] = 'Rationale is required before recording an audit decision.'
    return
  }
  decisionPendingByKey[key] = true
  try {
    const result = await api.decideExecutionApproval({ row, decision, rationale })
    decisionResultByKey[key] = String(result.note || 'Audit decision recorded.')
    rationaleByKey[key] = ''
    await refresh()
  } catch (error: any) {
    decisionErrorByKey[key] = String(error?.data?.detail?.message || error?.message || error)
  } finally {
    decisionPendingByKey[key] = false
  }
}

function canApplyApprovalContract(row: Dict) {
  return String(latestDecision(row).decision || '').toLowerCase() === 'approve_dry_run'
}

async function applyApprovalContract(row: Dict) {
  const key = rowKey(row)
  contractErrorByKey[key] = ''
  contractResultByKey[key] = ''
  const rationale = String(contractRationaleByKey[key] || '').trim()
  if (!rationale) {
    contractErrorByKey[key] = 'Rationale is required before updating the safety contract.'
    return
  }
  contractPendingByKey[key] = true
  try {
    const result = await api.applyExecutionApprovalContract({ row, confirm: true, rationale })
    contractResultByKey[key] = String(result.note || 'Execution safety contract updated.')
    contractRationaleByKey[key] = ''
    await refresh()
  } catch (error: any) {
    contractErrorByKey[key] = String(error?.data?.detail?.message || error?.message || error)
  } finally {
    contractPendingByKey[key] = false
  }
}

async function runReconciliation(row: Dict, apply = false) {
  const key = rowKey(row)
  reconcileErrorByKey[key] = ''
  reconcileResultByKey[key] = ''
  reconcilePendingByKey[key] = true
  try {
    const result = await api.runExecutionReconciliation({ row, apply, dry_run: !apply, confirm: apply ? true : undefined })
    const summary = asDict(result.summary)
    reconcileResultByKey[key] = `${result.note || 'Broker reconciliation completed.'} Targets: ${display(summary.target_count)}, reconciled: ${display(summary.reconciled_count)}, fills: ${display(summary.fill_count)}.`
    await refresh()
  } catch (error: any) {
    reconcileErrorByKey[key] = String(error?.data?.detail?.message || error?.message || error)
  } finally {
    reconcilePendingByKey[key] = false
  }
}

async function reviewEvidence(row: Dict, apply = false) {
  const key = rowKey(row)
  evidenceErrorByKey[key] = ''
  evidenceResultByKey[key] = ''
  const rationale = String(evidenceRationaleByKey[key] || '').trim()
  if (apply && !rationale) {
    evidenceErrorByKey[key] = 'Rationale is required before applying evidence status.'
    return
  }
  evidencePendingByKey[key] = true
  try {
    const result = await api.reviewExecutionEvidence({ row, apply, dry_run: !apply, confirm: apply ? true : undefined, rationale: apply ? rationale : undefined })
    const evidence = asDict(result.evidence)
    const blockers = Array.isArray(evidence.blockers) ? evidence.blockers.length : 0
    evidenceResultByKey[key] = `${result.note || 'Evidence review completed.'} Decision: ${display(result.decision)}. Successful runs: ${display(evidence.successful_runs)}/${display(evidence.required_runs)}. Blockers: ${display(blockers)}.`
    if (apply) evidenceRationaleByKey[key] = ''
    await refresh()
  } catch (error: any) {
    evidenceErrorByKey[key] = String(error?.data?.detail?.message || error?.message || error)
  } finally {
    evidencePendingByKey[key] = false
  }
}

async function applyLiveAllowance(row: Dict, apply = false) {
  const key = rowKey(row)
  liveAllowanceErrorByKey[key] = ''
  liveAllowanceResultByKey[key] = ''
  const rationale = String(liveAllowanceRationaleByKey[key] || '').trim()
  const confirmationPhrase = String(liveAllowancePhraseByKey[key] || '').trim()
  if (apply && !rationale) {
    liveAllowanceErrorByKey[key] = 'Rationale is required before applying live allowance.'
    return
  }
  if (apply && !confirmationPhrase) {
    liveAllowanceErrorByKey[key] = 'Exact confirmation phrase is required before applying live allowance.'
    return
  }
  liveAllowancePendingByKey[key] = true
  try {
    const result = await api.applyExecutionLiveAllowance({
      row,
      apply,
      dry_run: !apply,
      confirm: apply ? true : undefined,
      rationale: apply ? rationale : undefined,
      confirmation_phrase: apply ? confirmationPhrase : undefined
    })
    const boundary = asDict(result.operator_boundary)
    if (boundary.expected_confirmation_phrase) {
      liveAllowanceExpectedByKey[key] = String(boundary.expected_confirmation_phrase)
    }
    const blockers = Array.isArray(result.blockers) ? result.blockers.length : 0
    liveAllowanceResultByKey[key] = `${result.note || 'Live allowance review completed.'} Decision: ${display(result.decision)}. Blockers: ${display(blockers)}.`
    if (apply) {
      liveAllowanceRationaleByKey[key] = ''
      liveAllowancePhraseByKey[key] = ''
    }
    await refresh()
  } catch (error: any) {
    liveAllowanceErrorByKey[key] = String(error?.data?.detail?.message || error?.message || error)
  } finally {
    liveAllowancePendingByKey[key] = false
  }
}

async function previewLiveSubmit() {
  liveSubmitPreflightError.value = ''
  liveSubmitPreflight.value = null
  liveSubmitPreflightPending.value = true
  try {
    liveSubmitPreflight.value = await api.previewExecutionLiveSubmit({
      status: statusFilter.value === 'all' ? undefined : statusFilter.value,
      limit: limitFilter.value
    }) as unknown as Dict
  } catch (error: any) {
    liveSubmitPreflightError.value = String(error?.data?.detail?.message || error?.message || error)
  } finally {
    liveSubmitPreflightPending.value = false
  }
}
</script>

<template>
  <section class="rounded-[2rem] bg-ink p-8 text-paper shadow-soft">
    <div class="flex flex-wrap items-end justify-between gap-5">
      <div>
        <p class="text-sm font-semibold uppercase tracking-[0.35em] text-paper/55">Execution Approvals</p>
        <h1 class="mt-4 max-w-4xl text-5xl font-black tracking-tight">
          Broker execution stays blocked until approval and reconciliation are explicit.
        </h1>
        <p class="mt-4 max-w-3xl text-sm leading-6 text-paper/65">
          Read-only view of dry-run execution rows, safety contracts, approval state, reconciliation state, and blockers. This page does not approve, reconcile, or submit broker orders.
        </p>
      </div>
      <button class="rounded-full bg-sun px-5 py-3 text-sm font-black text-ink shadow-soft disabled:opacity-50" type="button" :disabled="pending" @click="() => refresh()">
        {{ pending ? 'Refreshing...' : 'Refresh' }}
      </button>
    </div>

    <div class="mt-6 grid gap-4 md:grid-cols-5">
      <MetricTile label="Rows" :value="display(summary.row_count)" note="latest as-of execution rows" />
      <MetricTile label="Blocked" :value="display(summary.blocked_count)" note="has safety blockers" />
      <MetricTile label="Missing Approval" :value="display(summary.missing_approval_count)" note="operator approval required" />
      <MetricTile label="Missing Reconcile" :value="display(summary.missing_reconciliation_count)" note="broker reconciliation required" />
      <MetricTile label="Missing Evidence" :value="display(summary.missing_evidence_count)" note="multi-run proof required" />
      <MetricTile label="Live Allowed" :value="display(summary.live_allowed_count)" note="should stay 0 unless deliberately enabled" />
      <MetricTile label="Post-Allow Approval" :value="display(summary.missing_post_live_allowance_approval_count)" note="fresh approval after live allowance" />
    </div>
  </section>

  <ApiErrorBanner v-if="loadError" class="mt-6" title="Could not load execution approvals" :error="loadError" />
  <SourceWarnings :warnings="sourceWarnings" />

  <section class="mt-6 rounded-3xl border border-rust/20 bg-rust/10 p-5">
    <p class="text-xs font-black uppercase tracking-[0.25em] text-rust">Safety Boundary</p>
    <p class="mt-2 text-sm leading-6 text-ink/70">
      {{ boundary.note || 'Read-only approval workbench. It does not mutate execution rows or broker state.' }}
    </p>
    <div class="mt-3 flex flex-wrap gap-2">
      <StatusPill tone="warning">approves execution: {{ display(boundary.approves_execution) }}</StatusPill>
      <StatusPill tone="warning">submits orders: {{ display(boundary.submits_broker_orders) }}</StatusPill>
      <StatusPill tone="warning">mutates execution rows: {{ display(boundary.mutates_execution_orders) }}</StatusPill>
      <StatusPill tone="danger">post-allowance approval required: {{ display(boundary.requires_post_live_allowance_approval) }}</StatusPill>
    </div>
  </section>

  <section class="mt-6 glass-panel rounded-3xl p-5">
    <div class="grid gap-3 md:grid-cols-[1fr_0.5fr_auto]">
      <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
        Status
        <select v-model="statusFilter" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss">
          <option value="all">All</option>
          <option value="planned">Planned</option>
          <option value="submit_blocked">Submit blocked</option>
          <option value="submit_error">Submit error</option>
          <option value="reconcile_error">Reconcile error</option>
        </select>
      </label>
      <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
        Limit
        <input v-model.number="limitFilter" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss" type="number" min="1" max="500" />
      </label>
      <button class="self-end rounded-full bg-ink px-4 py-2 text-sm font-black text-paper" type="button" @click="() => refresh()">
        Apply
      </button>
    </div>
  </section>

  <section class="mt-6 rounded-3xl border border-rust/25 bg-rust/10 p-5">
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div>
        <p class="text-xs font-black uppercase tracking-[0.25em] text-rust">Live submit preflight</p>
        <h2 class="mt-2 text-2xl font-black text-ink">Generate the manual CLI token and command</h2>
        <p class="mt-2 max-w-3xl text-sm leading-6 text-ink/65">
          This is read-only. It checks the current execution rows against approval, reconciliation, evidence, live allowance, fresh post-allowance approval, quantity, broker identity, and price gates. The UI never submits broker orders.
        </p>
      </div>
      <button class="rounded-full bg-rust px-5 py-3 text-sm font-black text-white shadow-soft disabled:opacity-50" type="button" :disabled="liveSubmitPreflightPending" @click="previewLiveSubmit">
        {{ liveSubmitPreflightPending ? 'Checking...' : 'Preview Live Submit' }}
      </button>
    </div>
    <p v-if="liveSubmitPreflightError" class="mt-4 rounded-2xl bg-rust/10 px-4 py-3 text-sm font-semibold text-rust">
      {{ liveSubmitPreflightError }}
    </p>
    <div v-if="liveSubmitPreflight" class="mt-4 grid gap-4">
      <div class="flex flex-wrap gap-2">
        <StatusPill :tone="String(liveSubmitPreflight.decision || '') === 'ready' ? 'success' : 'warning'">
          {{ display(liveSubmitPreflight.decision) }}
        </StatusPill>
        <StatusPill tone="danger">UI submits orders: {{ display(asDict(liveSubmitPreflight.operator_boundary).submits_broker_orders) }}</StatusPill>
        <StatusPill tone="info">Eligible rows: {{ display(asDict(liveSubmitPreflight.summary).eligible_planned_rows) }}</StatusPill>
        <StatusPill tone="warning">Skipped rows: {{ display(asDict(liveSubmitPreflight.summary).skipped_rows) }}</StatusPill>
      </div>
      <p class="rounded-2xl bg-white/80 px-4 py-3 text-sm leading-6 text-ink/70">
        {{ liveSubmitPreflight.note }}
      </p>
      <div v-if="liveSubmitPreflight.expected_live_token" class="rounded-2xl bg-white/80 px-4 py-3 text-sm text-ink/70">
        <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/45">Expected token</p>
        <code class="mt-2 block break-all rounded-xl bg-ink px-3 py-2 text-paper">{{ liveSubmitPreflight.expected_live_token }}</code>
      </div>
      <div v-if="liveSubmitPreflight.cli_command_preview" class="rounded-2xl bg-white/80 px-4 py-3 text-sm text-ink/70">
        <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/45">Manual CLI command</p>
        <code class="mt-2 block overflow-auto rounded-xl bg-ink px-3 py-2 text-paper">{{ liveSubmitPreflight.cli_command_preview }}</code>
        <p class="mt-2 text-xs font-semibold text-rust">Run this manually only after reviewing the exact order set. This page does not execute it.</p>
      </div>
      <div v-if="Array.isArray(liveSubmitPreflight.blockers) && liveSubmitPreflight.blockers.length" class="rounded-2xl border border-rust/25 bg-white/75 p-4">
        <p class="text-xs font-black uppercase tracking-[0.2em] text-rust">Preflight blockers</p>
        <ul class="mt-2 space-y-1 text-sm leading-6 text-ink/75">
          <li v-for="blocker in liveSubmitPreflight.blockers" :key="String(blocker)">- {{ blocker }}</li>
        </ul>
      </div>
    </div>
  </section>

  <section class="mt-6 space-y-4">
    <article v-for="row in rows" :key="`${row.asof_date}:${row.symbol}:${row.setup_id}:${row.unique_id}`" class="glass-panel rounded-3xl p-5">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div class="flex flex-wrap items-center gap-2">
            <SymbolLink :symbol="row.symbol" />
            <StatusPill :tone="rowTone(row)">{{ display(row.execution_status) }}</StatusPill>
            <StatusPill tone="dark">{{ display(row.transaction_type) }}</StatusPill>
          </div>
          <p class="mt-2 text-sm text-ink/60">{{ row.execution_reason || row.operator_action || 'No execution reason recorded.' }}</p>
        </div>
        <div class="text-right text-sm text-ink/60">
          <p class="font-black text-ink">{{ money(row.estimated_order_value_inr) }}</p>
          <p>{{ display(row.quantity) }} shares @ {{ money(row.reference_price) }}</p>
        </div>
      </div>

      <div class="mt-4 grid gap-3 md:grid-cols-5">
        <p class="rounded-2xl bg-white/70 px-4 py-3 text-sm"><b>Approval:</b> {{ display(contract(row).operator_approval_status) }}</p>
        <p class="rounded-2xl bg-white/70 px-4 py-3 text-sm"><b>Reconcile:</b> {{ display(contract(row).broker_reconciliation_status) }}</p>
        <p class="rounded-2xl bg-white/70 px-4 py-3 text-sm">
          <b>Evidence:</b> {{ display(contract(row).live_evidence_status) }}
          <span class="text-ink/45">({{ display(contract(row).live_evidence_successful_runs) }}/{{ display(contract(row).live_evidence_min_successful_runs) }})</span>
        </p>
        <p class="rounded-2xl bg-white/70 px-4 py-3 text-sm"><b>Live allowed:</b> {{ display(contract(row).live_submission_allowed) }}</p>
        <p class="rounded-2xl bg-white/70 px-4 py-3 text-sm"><b>Published:</b> {{ formatTime(row.published_on) }}</p>
      </div>

      <div v-if="readinessChecks(row).length" class="mt-4 rounded-2xl border border-black/10 bg-white/70 p-4">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.22em] text-ink/45">Live Readiness Checklist</p>
            <p class="mt-1 text-sm leading-6 text-ink/60">
              Every required gate must pass before the CLI preflight can produce a ready live-submit decision. This UI still cannot submit broker orders.
            </p>
          </div>
          <StatusPill :tone="Number(asDict(row.readiness_summary).blocked_count || 0) > 0 ? 'danger' : 'success'">
            {{ display(asDict(row.readiness_summary).passed_count || 0) }}/{{ display(asDict(row.readiness_summary).required_count || readinessChecks(row).length) }} passed
          </StatusPill>
        </div>
        <div class="mt-3 grid gap-2 md:grid-cols-3">
          <div v-for="check in readinessChecks(row)" :key="String(check.key)" class="rounded-2xl border border-black/10 bg-paper/80 px-4 py-3">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <p class="text-sm font-black text-ink">{{ display(check.label) }}</p>
              <StatusPill :tone="readinessTone(check)">{{ display(check.status) }}</StatusPill>
            </div>
            <p class="mt-1 text-xs font-semibold leading-5 text-ink/55">{{ display(check.detail) }}</p>
          </div>
        </div>
      </div>

      <div v-if="contract(row).live_submission_allowed === true || contract(row).post_live_allowance_approval_required" class="mt-4 rounded-2xl border border-rust/25 bg-rust/10 p-4">
        <p class="text-xs font-black uppercase tracking-[0.22em] text-rust">Post-live-allowance approval</p>
        <div class="mt-3 grid gap-3 md:grid-cols-4">
          <p class="rounded-2xl bg-white/75 px-4 py-3 text-sm"><b>Required:</b> {{ display(contract(row).post_live_allowance_approval_required) }}</p>
          <p class="rounded-2xl bg-white/75 px-4 py-3 text-sm"><b>Status:</b> {{ display(contract(row).post_live_allowance_approval_status) }}</p>
          <p class="rounded-2xl bg-white/75 px-4 py-3 text-sm"><b>Allowance at:</b> {{ formatTime(contract(row).live_allowance_allowed_at) }}</p>
          <p class="rounded-2xl bg-white/75 px-4 py-3 text-sm"><b>Approval at:</b> {{ formatTime(contract(row).post_live_allowance_approval_decided_at) }}</p>
        </div>
        <p class="mt-3 text-sm leading-6 text-ink/65">
          Live allowance alone is not enough. Record a fresh <b>Approve dry-run review</b> after the allowance timestamp, then apply the approval status again before previewing or running live submission.
        </p>
      </div>

      <div v-if="blockers(row).length" class="mt-4 rounded-2xl border border-rust/25 bg-rust/10 p-4">
        <p class="text-xs font-black uppercase tracking-[0.22em] text-rust">Why this cannot be submitted</p>
        <ul class="mt-2 space-y-1 text-sm leading-6 text-ink/75">
          <li v-for="blocker in blockers(row)" :key="blocker">- {{ blocker }}</li>
        </ul>
      </div>

      <div class="mt-4 rounded-2xl border border-black/10 bg-white/70 p-4">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.22em] text-ink/45">Audit-only operator decision</p>
            <p class="mt-1 text-sm leading-6 text-ink/60">
              This records review intent only. It does not set `operator_approval_status=approved`, reconcile broker state, update execution rows, or submit orders.
            </p>
          </div>
          <StatusPill tone="warning">AUDIT ONLY</StatusPill>
        </div>
        <div v-if="latestDecision(row).decision" class="mt-3 rounded-2xl bg-paper/80 px-4 py-3 text-sm text-ink/70">
          Latest decision: <b>{{ display(latestDecision(row).decision) }}</b>
          <span v-if="latestDecision(row).decided_at"> at {{ formatTime(latestDecision(row).decided_at) }}</span>
          <span v-if="latestDecision(row).rationale"> · {{ latestDecision(row).rationale }}</span>
        </div>
        <div class="mt-3 grid gap-3 md:grid-cols-[0.7fr_1.5fr_auto]">
          <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
            Decision
            <select v-model="decisionByKey[rowKey(row)]" class="rounded-xl border border-black/10 bg-white px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss">
              <option value="needs_more_data">Needs more data</option>
              <option value="needs_reconciliation">Needs reconciliation</option>
              <option value="approve_dry_run">Approve dry-run review</option>
              <option value="reject">Reject execution idea</option>
            </select>
          </label>
          <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
            Rationale
            <input v-model="rationaleByKey[rowKey(row)]" class="rounded-xl border border-black/10 bg-white px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss" placeholder="Why are you recording this decision?" />
          </label>
          <button class="self-end rounded-full bg-ink px-4 py-2 text-sm font-black text-paper disabled:opacity-50" type="button" :disabled="decisionPendingByKey[rowKey(row)]" @click="recordDecision(row)">
            {{ decisionPendingByKey[rowKey(row)] ? 'Recording...' : 'Record Audit' }}
          </button>
        </div>
        <p v-if="decisionResultByKey[rowKey(row)]" class="mt-3 rounded-2xl bg-moss/10 px-4 py-3 text-sm font-semibold text-moss">
          {{ decisionResultByKey[rowKey(row)] }}
        </p>
        <p v-if="decisionErrorByKey[rowKey(row)]" class="mt-3 rounded-2xl bg-rust/10 px-4 py-3 text-sm font-semibold text-rust">
          {{ decisionErrorByKey[rowKey(row)] }}
        </p>
      </div>

      <div class="mt-4 rounded-2xl border border-moss/20 bg-moss/10 p-4">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.22em] text-moss">Reviewed safety-contract update</p>
            <p class="mt-1 text-sm leading-6 text-ink/65">
              Available only after the latest audit decision is `approve_dry_run`. This updates operator approval status only; reconciliation stays unchanged and live submission remains blocked by safety gates.
            </p>
          </div>
          <StatusPill :tone="canApplyApprovalContract(row) ? 'info' : 'warning'">
            {{ canApplyApprovalContract(row) ? 'READY TO APPLY APPROVAL STATUS' : 'NEEDS APPROVE_DRY_RUN AUDIT' }}
          </StatusPill>
        </div>
        <div class="mt-3 grid gap-3 md:grid-cols-[1fr_auto]">
          <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
            Contract update rationale
            <input v-model="contractRationaleByKey[rowKey(row)]" class="rounded-xl border border-black/10 bg-white px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss" placeholder="Why should operator approval status be marked approved?" />
          </label>
          <button class="self-end rounded-full bg-moss px-4 py-2 text-sm font-black text-white disabled:opacity-50" type="button" :disabled="!canApplyApprovalContract(row) || contractPendingByKey[rowKey(row)]" @click="applyApprovalContract(row)">
            {{ contractPendingByKey[rowKey(row)] ? 'Applying...' : 'Apply Approval Status' }}
          </button>
        </div>
        <p v-if="contractResultByKey[rowKey(row)]" class="mt-3 rounded-2xl bg-moss/10 px-4 py-3 text-sm font-semibold text-moss">
          {{ contractResultByKey[rowKey(row)] }}
        </p>
        <p v-if="contractErrorByKey[rowKey(row)]" class="mt-3 rounded-2xl bg-rust/10 px-4 py-3 text-sm font-semibold text-rust">
          {{ contractErrorByKey[rowKey(row)] }}
        </p>
      </div>

      <div class="mt-4 rounded-2xl border border-sky-200 bg-sky-50 p-4">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.22em] text-sky-700">Broker reconciliation</p>
            <p class="mt-1 text-sm leading-6 text-ink/65">
              Preview reads broker order state for this symbol/setup. Persist updates reconciliation/fill rows only; it does not change operator approval, allow live submission, or submit broker orders.
            </p>
          </div>
          <StatusPill tone="info">NO ORDER SUBMISSION</StatusPill>
        </div>
        <div class="mt-3 flex flex-wrap gap-3">
          <button class="rounded-full bg-white px-4 py-2 text-sm font-black text-ink shadow-soft disabled:opacity-50" type="button" :disabled="reconcilePendingByKey[rowKey(row)]" @click="runReconciliation(row, false)">
            {{ reconcilePendingByKey[rowKey(row)] ? 'Reconciling...' : 'Preview Reconciliation' }}
          </button>
          <button class="rounded-full bg-sky-700 px-4 py-2 text-sm font-black text-white shadow-soft disabled:opacity-50" type="button" :disabled="reconcilePendingByKey[rowKey(row)]" @click="runReconciliation(row, true)">
            {{ reconcilePendingByKey[rowKey(row)] ? 'Persisting...' : 'Persist Reconciliation' }}
          </button>
        </div>
        <p v-if="reconcileResultByKey[rowKey(row)]" class="mt-3 rounded-2xl bg-moss/10 px-4 py-3 text-sm font-semibold text-moss">
          {{ reconcileResultByKey[rowKey(row)] }}
        </p>
        <p v-if="reconcileErrorByKey[rowKey(row)]" class="mt-3 rounded-2xl bg-rust/10 px-4 py-3 text-sm font-semibold text-rust">
          {{ reconcileErrorByKey[rowKey(row)] }}
        </p>
      </div>

      <div class="mt-4 rounded-2xl border border-amber-200 bg-amber-50 p-4">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.22em] text-amber-700">Live evidence review</p>
            <p class="mt-1 text-sm leading-6 text-ink/65">
              Checks repeated successful dry-run/reconciliation cycles for this symbol/setup/action. Apply can mark evidence passed, but still keeps live submission blocked and does not submit orders.
            </p>
          </div>
          <StatusPill tone="warning">EVIDENCE ONLY</StatusPill>
        </div>
        <div class="mt-3 grid gap-3 md:grid-cols-[1fr_auto_auto]">
          <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
            Evidence rationale
            <input v-model="evidenceRationaleByKey[rowKey(row)]" class="rounded-xl border border-black/10 bg-white px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss" placeholder="Why should this evidence gate be marked passed?" />
          </label>
          <button class="self-end rounded-full bg-white px-4 py-2 text-sm font-black text-ink shadow-soft disabled:opacity-50" type="button" :disabled="evidencePendingByKey[rowKey(row)]" @click="reviewEvidence(row, false)">
            {{ evidencePendingByKey[rowKey(row)] ? 'Reviewing...' : 'Preview Evidence' }}
          </button>
          <button class="self-end rounded-full bg-amber-700 px-4 py-2 text-sm font-black text-white shadow-soft disabled:opacity-50" type="button" :disabled="evidencePendingByKey[rowKey(row)]" @click="reviewEvidence(row, true)">
            {{ evidencePendingByKey[rowKey(row)] ? 'Applying...' : 'Apply Evidence Passed' }}
          </button>
        </div>
        <p v-if="evidenceResultByKey[rowKey(row)]" class="mt-3 rounded-2xl bg-moss/10 px-4 py-3 text-sm font-semibold text-moss">
          {{ evidenceResultByKey[rowKey(row)] }}
        </p>
        <p v-if="evidenceErrorByKey[rowKey(row)]" class="mt-3 rounded-2xl bg-rust/10 px-4 py-3 text-sm font-semibold text-rust">
          {{ evidenceErrorByKey[rowKey(row)] }}
        </p>
      </div>

      <div class="mt-4 rounded-2xl border border-rust/25 bg-rust/10 p-4">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.22em] text-rust">Final live allowance</p>
            <p class="mt-1 text-sm leading-6 text-ink/65">
              This workflow can set `live_submission_allowed=true`, but it resets post-allowance approval to missing. It still does not submit orders; live submission also requires a fresh approval after allowance and the separate per-run token in the CLI execution path.
            </p>
          </div>
          <StatusPill tone="danger">NO BROKER SUBMIT</StatusPill>
        </div>
        <div v-if="liveAllowanceExpectedByKey[rowKey(row)]" class="mt-3 rounded-2xl bg-white/80 px-4 py-3 text-sm text-ink/70">
          Required phrase: <b>{{ liveAllowanceExpectedByKey[rowKey(row)] }}</b>
        </div>
        <div class="mt-3 grid gap-3 md:grid-cols-[1fr_1fr_auto_auto]">
          <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
            Allowance rationale
            <input v-model="liveAllowanceRationaleByKey[rowKey(row)]" class="rounded-xl border border-black/10 bg-white px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss" placeholder="Why should this order be allowed for live submission?" />
          </label>
          <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
            Confirmation phrase
            <input v-model="liveAllowancePhraseByKey[rowKey(row)]" class="rounded-xl border border-black/10 bg-white px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss" placeholder="Type exact phrase from preview" />
          </label>
          <button class="self-end rounded-full bg-white px-4 py-2 text-sm font-black text-ink shadow-soft disabled:opacity-50" type="button" :disabled="liveAllowancePendingByKey[rowKey(row)]" @click="applyLiveAllowance(row, false)">
            {{ liveAllowancePendingByKey[rowKey(row)] ? 'Checking...' : 'Preview Allowance' }}
          </button>
          <button class="self-end rounded-full bg-rust px-4 py-2 text-sm font-black text-white shadow-soft disabled:opacity-50" type="button" :disabled="liveAllowancePendingByKey[rowKey(row)]" @click="applyLiveAllowance(row, true)">
            {{ liveAllowancePendingByKey[rowKey(row)] ? 'Applying...' : 'Apply Live Allowance' }}
          </button>
        </div>
        <p v-if="liveAllowanceResultByKey[rowKey(row)]" class="mt-3 rounded-2xl bg-moss/10 px-4 py-3 text-sm font-semibold text-moss">
          {{ liveAllowanceResultByKey[rowKey(row)] }}
        </p>
        <p v-if="liveAllowanceErrorByKey[rowKey(row)]" class="mt-3 rounded-2xl bg-rust/10 px-4 py-3 text-sm font-semibold text-rust">
          {{ liveAllowanceErrorByKey[rowKey(row)] }}
        </p>
      </div>
    </article>

    <p v-if="!rows.length && !pending" class="rounded-2xl bg-white/75 p-4 text-sm font-semibold text-ink/55">
      No execution approval rows for the current filter.
    </p>
  </section>

  <section v-if="skipped.length" class="mt-6 rounded-3xl border border-rust/20 bg-rust/10 p-5">
    <p class="text-xs font-black uppercase tracking-[0.25em] text-rust">Skipped Sources</p>
    <pre class="mt-3 overflow-auto rounded-2xl bg-ink p-4 text-xs text-paper">{{ skipped }}</pre>
  </section>
</template>
