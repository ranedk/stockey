<script setup lang="ts">
import type { Dict } from '~/types/api'

const api = useOperatorApi()
const { data, refresh, pending, error } = await useAsyncData('action-conflict-rules', () => api.getActionConflictRules(), {
  lazy: true,
  server: false
})
const ruleUpdates = reactive<Record<string, {
  pending: boolean
  error: string
  message: string
  editReason: string
  condition: {
    winning_action_code: string
    losing_action_code: string
    winning_source: string
    losing_source: string
  }
}>>({})
const promoteUpdates = reactive<Record<string, { pending: boolean, error: string, message: string }>>({})

const rules = computed(() => {
  const rows = data.value?.rules
  return Array.isArray(rows) ? rows.filter((row) => typeof row === 'object' && row !== null) as Dict[] : []
})

const unresolvedConflicts = computed(() => {
  const rows = data.value?.unresolved_conflicts
  return Array.isArray(rows) ? rows.filter((row) => typeof row === 'object' && row !== null) as Dict[] : []
})

function display(value: unknown) {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  return String(value)
}

function ruleTone(row: Dict): 'success' | 'warning' | 'neutral' {
  if (row.enabled === false) return 'neutral'
  const action = String(row.resolution_action || '').toLowerCase()
  if (action.includes('manual')) return 'warning'
  return 'success'
}

function currentReason(row: Dict) {
  return String(row.resolution_reason || row.reason || row.description || '')
}

function parseCondition(row: Dict) {
  const raw = row.condition_json
  let condition: Dict = {}
  if (typeof raw === 'string' && raw.trim()) {
    try {
      const parsed = JSON.parse(raw)
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) condition = parsed as Dict
    } catch {
      condition = {}
    }
  } else if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
    condition = raw as Dict
  }
  return {
    winning_action_code: String(condition.winning_action_code || ''),
    losing_action_code: String(condition.losing_action_code || ''),
    winning_source: String(condition.winning_source || ''),
    losing_source: String(condition.losing_source || '')
  }
}

function hasEditableCondition(row: Dict) {
  return Boolean(row.condition_json || row.promoted_from_conflict_key || String(row.rule_scope || '') === 'manual_resolution')
}

function ruleState(ruleId: unknown, row?: Dict) {
  const key = String(ruleId || '')
  if (!ruleUpdates[key]) {
    ruleUpdates[key] = {
      pending: false,
      error: '',
      message: '',
      editReason: row ? currentReason(row) : '',
      condition: row ? parseCondition(row) : {
        winning_action_code: '',
        losing_action_code: '',
        winning_source: '',
        losing_source: ''
      }
    }
  }
  return ruleUpdates[key]
}

async function toggleRule(rule: Dict) {
  const ruleId = String(rule.rule_id || '').trim()
  if (!ruleId) return
  const state = ruleState(ruleId, rule)
  const enabled = rule.enabled === false
  state.pending = true
  state.error = ''
  state.message = ''
  try {
    await api.updateActionConflictRule(ruleId, { enabled })
    state.message = enabled ? 'Rule enabled for future ranking.' : 'Rule disabled for future ranking.'
    await refresh()
  } catch (err) {
    state.error = err instanceof Error ? err.message : 'Could not update rule.'
  } finally {
    state.pending = false
  }
}

async function saveReason(rule: Dict) {
  const ruleId = String(rule.rule_id || '').trim()
  if (!ruleId) return
  const state = ruleState(ruleId, rule)
  const reason = String(state.editReason || '').trim()
  if (!reason) {
    state.error = 'Rule explanation is required.'
    state.message = ''
    return
  }
  state.pending = true
  state.error = ''
  state.message = ''
  try {
    await api.updateActionConflictRule(ruleId, { resolution_reason: reason })
    state.message = 'Rule explanation saved.'
    await refresh()
  } catch (err) {
    state.error = err instanceof Error ? err.message : 'Could not save rule explanation.'
  } finally {
    state.pending = false
  }
}

function resetReason(rule: Dict) {
  const state = ruleState(rule.rule_id, rule)
  state.editReason = currentReason(rule)
  state.error = ''
  state.message = ''
}

async function saveCondition(rule: Dict) {
  const ruleId = String(rule.rule_id || '').trim()
  if (!ruleId) return
  const state = ruleState(ruleId, rule)
  const condition = {
    condition_type: 'action_pair_exact',
    winning_action_code: state.condition.winning_action_code.trim(),
    losing_action_code: state.condition.losing_action_code.trim(),
    winning_source: state.condition.winning_source.trim(),
    losing_source: state.condition.losing_source.trim()
  }
  if (!condition.winning_action_code || !condition.losing_action_code) {
    state.error = 'Winner and loser actions are required.'
    state.message = ''
    return
  }
  state.pending = true
  state.error = ''
  state.message = ''
  try {
    await api.updateActionConflictRule(ruleId, { condition })
    state.message = 'Rule condition saved.'
    await refresh()
  } catch (err) {
    state.error = err instanceof Error ? err.message : 'Could not save rule condition.'
  } finally {
    state.pending = false
  }
}

