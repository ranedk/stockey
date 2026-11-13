<script setup lang="ts">
import type { DraftThesis } from '~/types/watchlist'

// Machine-written notes on watchlist names (the same ones the digest email sends). Reading only.
const { data, status } = useStockey<DraftThesis[]>('/api/drafts')
const expanded = ref(false)
const drafts = computed(() => data.value ?? [])
const sorted = computed(() => [...drafts.value].sort((a, b) => (b.confidence_score ?? 0) - (a.confidence_score ?? 0)))
const shown = ref(20)
</script>

<template>
  <UCard v-if="status !== 'pending' && drafts.length" :ui="{ body: expanded ? 'p-4 sm:p-4' : 'p-0 sm:p-0' }">
    <template #header>
      <div class="flex flex-wrap items-center gap-2">
        <UIcon name="i-lucide-notebook-pen" class="size-4 text-muted" />
        <h3 class="font-semibold text-highlighted">Candidate notes</h3>
        <UBadge color="neutral" variant="subtle" size="sm" :label="`${drafts.length} notes`" />
        <UButton class="ml-auto" size="xs" color="neutral" variant="outline" :trailing-icon="expanded ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'" :label="expanded ? 'Hide' : 'Show'" @click="expanded = !expanded" />
      </div>
      <p class="mt-1 text-xs text-muted">
        AI-written notes on watchlist names, the same ones the digest email sends. They are <strong>reading, not decisions</strong>:
        the nightly rules pick positions on their own and never consult these. The confidence number is the model's own and
        proves nothing until the hit-rate tables on Portfolio say otherwise.
      </p>
    </template>
    <ul v-if="expanded" class="space-y-2">
      <li v-for="d in sorted.slice(0, shown)" :key="d.company_master_id" class="rounded-lg border border-default p-3">
        <div class="flex flex-wrap items-center gap-2">
          <NuxtLink :to="`/watchlist/${encodeURIComponent(d.company_master_id)}`" class="font-mono text-sm font-medium text-highlighted hover:text-primary">{{ wlTicker(d.company_master_id) }}</NuxtLink>
          <UBadge v-if="d.confidence_score !== null" color="neutral" variant="outline" size="sm" :label="`model confidence ${d.confidence_score}`" />
          <UBadge v-if="d.source_alert_trigger_type" :color="wlEventColor(d.source_alert_trigger_type)" variant="subtle" size="sm" :label="wlEventLabel(d.source_alert_trigger_type)" />
        </div>
        <p class="mt-1.5 text-sm text-default">{{ d.prediction_text }}</p>
        <p class="mt-1 text-xs text-muted"><span class="font-medium">Wrong if:</span> {{ d.invalidation_criteria }}<template v-if="d.target_date"> · target {{ dateShort(d.target_date) }}</template></p>
      </li>
      <li v-if="sorted.length > shown" class="text-center">
        <UButton size="xs" variant="ghost" color="neutral" :label="`Show ${Math.min(20, sorted.length - shown)} more of ${sorted.length - shown}`" @click="shown += 20" />
      </li>
    </ul>
  </UCard>
</template>
