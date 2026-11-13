<script setup lang="ts">
// Per-table size and staleness from the nightly coverage job. The trend line is days behind
// over recent nightly reports: a rising line is a feed degrading before it fails outright.
const props = defineProps<{ data: OpsCoverage }>()
const tables = computed(() => props.data.tables ?? [])
const problems = computed(() => tables.value.filter(t => t.status !== 'ok'))
const view = ref<'problems' | 'all'>(props.data.not_ok_count ? 'problems' : 'all')
const visible = computed(() => (view.value === 'problems' ? problems.value : tables.value))
const tabs = computed(() => [
  { label: `Problems (${problems.value.length})`, value: 'problems' },
  { label: `All tables (${tables.value.length})`, value: 'all' },
])
const KIND: Record<string, { label: string, about: string }> = {
  daily: { label: 'Daily', about: 'Expected to gain new rows every trading day; flagged when it falls behind.' },
  informational: { label: 'Info only', about: 'Reference table; its size is reported but staleness is not checked.' },
  frozen: { label: 'Archive', about: 'Frozen archive, no longer written; staleness does not apply.' },
}
const kind = (t: OpsCoverageTable) => KIND[t.check_kind ?? ''] ?? { label: t.check_kind || '—', about: '' }
const history = (name: string) => (props.data.staleness_history?.[name] ?? []).map(p => p.staleness_days)
const statusLevel = (s: string) => (s === 'ok' ? 'ok' : s === 'error' || s === 'fail' ? 'error' : 'warn')
</script>

<template>
  <div class="space-y-3">
    <UTabs v-model="view" :items="tabs" size="xs" :content="false" class="w-fit" />
    <div class="overflow-x-auto rounded-lg border border-default">
      <table class="w-full min-w-[820px] text-sm">
        <thead class="bg-elevated/60 text-left text-xs text-muted">
          <tr>
            <th class="px-3 py-2 font-medium">Table</th>
            <th class="px-3 py-2 font-medium">Check</th>
            <th class="px-3 py-2 text-right font-medium">Rows</th>
            <th class="px-3 py-2 text-right font-medium">Stocks</th>
            <th class="px-3 py-2 font-medium">Data up to</th>
            <th class="px-3 py-2 text-right font-medium">
              <UTooltip text="Days between the newest row and the report date"><span class="underline decoration-dotted underline-offset-2">Days behind</span></UTooltip>
            </th>
            <th class="px-3 py-2 font-medium">Trend</th>
            <th class="px-3 py-2 font-medium">Status</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-default">
          <tr v-if="!visible.length">
            <td colspan="8" class="px-3 py-6 text-center text-sm text-muted">
              <UIcon name="i-lucide-circle-check" class="mr-1 inline size-4 align-text-bottom text-success" />No table has a problem in the latest report.
            </td>
          </tr>
          <tr v-for="t in visible" :key="t.table_name" class="align-middle">
            <td class="px-3 py-2">
              <div class="font-mono text-xs font-medium text-highlighted">{{ t.table_name }}</div>
              <div class="text-xs text-muted">{{ t.category || '—' }}</div>
            </td>
            <td class="px-3 py-2">
              <UTooltip :text="[kind(t).about, t.detail].filter(Boolean).join(' · ')" :disabled="!kind(t).about && !t.detail">
                <span class="whitespace-nowrap text-xs text-default">{{ kind(t).label }}</span>
              </UTooltip>
            </td>
            <td class="num px-3 py-2 text-right text-default">{{ num(t.rows) }}</td>
            <td class="num px-3 py-2 text-right text-muted">{{ num(t.symbols) }}</td>
            <td class="num whitespace-nowrap px-3 py-2 text-muted">{{ opsDate(t.max_date) }}</td>
            <td class="num px-3 py-2 text-right" :class="t.check_kind === 'daily' && (t.staleness_days ?? 0) > 3 ? 'font-medium text-warning' : 'text-default'">{{ num(t.staleness_days) }}</td>
            <td class="px-3 py-2"><OpsStaleSpark :points="history(t.table_name)" :warn="t.status !== 'ok'" /></td>
            <td class="px-3 py-2"><OpsStatusBadge :level="statusLevel(t.status)" :label="t.status === 'ok' ? 'OK' : t.status" size="xs" /></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
