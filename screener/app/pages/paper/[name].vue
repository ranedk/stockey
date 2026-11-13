<script setup lang="ts">
import type { PtStrategy, PtOrder } from '~/types/paperTracks'

const route = useRoute()
const name = computed(() => String(route.params.name))
useHead({ title: () => paperName(name.value) })

const { data, status, error } = useSystrader<PtStrategy>(() => `/api/paper/${name.value}`, { key: `paper-${name.value}`, watch: [name] })

const s = computed(() => data.value ?? null)
const reference = computed(() => s.value?.reference ?? null)
const own = computed(() => paperBook(s.value, 'strategy'))
const owner = computed(() => paperParadigmOwner(name.value))
const books = PAPER_BOOK_DEFS.map(b => ({ key: b.key, label: b.label, color: b.color }))
const refLong = computed(() => (reference.value?.nav?.length ?? 0) > 400)

// Holdings, worst first: the losers are what a person needs to look at.
const holdings = computed(() => [...(s.value?.holdings ?? [])].sort((a, b) => (a.return ?? 0) - (b.return ?? 0)))
const holdingsLimit = ref(12)

const orders = computed<PtOrder[]>(() => s.value?.pending?.orders ?? [])
const ordersLimit = ref(15)
const sideCount = computed(() => {
  const c: Record<string, number> = { BUY: 0, SELL: 0, EXIT: 0 }
  for (const o of orders.value) c[o.side] = (c[o.side] ?? 0) + 1
  return c
})

// The ₹1 crore account's side of the sheet: real orders, rupees, and Dhan's charges.
// Rows within 10% of target trade nothing.
const account = computed(() => {
  let trades = 0, traded = 0, cost = 0
  for (const o of orders.value) {
    if (o.value_rs && o.value_rs > 0) { trades++; traded += o.value_rs; cost += o.cost_rs ?? 0 }
  }
  return { trades, traded, cost, any: orders.value.some(o => o.to_shares !== null) }
})

function sharesMove(o: PtOrder): string {
  if (o.to_shares === null || o.from_shares === null) return '—'
  const a = Math.round(o.from_shares), b = Math.round(o.to_shares)
  if (!o.value_rs) return a === 0 && b === 0 ? 'none (under one share)' : `${b} · hold, within 10%`
  return `${a} → ${b}`
}

const SIDE: Record<string, { color: 'success' | 'warning' | 'error', label: string, icon: string }> = {
  BUY: { color: 'success', label: 'Buy', icon: 'i-lucide-arrow-up' },
  SELL: { color: 'warning', label: 'Trim', icon: 'i-lucide-arrow-down' },
  EXIT: { color: 'error', label: 'Exit', icon: 'i-lucide-x' },
}

const specRows = computed(() => s.value ? [
  { k: 'Picks from', v: s.value.spec.universe, icon: 'i-lucide-globe' },
  { k: 'Signal', v: s.value.spec.signal, icon: 'i-lucide-radar' },
  { k: 'Which stocks', v: s.value.spec.selection, icon: 'i-lucide-list-filter' },
  { k: 'How much of each', v: s.value.spec.weighting, icon: 'i-lucide-scale' },
  { k: 'How often it trades', v: s.value.spec.rebalance, icon: 'i-lucide-calendar-sync' },
  { k: 'When orders fill', v: s.value.spec.execution, icon: 'i-lucide-clock' },
  { k: 'Costs assumed', v: s.value.spec.costs, icon: 'i-lucide-receipt' },
] : [])

// This write-up is the trend-quintile strategy's own history (systrader LEDGER rows 17-18).
// a zero date (0001-01-01) means no sheet has been computed yet
const sheetDate = computed(() => { const d = s.value?.pending?.based_on; return d && Number(d.slice(0, 4)) > 1900 ? d : null })

const hasStory = computed(() => name.value === 'trend-quintile')
</script>

