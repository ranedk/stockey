<script setup lang="ts">
// Days-behind history for one table. Neutral colour on purpose: the shared Sparkline paints a
// rising line green, but rising staleness is bad. Missing days are gaps, not zeros.
const props = withDefaults(defineProps<{ points: (number | null)[], width?: number, height?: number, warn?: boolean }>(), { width: 96, height: 24 })
const geo = computed(() => {
  const vals = props.points.filter((v): v is number => typeof v === 'number' && Number.isFinite(v))
  if (vals.length < 2) return null
  // Floor of a week so the normal 1-3 day weekend wobble stays a ripple, not a spike.
  const max = Math.max(...vals, 7)
  const peak = Math.max(...vals)
  const n = props.points.length
  const pad = 2
  let d = ''
  let pen = false
  let last: [number, number] | null = null
  props.points.forEach((v, i) => {
    if (typeof v !== 'number' || !Number.isFinite(v)) { pen = false; return }
    const x = (i / (n - 1)) * (props.width - pad * 2) + pad
    const y = props.height - pad - (v / max) * (props.height - pad * 2)
    d += `${pen ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`
    pen = true
    last = [x, y]
  })
  return { d, last: last as [number, number] | null, max: peak, latest: vals.at(-1)! }
})
</script>

<template>
  <UTooltip v-if="geo" :text="`Last ${points.length} nightly reports · highest ${geo.max} days behind · now ${geo.latest}`">
    <svg :width="width" :height="height" :viewBox="`0 0 ${width} ${height}`" class="overflow-visible" role="img" :aria-label="`Days behind, now ${geo.latest}`">
      <line :x1="2" :x2="width - 2" :y1="height - 2" :y2="height - 2" stroke="var(--chart-grid)" stroke-width="1" />
      <path :d="geo.d" fill="none" :stroke="warn ? 'var(--series-4)' : 'var(--series-1)'" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" />
      <circle v-if="geo.last" :cx="geo.last[0]" :cy="geo.last[1]" r="2.5" :fill="warn ? 'var(--series-4)' : 'var(--series-1)'" />
    </svg>
  </UTooltip>
  <span v-else class="text-xs text-dimmed">—</span>
</template>
