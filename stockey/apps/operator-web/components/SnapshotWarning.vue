<script setup lang="ts">
import type { Dict } from '~/types/api'

const props = defineProps<{
  snapshot?: Dict | null
  warning?: Dict | null
  generatedAt?: string | null
}>()

const snapshotMeta = computed(() => asDict(props.snapshot))
const warningMeta = computed(() => asDict(props.warning))
const hasWarning = computed(() => Object.keys(warningMeta.value).length > 0)
const isStale = computed(() => hasWarning.value || String(snapshotMeta.value.freshness || '').toLowerCase() === 'stale')
const shouldShow = computed(() => isStale.value || Boolean(snapshotMeta.value.generated_at || snapshotMeta.value.source))
const generatedAtText = computed(() => String(warningMeta.value.generated_at || snapshotMeta.value.generated_at || props.generatedAt || '-'))
const ageTextValue = computed(() => ageText(warningMeta.value.age_seconds ?? snapshotMeta.value.age_seconds))
const sourceText = computed(() => String(warningMeta.value.source || snapshotMeta.value.source || 'unknown'))
const title = computed(() => String(warningMeta.value.title || (isStale.value ? 'Stale operator snapshot' : 'Fresh operator snapshot')))
const message = computed(() => String(warningMeta.value.message || 'Operator snapshot metadata is attached to this payload.'))

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
</script>

<template>
  <section
    v-if="shouldShow"
    class="rounded-3xl border p-4 shadow-soft"
    :class="isStale ? 'border-sun/60 bg-sun/20 text-ink' : 'border-moss/20 bg-moss/10 text-ink/70'"
  >
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <p class="text-xs font-black uppercase" :class="isStale ? 'text-rust' : 'text-moss'">
          {{ title }}
        </p>
        <p class="mt-1 text-sm font-semibold">
          Created {{ generatedAtText }} · {{ ageTextValue }} · source {{ sourceText }}
        </p>
      </div>
      <p v-if="isStale" class="max-w-2xl text-sm leading-6 text-ink/65">
        {{ message }} Run <code>operator_snapshot_refresh</code> from Operations, run <code>python -m advisory.operator_snapshot</code>, or wait for the next advisory/watchers cycle.
      </p>
    </div>
  </section>
</template>
