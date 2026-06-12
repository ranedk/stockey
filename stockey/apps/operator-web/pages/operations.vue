<script setup lang="ts">
import type { Dict } from '~/types/api'

const api = useOperatorApi()
const [{ data: smoke, refresh: refreshSmoke, pending: smokePending, error: smokeError }, { data: cronStatus, refresh: refreshCronStatus, error: cronStatusError }, { data: cronLogs, refresh: refreshLogs, error: cronLogsError }, { data: mlGate, refresh: refreshMlGate, error: mlGateError }, { data: artifacts, refresh: refreshArtifacts, error: artifactsError }, { data: commands, refresh: refreshCommands, error: commandsError }, { data: apiErrors, refresh: refreshApiErrors, error: apiErrorsLoadError }] = await Promise.all([
  useAsyncData('operations-smoke', () => api.runOperationsSmoke(), { immediate: true }),
  useAsyncData('operations-cron-status', () => api.getCronStatus(50, 10)),
  useAsyncData('operations-cron-logs', () => api.getCronLogs(12, 60)),
  useAsyncData('operations-event-model-promotion-check', () => api.getEventModelPromotionCheck()),
  useAsyncData('operations-event-model-artifacts', () => api.getEventModelArtifacts()),
  useAsyncData('operations-commands', () => api.getOperatorCommands(20)),
  useAsyncData('operations-api-errors', () => api.getOperatorApiErrors(25))
])

const fixHints = computed(() => asList(smoke.value?.fix_hints))
const smokeContract = computed(() => typeof smoke.value?.operator_smoke === 'object' && smoke.value?.operator_smoke !== null ? smoke.value.operator_smoke as Dict : {})
const smokeCommand = computed(() => commandList.value.find((command) => String(command.key) === 'operator_smoke') || null)
const otherCommands = computed(() => commandList.value.filter((command) => String(command.key) !== 'operator_smoke'))
const smokeCounts = computed(() => asDict(smokeContract.value.counts))
const smokeNextCommands = computed(() => asStringList(smoke.value?.next_commands || smokeContract.value.next_commands))
const cronJobs = computed(() => asList(cronStatus.value?.jobs))
const cronCounts = computed(() => asDict(cronStatus.value?.counts))
const logs = computed(() => asList(cronLogs.value?.logs))
const logPageMeta = computed(() => asDict(cronLogs.value?.pagination?.logs))
const failedGates = computed(() => mlGate.value?.failed_gates || [])
const gates = computed(() => asList(mlGate.value?.gates))
const mlScorecard = computed(() => asDict(mlGate.value?.scorecard))
const mlScorecardMetrics = computed(() => asDict(mlScorecard.value.key_metrics))
const latestHeads = computed(() => asList(artifacts.value?.latest_s3_heads))
const artifactPageMeta = computed(() => asDict(artifacts.value?.pagination?.artifact_files))
const commandList = computed(() => asList(commands.value?.commands))
const recentRuns = computed(() => asList(commands.value?.recent_runs))
const operatorApiErrors = computed(() => asList(apiErrors.value?.errors))
const loadErrors = computed(() => [
  { title: 'Operations smoke failed', error: smokeError.value },
  { title: 'Cron status failed', error: cronStatusError.value },
  { title: 'Cron logs failed', error: cronLogsError.value },
  { title: 'ML gate failed', error: mlGateError.value },
  { title: 'Artifact check failed', error: artifactsError.value },
  { title: 'Command list failed', error: commandsError.value },
  { title: 'API error history failed', error: apiErrorsLoadError.value }
].filter((row) => row.error))
const operatorId = ref('operator')
const requestedReason = ref('UI-triggered safe operations check')
const runningCommand = ref('')
const runError = ref('')
const latestRunResult = ref<Dict | null>(null)

function asList(value: unknown): Dict[] {
  return Array.isArray(value) ? value.filter((item) => typeof item === 'object' && item !== null) as Dict[] : []
}

function asDict(value: unknown): Dict {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Dict : {}
}

function asStringList(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)) : []
}

