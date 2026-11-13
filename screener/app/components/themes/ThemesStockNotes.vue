<script setup lang="ts">
// The expanded write-up under a theme member: overnight write-up, check notes, web check.
const props = defineProps<{ stock: ThemesStock }>()
const notes = computed(() => props.stock.reasons?.replaceAll('note: ', '').trim() || null)
const web = computed(() => (props.stock.web_verdict ? THEMES_WEB[props.stock.web_verdict] : null))
</script>

<template>
  <div class="max-w-4xl space-y-3 text-sm">
    <p class="text-default">{{ stock.reasoning ?? 'No write-up yet (written overnight).' }}</p>
    <p v-if="notes" class="flex gap-1.5 text-xs text-muted">
      <UIcon name="i-lucide-info" class="mt-0.5 size-3.5 shrink-0" />{{ notes }}
    </p>
    <div v-if="web" class="rounded-lg border border-default bg-default p-3 text-xs">
      <div class="mb-1.5 flex flex-wrap items-center gap-2">
        <UBadge :label="web.label" :icon="web.icon" :color="web.color" variant="subtle" size="xs" />
        <span class="text-muted">checked {{ dateShort(stock.web_checked_at) }}</span>
      </div>
      <p v-if="stock.web_summary" class="text-toned">{{ stock.web_summary }}</p>
      <p v-if="stock.web_recent_changes" class="mt-1 text-toned">{{ stock.web_recent_changes }}</p>
      <p v-if="stock.web_red_flags" class="mt-1 text-error">{{ stock.web_red_flags }}</p>
      <div v-if="stock.web_sources?.length" class="mt-2 flex flex-wrap gap-x-3 gap-y-1">
        <a v-for="u in stock.web_sources" :key="u" :href="u" target="_blank" rel="noopener" class="inline-flex items-center gap-1 text-muted underline-offset-2 hover:text-primary hover:underline" @click.stop>
          <UIcon name="i-lucide-external-link" class="size-3" />{{ themesHost(u) }}
        </a>
      </div>
    </div>
    <p v-else class="text-xs text-dimmed">Web check pending (runs overnight).</p>
  </div>
</template>
