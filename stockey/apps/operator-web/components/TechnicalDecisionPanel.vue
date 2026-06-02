<script setup lang="ts">
import type { Dict } from '~/types/api'

const props = defineProps<{
  record: Dict
  compact?: boolean
}>()

const scoreKeys = [
  ['Trend', 'technical_trend_score'],
  ['Structure', 'technical_structure_score'],
  ['Participation', 'technical_participation_score'],
  ['RS', 'technical_relative_strength_score'],
  ['Tradability', 'technical_tradability_score']
] as const

const visible = computed(() => Boolean(
  first(['technical_context', 'technical_state', 'technical_trigger_type', 'technical_score', 'setup_score', 'stop_price', 'recommended_stop_price', 'invalidation_price', 'target_price', 'technical_trigger_note', 'active_exit_condition', 'exit_strategy'])
))

function first(keys: string[]) {
  for (const key of keys) {
    const value = props.record[key]
    if (value !== null && value !== undefined && value !== '') return value
  }
  return null
}

function numberText(value: unknown, digits = 2) {
  const num = Number(value)
  if (!Number.isFinite(num)) return '-'
  return Intl.NumberFormat('en-IN', { maximumFractionDigits: digits }).format(num)
}

function scoreText(value: unknown) {
  const num = Number(value)
  if (!Number.isFinite(num)) return '-'
  if (num <= 1) return `${numberText(num * 100, 1)}/100`
  return `${numberText(num, 1)}/100`
}

function priceText(value: unknown) {
  const num = Number(value)
  if (!Number.isFinite(num)) return '-'
  return Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 }).format(num)
}

function toneClass(value: unknown) {
  const text = String(value || '').toLowerCase()
  if (text.includes('buy') || text.includes('ready') || text.includes('trigger')) return 'bg-moss text-paper'
  if (text.includes('exit') || text.includes('fail') || text.includes('reject')) return 'bg-rust text-paper'
  if (text.includes('watch') || text.includes('manual') || text.includes('near')) return 'bg-sun text-ink'
  return 'bg-ink text-paper'
}
</script>

<template>
  <section v-if="visible" class="rounded-2xl bg-paper/70 p-4">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <p class="text-xs font-black uppercase tracking-[0.22em] text-ink/45">Technical Decision</p>
        <p class="mt-1 text-sm leading-6 text-ink/65">
          {{ first(['technical_context', 'technical_trigger_note', 'technical_state']) || 'Technical context available.' }}
        </p>
      </div>
      <span class="rounded-full px-3 py-1 text-xs font-black" :class="toneClass(first(['technical_state', 'technical_trigger_type', 'action_code', 'status']))">
        {{ first(['technical_state', 'technical_trigger_type']) || 'TECHNICAL' }}
      </span>
    </div>

    <div class="mt-3 grid gap-2 text-sm md:grid-cols-4">
      <p class="rounded-xl bg-white/75 px-3 py-2"><b>Total:</b> {{ scoreText(first(['technical_score', 'setup_score'])) }}</p>
      <p class="rounded-xl bg-white/75 px-3 py-2"><b>Trigger:</b> {{ first(['technical_trigger_type', 'entry_type', 'trigger_type']) || '-' }}</p>
      <p class="rounded-xl bg-white/75 px-3 py-2"><b>Pivot:</b> {{ priceText(first(['pivot_price', 'trigger_price', 'entry_price'])) }}</p>
      <p class="rounded-xl bg-white/75 px-3 py-2"><b>Stop:</b> {{ priceText(first(['recommended_stop_price', 'stop_price', 'invalidation_price'])) }}</p>
    </div>

    <div v-if="!compact" class="mt-3 grid gap-2 text-sm md:grid-cols-3">
      <p class="rounded-xl bg-white/75 px-3 py-2"><b>Invalidation:</b> {{ first(['invalidation_rule', 'active_exit_condition']) || priceText(first(['invalidation_price'])) }}</p>
      <p class="rounded-xl bg-white/75 px-3 py-2"><b>Target:</b> {{ priceText(first(['target_price', 'recommended_target_price'])) }}</p>
      <p class="rounded-xl bg-white/75 px-3 py-2"><b>Exit plan:</b> {{ first(['exit_strategy', 'partial_exit_plan', 'exit_condition_status']) || '-' }}</p>
    </div>

    <details v-if="!compact" class="mt-3">
      <summary class="cursor-pointer text-sm font-black text-moss">Show technical score buckets</summary>
      <div class="mt-3 grid gap-2 text-sm md:grid-cols-5">
        <p v-for="[label, key] in scoreKeys" :key="key" class="rounded-xl bg-white/75 px-3 py-2">
          <b>{{ label }}:</b> {{ scoreText(record[key]) }}
        </p>
      </div>
    </details>
  </section>
</template>
