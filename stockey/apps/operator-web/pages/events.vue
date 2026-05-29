<script setup lang="ts">
import type { TraceSummary } from '~/types/api'

const api = useOperatorApi()
const { data } = await useAsyncData('events', () => api.getEvents(100))
const events = computed(() => data.value?.events || [])
const traces = reactive<Record<string, TraceSummary>>({})
const loadingTrace = reactive<Record<string, boolean>>({})

async function loadTrace(row: Record<string, unknown>) {
  const uniqueId = String(row.unique_id || '')
  if (!uniqueId || traces[uniqueId] || loadingTrace[uniqueId]) return
  loadingTrace[uniqueId] = true
  try {
    traces[uniqueId] = await api.getEventTraceSummary(uniqueId)
  } finally {
    loadingTrace[uniqueId] = false
  }
}
</script>

<template>
  <section>
    <p class="text-sm font-semibold uppercase tracking-[0.3em] text-ink/45">Event Inbox</p>
    <h1 class="mt-3 text-4xl font-black">News and announcement processing</h1>
    <p class="mt-3 max-w-3xl text-ink/60">
      V1 shows the existing event rows. Decision-trace stages will attach here as the trace tables are implemented.
    </p>
  </section>

  <section class="mt-8 grid gap-4">
    <RecordCard
      v-for="(row, idx) in events"
      :key="idx"
      :title="String(row.symbol || row.unique_id || 'Event')"
      :subtitle="String(row.subject || row.concise_summary_text || '')"
      :record="row"
    >
      <template #badge>
        <span class="rounded-full bg-ember px-3 py-1 text-xs font-bold text-white">{{ row.event_status || row.parse_status || 'EVENT' }}</span>
      </template>
      <button
        class="mt-4 rounded-full bg-ink px-4 py-2 text-sm font-bold text-paper"
        type="button"
        @click="loadTrace(row)"
      >
        {{ loadingTrace[String(row.unique_id || '')] ? 'Loading trace...' : 'Load decision trace' }}
      </button>
      <NuxtLink
        v-if="row.unique_id"
        class="ml-2 inline-flex rounded-full bg-white px-4 py-2 text-sm font-bold text-ink"
        :to="`/decision-trace?unique_id=${encodeURIComponent(String(row.unique_id || ''))}`"
      >
        Open trace page
      </NuxtLink>
      <TraceTimeline
        v-if="traces[String(row.unique_id || '')]"
        class="mt-4"
        :trace="traces[String(row.unique_id || '')]"
        title="Event trace"
      />
    </RecordCard>
    <p v-if="!events.length" class="glass-panel rounded-3xl p-6 text-ink/60">No event rows found.</p>
  </section>
</template>
