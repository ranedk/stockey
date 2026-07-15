<script setup lang="ts">
type Dict = Record<string, unknown>

const api = useOperatorApi()
const { data, pending, error: loadError, refresh } = await useAsyncData('advisory', () => api.getDailyAdvisory())

const status = computed(() => String(data.value?.status || ''))
const asof = computed(() => String(data.value?.asof_date || '—'))
const context = computed(() => asDict(data.value?.context))
const freshness = computed(() => asDict(data.value?.freshness))
const chain = computed(() => asList(data.value?.chain))
const picks = computed(() => asList(data.value?.picks))
const bookOpen = computed(() => asList(data.value?.book_open))
const summary = computed(() => asDict(data.value?.book_summary))
const actions = computed(() => asDict(data.value?.book_actions))
const track = computed(() => asDict(data.value?.track_record))
const exits = computed(() => asList(actions.value.exits))
const trims = computed(() => asList(actions.value.trims))
const buys = computed(() => asList(actions.value.buys))
const isCurrent = computed(() => Boolean(freshness.value.is_current))
const flaggedCount = computed(() => Number(summary.value.flagged || 0))
const generatedAt = computed(() => String(freshness.value.generated_at || '').slice(0, 16).replace('T', ' '))

const series = computed(() => asNums(track.value.series))
const bars = computed(() => {
  const s = series.value
  if (!s.length) return [] as { x: number; y: number; w: number; h: number; up: boolean }[]
  const w = 280, h = 44, mid = h / 2, mx = Math.max(1e-9, ...s.map((v) => Math.abs(v))), bw = w / s.length
  return s.map((v, i) => ({
    x: i * bw, w: Math.max(1, bw - 1.2), h: (Math.abs(v) / mx) * (mid - 1),
    y: v >= 0 ? mid - (Math.abs(v) / mx) * (mid - 1) : mid, up: v >= 0,
  }))
})

function asDict(value: unknown): Dict {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Dict : {}
}
function asList(value: unknown): Dict[] {
  return Array.isArray(value) ? value.filter((item) => typeof item === 'object' && item !== null) as Dict[] : []
}
function asNums(value: unknown): number[] {
  return Array.isArray(value) ? value.filter((v) => typeof v === 'number') as number[] : []
}
function num(value: unknown, digits = 2): string {
  return typeof value === 'number' ? value.toFixed(digits) : (value == null ? '—' : String(value))
}
function signed(value: unknown): string {
  return typeof value === 'number' ? `${value >= 0 ? '+' : ''}${value.toFixed(2)}%` : '—'
}
function signClass(value: unknown): string {
  if (typeof value !== 'number') return 'text-ink/50'
  return value >= 0 ? 'text-moss font-semibold' : 'text-rust font-semibold'
}
function breadthLabel(value: unknown): string {
  return typeof value === 'number' ? `${Math.round(value * 100)}%` : '—'
}
function actionTone(value: unknown): 'success' | 'warning' | 'danger' | 'neutral' {
  const s = String(value || 'HOLD')
  if (s === 'EXIT') return 'danger'
  if (s === 'TRIM') return 'warning'
  if (s === 'BUY') return 'success'
  return 'neutral'
}
function reason(value: unknown): string {
  return String(value || '').replace(/_/g, ' ')
}
</script>

