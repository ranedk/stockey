<script setup lang="ts">
// One card per data check, problems first. Where a check reports "this many against the
// normal", that ratio is drawn as a meter; every raw metric is still listed underneath.
const props = defineProps<{ findings: OpsDataHealthFinding[] }>()
const ordered = computed(() => [...props.findings].sort((a, b) => opsLevel(a.level).rank - opsLevel(b.level).rank))

const METRIC_LABELS: Record<string, string> = {
  rows: 'Rows', price_bars: 'Price bars', session: 'Session', tickers: 'Stocks', median: 'Recent normal',
  missing: 'Missing', universe: 'Active stocks', window_days: 'Window (days)', latest: 'Filled up to',
  runway_days: 'Days left',
}
const metricLabel = (k: string) => METRIC_LABELS[k] ?? k.replaceAll('_', ' ').replace(/^./, c => c.toUpperCase())
const metricValue = (v: unknown) => (typeof v === 'number' ? num(v, Number.isInteger(v) ? 0 : 1) : v == null || v === '' ? '—' : String(v))

interface Meter { pct: number, value: string, caption: string }
function meter(f: OpsDataHealthFinding): Meter | null {
  const m = f.metrics ?? {}
  const tickers = Number(m.tickers), median = Number(m.median)
  if (Number.isFinite(tickers) && median > 0) {
    return { pct: tickers / median, value: `${Math.round(tickers / median * 100)}%`, caption: `of the usual ${num(median)} stocks` }
  }
  const missing = Number(m.missing), universe = Number(m.universe)
  if (Number.isFinite(missing) && universe > 0) {
    return { pct: 1 - missing / universe, value: `${num(universe - missing)} of ${num(universe)}`, caption: 'active stocks have data' }
  }
  return null
}
function headline(f: OpsDataHealthFinding): { value: string, caption: string } | null {
  const m = f.metrics ?? {}
  if (typeof m.runway_days === 'number') return { value: `${m.runway_days} days`, caption: `calendar filled to ${opsDate(m.latest)}` }
  if (typeof m.rows === 'number') return { value: num(m.rows), caption: 'rows loaded' }
  if (typeof m.price_bars === 'number') return { value: num(m.price_bars), caption: 'price bars, all adjusted' }
  return null
}
const meterColor = (f: OpsDataHealthFinding) => opsLevel(f.level).color === 'neutral' ? 'neutral' : opsLevel(f.level).color
const metrics = (f: OpsDataHealthFinding) => Object.entries(f.metrics ?? {})
</script>

<template>
  <div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
    <UCard
      v-for="f in ordered" :key="f.check"
      :ui="{ body: 'p-4 sm:p-4 flex h-full flex-col gap-3' }"
      :class="f.level === 'error' ? 'ring-error/40' : f.level === 'warn' ? 'ring-warning/40' : ''"
    >
      <div class="flex items-start justify-between gap-2">
        <div class="min-w-0">
          <div class="flex items-center gap-1.5">
            <span class="font-medium text-highlighted">{{ opsCheck(f.check).label }}</span>
            <UTooltip v-if="opsCheck(f.check).about" :text="opsCheck(f.check).about">
              <UIcon name="i-lucide-info" class="size-3.5 shrink-0 text-dimmed" />
            </UTooltip>
          </div>
          <code class="text-[11px] text-dimmed">{{ f.check }}</code>
        </div>
        <OpsStatusBadge :level="f.level" />
      </div>

      <div v-if="meter(f)">
        <div class="flex items-baseline gap-1.5">
          <span class="num text-xl font-semibold text-highlighted">{{ meter(f)!.value }}</span>
          <span class="text-xs text-muted">{{ meter(f)!.caption }}</span>
        </div>
        <UProgress :model-value="Math.min(100, meter(f)!.pct * 100)" size="xs" :color="meterColor(f)" class="mt-2" />
      </div>
      <div v-else-if="headline(f)" class="flex items-baseline gap-1.5">
        <span class="num text-xl font-semibold text-highlighted">{{ headline(f)!.value }}</span>
        <span class="text-xs text-muted">{{ headline(f)!.caption }}</span>
      </div>

      <p class="text-sm text-default">{{ f.message }}</p>

      <dl v-if="metrics(f).length" class="mt-auto flex flex-wrap gap-x-4 gap-y-1 border-t border-default pt-2 text-xs">
        <div v-for="[k, v] in metrics(f)" :key="k" class="flex gap-1">
          <dt class="text-muted">{{ metricLabel(k) }}</dt>
          <dd class="num font-medium text-highlighted">{{ metricValue(v) }}</dd>
        </div>
      </dl>
    </UCard>
  </div>
</template>
