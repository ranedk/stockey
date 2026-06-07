<script setup lang="ts">
import type { Dict } from '~/types/api'

const api = useOperatorApi()
const { data, refresh, pending, error } = await useAsyncData('action-conflict-rules', () => api.getActionConflictRules(), {
  lazy: true,
  server: false
})

const rules = computed(() => {
  const rows = data.value?.rules
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
    <MetricTile label="Manual Required" :value="String(rules.filter((row) => String(row.resolution_action || '').includes('manual')).length)" note="Escalation rules" />
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
          <p class="mt-2 max-w-4xl text-sm leading-6 text-ink/65">{{ rule.reason || rule.description || 'No rule explanation was stored.' }}</p>
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