<template>
  <UDashboardPanel id="paper-detail">
    <template #header>
      <PageNavbar :title="paperName(name)" icon="i-lucide-notebook-pen">
        <template #right>
          <UButton to="/paper" variant="ghost" color="neutral" size="sm" icon="i-lucide-arrow-left" label="All tracks" class="hidden sm:flex" />
        </template>
      </PageNavbar>
    </template>

    <template #body>
      <div class="mx-auto w-full max-w-7xl space-y-6">
        <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-plug-zap" title="Could not load this strategy"
          :description="error.statusCode === 404 ? `No paper track is called “${name}”.` : 'The paper-trading API (systrader cmd/api on :8090) did not answer.'" />

        <template v-else-if="status === 'pending' && !s">
          <USkeleton class="h-24 rounded-xl" />
          <div class="grid grid-cols-2 gap-3 lg:grid-cols-5"><USkeleton v-for="i in 5" :key="i" class="h-24 rounded-xl" /></div>
          <USkeleton class="h-80 rounded-xl" />
        </template>

        <template v-else-if="s">
          <!-- title block -->
          <div class="flex flex-wrap items-start justify-between gap-3">
            <div class="min-w-0 max-w-3xl">
              <div class="flex flex-wrap items-center gap-2">
                <h1 class="text-xl font-semibold text-highlighted">{{ paperName(name) }}</h1>
                <UBadge v-if="owner" color="info" variant="subtle" icon="i-lucide-git-compare" :label="`Copy of ${owner}`" />
                <UBadge v-if="s.days === 0" color="neutral" variant="subtle" icon="i-lucide-hourglass" label="Waiting for first fill" />
                <UBadge v-else color="primary" variant="subtle" icon="i-lucide-activity" :label="`${s.days} trading days`" />
              </div>
              <p class="mt-1 text-sm text-muted">{{ s.spec.signal }}</p>
              <p class="mt-1 text-xs text-dimmed">
                A live record for one frozen strategy, from data nobody chose. Nothing here is a recommendation and no money moves off it.
              </p>
            </div>
          </div>

          <!-- headline -->
          <div v-if="s.days > 0" class="grid grid-cols-2 gap-3 lg:grid-cols-5">
            <UCard :ui="{ body: 'p-4 sm:p-4' }">
              <div class="text-xs text-muted">Since {{ dateShort(s.start) }}</div>
              <Change :value="own?.total_return" class="mt-1 text-2xl" />
            </UCard>
            <UCard :ui="{ body: 'p-4 sm:p-4' }">
              <div class="text-xs text-muted">Last session</div>
              <Change :value="own?.day_return" :digits="2" class="mt-1 text-2xl" />
              <div class="text-[11px] text-dimmed">{{ dateShort(s.as_of) }}</div>
            </UCard>
            <UCard :ui="{ body: 'p-4 sm:p-4' }">
              <UTooltip text="Strategy return minus the return of every eligible stock held in equal amounts. A strategy up 12% in a market up 14% has lost.">
                <div class="cursor-help text-xs text-muted underline decoration-dotted underline-offset-2">Ahead of the market</div>
              </UTooltip>
              <Change :value="paperExcess(s)" class="mt-1 text-2xl" />
            </UCard>
            <UCard :ui="{ body: 'p-4 sm:p-4' }">
              <div class="text-xs text-muted">Stocks up / down</div>
              <div class="num mt-1 text-2xl font-semibold">
                <span class="text-up">{{ s.winners }}</span><span class="text-dimmed"> / </span><span class="text-down">{{ s.losers }}</span>
              </div>
              <SegmentedBar v-if="s.winners + s.losers" class="mt-2" :height="6"
                :parts="[{ label: 'Up', value: s.winners, color: 'var(--up)' }, { label: 'Down', value: s.losers, color: 'var(--down)' }]" />
            </UCard>
            <UCard :ui="{ body: 'p-4 sm:p-4' }" class="col-span-2 lg:col-span-1">
              <UTooltip text="The biggest fall from a high point to a later low.">
                <div class="cursor-help text-xs text-muted underline decoration-dotted underline-offset-2">Worst drop</div>
              </UTooltip>
              <div class="num mt-1 text-2xl font-semibold" :class="signClass(own?.max_drawdown)">{{ pct(own?.max_drawdown, 1) }}</div>
            </UCard>
          </div>
          <UCard v-else>
            <div class="flex flex-wrap items-center gap-x-8 gap-y-2 text-sm">
              <div><span class="text-muted">Frozen</span> <span class="ml-1 font-medium text-highlighted">{{ dateShort(s.start) }}</span></div>
              <div><span class="text-muted">Status</span> <span class="ml-1 font-medium text-highlighted">waiting for the first fill</span></div>
              <div v-if="orders.length"><span class="text-muted">On the sheet</span> <span class="ml-1 font-medium text-highlighted">{{ orders.length }} orders</span></div>
            </div>
          </UCard>

          <!-- forward record + spec -->
          <div class="grid items-start gap-4 lg:grid-cols-3">
            <UCard class="lg:col-span-2">
              <template #header>
                <h3 class="font-semibold text-highlighted">Live record</h3>
                <p class="text-xs text-muted">Gain since {{ dateShort(s.start) }}, against the two control books. Every day adds one observation that could not have been searched for.</p>
              </template>
              <UEmpty v-if="s.days === 0" icon="i-lucide-hourglass" title="No trading days yet"
                description="The record begins with the first fill after the strategy was frozen. Judgement waits for 12 months and 12 rebalances, not the first good week." class="py-8" />
              <template v-else>
                <PaperNavChart :points="s.nav ?? []" :books="books" mode="return" />
                <div class="mt-4"><PaperBookTable :summaries="s.summaries ?? []" /></div>
              </template>
            </UCard>

            <UCard>
              <template #header><h3 class="font-semibold text-highlighted">The rules</h3></template>
              <dl class="space-y-3">
                <div v-for="r in specRows" :key="r.k" class="flex gap-2.5">
                  <UIcon :name="r.icon" class="mt-0.5 size-4 shrink-0 text-dimmed" />
                  <div class="min-w-0">
                    <dt class="text-xs text-muted">{{ r.k }}</dt>
                    <dd class="text-sm text-default">{{ r.v }}</dd>
                  </div>
                </div>
                <div class="flex gap-2.5">
                  <UIcon name="i-lucide-file-lock" class="mt-0.5 size-4 shrink-0 text-dimmed" />
                  <div class="min-w-0">
                    <dt class="text-xs text-muted">Frozen write-up</dt>
                    <dd class="break-all font-mono text-xs text-default">{{ s.spec.doc }}</dd>
                  </div>
                </div>
              </dl>
            </UCard>
          </div>

          <!-- order sheet -->
          <UCard :ui="{ body: 'p-0 sm:p-0' }">
            <template #header>
              <div class="flex flex-wrap items-start justify-between gap-2">
                <div>
                  <h3 class="font-semibold text-highlighted">Orders for the next session</h3>
                  <p class="text-xs text-muted">
                    What the frozen rules would place at the next open<template v-if="sheetDate">, priced off the close of {{ dateShort(sheetDate) }}</template>.
                    On a hold day the list is shown anyway, so the book can be watched without being touched.
                  </p>
                </div>
                <UBadge v-if="s.pending?.due" color="success" variant="subtle" icon="i-lucide-refresh-cw" label="Rebalance due" />
                <UBadge v-else-if="s.pending && sheetDate" color="neutral" variant="subtle" icon="i-lucide-pause"
                  :label="`Hold · next rebalance in ${s.pending.days_to_due} trading days`" />
              </div>
              <div v-if="orders.length" class="mt-3 flex flex-wrap gap-2 text-xs">
                <UBadge v-for="(n, side) in sideCount" v-show="n" :key="side" :color="SIDE[side]!.color" variant="outline" size="sm" :label="`${n} ${SIDE[side]!.label.toLowerCase()}`" />
              </div>
              <div v-if="account.any" class="mt-3 grid grid-cols-3 gap-2 rounded-lg bg-elevated/60 p-3">
                <div>
                  <div class="text-[11px] text-muted">₹1 crore account: orders</div>
                  <div class="num font-semibold text-highlighted">{{ account.trades }}</div>
                </div>
                <div>
                  <div class="text-[11px] text-muted">Value traded</div>
                  <div class="num font-semibold text-highlighted">{{ paperRupees(account.traded) }}</div>
                </div>
                <div>
                  <div class="text-[11px] text-muted">Dhan charges</div>
                  <div class="num font-semibold text-highlighted">{{ paperRupees(account.cost) }}
                    <span v-if="account.traded > 0" class="text-xs font-normal text-muted">({{ (account.cost / account.traded * 1e4).toFixed(1) }} bps)</span>
                  </div>
                </div>
                <div class="col-span-3 text-[11px] text-dimmed">Whole shares at the opening auction; a position within 10% of its target is left alone.</div>
              </div>
            </template>
            <UEmpty v-if="!orders.length" icon="i-lucide-check-check" title="No orders" description="The book already matches its target." class="py-8" />
            <div v-else class="overflow-x-auto">
              <table class="w-full min-w-[48rem] text-sm">
                <thead>
                  <tr class="border-b border-default text-xs text-muted">
                    <th class="px-4 py-2 text-left font-medium">Action</th>
                    <th class="px-3 py-2 text-left font-medium">Stock</th>
                    <th class="px-3 py-2 text-right font-medium">Weight now</th>
                    <th class="px-3 py-2 text-right font-medium">Target</th>
                    <th class="px-3 py-2 text-right font-medium">Reference price</th>
                    <th class="px-3 py-2 text-right font-medium">Shares (₹1 Cr acct)</th>
                    <th class="px-3 py-2 text-right font-medium">Value</th>
                    <th class="px-4 py-2 text-right font-medium">Charges</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-default">
                  <tr v-for="o in orders.slice(0, ordersLimit)" :key="o.symbol" class="hover:bg-elevated/40">
                    <td class="px-4 py-1.5"><UBadge :color="SIDE[o.side]?.color ?? 'neutral'" variant="subtle" size="sm" :icon="SIDE[o.side]?.icon" :label="SIDE[o.side]?.label ?? o.side" /></td>
                    <td class="px-3 py-1.5 font-medium text-highlighted"><NuxtLink :to="`/company/${encodeURIComponent(o.symbol)}`" class="hover:text-primary">{{ o.symbol }}</NuxtLink></td>
                    <td class="num px-3 py-1.5 text-right text-muted">{{ pct(o.from_weight, 2) }}</td>
                    <td class="num px-3 py-1.5 text-right">{{ pct(o.to_weight, 2) }}</td>
                    <td class="num px-3 py-1.5 text-right">{{ inr(o.fill_price) }}</td>
                    <td class="num px-3 py-1.5 text-right text-muted">{{ sharesMove(o) }}</td>
                    <td class="num px-3 py-1.5 text-right">{{ o.value_rs ? paperRupees(o.value_rs) : '—' }}</td>
                    <td class="num px-4 py-1.5 text-right text-muted">{{ o.value_rs ? paperRupees(o.cost_rs) : '—' }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div v-if="orders.length > 15" class="border-t border-default p-2 text-center">
              <UButton variant="ghost" color="neutral" size="sm"
                :label="ordersLimit < orders.length ? `Show all ${orders.length} orders` : 'Show fewer'"
                @click="ordersLimit = ordersLimit < orders.length ? orders.length : 15" />
            </div>
          </UCard>

          <!-- current book -->
          <UCard v-if="holdings.length" :ui="{ body: 'p-0 sm:p-0' }">
            <template #header>
              <div class="flex flex-wrap items-baseline justify-between gap-2">
                <div>
                  <h3 class="font-semibold text-highlighted">What it holds now</h3>
                  <p class="text-xs text-muted">As of {{ dateShort(s.holdings_as_of) }} · worst first, because the losers are what need looking at</p>
                </div>
                <span class="text-xs text-muted"><span class="text-up">▲ {{ s.winners }} up</span> · <span class="text-down">▼ {{ s.losers }} down</span></span>
              </div>
            </template>
            <div class="overflow-x-auto">
              <table class="w-full min-w-[42rem] text-sm">
                <thead>
                  <tr class="border-b border-default text-xs text-muted">
                    <th class="px-4 py-2 text-left font-medium">Stock</th>
                    <th class="px-3 py-2 text-right font-medium">Since bought</th>
                    <th class="px-3 py-2 text-right font-medium">Weight</th>
                    <th class="px-3 py-2 text-right font-medium">Shares (₹1 Cr acct)</th>
                    <th class="px-3 py-2 text-left font-medium">Bought</th>
                    <th class="px-3 py-2 text-right font-medium">Buy price</th>
                    <th class="px-4 py-2 text-right font-medium">Last price</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-default">
                  <tr v-for="h in holdings.slice(0, holdingsLimit)" :key="h.symbol" class="hover:bg-elevated/40">
                    <td class="px-4 py-1.5 font-medium text-highlighted"><NuxtLink :to="`/company/${encodeURIComponent(h.symbol)}`" class="hover:text-primary">{{ h.symbol }}</NuxtLink></td>
                    <td class="px-3 py-1.5 text-right"><Change :value="h.return" /></td>
                    <td class="num px-3 py-1.5 text-right text-muted">{{ pct(h.weight, 2) }}</td>
                    <td class="num px-3 py-1.5 text-right text-muted">{{ h.shares != null ? num(Math.round(h.shares)) : '—' }}</td>
                    <td class="px-3 py-1.5 text-muted">{{ dateShort(h.entry_date) }}</td>
                    <td class="num px-3 py-1.5 text-right text-muted">{{ inr(h.entry_price) }}</td>
                    <td class="num px-4 py-1.5 text-right">{{ inr(h.last_price) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div v-if="holdings.length > 12" class="border-t border-default p-2 text-center">
              <UButton variant="ghost" color="neutral" size="sm"
                :label="holdingsLimit < holdings.length ? `Show all ${holdings.length} stocks` : 'Show fewer'"
                @click="holdingsLimit = holdingsLimit < holdings.length ? holdings.length : 12" />
            </div>
          </UCard>

          <!-- backtest, clearly separated -->
          <UCard v-if="reference" class="ring-warning/40">
            <template #header>
              <div class="flex flex-wrap items-center gap-2">
                <UIcon name="i-lucide-history" class="size-4 text-warning" />
                <h3 class="font-semibold text-highlighted">Backtest of the same rules</h3>
                <UBadge color="warning" variant="subtle" size="sm" label="Not evidence" />
              </div>
              <p class="mt-1 max-w-3xl text-xs text-muted">
                The frozen rules run over {{ dateShort(reference.start) }} to {{ dateShort(reference.as_of) }}. The idea was found by
                searching this very data, so these numbers are what searching produces and cannot support the strategy. They are here
                to show what was expected, so the live record above can be compared against it.
              </p>
            </template>
            <div class="mb-1 text-xs font-medium text-muted">{{ refLong ? 'What ₹100 grew to (log scale)' : 'Gain since the start' }}</div>
            <PaperNavChart :points="reference.nav ?? []" :books="books" :mode="refLong ? 'growth' : 'return'" />
            <div class="mt-4"><PaperBookTable :summaries="reference.summaries ?? []" /></div>
          </UCard>

          <!-- history of this one strategy -->
          <div v-if="hasStory" class="grid gap-4 lg:grid-cols-2">
            <UCard>
              <template #header><h3 class="font-semibold text-highlighted">Why these rules might work</h3></template>
              <p class="text-sm text-muted">
                Trend followers are paid by sellers who sell winners too early, by funds that must sell what has risen to rebalance,
                and by traders anchored to old prices. Across many stocks this shows up as recent winners continuing to beat the rest:
                the well-documented momentum effect, which this almost certainly re-finds rather than discovers. Its known way to fail
                is a momentum crash: a sharp rebound off a market bottom punishes a book holding the previous winners.
              </p>
            </UCard>
            <UCard>
              <template #header><h3 class="font-semibold text-highlighted">How this strategy got here</h3></template>
              <ol class="space-y-3 text-sm">
                <li v-for="(st, i) in [
                  { t: 'Exploration', d: 'A slice explorer cut every rule\'s edge by liquidity, size, volatility, price, sector, trend state, market breadth and year, each slice measured against itself on the same day. Trend across stocks, never tested on this universe before, came out steady and not explained by taking more risk. 62 buckets were examined, so no single bucket was a result. (Ledger row 17.)' },
                  { t: 'Cost screen', d: 'The portfolio was then run with real turnover and costs against two controls. It held: it beat an equal-weight book and a random ranking with the same turnover, survived doubled costs, held in both halves of the sample, and sat on a flat nine-point parameter plateau. Declared in advance able to kill the idea but not to support it, because it ran on the data the idea came from. (Ledger row 18.)' },
                  { t: 'What did not survive', d: 'The most striking exploration finding, that the signal flips when market breadth collapses, barely moves a yearly number because such washouts are 3% of days. It is deliberately left out.' },
                  { t: 'Live record', d: 'The setup was frozen in writing before any live data existed, with its stop rules: 18 months of losing to the equal-weight book month by month, or real costs above 100 bps, stops it. Changing any setting restarts the clock.' },
                ]" :key="st.t" class="flex gap-3">
                  <span class="grid size-6 shrink-0 place-items-center rounded-full bg-primary/10 text-xs font-semibold text-primary">{{ i + 1 }}</span>
                  <div><div class="font-medium text-highlighted">{{ st.t }}</div><p class="text-muted">{{ st.d }}</p></div>
                </li>
              </ol>
            </UCard>
          </div>
        </template>
      </div>
    </template>
  </UDashboardPanel>
</template>
