<script setup lang="ts">
import type { CollectorClassification, CollectorRow, CollectorsResponse } from '~/types/api'

// The section that would have shown the 2026-08-26..31 Dhan outage on day one.
// advisory_sync_state has carried per-collector status all along; nothing read it
// except a nightly log.
const api = useApi()
const { data, status, error } = await useAsyncData(
  'collectors',
  () => api.get<CollectorsResponse>('/api/collectors'),
)

// Deliberately-noisy rows are collapsed by default. `orphaned` rows can NEVER go
// green -- the module is gone and nothing prunes the table -- so showing them beside
// live failures is what turns a health page into wallpaper.
const showQuiet = ref(false)

const CLASS_META: Record<CollectorClassification, { label: string; tone: 'bad' | 'warn' | 'neutral' | 'good'; blurb: string }> = {
  failing: { label: 'Failing', tone: 'bad', blurb: 'Live collector, currently erroring.' },
  frozen: { label: 'Frozen by design', tone: 'neutral', blurb: 'Deliberately unscheduled — any error is frozen history, not a live failure.' },
  orphaned: { label: 'Orphaned row', tone: 'neutral', blurb: 'Module no longer in the registry (deleted or renamed). Stale state, not a failure.' },
  ok: { label: 'OK', tone: 'good', blurb: '' },
}

const failing = computed(() => (data.value?.collectors ?? []).filter(c => c.classification === 'failing'))
const healthy = computed(() => (data.value?.collectors ?? []).filter(c => c.classification === 'ok'))
const quiet = computed(() =>
  (data.value?.collectors ?? []).filter(c => c.classification === 'frozen' || c.classification === 'orphaned'),
)

function rowKey(c: CollectorRow): string {
  return `${c.source_name}::${c.scope_key ?? ''}`
}
</script>

<template>
  <section class="mt-6">
    <div class="flex flex-wrap items-center gap-3">
      <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-500">Collectors</h2>
      <BadgePill
        v-if="data"
        :label="data.failing_count === 0 ? 'all live collectors healthy' : `${data.failing_count} failing`"
        :tone="data.failing_count === 0 ? 'good' : 'bad'"
      />
      <span v-if="data" class="text-xs text-slate-400">
        {{ healthy.length }} ok · {{ quiet.length }} frozen/orphaned · {{ data.registry_module_count }} modules in the live registry
      </span>
    </div>

    <p class="mt-1 text-xs text-slate-400">
      Status per collector, reconciled against the pipeline's live registry.
      <code>advisory_sync_state</code> is never pruned, so a deleted or renamed module keeps
      its last error row forever — those are separated out rather than counted as failures.
    </p>

    <div v-if="error" class="mt-3 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">
      Could not load collector status ({{ error.message }}).
    </div>
    <div v-else-if="status === 'pending'" class="mt-3 text-sm text-slate-500">Loading…</div>

    <template v-else-if="data">
      <div v-if="failing.length === 0" class="mt-3 rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800">
        Every collector in the live registry is reporting OK.
      </div>

      <div v-for="c in failing" :key="rowKey(c)" class="mt-2 rounded-lg border border-rose-200 bg-white p-3">
        <div class="flex flex-wrap items-center gap-2">
          <BadgePill :label="CLASS_META[c.classification].label" :tone="CLASS_META[c.classification].tone" />
          <span class="font-medium text-sm">{{ c.module }}</span>
          <code v-if="c.scope_key" class="text-xs text-slate-400">{{ c.scope_key }}</code>
        </div>
        <!-- Error text verbatim: a collector failure summarized into a status word is
             how a root cause gets lost. -->
        <pre v-if="c.error_text" class="mt-2 overflow-x-auto whitespace-pre-wrap break-words rounded bg-slate-50 p-2 text-xs text-slate-700">{{ c.error_text }}</pre>
        <p class="mt-1 text-xs text-slate-400">
          last success: {{ c.last_success_at ? formatDate(c.last_success_at) : 'never' }}
          · updated {{ c.updated_at ? formatDate(c.updated_at) : '—' }}
        </p>
      </div>

      <button
        class="mt-3 text-xs font-medium text-slate-500 hover:text-slate-900"
        @click="showQuiet = !showQuiet"
      >
        {{ showQuiet ? 'Hide' : 'Show' }} {{ quiet.length }} frozen / orphaned row(s)
      </button>

      <div v-if="showQuiet" class="mt-2 overflow-x-auto rounded-lg border border-slate-200 bg-white">
        <table class="w-full text-xs">
          <thead class="border-b border-slate-200 bg-slate-50 text-left text-slate-500">
            <tr>
              <th class="px-3 py-2 font-medium">Source</th>
              <th class="px-3 py-2 font-medium">Why it is not a failure</th>
              <th class="px-3 py-2 font-medium">Status</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="c in quiet" :key="rowKey(c)" class="border-b border-slate-100 last:border-0">
              <td class="px-3 py-1.5 text-slate-700">{{ c.source_name }}</td>
              <td class="px-3 py-1.5 text-slate-500">{{ CLASS_META[c.classification].blurb }}</td>
              <td class="px-3 py-1.5 text-slate-400">{{ c.status }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </section>
</template>
