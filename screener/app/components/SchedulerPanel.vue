<script setup lang="ts">
import type { SchedulerFinding, SchedulerHealthResponse } from '~/types/api'

// The signal whose silence caused the 2026-08-19 five-day outage and a separate
// 2.5-hour one: go-crond can be alive as a process while scheduling nothing. This
// panel reports FIRING, not just running.
const api = useApi()
const { data, status, error } = await useAsyncData(
  'scheduler-health',
  () => api.get<SchedulerHealthResponse>('/api/scheduler-health'),
)

const TONE: Record<string, 'good' | 'warn' | 'bad'> = { ok: 'good', warn: 'warn', error: 'bad' }
const LABEL: Record<string, string> = { ok: 'OK', warn: 'Warning', error: 'Failing' }

// Problems first; healthy checks stay visible but below them.
const ordered = computed<SchedulerFinding[]>(() => {
  const rank: Record<string, number> = { error: 0, warn: 1, ok: 2 }
  return [...(data.value?.findings ?? [])].sort((a, b) => (rank[a.level] ?? 3) - (rank[b.level] ?? 3))
})
</script>

<template>
  <section class="mt-6">
    <div class="flex flex-wrap items-center gap-3">
      <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-500">Scheduler &amp; host</h2>
      <BadgePill
        v-if="data"
        :label="data.status === 'ok' ? 'scheduler healthy' : data.status === 'warn' ? `${data.warn_count} warning(s)` : `${data.error_count} failing`"
        :tone="TONE[data.status]"
      />
      <span v-if="data" class="text-xs text-slate-400">checked {{ formatDate(data.checked_at) }}</span>
    </div>

    <p class="mt-1 text-xs text-slate-400">
      Reports whether go-crond is <strong>firing</strong>, not merely running — it has twice
      been alive while scheduling nothing. Also covers stale job locks, crontab drift, the
      OS-level watchdog, and Chrome CDP.
    </p>

    <div v-if="error" class="mt-3 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">
      Could not run the scheduler check ({{ error.message }}).
    </div>
    <div v-else-if="status === 'pending'" class="mt-3 text-sm text-slate-500">Checking…</div>

    <ul v-else-if="data" class="mt-3 space-y-1">
      <li
        v-for="(f, i) in ordered"
        :key="`${f.section}-${i}`"
        class="flex flex-wrap items-baseline gap-2 rounded border bg-white px-3 py-2 text-sm"
        :class="f.level === 'error' ? 'border-rose-200' : f.level === 'warn' ? 'border-amber-200' : 'border-slate-200'"
      >
        <BadgePill :label="LABEL[f.level] ?? f.level" :tone="TONE[f.level] ?? 'warn'" />
        <span class="text-xs uppercase tracking-wide text-slate-400">{{ f.section }}</span>
        <span class="text-slate-700">{{ f.message }}</span>
      </li>
    </ul>

    <p class="mt-2 text-xs text-slate-400">
      Caveat: this API is served by a process the scheduler manages, so it cannot fully
      report on the scheduler's own death. The OS-crontab watchdog remains the backstop.
    </p>
  </section>
</template>
