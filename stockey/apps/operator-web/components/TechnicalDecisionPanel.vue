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
  || nestedFirst([
    ['recommendation_reason', 'evidence', 'technical', 'technical_state'],
    ['recommendation_reason', 'evidence', 'technical', 'technical_trigger_type'],
    ['recommendation_reason', 'evidence', 'technical', 'technical_score'],
    ['recommendation_reason', 'evidence', 'technical', 'setup_score']
  ])
))

function first(keys: string[]) {
  for (const key of keys) {
    const value = props.record[key]
    if (value !== null && value !== undefined && value !== '') return value
  }
  return null
}

function nested(path: string[]) {
  let current: unknown = props.record
  for (const key of path) {
    if (!current || typeof current !== 'object' || Array.isArray(current)) return null
    current = (current as Dict)[key]
  }
  return current === null || current === undefined || current === '' ? null : current
}

function nestedFirst(paths: string[][]) {
  for (const path of paths) {
    const value = nested(path)
    if (value !== null) return value
  }
  return null
}

function value(keys: string[], paths: string[][] = []) {
  return first(keys) ?? nestedFirst(paths)
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

function scoreValue(key: string) {
  return first([key]) ?? nested(['recommendation_reason', 'evidence', 'technical', key])
}
</script>

<template>
  <section v-if="visible" class="rounded-2xl border border-moss/15 bg-gradient-to-br from-moss/10 via-white/80 to-sun/10 p-4">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <p class="text-xs font-black uppercase tracking-[0.22em] text-moss">Technical Decision</p>
        <p class="mt-1 text-sm leading-6 text-ink/65">
          {{ value(['technical_context', 'technical_trigger_note', 'technical_state'], [
            ['recommendation_reason', 'evidence', 'technical', 'technical_trigger_note'],
            ['recommendation_reason', 'evidence', 'technical', 'technical_state']
          ]) || 'Technical context available.' }}
        </p>
      </div>
      <StatusPill :tone="String(value(['technical_state', 'technical_trigger_type', 'action_code', 'status'], [['recommendation_reason', 'evidence', 'technical', 'technical_state']]) || '').toLowerCase().includes('reject') ? 'danger' : 'info'">
        {{ value(['technical_state', 'technical_trigger_type'], [['recommendation_reason', 'evidence', 'technical', 'technical_state'], ['recommendation_reason', 'evidence', 'technical', 'technical_trigger_type']]) || 'TECHNICAL' }}
      </StatusPill>
    </div>

    <div class="mt-3 grid gap-2 text-sm md:grid-cols-4">
      <p class="rounded-xl bg-white/80 px-3 py-2"><b>Total:</b> {{ scoreText(value(['technical_score', 'setup_score'], [['recommendation_reason', 'evidence', 'technical', 'technical_score'], ['recommendation_reason', 'evidence', 'technical', 'setup_score']])) }}</p>
      <p class="rounded-xl bg-white/80 px-3 py-2"><b>Trigger:</b> {{ value(['technical_trigger_type', 'entry_type', 'trigger_type'], [['recommendation_reason', 'evidence', 'technical', 'technical_trigger_type']]) || '-' }}</p>
      <p class="rounded-xl bg-white/80 px-3 py-2"><b>Pivot:</b> {{ priceText(value(['pivot_price', 'trigger_price', 'entry_price'], [['recommendation_reason', 'evidence', 'technical', 'pivot_price'], ['recommendation_reason', 'evidence', 'technical', 'trigger_price']])) }}</p>
      <p class="rounded-xl bg-white/80 px-3 py-2"><b>Stop:</b> {{ priceText(value(['recommended_stop_price', 'stop_price', 'invalidation_price'], [['recommendation_reason', 'evidence', 'risk', 'recommended_stop_price'], ['recommendation_reason', 'evidence', 'risk', 'stop_price'], ['recommendation_reason', 'evidence', 'risk', 'invalidation_price']])) }}</p>
    </div>

    <div class="mt-3 grid gap-2 text-sm md:grid-cols-3">
      <p class="rounded-xl bg-white/75 px-3 py-2"><b>Invalidation:</b> {{ first(['invalidation_rule', 'active_exit_condition']) || priceText(first(['invalidation_price'])) }}</p>
      <p class="rounded-xl bg-white/75 px-3 py-2"><b>Target:</b> {{ priceText(value(['target_price', 'recommended_target_price'], [['recommendation_reason', 'evidence', 'risk', 'recommended_target_price']])) }}</p>
      <p class="rounded-xl bg-white/75 px-3 py-2"><b>Algorithm:</b> {{ value(['technical_algorithm', 'setup_id', 'setup_name'], [['recommendation_reason', 'setup_id']]) || '-' }}</p>
    </div>

    <details class="mt-3" :open="!compact">
      <summary class="cursor-pointer text-sm font-black text-moss">Show score buckets and exit context</summary>
      <div class="mt-3 grid gap-2 text-sm md:grid-cols-5">
        <p v-for="[label, key] in scoreKeys" :key="key" class="rounded-xl bg-white/75 px-3 py-2">
          <b>{{ label }}:</b> {{ scoreText(scoreValue(key)) }}
        </p>
      </div>
      <div class="mt-3 grid gap-2 text-sm md:grid-cols-3">
        <p class="rounded-xl bg-white/75 px-3 py-2"><b>Invalidation:</b> {{ value(['invalidation_rule', 'active_exit_condition'], [['recommendation_reason', 'evidence', 'lifecycle', 'next_action_reason']]) || priceText(value(['invalidation_price'], [['recommendation_reason', 'evidence', 'risk', 'invalidation_price']])) }}</p>
        <p class="rounded-xl bg-white/75 px-3 py-2"><b>Exit plan:</b> {{ first(['exit_strategy', 'partial_exit_plan', 'exit_condition_status']) || '-' }}</p>
        <p class="rounded-xl bg-white/75 px-3 py-2"><b>Setup score:</b> {{ scoreText(value(['setup_score'], [['recommendation_reason', 'evidence', 'technical', 'setup_score']])) }}</p>
      </div>
    </details>
  </section>
</template>
