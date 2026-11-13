<script setup lang="ts">
import type { PtOptionTrack } from '~/types/paperTracks'
// Nifty option paper tracks (systrader LEDGER 78). Per expiry session, in basis points of
// notional after costs -- not a running value: the strategy holds nothing between expiries.
defineProps<{ tracks: PtOptionTrack[] }>()
const { t, base, axis, tooltip } = useChartTheme()

const bpsClass = (v: number | null | undefined) => v == null ? 'text-dimmed' : v > 0 ? 'text-up' : v < 0 ? 'text-down' : 'text-muted'
const memberText = (m: Record<string, number | null>) => Object.entries(m).map(([k, v]) => `${k}: ${paperBps(v)}`).join(' · ')

function barOption(tr: PtOptionTrack) {
  const s = tr.sessions
  return {
    ...base.value,
    grid: { left: 8, right: 8, top: 8, bottom: 4, containLabel: true },
    tooltip: { ...tooltip.value, trigger: 'axis', axisPointer: { type: 'shadow' }, valueFormatter: (v: number) => paperBps(v) },
    xAxis: { type: 'category', data: s.map(x => dateShort(x.date)), ...axis.value, splitLine: { show: false } },
    yAxis: { type: 'value', ...axis.value, axisLine: { show: false }, axisLabel: { ...axis.value.axisLabel, formatter: (v: number) => `${v} bps` } },
    series: [{ name: 'After costs', type: 'bar', barMaxWidth: 20,
      data: s.map(x => ({ value: x.basket_bps, itemStyle: { color: (x.basket_bps ?? 0) >= 0 ? t.value.up : t.value.down,
        borderRadius: (x.basket_bps ?? 0) >= 0 ? [4, 4, 0, 0] : [0, 0, 4, 4] } })) }],
  }
}
</script>

<template>
  <UCard v-for="tr in tracks" :key="tr.name">
    <template #header>
      <div class="flex flex-wrap items-center gap-2">
        <UIcon name="i-lucide-calendar-clock" class="size-4 text-muted" />
        <h3 class="font-semibold text-highlighted">Nifty options · {{ paperName(tr.name) }}</h3>
        <UBadge color="neutral" variant="subtle" size="sm" label="Paper only" />
        <span class="text-xs text-muted">from {{ dateShort(tr.start) }}</span>
      </div>
      <p class="mt-1 max-w-3xl text-xs text-muted">{{ tr.rule }}.</p>
    </template>

    <div class="grid gap-3 sm:grid-cols-3">
      <div class="rounded-lg bg-elevated/60 p-3">
        <div class="text-xs text-muted">Average per expiry</div>
        <div class="num mt-1 text-xl font-semibold" :class="bpsClass(tr.mean_bps)">{{ paperBps(tr.mean_bps) }}</div>
        <div class="mt-1 text-[11px] text-dimmed">of the amount traded, after costs · backtest check period {{ paperBps(tr.check_bps) }}</div>
      </div>
      <div class="rounded-lg bg-elevated/60 p-3">
        <div class="text-xs text-muted">Expiries recorded</div>
        <div class="num mt-1 text-xl font-semibold text-highlighted">{{ tr.count }} <span class="text-sm font-normal text-muted">of 52</span></div>
        <UProgress :model-value="Math.min(100, (tr.count / 52) * 100)" size="xs" class="mt-2" />
      </div>
      <div class="rounded-lg bg-elevated/60 p-3">
        <div class="text-xs text-muted">Expiries that made money</div>
        <div class="num mt-1 text-xl font-semibold text-highlighted">{{ tr.positive }} <span class="text-sm font-normal text-muted">of {{ tr.count }}</span></div>
        <div class="mt-1 text-[11px] text-dimmed">Judged {{ tr.judged }}</div>
      </div>
    </div>

    <UEmpty v-if="!tr.sessions.length" icon="i-lucide-hourglass" title="No expiry recorded yet"
      description="Each one appears the evening after the expiry, once the option data lands." class="mt-4 py-6" />
    <template v-else>
      <div class="mt-4 text-xs font-medium text-muted">Result per expiry, basis points</div>
      <EChart :option="barOption(tr)" height="180px" />
      <div class="mt-3 overflow-x-auto">
        <table class="w-full min-w-[30rem] text-sm">
          <thead>
            <tr class="border-b border-default text-xs text-muted">
              <th class="py-2 pr-3 text-left font-medium">Expiry</th>
              <th class="px-3 py-2 text-right font-medium">Strike at 09:20</th>
              <th class="px-3 py-2 text-right font-medium">Result</th>
              <th class="px-3 py-2 text-right font-medium">Running total</th>
              <th class="py-2 pl-3 text-right font-medium">Legs missing</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-default">
            <tr v-for="s in [...tr.sessions].reverse()" :key="s.date">
              <td class="py-1.5 pr-3">
                <UTooltip :text="memberText(s.members)">
                  <span class="cursor-help underline decoration-dotted underline-offset-2">{{ dateShort(s.date) }}</span>
                </UTooltip>
              </td>
              <td class="num px-3 py-1.5 text-right text-muted">{{ s.strike != null ? num(s.strike) : '—' }}</td>
              <td class="num px-3 py-1.5 text-right" :class="bpsClass(s.basket_bps)">{{ paperBps(s.basket_bps) }}</td>
              <td class="num px-3 py-1.5 text-right" :class="bpsClass(s.cum_bps)">{{ paperBps(s.cum_bps) }}</td>
              <td class="num py-1.5 pl-3 text-right text-muted">{{ s.missing_legs }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="mt-2 text-[11px] text-dimmed">Hover a date for the six versions that make up the average.</p>
    </template>

    <template #footer>
      <div class="space-y-1 text-[11px] text-dimmed">
        <div><span class="font-medium text-muted">Costs counted:</span> {{ tr.costs }}.</div>
        <div>1 basis point (bp) is 0.01%: about 2.5 Nifty points per straddle at Nifty 25,000.</div>
      </div>
    </template>
  </UCard>
</template>
