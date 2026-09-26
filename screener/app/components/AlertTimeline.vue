<script setup lang="ts">
import type { AlertItem } from '~/types/api'

defineProps<{ alerts: AlertItem[] }>()

const expanded = reactive<Record<string, boolean>>({})
function toggle(key: string) {
  expanded[key] = !expanded[key]
}

const originTone: Record<string, 'neutral' | 'warn'> = { rule: 'neutral', llm_triage: 'warn' }
</script>

<template>
  <ol class="space-y-2">
    <li v-for="alert in alerts" :key="`${alert.source}:${alert.news_id}:${alert.trigger_type}`" class="rounded-lg border border-slate-200 bg-white p-3">
      <button class="flex w-full items-start justify-between gap-3 text-left" @click="toggle(`${alert.source}:${alert.news_id}:${alert.trigger_type}`)">
        <div>
          <div class="flex flex-wrap items-center gap-2">
            <BadgePill :label="alert.trigger_type" tone="neutral" />
            <BadgePill :label="alert.origin" :tone="originTone[alert.origin] || 'neutral'" />
            <span class="text-xs text-slate-400">{{ formatDate(alert.alert_date) }}</span>
          </div>
          <p class="mt-1.5 text-sm text-slate-700">{{ alert.reasoning }}</p>
        </div>
        <span class="shrink-0 text-xs text-slate-400">
          {{ expanded[`${alert.source}:${alert.news_id}:${alert.trigger_type}`] ? 'hide evidence' : 'show evidence' }}
        </span>
      </button>
      <pre
        v-if="expanded[`${alert.source}:${alert.news_id}:${alert.trigger_type}`]"
        class="mt-3 max-h-96 overflow-auto rounded bg-slate-50 p-3 text-xs text-slate-700"
      >{{ JSON.stringify(alert.evidence_bundle, null, 2) }}</pre>
    </li>
  </ol>
</template>
