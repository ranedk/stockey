<script setup lang="ts">
import type { AlertItem } from '~/types/watchlist'

// Every event that led a company here, newest first, each with the raw evidence the rule saw.
const props = defineProps<{ alerts: AlertItem[] }>()
const open = reactive<Record<string, boolean>>({})
const key = (a: AlertItem) => `${a.source}:${a.news_id}:${a.trigger_type}`
const headline = (a: AlertItem) => ((a.evidence_bundle as any)?.event?.headline as string | undefined) || null
const ORIGIN: Record<string, { label: string, color: 'neutral' | 'warning', tip: string }> = {
  rule: { label: 'Rule', color: 'neutral', tip: 'Raised by a fixed rule' },
  llm_triage: { label: 'AI reader', color: 'warning', tip: 'Raised by an AI model reading the filing, not by a fixed rule' },
}
const sorted = computed(() => [...props.alerts].sort((a, b) => String(b.alert_date).localeCompare(String(a.alert_date))))
</script>

<template>
  <UEmpty v-if="!alerts.length" icon="i-lucide-history" title="No events yet" description="Nothing has been raised for this company." class="py-6" />
  <ol v-else class="relative space-y-3 border-l border-default pl-5">
    <li v-for="a in sorted" :key="key(a)" class="relative">
      <span class="absolute -left-[26px] top-1.5 size-2.5 rounded-full ring-4 ring-default" :class="wlEventColor(a.trigger_type) === 'warning' ? 'bg-warning' : 'bg-primary'" />
      <div class="flex flex-wrap items-center gap-2">
        <span class="num text-xs text-muted">{{ dateShort(a.alert_date) }}</span>
        <UBadge :color="wlEventColor(a.trigger_type)" variant="subtle" size="sm" :label="wlEventLabel(a.trigger_type)" />
        <UTooltip :text="ORIGIN[a.origin]?.tip || a.origin">
          <UBadge :color="ORIGIN[a.origin]?.color || 'neutral'" variant="outline" size="sm" :label="ORIGIN[a.origin]?.label || a.origin" />
        </UTooltip>
      </div>
      <p v-if="headline(a)" class="mt-1 text-sm font-medium text-highlighted">{{ headline(a) }}</p>
      <p class="mt-1 text-sm text-default">{{ a.reasoning || '—' }}</p>
      <UButton v-if="a.evidence_bundle" size="xs" variant="link" color="neutral" class="mt-1 px-0"
        :icon="open[key(a)] ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'" :label="open[key(a)] ? 'Hide evidence' : 'Show evidence'"
        @click="open[key(a)] = !open[key(a)]" />
      <pre v-if="open[key(a)]" class="mt-2 max-h-96 overflow-auto rounded-lg bg-elevated p-3 text-xs text-default">{{ JSON.stringify(a.evidence_bundle, null, 2) }}</pre>
    </li>
  </ol>
</template>