function statusClass(value: unknown) {
  const status = String(value || '').toLowerCase()
  if (status === 'ok' || status === 'uploaded' || status === 'review_candidate' || status === 'usable_for_manual_review') return 'bg-moss text-paper'
  if (status === 'error' || status === 'failed' || status === 'timeout' || status === 'missing_artifact' || status === 'hold_research_only' || status === 'not_usable') return 'bg-rust text-paper'
  return 'bg-sun text-ink'
}

function display(value: unknown) {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  if (typeof value === 'number') return Intl.NumberFormat('en-IN', { maximumFractionDigits: 3 }).format(value)
  return String(value)
}

function shortPath(value: unknown) {
  const text = String(value || '')
  if (text.length <= 70) return text
  return `...${text.slice(-67)}`
}

function latestMarker(log: Dict): Dict {
  return typeof log.latest_marker === 'object' && log.latest_marker !== null && !Array.isArray(log.latest_marker) ? log.latest_marker as Dict : {}
}

function tailLines(value: unknown): string[] {
  return Array.isArray(value) ? value.map((line) => String(line)) : []
}

async function refreshAll() {
  await Promise.all([refreshSmoke(), refreshCronStatus(), refreshLogs(), refreshMlGate(), refreshArtifacts(), refreshCommands(), refreshApiErrors()])
}

function commandArgs(value: unknown): string {
  if (Array.isArray(value)) return value.map((part) => String(part)).join(' ')
  return String(value || '')
}

function runTail(row: Dict, key: 'stdout_tail' | 'stderr_tail') {
  return String(row[key] || '').trim()
}

async function runCommand(command: Dict) {
  const key = String(command.key || '')
  if (!key) return
  runError.value = ''
  latestRunResult.value = null
  runningCommand.value = key
  try {
    const result = await api.runOperatorCommand({
      command_key: key,
      confirm: true,
      operator_id: operatorId.value || 'operator',
      requested_reason: requestedReason.value || 'UI-triggered safe operations check'
    })
    latestRunResult.value = result.run
    await Promise.all([refreshCommands(), refreshSmoke(), refreshCronStatus(), refreshLogs(), refreshApiErrors()])
  } catch (err) {
    runError.value = err instanceof Error ? err.message : String(err)
  } finally {
    runningCommand.value = ''
  }
}

async function runSmokeCommand() {
  if (!smokeCommand.value) return
  await runCommand(smokeCommand.value)
}
</script>

