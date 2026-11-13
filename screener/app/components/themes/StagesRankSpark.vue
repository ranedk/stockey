<script setup lang="ts">
// Industry rank over the last 26 weeks, rank 1 at the top, on the same scale for every row.
// Green when the rank improved over the window, red when it slipped.
const props = withDefaults(defineProps<{ history: number[], ranked: number, width?: number, height?: number }>(), { width: 96, height: 26 })
const geo = computed(() => {
  const h = props.history
  const span = Math.max(props.ranked - 1, 1)
  const pad = 2
  const pts = h.map((r, k) => ({ r, x: (k / Math.max(h.length - 1, 1)) * (props.width - pad * 2) + pad }))
    .filter(p => p.r > 0)
    .map(p => ({ ...p, y: pad + ((p.r - 1) / span) * (props.height - pad * 2) }))
  if (pts.length < 2) return null
  const first = pts[0]!, last = pts.at(-1)!
  const tone = last.r < first.r ? 'var(--up)' : last.r > first.r ? 'var(--down)' : 'var(--chart-axis)'
  return { d: pts.map((p, i) => `${i ? 'L' : 'M'}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(''), tone, last, first }
})
</script>

<template>
  <UTooltip v-if="geo" :text="`Rank ${geo.first.r} → ${geo.last.r} over ${history.length} weeks (1 = strongest)`">
    <svg :width="width" :height="height" :viewBox="`0 0 ${width} ${height}`" class="overflow-visible" aria-hidden="true">
      <line x1="0" :x2="width" y1="2" y2="2" stroke="var(--chart-grid)" stroke-dasharray="2 3" />
      <path :d="geo.d" fill="none" :stroke="geo.tone" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" />
      <circle :cx="geo.last.x" :cy="geo.last.y" r="2.5" :fill="geo.tone" />
    </svg>
  </UTooltip>
  <span v-else class="text-xs text-dimmed">—</span>
</template>
