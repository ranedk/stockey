<script setup lang="ts">
// Plain SVG: tables render dozens of these, a canvas chart each would be heavy.
const props = withDefaults(defineProps<{ data?: number[] | null, width?: number, height?: number, fill?: boolean }>(),
  { width: 96, height: 28, fill: true })
const geo = computed(() => {
  const d = (props.data || []).filter(v => Number.isFinite(v))
  if (d.length < 2) return null
  const min = Math.min(...d), max = Math.max(...d)
  const span = max - min || 1
  const pad = 2
  const pts = d.map((v, i) => [
    (i / (d.length - 1)) * (props.width - pad * 2) + pad,
    props.height - pad - ((v - min) / span) * (props.height - pad * 2),
  ])
  const line = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0]!.toFixed(1)},${p[1]!.toFixed(1)}`).join('')
  const area = `${line}L${pts.at(-1)![0]!.toFixed(1)},${props.height}L${pts[0]![0]!.toFixed(1)},${props.height}Z`
  const up = d.at(-1)! >= d[0]!
  return { line, area, up, end: pts.at(-1)! }
})
</script>

<template>
  <svg v-if="geo" :width="width" :height="height" :viewBox="`0 0 ${width} ${height}`" class="overflow-visible" aria-hidden="true">
    <path v-if="fill" :d="geo.area" :fill="geo.up ? 'var(--up-soft)' : 'var(--down-soft)'" />
    <path :d="geo.line" fill="none" :stroke="geo.up ? 'var(--up)' : 'var(--down)'" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" />
    <circle :cx="geo.end[0]" :cy="geo.end[1]" r="2.5" :fill="geo.up ? 'var(--up)' : 'var(--down)'" />
  </svg>
  <span v-else class="text-xs text-dimmed">—</span>
</template>
