<script setup lang="ts">
import type { Dict } from '~/types/api'

const props = defineProps<{
  items?: Dict[] | null
}>()

const normalizedItems = computed(() => (props.items || [])
  .map((item) => normalizeItem(item))
  .filter((item) => item.hasData))

const hasAnyStale = computed(() => normalizedItems.value.some((item) => item.isStale))

function asDict(value: unknown): Dict {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Dict : {}
}

function ageText(value: unknown) {
  const seconds = Number(value)
  if (!Number.isFinite(seconds)) return 'age unknown'
  if (seconds < 60) return `${Math.round(seconds)}s old`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m old`
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h old`
  return `${Math.round(seconds / 86400)}d old`
}

function normalizeItem(item: Dict) {
  const snapshot = asDict(item.snapshot)
  const warning = asDict(item.warning || item.snapshot_warning)
  const generatedAt = String(warning.generated_at || snapshot.generated_at || item.generated_at || item.generatedAt || '')
  const source = String(warning.source || snapshot.source || item.source || item.label || 'payload')
  const status = String(warning.status || snapshot.freshness || item.status || '').toLowerCase()
  const label = String(item.label || source || 'Payload')
  const title = String(warning.title || item.title || label)
  const message = String(warning.message || item.message || '')
  const isStale = Boolean(Object.keys(warning).length) || status.includes('stale') || status.includes('blocked') || status.includes('error') || status.includes('unavailable')
  const hasData = Boolean(generatedAt || source || status || message || Object.keys(snapshot).length || Object.keys(warning).length)
  return {
    label,
    source,
    status: status || (isStale ? 'warning' : 'fresh'),
    title,
    message,
    generatedAt: generatedAt || '-',
    age: ageText(warning.age_seconds ?? snapshot.age_seconds ?? item.age_seconds),
    isStale,
    hasData
  }
}
</script>

<template>
  <section
    v-if="normalizedItems.length"
    class="rounded-3xl border p-4 shadow-soft"
    :class="hasAnyStale ? 'border-sun/50 bg-sun/15' : 'border-moss/20 bg-moss/10'"
  >
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <p class="text-xs font-black uppercase tracking-[0.25em]" :class="hasAnyStale ? 'text-rust' : 'text-moss'">
          Payload freshness
        </p>
        <p class="mt-1 max-w-3xl text-sm leading-6 text-ink/65">
          Check source age before trusting recommendations. Stale or blocked payloads need a refresh, rerun, or source repair before acting.
        </p>
      </div>
      <StatusPill :tone="hasAnyStale ? 'warning' : 'success'">
        {{ hasAnyStale ? 'review freshness' : 'freshness tracked' }}
      </StatusPill>
    </div>
    <div class="mt-4 grid gap-2 md:grid-cols-2 xl:grid-cols-4">
      <div
        v-for="item in normalizedItems"
        :key="`${item.label}-${item.source}`"
        class="rounded-2xl border bg-white/85 p-3 text-sm leading-6"
        :class="item.isStale ? 'border-sun/50' : 'border-black/5'"
        :title="item.message || item.title"
      >
        <div class="flex items-start justify-between gap-2">
          <b>{{ item.label }}</b>
          <StatusPill :tone="item.isStale ? 'warning' : 'success'">
            {{ item.status.replaceAll('_', ' ') }}
          </StatusPill>
        </div>
        <p class="mt-1 text-ink/60">{{ item.generatedAt }} · {{ item.age }}</p>
        <p v-if="item.message" class="mt-1 text-xs font-semibold text-rust">{{ item.message }}</p>
      </div>
    </div>
  </section>
</template>