<template>
  <section class="rounded-[2rem] bg-ink p-8 text-paper shadow-soft">
    <p class="text-sm font-semibold uppercase tracking-[0.35em] text-paper/55">UI-first operations</p>
    <div class="mt-4 flex flex-wrap items-end justify-between gap-5">
      <div>
        <h1 class="max-w-4xl text-5xl font-black tracking-tight">Run the project from one auditable cockpit.</h1>
        <p class="mt-4 max-w-3xl text-lg leading-8 text-paper/70">
          Read-only operational checks, cron state, model evidence gates, and artifact status. No broker actions are triggered here.
        </p>
      </div>
      <button class="rounded-full bg-paper px-5 py-3 text-sm font-black text-ink disabled:opacity-50" :disabled="smokePending" type="button" @click="refreshAll">
        {{ smokePending ? 'Refreshing...' : 'Refresh checks' }}
      </button>
    </div>
  </section>

  <section v-if="loadErrors.length" class="mt-6 grid gap-3">
    <ApiErrorBanner v-for="row in loadErrors" :key="row.title" :title="row.title" :error="row.error" />
  </section>

  <section class="mt-6 grid gap-4 md:grid-cols-4">
    <MetricTile label="Smoke" :value="String(smoke?.status || '-').toUpperCase()" :note="smoke?.generated_at || '-'" />
    <MetricTile label="Trust" :value="String(smoke?.trust_level || smokeContract.trust_level || '-').replaceAll('_', ' ').toUpperCase()" :note="String(smoke?.trust_status || smokeContract.trust_status || '-')" />
    <MetricTile label="Fix Hints" :value="String(fixHints.length)" note="Errors and stale-data next steps" />
    <MetricTile label="ML Gate" :value="String(mlScorecard.status || mlGate?.decision || '-')" :note="String(mlScorecard.operator_action || (mlGate?.ready_for_operator_review ? 'Ready for manual review' : 'Research-only for now'))" />
    <MetricTile label="API Errors" :value="String(operatorApiErrors.length)" :note="`${recentRuns.length} command runs`" />
  </section>

  <section class="mt-8 rounded-[2rem] border border-moss/25 bg-moss/10 p-6 shadow-soft">
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div>
        <p class="text-xs font-black uppercase tracking-[0.3em] text-ink/45">One-click preflight</p>
        <h2 class="mt-2 text-3xl font-black">Run Operator Smoke Check</h2>
        <p class="mt-2 max-w-4xl text-sm leading-6 text-ink/70">
          {{ smoke?.recommendation || smokeContract.recommendation || 'Runs the compact read-only trust preflight and records the run in the audited command table.' }}
        </p>
      </div>
      <button
        class="rounded-full bg-ink px-5 py-3 text-sm font-black text-paper disabled:opacity-50"
        type="button"
        :disabled="Boolean(runningCommand) || !smokeCommand"
        @click="runSmokeCommand"
      >
        {{ runningCommand === 'operator_smoke' ? 'Running smoke...' : 'Run Smoke Check' }}
      </button>
    </div>
    <div class="mt-5 grid gap-3 md:grid-cols-4">
      <MetricTile label="Trust Level" :value="String(smoke?.trust_level || smokeContract.trust_level || '-').replaceAll('_', ' ').toUpperCase()" note="Current use boundary" />
      <MetricTile label="Trust Status" :value="String(smoke?.trust_status || smokeContract.trust_status || '-').toUpperCase()" note="Worst trust check" />
      <MetricTile label="Blockers" :value="String(smokeCounts.current_blockers ?? '-')" note="Current blocker count" />
      <MetricTile label="Next Commands" :value="String(smokeNextCommands.length)" note="Suggested fixes" />
    </div>
    <div v-if="smokeNextCommands.length" class="mt-4 grid gap-2">
      <code v-for="command in smokeNextCommands.slice(0, 4)" :key="command" class="block overflow-auto rounded-xl bg-ink px-3 py-2 text-xs text-paper">
        {{ command }}
      </code>
    </div>
  </section>

  <section class="mt-8 glass-panel rounded-3xl p-6">
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div>
        <p class="text-xs font-bold uppercase tracking-[0.3em] text-ink/45">Operator API errors</p>
        <h2 class="mt-2 text-2xl font-black">Recent frontend/API failures</h2>
        <p class="mt-2 max-w-3xl text-sm leading-6 text-ink/60">
          These are backend endpoint exceptions recorded by the API guard. Use this before checking raw server logs.
        </p>
      </div>
      <button class="rounded-full bg-ink px-4 py-2 text-sm font-black text-paper" type="button" @click="refreshApiErrors()">Refresh errors</button>
    </div>
    <div class="mt-5 grid gap-4 lg:grid-cols-2">
      <article v-for="row in operatorApiErrors" :key="String(row.error_id)" class="rounded-3xl bg-white/75 p-5">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="font-black text-ink">{{ row.route || row.operation || 'Operator API' }}</p>
            <p class="mt-1 text-sm text-ink/60">{{ row.error_type }} · {{ row.error_message }}</p>
          </div>
          <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(Number(row.status_code) >= 500 ? 'error' : 'warn')">HTTP {{ row.status_code || '-' }}</span>
        </div>
        <p class="mt-3 text-xs font-semibold text-ink/50">{{ row.occurred_at || '-' }} · {{ row.error_id || '-' }}</p>
        <details v-if="row.traceback_tail" class="mt-3">
          <summary class="cursor-pointer text-sm font-black text-rust">Show traceback tail</summary>
          <pre class="mt-3 max-h-80 overflow-auto rounded-2xl bg-ink p-4 text-xs leading-5 text-paper">{{ row.traceback_tail }}</pre>
        </details>
      </article>
      <p v-if="!operatorApiErrors.length" class="rounded-2xl bg-white/70 p-4 text-sm text-ink/60">No operator API errors returned.</p>
    </div>
  </section>

  <section class="mt-8 glass-panel rounded-3xl p-6">
    <div class="flex flex-wrap items-start justify-between gap-5">
      <div>
        <p class="text-xs font-bold uppercase tracking-[0.3em] text-ink/45">Safe command center</p>
        <h2 class="mt-2 text-2xl font-black">Run whitelisted checks</h2>
        <p class="mt-2 max-w-3xl text-sm leading-6 text-ink/60">
          These commands are read-only or dry-run and every execution is written to an audit table with output tails. Long-running ML, advisory, broker, and trading jobs are intentionally not available here.
        </p>
      </div>
      <div class="grid gap-2 md:min-w-80">
        <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
          Operator id
          <input v-model="operatorId" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss" placeholder="operator" />
        </label>
        <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
          Reason
          <input v-model="requestedReason" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss" placeholder="Why are you running this?" />
        </label>
      </div>
    </div>
    <p v-if="runError" class="mt-4 rounded-2xl bg-rust/10 p-3 text-sm font-bold text-rust">{{ runError }}</p>
    <div v-if="latestRunResult" class="mt-4 rounded-2xl bg-moss/10 p-4">
      <p class="text-sm font-black text-moss">Latest run: {{ latestRunResult.command_label || latestRunResult.command_key }} · {{ latestRunResult.status }}</p>
      <p class="mt-1 text-xs text-ink/55">Run id: {{ latestRunResult.run_id }} · elapsed {{ display(latestRunResult.elapsed_ms) }} ms</p>
    </div>
    <div class="mt-5 grid gap-4 lg:grid-cols-2">
      <article v-for="command in otherCommands" :key="String(command.key)" class="rounded-3xl bg-white/75 p-5">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="font-black text-ink">{{ command.label }}</p>
            <p class="mt-1 text-sm leading-6 text-ink/60">{{ command.description }}</p>
          </div>
          <span class="rounded-full px-3 py-1 text-xs font-black" :class="String(command.risk) === 'safe_read_only' ? 'bg-moss text-paper' : 'bg-sun text-ink'">{{ command.risk }}</span>
        </div>
        <code class="mt-4 block overflow-auto rounded-2xl bg-ink px-3 py-2 text-xs text-paper">{{ commandArgs(command.args) }}</code>
        <div class="mt-4 flex flex-wrap items-center justify-between gap-3">
          <p class="text-xs font-semibold text-ink/50">Timeout {{ display(command.timeout_seconds) }}s · dry run {{ command.dry_run ? 'yes' : 'no' }}</p>
          <button class="rounded-full bg-moss px-5 py-3 text-sm font-black text-paper disabled:opacity-50" :disabled="Boolean(runningCommand)" type="button" @click="runCommand(command)">
            {{ runningCommand === command.key ? 'Running...' : 'Run audited check' }}
          </button>
        </div>
      </article>
      <p v-if="!commandList.length" class="rounded-2xl bg-white/70 p-4 text-sm text-ink/60">No whitelisted operator commands returned by the API.</p>
    </div>
  </section>

  <section class="mt-8 grid gap-6 lg:grid-cols-[1fr_1.1fr]">
    <div class="glass-panel rounded-3xl p-6">
      <div class="flex items-start justify-between gap-4">
        <div>
          <p class="text-xs font-bold uppercase tracking-[0.3em] text-ink/45">Health smoke</p>
          <h2 class="mt-2 text-2xl font-black">Current fix hints</h2>
        </div>
        <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(smoke?.status)">{{ String(smoke?.status || 'unknown').toUpperCase() }}</span>
      </div>
      <p class="mt-3 text-sm leading-6 text-ink/60">{{ smoke?.note || 'Read-only health snapshot.' }}</p>
      <div class="mt-5 space-y-3">
        <div v-for="hint in fixHints" :key="String(hint.title || hint.command || JSON.stringify(hint))" class="rounded-2xl bg-white/75 p-4">
          <div class="flex flex-wrap items-start justify-between gap-3">
            <div>
              <p class="font-black text-ink">{{ hint.title || hint.source || 'Fix hint' }}</p>
              <p class="mt-1 text-sm leading-6 text-ink/60">{{ hint.message || hint.reason || '-' }}</p>
            </div>
            <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(hint.status)">{{ String(hint.status || 'check').toUpperCase() }}</span>
          </div>
          <code v-if="hint.command" class="mt-3 block overflow-x-auto rounded-xl bg-ink px-3 py-2 text-xs text-paper">{{ hint.command }}</code>
        </div>
        <p v-if="!fixHints.length" class="rounded-2xl bg-white/70 p-4 text-sm text-ink/60">No fix hints returned by the smoke check.</p>
      </div>
    </div>

    <div class="glass-panel rounded-3xl p-6">
      <div class="flex items-start justify-between gap-4">
        <div>
          <p class="text-xs font-bold uppercase tracking-[0.3em] text-ink/45">Research evidence</p>
          <h2 class="mt-2 text-2xl font-black">Event-model promotion gate</h2>
        </div>
        <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(mlScorecard.status || mlGate?.decision)">{{ String(mlScorecard.status || mlGate?.decision || 'unknown').toUpperCase() }}</span>
      </div>
      <p class="mt-3 text-sm leading-6 text-ink/60">
        {{ mlScorecard.headline || 'Passing this gate means “review for low-weight integration”, not auto-promotion.' }}
      </p>
      <div class="mt-4 rounded-2xl bg-ink p-4 text-paper">
        <p class="text-xs font-black uppercase tracking-[0.2em] text-paper/50">Operator action</p>
        <p class="mt-2 text-sm font-bold leading-6">{{ mlScorecard.operator_action || 'Keep model research-only until the gate passes.' }}</p>
        <p class="mt-2 text-xs text-paper/55">
          Authority: {{ mlScorecard.authority || 'research_only_manual_review' }} · broker allowed: {{ mlScorecard.broker_execution_allowed ? 'yes' : 'no' }} · auto promotion: {{ mlScorecard.policy_auto_promotion_allowed ? 'yes' : 'no' }}
        </p>
      </div>
      <div class="mt-5 grid gap-3 md:grid-cols-3">
        <div class="rounded-2xl bg-white/75 p-4">
          <p class="text-xs font-bold uppercase tracking-[0.2em] text-ink/45">Labels</p>
          <p class="mt-2 text-2xl font-black">{{ display(mlScorecardMetrics.labeled_rows ?? mlGate?.coverage?.labeled_rows) }}</p>
          <p class="mt-1 text-xs text-ink/55">rows across {{ display(mlScorecardMetrics.date_count ?? mlGate?.coverage?.date_count) }} dates</p>
        </div>
        <div class="rounded-2xl bg-white/75 p-4">
          <p class="text-xs font-bold uppercase tracking-[0.2em] text-ink/45">Weekly Runs</p>
          <p class="mt-2 text-2xl font-black">{{ display(mlScorecardMetrics.successful_runs ?? mlGate?.weekly_runs?.successful_runs) }}</p>
          <p class="mt-1 text-xs text-ink/55">last {{ display(mlGate?.weekly_runs?.since_days) }} days</p>
        </div>
        <div class="rounded-2xl bg-white/75 p-4">
          <p class="text-xs font-bold uppercase tracking-[0.2em] text-ink/45">Score Rows</p>
          <p class="mt-2 text-2xl font-black">{{ display(mlScorecardMetrics.score_rows ?? mlGate?.score_freshness?.score_rows) }}</p>
          <p class="mt-1 text-xs text-ink/55">{{ display(mlGate?.score_freshness?.latest_scored_at) }}</p>
        </div>
      </div>
      <div class="mt-3 grid gap-3 md:grid-cols-3">
        <div class="rounded-2xl bg-white/75 p-4">
          <p class="text-xs font-bold uppercase tracking-[0.2em] text-ink/45">Precision Lift</p>
          <p class="mt-2 text-2xl font-black">{{ display(mlScorecardMetrics.precision_lift_vs_positive_rate_test) }}</p>
        </div>
        <div class="rounded-2xl bg-white/75 p-4">
          <p class="text-xs font-bold uppercase tracking-[0.2em] text-ink/45">ROC-AUC</p>
          <p class="mt-2 text-2xl font-black">{{ display(mlScorecardMetrics.roc_auc) }}</p>
        </div>
        <div class="rounded-2xl bg-white/75 p-4">
          <p class="text-xs font-bold uppercase tracking-[0.2em] text-ink/45">Failed Gates</p>
          <p class="mt-2 text-2xl font-black">{{ display(mlScorecard.failed_gate_count ?? failedGates.length) }}</p>
        </div>
      </div>
      <div class="mt-5">
        <p class="text-sm font-black text-ink">Failed gates</p>
        <div class="mt-2 flex flex-wrap gap-2">
          <span v-for="gate in failedGates" :key="gate" class="rounded-full bg-rust px-3 py-1 text-xs font-black text-paper">{{ gate }}</span>
          <span v-if="!failedGates.length" class="rounded-full bg-moss px-3 py-1 text-xs font-black text-paper">none</span>
        </div>
      </div>
      <details class="mt-5">
        <summary class="cursor-pointer text-sm font-black text-ink">Show all gates</summary>
        <div class="mt-3 grid gap-2">
          <div v-for="gate in gates" :key="String(gate.gate)" class="rounded-2xl bg-white/70 p-3 text-sm">
            <div class="flex flex-wrap items-center justify-between gap-3">
              <span class="font-bold">{{ gate.gate }}</span>
              <span class="rounded-full px-3 py-1 text-xs font-black" :class="gate.passed ? 'bg-moss text-paper' : 'bg-rust text-paper'">{{ gate.passed ? 'PASS' : 'FAIL' }}</span>
            </div>
            <p class="mt-1 text-ink/60">value: {{ display(gate.value) }} | threshold: {{ display(gate.threshold) }}</p>
          </div>
        </div>
      </details>
    </div>
  </section>

  <section class="mt-8 grid gap-6 lg:grid-cols-[1fr_1fr]">
    <div class="glass-panel rounded-3xl p-6 lg:col-span-2">
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p class="text-xs font-bold uppercase tracking-[0.3em] text-ink/45">Scheduled jobs</p>
          <h2 class="mt-2 text-2xl font-black">Cron status, locks, and latest run markers</h2>
          <p class="mt-2 max-w-4xl text-sm leading-6 text-ink/60">
            Parsed from the generated crontab and joined to lock directories plus bounded log tails. This is read-only and does not start or stop jobs.
          </p>
        </div>
        <button class="rounded-full bg-ink px-4 py-2 text-sm font-black text-paper" type="button" @click="refreshCronStatus()">Refresh schedule</button>
      </div>

      <div class="mt-5 grid gap-3 md:grid-cols-5">
        <MetricTile label="Jobs" :value="String(cronJobs.length)" :note="String(cronStatus?.crontab_path || '-')" />
        <MetricTile label="OK" :value="display(cronCounts.ok || 0)" note="No issue detected" />
        <MetricTile label="Running" :value="display(cronCounts.running || 0)" note="Active lock present" />
        <MetricTile label="Warnings" :value="display(cronCounts.warning || 0)" note="Errors in tail or interrupted" />
        <MetricTile label="Errors" :value="display(cronCounts.error || 0)" note="Traceback, failure, or stale lock" />
      </div>

      <div class="mt-5 grid gap-4 xl:grid-cols-2">
        <article v-for="job in cronJobs" :key="`${job.job_name}-${job.line_no}`" class="rounded-3xl bg-white/75 p-5">
          <div class="flex flex-wrap items-start justify-between gap-3">
            <div>
              <p class="text-lg font-black text-ink">{{ job.job_name || 'cron_job' }}</p>
              <p class="mt-1 text-xs font-semibold uppercase tracking-[0.16em] text-ink/45">{{ job.schedule }}</p>
            </div>
            <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(job.status)">{{ String(job.status || 'unknown').toUpperCase() }}</span>
          </div>

          <div class="mt-4 grid gap-3 md:grid-cols-3">
            <div class="rounded-2xl bg-paper/80 p-3">
              <p class="text-xs font-black uppercase tracking-[0.18em] text-ink/45">Next Run</p>
              <p class="mt-1 text-sm font-bold text-ink/70">{{ display(job.next_run_estimate) }}</p>
            </div>
            <div class="rounded-2xl bg-paper/80 p-3">
              <p class="text-xs font-black uppercase tracking-[0.18em] text-ink/45">Last Log</p>
              <p class="mt-1 text-sm font-bold text-ink/70">{{ display(job.last_log_at) }}</p>
            </div>
            <div class="rounded-2xl bg-paper/80 p-3">
              <p class="text-xs font-black uppercase tracking-[0.18em] text-ink/45">Lock</p>
              <p class="mt-1 text-sm font-bold text-ink/70">{{ job.lock_active ? (job.lock_stale ? 'stale' : 'active') : 'clear' }}</p>
            </div>
          </div>

          <div class="mt-4 flex flex-wrap gap-2">
            <MetaChip tone="plain" label="log">{{ display(job.log_file) }}</MetaChip>
            <MetaChip v-if="job.lock_file" :tone="job.lock_stale ? 'red' : 'blue'" label="lock">{{ display(job.lock_file) }}</MetaChip>
            <MetaChip v-if="job.latest_run_status" tone="green" label="latest">{{ display(job.latest_run_status) }}</MetaChip>
          </div>

          <details class="mt-4">
            <summary class="cursor-pointer text-sm font-black text-ink">Show command and log tail</summary>
            <code class="mt-3 block max-h-28 overflow-auto rounded-2xl bg-ink px-3 py-2 text-xs text-paper">{{ job.command }}</code>
            <pre class="mt-3 max-h-56 overflow-auto rounded-2xl bg-ink p-4 text-xs leading-5 text-paper">{{ tailLines(job.tail).join('\n') }}</pre>
          </details>
        </article>
        <p v-if="!cronJobs.length" class="rounded-2xl bg-white/70 p-4 text-sm text-ink/60">No scheduled jobs parsed from generated crontab.</p>
      </div>
    </div>

    <div class="glass-panel rounded-3xl p-6">
      <div class="flex items-start justify-between gap-4">
        <div>
          <p class="text-xs font-bold uppercase tracking-[0.3em] text-ink/45">Artifacts</p>
          <h2 class="mt-2 text-2xl font-black">Event-model S3 backup</h2>
        </div>
        <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(artifacts?.status)">{{ String(artifacts?.status || 'unknown').toUpperCase() }}</span>
      </div>
      <p class="mt-3 text-sm leading-6 text-ink/60">Local manifest plus latest S3 object heads when credentials are available.</p>
      <div class="mt-5 rounded-2xl bg-white/75 p-4 text-sm">
        <p><b>Version:</b> {{ display(artifacts?.artifact?.model_version) }}</p>
        <p><b>Latest prefix:</b> {{ shortPath(artifacts?.artifact?.latest_prefix) }}</p>
        <p><b>Version prefix:</b> {{ shortPath(artifacts?.artifact?.version_prefix) }}</p>
        <p><b>Files shown:</b> {{ display(artifactPageMeta.returned_count) }} / {{ display(artifactPageMeta.total_count) }}</p>
      </div>
      <div class="mt-4 space-y-3">
        <div v-for="row in latestHeads" :key="String(row.key || row.status)" class="rounded-2xl bg-white/70 p-3 text-sm">
          <div class="flex flex-wrap items-start justify-between gap-3">
            <p class="break-all font-bold">{{ shortPath(row.key || row.error || row.status) }}</p>
            <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(row.status)">{{ String(row.status || 'unknown').toUpperCase() }}</span>
          </div>
          <p class="mt-1 text-ink/55">bytes: {{ display(row.content_length) }} | modified: {{ display(row.last_modified) }}</p>
        </div>
      </div>
    </div>

    <div class="glass-panel rounded-3xl p-6">
      <div>
        <p class="text-xs font-bold uppercase tracking-[0.3em] text-ink/45">Cron and logs</p>
        <h2 class="mt-2 text-2xl font-black">Latest run tails</h2>
        <p class="mt-2 text-sm font-semibold text-ink/55">Showing {{ display(logPageMeta.returned_count) }} / {{ display(logPageMeta.total_count) }} log files.</p>
      </div>
      <div class="mt-5 space-y-4">
        <details v-for="log in logs" :key="String(log.name)" class="rounded-2xl bg-white/75 p-4">
          <summary class="cursor-pointer">
            <span class="font-black">{{ log.name }}</span>
            <span class="ml-2 rounded-full px-3 py-1 text-xs font-black" :class="statusClass(log.status)">{{ String(log.status || 'unknown').toUpperCase() }}</span>
          </summary>
          <p class="mt-2 text-xs text-ink/55">Modified {{ display(log.modified_at) }} | {{ display(log.bytes) }} bytes</p>
          <p v-if="latestMarker(log).status" class="mt-1 text-xs text-ink/55">Latest marker: {{ latestMarker(log).status }} at {{ latestMarker(log).timestamp }}</p>
          <pre class="mt-3 max-h-80 overflow-auto rounded-2xl bg-ink p-4 text-xs leading-5 text-paper">{{ asStringList(log.tail).join('\n') }}</pre>
        </details>
        <p v-if="!logs.length" class="rounded-2xl bg-white/70 p-4 text-sm text-ink/60">No cron logs found.</p>
      </div>
    </div>
  </section>

  <section class="mt-8 glass-panel rounded-3xl p-6">
    <div>
      <p class="text-xs font-bold uppercase tracking-[0.3em] text-ink/45">Audit history</p>
      <h2 class="mt-2 text-2xl font-black">Recent UI-triggered command runs</h2>
    </div>
    <div class="mt-5 space-y-4">
      <details v-for="run in recentRuns" :key="String(run.run_id)" class="rounded-2xl bg-white/75 p-4">
        <summary class="cursor-pointer">
          <span class="font-black">{{ run.command_label || run.command_key }}</span>
          <span class="ml-2 rounded-full px-3 py-1 text-xs font-black" :class="statusClass(run.status)">{{ String(run.status || 'unknown').toUpperCase() }}</span>
        </summary>
        <div class="mt-3 grid gap-2 text-sm md:grid-cols-4">
          <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Started:</b> {{ display(run.started_at) }}</p>
          <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Elapsed:</b> {{ display(run.elapsed_ms) }} ms</p>
          <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Exit:</b> {{ display(run.returncode) }}</p>
          <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Operator:</b> {{ display(run.operator_id) }}</p>
        </div>
        <p class="mt-2 text-sm text-ink/60">{{ run.requested_reason || '-' }}</p>
        <code class="mt-3 block overflow-auto rounded-2xl bg-ink px-3 py-2 text-xs text-paper">{{ commandArgs(run.command_args) }}</code>
        <pre v-if="runTail(run, 'stdout_tail')" class="mt-3 max-h-72 overflow-auto rounded-2xl bg-ink p-4 text-xs leading-5 text-paper">{{ runTail(run, 'stdout_tail') }}</pre>
        <pre v-if="runTail(run, 'stderr_tail')" class="mt-3 max-h-72 overflow-auto rounded-2xl bg-rust p-4 text-xs leading-5 text-paper">{{ runTail(run, 'stderr_tail') }}</pre>
        <p v-if="run.error" class="mt-3 rounded-2xl bg-rust/10 p-3 text-sm font-bold text-rust">{{ run.error }}</p>
      </details>
      <p v-if="!recentRuns.length" class="rounded-2xl bg-white/70 p-4 text-sm text-ink/60">No UI-triggered command runs recorded yet.</p>
    </div>
  </section>
</template>
