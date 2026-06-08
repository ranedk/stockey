<script setup lang="ts">
import type { Dict } from '~/types/api'

defineProps<{
  warnings?: Dict[]
}>()

function display(value: unknown) {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'number') return Intl.NumberFormat('en-IN').format(value)
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  return String(value)
}

function formatAge(seconds: unknown) {
  const value = Number(seconds)
  if (!Number.isFinite(value)) return '-'
  if (value < 3600) return `${Math.round(value / 60)}m`
  if (value < 86400) return `${Math.round(value / 3600)}h`
  return `${Math.round(value / 86400)}d`
}
</script>

<template>
  <section v-if="warnings?.length" class="mt-6 rounded-3xl border border-sun/30 bg-sun/10 p-5">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Source Freshness</p>
        <h2 class="mt-1 text-xl font-black text-ink">Some source rows may be stale or incomplete</h2>
      </div>
      <StatusPill tone="warning">{{ warnings.length }}</StatusPill>
    </div>
    <div class="mt-4 grid gap-3 md:grid-cols-2">
      <article v-for="warning in warnings" :key="`${warning.source}-${warning.reason}-${warning.latest_at || warning.error}`" class="rounded-2xl bg-white/80 p-4">
        <div class="flex flex-wrap items-center gap-2">
          <StatusPill tone="warning">{{ display(warning.reason) }}</StatusPill>
          <span class="text-sm font-black text-ink">{{ display(warning.source) }}</span>
        </div>
        <p class="mt-2 text-sm font-semibold leading-6 text-ink/65">{{ display(warning.message) }}</p>
        <div class="mt-3 flex flex-wrap gap-2 text-xs font-bold text-ink/55">
          <span v-if="warning.latest_at" class="rounded-full bg-paper px-3 py-1">latest {{ display(warning.latest_at) }}</span>
          <span v-if="warning.age_seconds !== undefined" class="rounded-full bg-paper px-3 py-1">age {{ formatAge(warning.age_seconds) }}</span>
          <span v-if="warning.affected_rows !== undefined" class="rounded-full bg-paper px-3 py-1">rows {{ display(warning.affected_rows) }}</span>
          <span v-if="warning.error" class="rounded-full bg-rust/10 px-3 py-1 text-rust">{{ display(warning.error) }}</span>
        </div>
      </article>
    </div>
  </section>
</template>
