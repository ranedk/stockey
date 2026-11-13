<script setup lang="ts">
// The raw numbers behind a theme's strength, as small labelled tiles.
const props = defineProps<{ theme: ThemesRow }>()
const rising = computed(() => (isNum(props.theme.breadth_stage2_pct) ? props.theme.breadth_stage2_pct / 100 : null))
</script>

<template>
  <div class="grid grid-cols-2 gap-2 sm:grid-cols-4">
    <div class="rounded-lg bg-elevated/60 p-2.5">
      <div class="text-[11px] text-muted">Members in an uptrend</div>
      <div class="num mt-0.5 font-semibold text-highlighted">{{ pct(rising, 0) }}</div>
      <UProgress :model-value="(rising ?? 0) * 100" size="2xs" class="mt-1.5" :color="(rising ?? 0) >= 0.5 ? 'success' : (rising ?? 0) >= 0.3 ? 'warning' : 'error'" />
    </div>
    <div class="rounded-lg bg-elevated/60 p-2.5">
      <div class="text-[11px] text-muted">Members' sales growth</div>
      <div class="mt-0.5"><Change :value="themesFrac(theme.sales_yoy_pct)" :digits="0" /></div>
      <div class="mt-1 text-[11px] text-dimmed">latest quarter, year on year</div>
    </div>
    <div class="rounded-lg bg-elevated/60 p-2.5">
      <div class="text-[11px] text-muted">Members' margin change</div>
      <div class="num mt-0.5 font-medium" :class="signClass(theme.margin_change_pp)">
        <span v-if="arrow(theme.margin_change_pp)" class="text-[0.6em]">{{ arrow(theme.margin_change_pp) }}</span>
        {{ isNum(theme.margin_change_pp) ? `${Math.abs(theme.margin_change_pp).toFixed(1)} pts` : '—' }}
      </div>
      <div class="mt-1 text-[11px] text-dimmed">operating margin vs a year ago</div>
    </div>
    <div class="rounded-lg bg-elevated/60 p-2.5">
      <div class="text-[11px] text-muted">Theme index vs market</div>
      <div class="mt-0.5"><Change :value="themesFrac(theme.index_rs26_pct)" :digits="0" /></div>
      <div class="mt-1 text-[11px] text-dimmed">over 26 weeks</div>
    </div>
  </div>
</template>