<template>
  <section class="space-y-5">
    <header class="rounded-3xl border border-ink/10 bg-gradient-to-br from-slate-950 via-ink to-moss px-8 py-7 text-paper shadow-soft">
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p class="text-xs font-black uppercase tracking-[0.22em] text-paper/55">Review only · no broker</p>
          <h1 class="mt-2 text-4xl font-black tracking-tight">Daily Advisory &amp; Book</h1>
          <p class="mt-2 max-w-2xl text-paper/70">
            Own the strongest relative-strength names, risk-sized and scaled by market health — and once
            bought, managed to an exit (stop, 20-day cap, or fading momentum). Recommendations to review;
            nothing is placed or executed here.
          </p>
        </div>
        <div class="flex shrink-0 flex-col items-end gap-2">
          <span v-if="status === 'ok'" class="rounded-full px-3 py-1 text-xs font-black uppercase tracking-[0.12em]"
            :class="isCurrent ? 'bg-moss/25 text-paper' : 'bg-sun/30 text-paper'">
            {{ isCurrent ? 'Current' : 'Behind' }} · as of {{ asof }}
          </span>
          <button
            class="rounded-full border border-paper/30 px-4 py-2 text-sm font-bold text-paper/90 hover:bg-paper/10 disabled:opacity-40"
            :disabled="pending" @click="refresh()"
          >{{ pending ? 'Refreshing…' : 'Refresh' }}</button>
        </div>
      </div>
      <div class="mt-5 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <MetricTile label="Market breadth" :value="breadthLabel(context.breadth_pct_above_50)"
          :note="`deployment ×${num(context.deployment_exposure, 2)}`" />
        <MetricTile label="Open positions" :value="String(summary.open ?? '—')"
          :note="`${summary.closed ?? 0} closed · ${flaggedCount} flagged`" />
        <MetricTile label="Realized vs NIFTY" :value="signed(summary.avg_realized_excess_pct)"
          :note="`${summary.win_rate_pct ?? '—'}% winners`" />
        <MetricTile label="Book P&amp;L (open)" :value="signed(summary.avg_unrealized_pct)"
          :note="generatedAt ? `refreshed ${generatedAt}` : 'avg across open'" />
      </div>
    </header>

    <ApiErrorBanner v-if="loadError" :error="loadError" title="Could not load the daily advisory" />
    <p v-if="pending" class="text-sm text-ink/40">loading…</p>

    <div v-if="status === 'missing_table' && !pending" class="rounded-2xl border border-ink/15 bg-white/70 px-5 py-5 text-sm text-ink/65">
      <span class="font-black text-ink/80">No advisory yet.</span>
      The daily advisory and paper book are populated by the evening cron chain
      (<code>paper_advisory</code> → <code>paper_book</code>). Run them once to populate this view.
    </div>

    <!-- freshness / stale warning (no silent fallback) -->
    <div v-if="status === 'ok' && !isCurrent" class="rounded-2xl border border-sun/45 bg-sun/10 px-5 py-3 text-sm text-ember">
      <span class="font-black">Advisory may be behind.</span>
      Showing <b>{{ asof }}</b>, but the latest market data is <b>{{ freshness.latest_market_date || '—' }}</b>.
      The evening chain may not have run on the freshest close — check the pipeline below.
    </div>
    <div v-if="status === 'ok' && flaggedCount" class="rounded-2xl border border-sun/45 bg-sun/10 px-5 py-3 text-sm text-ember">
      <span class="font-black">{{ flaggedCount }} position{{ flaggedCount === 1 ? '' : 's' }} flagged</span>
      — a stale price or a corporate action mid-hold. Look for the ⚑ in the open book and check the name.
    </div>

    <!-- pipeline health (maintenance at a glance) -->
    <div v-if="status === 'ok'" class="rounded-[2rem] border border-black/10 bg-white/80 p-5 shadow-soft">
      <div class="flex items-baseline justify-between">
        <h2 class="text-sm font-black uppercase tracking-[0.16em] text-ink/45">Pipeline</h2>
        <NuxtLink to="/operations" class="text-xs font-semibold text-sky hover:underline">Operations →</NuxtLink>
      </div>
      <div class="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-3">
        <div v-for="(c, i) in chain" :key="`c${i}`" class="rounded-xl border px-3 py-2"
          :class="c.current ? 'border-moss/25 bg-moss/5' : 'border-sun/45 bg-sun/10'">
          <div class="flex items-center justify-between">
            <span class="font-bold text-ink/80">{{ c.step }}</span>
            <StatusPill :tone="c.current ? 'success' : 'warning'">{{ c.current ? 'current' : 'behind' }}</StatusPill>
          </div>
          <p class="mt-1 text-xs text-ink/55">{{ c.last_date || 'no data' }} · {{ c.rows }} rows</p>
        </div>
      </div>
    </div>

    <!-- today's actions -->
    <div v-if="status === 'ok'" class="rounded-[2rem] border border-black/10 bg-white/80 p-5 shadow-soft">
      <div class="flex items-baseline justify-between">
        <h2 class="text-xl font-black tracking-tight text-ink">Today's actions</h2>
        <span class="text-xs font-bold uppercase tracking-[0.18em] text-ink/40">{{ String(actions.asof || asof) }}</span>
      </div>
      <div v-if="!exits.length && !trims.length && !buys.length" class="mt-3 text-sm text-ink/55">
        No exits or trims today.<span v-if="!buys.length"> No new buys.</span>
      </div>
      <div v-else-if="exits.length || trims.length" class="mt-3 overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="text-left text-xs font-black uppercase tracking-[0.12em] text-ink/45">
              <th class="py-2 pr-3">Action</th><th class="py-2 pr-3">Symbol</th><th class="py-2 pr-3">Reason</th>
              <th class="py-2 pr-3 text-right">Return</th><th class="py-2 pr-3 text-right">vs NIFTY</th><th class="py-2 text-right">Held</th>
            </tr>
          </thead>
          <tbody class="tabular-nums">
            <tr v-for="(row, i) in exits" :key="`x${i}`" class="border-t border-black/5">
              <td class="py-2 pr-3"><StatusPill tone="danger">EXIT</StatusPill></td>
              <td class="py-2 pr-3"><SymbolLink :symbol="String(row.symbol)" /></td>
              <td class="py-2 pr-3 text-ink/70">{{ reason(row.exit_reason) }}</td>
              <td class="py-2 pr-3 text-right" :class="signClass(row.realized_return_pct)">{{ signed(row.realized_return_pct) }}</td>
              <td class="py-2 pr-3 text-right" :class="signClass(row.realized_excess_pct)">{{ signed(row.realized_excess_pct) }}</td>
              <td class="py-2 text-right text-ink/60">{{ row.days_held }}d</td>
            </tr>
            <tr v-for="(row, i) in trims" :key="`t${i}`" class="border-t border-black/5">
              <td class="py-2 pr-3"><StatusPill tone="warning">TRIM</StatusPill></td>
              <td class="py-2 pr-3"><SymbolLink :symbol="String(row.symbol)" /></td>
              <td class="py-2 pr-3 text-ink/70">{{ reason(row.last_action_reason) }}</td>
              <td class="py-2 pr-3 text-right" :class="signClass(row.unrealized_return_pct)">{{ signed(row.unrealized_return_pct) }}</td>
              <td class="py-2 pr-3 text-right text-ink/40">—</td>
              <td class="py-2 text-right text-ink/60">{{ row.days_held }}d</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="buys.length" class="mt-3 text-sm text-ink/70">
        <StatusPill tone="success">BUY {{ buys.length }}</StatusPill>
        <span class="ml-2">{{ buys.map((b) => String(b.symbol)).join(', ') }}</span>
      </p>
    </div>

    <!-- track record vs NIFTY -->
    <div v-if="status === 'ok' && series.length" class="rounded-[2rem] border border-black/10 bg-white/80 p-5 shadow-soft">
      <div class="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h2 class="text-sm font-black uppercase tracking-[0.16em] text-ink/45">Closed track record vs NIFTY</h2>
          <p class="mt-1">
            <span class="text-2xl font-black tabular-nums" :class="signClass(track.avg_excess)">{{ signed(track.avg_excess) }}</span>
            <span class="ml-2 text-sm text-ink/55">avg excess/trade · {{ num(track.win, 0) }}% winners · {{ track.n }} closed</span>
          </p>
        </div>
        <svg viewBox="0 0 280 44" class="h-11 w-72 max-w-full" role="img" aria-label="realized returns per closed trade">
          <rect v-for="(b, i) in bars" :key="`s${i}`" :x="b.x" :y="b.y" :width="b.w" :height="b.h" rx="0.5"
            :style="{ fill: b.up ? '#0f4c5c' : '#9a3412' }" />
          <line x1="0" y1="22" x2="280" y2="22" stroke-width="1" style="stroke: rgba(23,33,29,0.15)" />
        </svg>
      </div>
    </div>

    <!-- today's picks -->
    <div v-if="status === 'ok'" class="rounded-[2rem] border border-black/10 bg-white/80 p-5 shadow-soft">
      <h2 class="text-xl font-black tracking-tight text-ink">Today's picks</h2>
      <p class="mt-1 text-sm text-ink/55">Weight is the share of the book per name (volatility-sized × the market-health dial, capped 5%). Stop is ~2.5× the stock's daily range below entry.</p>
      <div v-if="!picks.length" class="mt-3 text-sm text-ink/55">No picks for {{ asof }}.</div>
      <div v-else class="mt-3 overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="text-left text-xs font-black uppercase tracking-[0.12em] text-ink/45">
              <th class="py-2 pr-3">#</th><th class="py-2 pr-3">Symbol</th>
              <th class="py-2 pr-3 text-right">RS</th><th class="py-2 pr-3 text-right">Weight %</th>
              <th class="py-2 pr-3 text-right">Entry</th><th class="py-2 pr-3 text-right">Stop</th>
              <th class="py-2 pr-3 text-right">ATR %</th><th class="py-2 text-right">Cost %</th>
            </tr>
          </thead>
          <tbody class="tabular-nums">
            <tr v-for="(row, i) in picks" :key="`p${i}`" class="border-t border-black/5 hover:bg-paper/60">
              <td class="py-2 pr-3 text-ink/50">{{ row.rank }}</td>
              <td class="py-2 pr-3 font-bold"><SymbolLink :symbol="String(row.symbol)" /></td>
              <td class="py-2 pr-3 text-right">{{ num(row.rs_percentile, 1) }}</td>
              <td class="py-2 pr-3 text-right font-semibold text-moss">{{ num(row.advisory_weight_pct, 2) }}</td>
              <td class="py-2 pr-3 text-right">{{ num(row.entry_price, 2) }}</td>
              <td class="py-2 pr-3 text-right text-ink/70">{{ num(row.stop_price, 2) }}</td>
              <td class="py-2 pr-3 text-right text-ink/60">{{ num(typeof row.atr_pct === 'number' ? row.atr_pct * 100 : row.atr_pct, 1) }}</td>
              <td class="py-2 text-right text-ink/60">{{ num(typeof row.cost_fraction === 'number' ? row.cost_fraction * 100 : row.cost_fraction, 2) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- open book -->
    <div v-if="status === 'ok'" class="rounded-[2rem] border border-black/10 bg-white/80 p-5 shadow-soft">
      <div class="flex items-baseline justify-between">
        <h2 class="text-xl font-black tracking-tight text-ink">Open book</h2>
        <span class="text-sm text-ink/55">avg unrealized <span :class="signClass(summary.avg_unrealized_pct)">{{ signed(summary.avg_unrealized_pct) }}</span></span>
      </div>
      <div v-if="!bookOpen.length" class="mt-3 text-sm text-ink/55">No open positions.</div>
      <div v-else class="mt-3 overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="text-left text-xs font-black uppercase tracking-[0.12em] text-ink/45">
              <th class="py-2 pr-3">Symbol</th><th class="py-2 pr-3">Entry</th>
              <th class="py-2 pr-3 text-right">Days</th><th class="py-2 pr-3 text-right">P&amp;L</th>
              <th class="py-2 pr-3 text-right">Entry RS</th><th class="py-2 pr-3">Signal</th>
            </tr>
          </thead>
          <tbody class="tabular-nums">
            <tr v-for="(row, i) in bookOpen" :key="`b${i}`" class="border-t border-black/5 hover:bg-paper/60">
              <td class="py-2 pr-3 font-bold">
                <SymbolLink :symbol="String(row.symbol)" />
                <span v-if="Number(row.missing_days) > 0 || row.ca_flag" class="ml-1 text-sun" title="stale price or corporate action — check">⚑</span>
              </td>
              <td class="py-2 pr-3 text-ink/60">{{ row.entry_date }}</td>
              <td class="py-2 pr-3 text-right text-ink/60">{{ row.days_held }}</td>
              <td class="py-2 pr-3 text-right" :class="signClass(row.unrealized_return_pct)">{{ signed(row.unrealized_return_pct) }}</td>
              <td class="py-2 pr-3 text-right text-ink/60">{{ num(row.entry_rs, 1) }}</td>
              <td class="py-2 pr-3"><StatusPill :tone="actionTone(row.last_action)">{{ String(row.last_action || 'HOLD') }}</StatusPill></td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="mt-4 text-xs leading-relaxed text-ink/45">
        Signals: <b class="text-ink/70">EXIT</b> a hard rule fired (stop, 20-day cap, or the price stopped
        printing) · <b class="text-ink/70">TRIM</b> momentum faded · <b class="text-ink/70">HOLD</b>
        otherwise · ⚑ flags a stale price or corporate action to check. Paper only, ~13 months / one regime
        — evidence to watch, not a guarantee.
      </p>
    </div>
  </section>
</template>
