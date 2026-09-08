<script setup lang="ts">
import type { PaperNavPoint } from '~/types/systrader'

// A dependency-free line chart. Three books on one pair of axes is the whole
// requirement, and a charting library would be a large addition for it.
const props = defineProps<{
  points: PaperNavPoint[]
  books: { key: string; label: string; colour: string }[]
  height?: number
}>()

const H = computed(() => props.height ?? 220)
const W = 900
const PAD = { top: 12, right: 12, bottom: 22, left: 44 }

const bounds = computed(() => {
  let lo = Infinity
  let hi = -Infinity
  for (const p of props.points) {
    for (const b of props.books) {
      const v = p.values[b.key]
      if (v === undefined) continue
      if (v < lo) lo = v
      if (v > hi) hi = v
    }
  }
  if (!Number.isFinite(lo)) return { lo: 90, hi: 110 }
  if (hi - lo < 1) { lo -= 1; hi += 1 }
  const pad = (hi - lo) * 0.06
  return { lo: lo - pad, hi: hi + pad }
})

function x(i: number): number {
  const n = Math.max(props.points.length - 1, 1)
  return PAD.left + (i / n) * (W - PAD.left - PAD.right)
}

function y(v: number): number {
  const { lo, hi } = bounds.value
  const t = (v - lo) / (hi - lo || 1)
  return H.value - PAD.bottom - t * (H.value - PAD.top - PAD.bottom)
}

function path(bookKey: string): string {
  const parts: string[] = []
  props.points.forEach((p, i) => {
    const v = p.values[bookKey]
    if (v === undefined) return
    parts.push(`${parts.length === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(v).toFixed(1)}`)
  })
  return parts.join(' ')
}

const ticks = computed(() => {
  const { lo, hi } = bounds.value
  return [lo, (lo + hi) / 2, hi].map(v => ({ v, y: y(v) }))
})

const xLabels = computed(() => {
  const n = props.points.length
  if (n === 0) return []
  const idx = n === 1 ? [0] : [0, Math.floor(n / 2), n - 1]
  return idx.map(i => ({ x: x(i), label: props.points[i]!.date.slice(0, 7) }))
})
</script>

<template>
  <svg :viewBox="`0 0 ${W} ${H}`" class="w-full" :style="{ height: `${H}px` }" role="img"
       aria-label="Net asset value of the strategy against its benchmarks">
    <line v-for="t in ticks" :key="t.v" :x1="PAD.left" :x2="W - PAD.right" :y1="t.y" :y2="t.y"
          stroke="#e2e8f0" stroke-width="1" />
    <text v-for="t in ticks" :key="`l-${t.v}`" :x="PAD.left - 6" :y="t.y + 3"
          text-anchor="end" class="fill-slate-400" style="font-size: 10px">
      {{ t.v.toFixed(0) }}
    </text>
    <text v-for="l in xLabels" :key="l.label" :x="l.x" :y="H - 6"
          text-anchor="middle" class="fill-slate-400" style="font-size: 10px">
      {{ l.label }}
    </text>
    <path v-for="b in books" :key="b.key" :d="path(b.key)" fill="none" :stroke="b.colour" stroke-width="1.6" />
  </svg>
  <div class="mt-1 flex flex-wrap gap-4 text-xs text-slate-600">
    <span v-for="b in books" :key="b.key" class="inline-flex items-center gap-1.5">
      <span class="inline-block h-0.5 w-4 rounded" :style="{ backgroundColor: b.colour }" />
      {{ b.label }}
    </span>
  </div>
</template>