function resetCondition(rule: Dict) {
  const state = ruleState(rule.rule_id, rule)
  state.condition = parseCondition(rule)
  state.error = ''
  state.message = ''
}

function conflictKey(row: Dict) {
  return [
    row.asof_date,
    row.symbol,
    row.winning_action_code,
    row.losing_action_code,
    row.winning_source,
    row.losing_source,
    row.losing_setup_id,
    row.losing_unique_id
  ].map((value) => String(value || '')).join(':')
}

function promoteState(row: Dict) {
  const key = conflictKey(row)
  if (!promoteUpdates[key]) promoteUpdates[key] = { pending: false, error: '', message: '' }
  return promoteUpdates[key]
}

async function promoteConflict(row: Dict) {
  const state = promoteState(row)
  state.pending = true
  state.error = ''
  state.message = ''
  try {
    await api.promoteActionConflictRule({
      conflict: row,
      resolution_reason: row.resolution_reason || row.lost_reason || `${display(row.winning_action_code)} wins over ${display(row.losing_action_code)}.`,
      enabled: false
    })
    state.message = 'Promoted as a disabled rule candidate.'
    await refresh()
  } catch (err) {
    state.error = err instanceof Error ? err.message : 'Could not promote conflict.'
  } finally {
    state.pending = false
  }
}
</script>

