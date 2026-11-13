<script setup lang="ts">
// Fundamentals check (pass / fail / no score) with the story score and its strongest part,
// and any flaw that failed it.
const props = defineProps<{ row?: StagesStoryRow, verdict: 'pass' | 'fail' | 'unknown' }>()
const f = computed(() => STAGES_FILTER[props.verdict]!)
const dim = computed(() => props.row?.primary_dimension?.replaceAll('_', ' '))
</script>

<template>
  <div class="flex items-center gap-2 whitespace-nowrap">
    <UBadge :label="f.label" :icon="f.icon" :color="f.color" variant="subtle" size="xs" class="w-[4.5rem]" />
    <div v-if="isNum(row?.story_score)" class="min-w-0 text-xs">
      <span class="num font-semibold text-highlighted">{{ Math.round(row!.story_score!) }}</span>
      <span v-if="dim" class="capitalize text-muted"> · {{ dim }}</span>
      <UTooltip v-if="row?.flaws" :text="row.flaws">
        <div class="max-w-40 truncate text-[11px] text-error">{{ row.flaws }}</div>
      </UTooltip>
    </div>
  </div>
</template>
