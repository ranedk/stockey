<script setup lang="ts">
// Status per collector, reconciled against the pipeline's live registry. The status table
// (advisory_sync_state) is never pruned, so deleted/renamed modules keep their last error
// forever; those and deliberately paused ones are folded away instead of counted as failures.
const props = defineProps<{ data: OpsCollectors }>()
const showQuiet = ref(false)

const rows = computed(() => props.data.collectors ?? [])
const failing = computed(() => rows.value.filter(c => c.classification === 'failing'))
const healthy = computed(() => rows.value.filter(c => c.classification === 'ok')
  .sort((a, b) => opsCollectorName(a).source.localeCompare(opsCollectorName(b).source) || opsCollectorName(a).name.localeCompare(opsCollectorName(b).name)))
const quiet = computed(() => rows.value.filter(c => c.classification === 'frozen' || c.classification === 'orphaned'))
const parts = computed(() => [
  { label: 'OK', value: healthy.value.length, color: 'var(--up)' },
  { label: 'Failing', value: failing.value.length, color: 'var(--down)' },
  { label: 'Paused on purpose', value: rows.value.filter(c => c.classification === 'frozen').length, color: 'var(--chart-axis)' },
  { label: 'Left-over rows', value: rows.value.filter(c => c.classification === 'orphaned').length, color: 'var(--chart-grid)' },
].filter(p => p.value > 0))
const key = (c: OpsCollector) => `${c.source_name}::${c.scope_key ?? ''}`
</script>

<template>
  <div class="space-y-4">
    <SegmentedBar :parts="parts" :height="10" />

    <div v-if="failing.length === 0" class="flex items-center gap-2 rounded-lg bg-success/10 px-3 py-2.5 text-sm text-success">
      <UIcon name="i-lucide-circle-check" class="size-4 shrink-0" />
      Every collector in the live registry is reporting OK.
    </div>

    <UCard v-for="c in failing" :key="key(c)" class="ring-error/40" :ui="{ body: 'p-4 sm:p-4 space-y-2' }">
      <div class="flex flex-wrap items-center gap-2">
        <OpsStatusBadge level="error" :tip="OPS_COLLECTOR_CLASS.failing.about" />
        <span class="font-medium text-highlighted">{{ opsCollectorName(c).source }} · {{ opsCollectorName(c).name }}</span>
        <code class="text-xs text-dimmed">{{ c.module }}</code>
        <code v-if="c.scope_key" class="text-xs text-dimmed">scope {{ c.scope_key }}</code>
      </div>
      <!-- Error text verbatim: summarising a failure into a word is how a root cause gets lost. -->
      <pre v-if="c.error_text" class="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md bg-elevated p-3 font-mono text-xs text-default">{{ c.error_text }}</pre>
      <p class="text-xs text-muted">
        Last success: {{ c.last_success_at ? opsDateTime(c.last_success_at) : 'never' }}
        · status updated {{ opsDateTime(c.updated_at) }}
      </p>
    </UCard>

    <div v-if="healthy.length">
      <h4 class="mb-2 text-xs font-medium text-muted">Healthy collectors · last successful run</h4>
      <ul class="grid gap-x-6 sm:grid-cols-2 xl:grid-cols-3">
        <li v-for="c in healthy" :key="key(c)" class="flex items-center gap-2 border-b border-default py-2 text-sm">
          <UIcon name="i-lucide-circle-check" class="size-4 shrink-0 text-success" aria-label="OK" />
          <div class="min-w-0 flex-1">
            <UTooltip :text="c.source_name">
              <div class="truncate text-highlighted">{{ opsCollectorName(c).name }}</div>
            </UTooltip>
            <div class="truncate text-xs text-muted">{{ opsCollectorName(c).source }}<template v-if="c.scope_key && c.scope_key !== 'default'"> · {{ c.scope_key }}</template></div>
          </div>
          <UTooltip :text="`Last success ${opsDateTime(c.last_success_at)}`">
            <span class="num shrink-0 text-xs text-muted">{{ opsAgo(c.last_success_at) }}</span>
          </UTooltip>
        </li>
      </ul>
    </div>

    <div v-if="quiet.length">
      <UButton
        :label="`${showQuiet ? 'Hide' : 'Show'} ${quiet.length} paused or left-over row${quiet.length === 1 ? '' : 's'}`"
        variant="link" color="neutral" size="xs" class="px-0"
        :trailing-icon="showQuiet ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'"
        @click="showQuiet = !showQuiet"
      />
      <div v-if="showQuiet" class="mt-2 overflow-x-auto rounded-lg border border-default">
        <table class="w-full min-w-[640px] text-xs">
          <thead class="bg-elevated/60 text-left text-muted">
            <tr>
              <th class="px-3 py-2 font-medium">Source</th>
              <th class="px-3 py-2 font-medium">Kind</th>
              <th class="px-3 py-2 font-medium">Why it is not a failure</th>
              <th class="px-3 py-2 font-medium">Last status</th>
              <th class="px-3 py-2 font-medium">Updated</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-default">
            <tr v-for="c in quiet" :key="key(c)">
              <td class="px-3 py-1.5 font-mono text-highlighted">{{ c.source_name }}</td>
              <td class="px-3 py-1.5">
                <UBadge :label="OPS_COLLECTOR_CLASS[c.classification].label" :icon="OPS_COLLECTOR_CLASS[c.classification].icon" color="neutral" variant="subtle" size="sm" class="whitespace-nowrap" />
              </td>
              <td class="px-3 py-1.5 text-muted">{{ OPS_COLLECTOR_CLASS[c.classification].about }}</td>
              <td class="px-3 py-1.5 text-muted">{{ c.status }}</td>
              <td class="num whitespace-nowrap px-3 py-1.5 text-muted">{{ opsDate(c.updated_at) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>