<template>
  <section class="rounded-[2rem] bg-ink p-8 text-paper shadow-soft">
    <p class="text-sm font-semibold uppercase tracking-[0.35em] text-paper/55">Conflict Rules</p>
    <div class="mt-4 flex flex-wrap items-end justify-between gap-5">
      <div>
        <h1 class="max-w-4xl text-5xl font-black tracking-tight">How competing action signals are resolved.</h1>
        <p class="mt-4 max-w-3xl text-lg leading-8 text-paper/70">
          These deterministic rules explain why one action wins when the same symbol has buy, sell, watch, hold, event, or lifecycle candidates.
        </p>
      </div>
      <button class="rounded-2xl bg-paper px-5 py-3 text-sm font-black text-ink disabled:opacity-50" :disabled="pending" type="button" @click="refresh()">
        {{ pending ? 'Refreshing...' : 'Refresh rules' }}
      </button>
    </div>
  </section>

  <section v-if="error" class="mt-6">
    <ApiErrorBanner title="Conflict rules failed" :error="error" />
  </section>

  <section class="mt-6 grid gap-4 md:grid-cols-3">
    <MetricTile label="Rules" :value="String(data?.row_count || rules.length || 0)" note="Deterministic conflict policies" />
    <MetricTile label="Enabled" :value="String(rules.filter((row) => row.enabled !== false).length)" note="Currently active" />
    <MetricTile label="Unresolved" :value="String(data?.unresolved_count || unresolvedConflicts.length || 0)" note="Promotable latest conflicts" />
  </section>

  <section v-if="unresolvedConflicts.length" class="mt-8 rounded-3xl border border-black/10 bg-white/80 p-5 shadow-soft">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 class="text-2xl font-black text-ink">Promote unresolved conflicts</h2>
        <p class="mt-2 max-w-3xl text-sm leading-6 text-ink/60">
          Promotion creates an exact action/source match rule candidate. It stays disabled until an operator enables it.
        </p>
      </div>
      <MetaChip label="scope" tone="blue">manual resolution</MetaChip>
    </div>
    <div class="mt-4 overflow-x-auto">
      <table class="min-w-full text-left text-sm">
        <thead class="text-xs uppercase tracking-[0.2em] text-ink/45">
          <tr>
            <th class="px-3 py-2">Symbol</th>
            <th class="px-3 py-2">Winner</th>
            <th class="px-3 py-2">Loser</th>
            <th class="px-3 py-2">Reason</th>
            <th class="px-3 py-2"></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="conflict in unresolvedConflicts" :key="conflictKey(conflict)" class="border-t border-black/10 align-top">
            <td class="px-3 py-3 font-black text-ink">{{ display(conflict.symbol) }}</td>
            <td class="px-3 py-3">
              <p class="font-black text-ink">{{ display(conflict.winning_action_code) }}</p>
              <p class="text-xs font-semibold text-ink/50">{{ display(conflict.winning_source) }}</p>
            </td>
            <td class="px-3 py-3">
              <p class="font-black text-ink">{{ display(conflict.losing_action_code) }}</p>
              <p class="text-xs font-semibold text-ink/50">{{ display(conflict.losing_source) }}</p>
            </td>
            <td class="max-w-xl px-3 py-3 text-xs font-semibold leading-5 text-ink/60">{{ display(conflict.lost_reason || conflict.resolution_reason) }}</td>
            <td class="px-3 py-3 text-right">
              <button
                class="rounded-2xl bg-ink px-4 py-2 text-xs font-black text-paper disabled:opacity-50"
                type="button"
                :disabled="promoteState(conflict).pending"
                @click="promoteConflict(conflict)"
              >
                {{ promoteState(conflict).pending ? 'Promoting...' : 'Promote' }}
              </button>
              <p v-if="promoteState(conflict).message" class="mt-2 text-xs font-semibold text-moss">{{ promoteState(conflict).message }}</p>
              <p v-if="promoteState(conflict).error" class="mt-2 text-xs font-semibold text-ember">{{ promoteState(conflict).error }}</p>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>

  <section class="mt-8 space-y-4">
    <article
      v-for="rule in rules"
      :key="String(rule.rule_id)"
      class="rounded-3xl border border-black/10 bg-gradient-to-br from-white/85 via-white/75 to-sky/10 p-5 shadow-soft"
    >
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div class="flex flex-wrap gap-2">
            <StatusPill :tone="ruleTone(rule)" :title="rule.enabled === false ? 'This rule is present but disabled.' : 'This rule is active during action conflict resolution.'">
              {{ rule.enabled === false ? 'DISABLED' : 'ENABLED' }}
            </StatusPill>
            <MetaChip label="priority" tone="blue" title="Higher-priority rules are evaluated first by the resolver.">{{ display(rule.priority) }}</MetaChip>
            <MetaChip label="action" tone="yellow" title="What the resolver does when this rule matches.">{{ display(rule.resolution_action) }}</MetaChip>
          </div>
          <h2 class="mt-3 text-2xl font-black text-ink">{{ rule.rule_id }}</h2>
          <p class="mt-2 max-w-4xl text-sm leading-6 text-ink/65">{{ currentReason(rule) || 'No rule explanation was stored.' }}</p>
        </div>
        <div class="flex flex-col items-end gap-2">
          <button
            class="rounded-2xl border border-black/10 bg-ink px-4 py-2 text-sm font-black text-paper disabled:opacity-50"
            type="button"
            :disabled="ruleState(rule.rule_id, rule).pending"
            :title="rule.enabled === false ? 'Enable this deterministic conflict rule for future ranking.' : 'Disable this deterministic conflict rule for future ranking.'"
            @click="toggleRule(rule)"
          >
            {{ ruleState(rule.rule_id, rule).pending ? 'Updating...' : rule.enabled === false ? 'Enable rule' : 'Disable rule' }}
          </button>
        </div>
      </div>
      <div class="mt-4 rounded-2xl border border-black/10 bg-white/70 p-4">
        <label class="text-xs font-black uppercase tracking-[0.25em] text-ink/50" :for="`rule-reason-${rule.rule_id}`">Rule explanation</label>
        <textarea
          :id="`rule-reason-${rule.rule_id}`"
          v-model="ruleState(rule.rule_id, rule).editReason"
          class="mt-2 min-h-24 w-full resize-y rounded-2xl border border-black/10 bg-white px-3 py-2 text-sm font-semibold leading-6 text-ink outline-none focus:border-moss"
          maxlength="2000"
        />
        <div class="mt-3 flex flex-wrap items-center justify-between gap-3">
          <div>
            <p v-if="ruleState(rule.rule_id, rule).message" class="text-xs font-semibold text-moss">{{ ruleState(rule.rule_id, rule).message }}</p>
            <p v-if="ruleState(rule.rule_id, rule).error" class="text-xs font-semibold text-ember">{{ ruleState(rule.rule_id, rule).error }}</p>
          </div>
          <div class="flex flex-wrap gap-2">
            <button
              class="rounded-2xl border border-black/10 bg-white px-4 py-2 text-sm font-black text-ink disabled:opacity-50"
              type="button"
              :disabled="ruleState(rule.rule_id, rule).pending"
              @click="resetReason(rule)"
            >
              Reset text
            </button>
            <button
              class="rounded-2xl border border-black/10 bg-moss px-4 py-2 text-sm font-black text-white disabled:opacity-50"
              type="button"
              :disabled="ruleState(rule.rule_id, rule).pending"
              @click="saveReason(rule)"
            >
              {{ ruleState(rule.rule_id, rule).pending ? 'Saving...' : 'Save text' }}
            </button>
          </div>
        </div>
      </div>
      <div v-if="hasEditableCondition(rule)" class="mt-4 rounded-2xl border border-black/10 bg-white/70 p-4">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/50">Exact-match condition</p>
            <p class="mt-1 text-xs font-semibold leading-5 text-ink/55">Promoted rules match a winning action/source against a losing action/source for the same symbol/date.</p>
          </div>
          <MetaChip label="type" tone="blue">action_pair_exact</MetaChip>
        </div>
        <div class="mt-4 grid gap-3 md:grid-cols-4">
          <label class="text-xs font-black uppercase tracking-[0.16em] text-ink/50" :for="`rule-win-action-${rule.rule_id}`">
            Winner action
            <input
              :id="`rule-win-action-${rule.rule_id}`"
              v-model="ruleState(rule.rule_id, rule).condition.winning_action_code"
              class="mt-2 w-full rounded-xl border border-black/10 bg-white px-3 py-2 text-sm font-semibold uppercase text-ink outline-none focus:border-moss"
              maxlength="64"
            >
          </label>
          <label class="text-xs font-black uppercase tracking-[0.16em] text-ink/50" :for="`rule-lose-action-${rule.rule_id}`">
            Loser action
            <input
              :id="`rule-lose-action-${rule.rule_id}`"
              v-model="ruleState(rule.rule_id, rule).condition.losing_action_code"
              class="mt-2 w-full rounded-xl border border-black/10 bg-white px-3 py-2 text-sm font-semibold uppercase text-ink outline-none focus:border-moss"
              maxlength="64"
            >
          </label>
          <label class="text-xs font-black uppercase tracking-[0.16em] text-ink/50" :for="`rule-win-source-${rule.rule_id}`">
            Winner source
            <input
              :id="`rule-win-source-${rule.rule_id}`"
              v-model="ruleState(rule.rule_id, rule).condition.winning_source"
              class="mt-2 w-full rounded-xl border border-black/10 bg-white px-3 py-2 text-sm font-semibold text-ink outline-none focus:border-moss"
              maxlength="80"
            >
          </label>
          <label class="text-xs font-black uppercase tracking-[0.16em] text-ink/50" :for="`rule-lose-source-${rule.rule_id}`">
            Loser source
            <input
              :id="`rule-lose-source-${rule.rule_id}`"
              v-model="ruleState(rule.rule_id, rule).condition.losing_source"
              class="mt-2 w-full rounded-xl border border-black/10 bg-white px-3 py-2 text-sm font-semibold text-ink outline-none focus:border-moss"
              maxlength="80"
            >
          </label>
        </div>
        <div class="mt-3 flex justify-end gap-2">
          <button
            class="rounded-2xl border border-black/10 bg-white px-4 py-2 text-sm font-black text-ink disabled:opacity-50"
            type="button"
            :disabled="ruleState(rule.rule_id, rule).pending"
            @click="resetCondition(rule)"
          >
            Reset condition
          </button>
          <button
            class="rounded-2xl border border-black/10 bg-moss px-4 py-2 text-sm font-black text-white disabled:opacity-50"
            type="button"
            :disabled="ruleState(rule.rule_id, rule).pending"
            @click="saveCondition(rule)"
          >
            {{ ruleState(rule.rule_id, rule).pending ? 'Saving...' : 'Save condition' }}
          </button>
        </div>
      </div>
      <div class="mt-4 grid gap-2 text-sm md:grid-cols-3">
        <p class="rounded-xl bg-white/80 px-3 py-2"><b>Status:</b> {{ display(rule.resolution_status) }}</p>
        <p class="rounded-xl bg-white/80 px-3 py-2"><b>Updated:</b> {{ display(rule.updated_at || rule.load_ts) }}</p>
        <p class="rounded-xl bg-white/80 px-3 py-2"><b>Created:</b> {{ display(rule.created_at) }}</p>
      </div>
      <details class="mt-4 rounded-2xl border border-black/10 bg-white/60 px-4 py-3">
        <summary class="cursor-pointer text-sm font-black text-moss">Show raw rule</summary>
        <pre class="mt-3 max-h-80 overflow-auto rounded-2xl bg-ink p-4 text-xs leading-5 text-paper">{{ JSON.stringify(rule, null, 2) }}</pre>
      </details>
    </article>
    <p v-if="!rules.length" class="rounded-3xl bg-white/75 p-6 text-sm font-semibold text-ink/55">
      No conflict rules found. Run the action conflict resolver or advisory pipeline so `advisory_action_conflict_rules` is created.
    </p>
  </section>
</template>
