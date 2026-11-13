<script setup lang="ts">
import type { InvestorClassification } from '~/types/watchlist'

useHead({ title: 'Investors' })
const { data, status, error, refresh } = useStockey<InvestorClassification[]>('/api/investors')
const toast = useToast()

type Tier = 'marquee' | 'recognized' | 'unknown'
const TIER: Record<Tier, { label: string, color: 'success' | 'info' | 'neutral', desc: string, swatch: string }> = {
  marquee: { label: 'Marquee', color: 'success', desc: 'A household name among investors', swatch: 'var(--up)' },
  recognized: { label: 'Recognized', color: 'info', desc: 'A real, identifiable investment name, but not a famous one', swatch: 'var(--series-1)' },
  unknown: { label: 'Unknown', color: 'neutral', desc: 'Not a name the model recognizes as an investor', swatch: 'var(--chart-axis)' },
}
const effective = (i: InvestorClassification): Tier => (i.override_tier || i.llm_tier || 'unknown') as Tier

const q = ref('')
const tierFilter = ref<Tier | 'all' | 'corrected'>('all')
const page = ref(1)
const PAGE = 50
const counts = computed(() => {
  const c = { marquee: 0, recognized: 0, unknown: 0, corrected: 0 }
  for (const i of data.value ?? []) { c[effective(i)]++; if (i.override_tier) c.corrected++ }
  return c
})
const filtered = computed(() => {
  const term = q.value.trim().toLowerCase()
  return (data.value ?? []).filter(i => (tierFilter.value === 'all' || (tierFilter.value === 'corrected' ? !!i.override_tier : effective(i) === tierFilter.value))
    && (!term || i.investor_name_display.toLowerCase().includes(term) || i.investor_key.includes(term)))
})
watch([q, tierFilter], () => { page.value = 1 })
const pageRows = computed(() => filtered.value.slice((page.value - 1) * PAGE, page.value * PAGE))
const parts = computed(() => (Object.keys(TIER) as Tier[]).map(k => ({ label: TIER[k].label, value: counts.value[k], color: TIER[k].swatch })))

// Override form: the correction always wins over the model's suggestion.
const editingKey = ref<string | null>(null)
const editTier = ref<Tier>('marquee')
const editNotes = ref('')
const submitting = ref(false)
function startEdit(i: InvestorClassification) {
  editingKey.value = i.investor_key
  editTier.value = effective(i)
  editNotes.value = i.override_notes || ''
}
async function save(key: string) {
  submitting.value = true
  try {
    await stockeyApi(`/api/investors/${encodeURIComponent(key)}/override`, { method: 'POST', body: { tier: editTier.value, notes: editNotes.value || null } })
    editingKey.value = null
    toast.add({ title: 'Correction saved', color: 'success', icon: 'i-lucide-check' })
    await refresh()
  } catch (e: any) {
    toast.add({ title: 'Could not save the correction', description: e?.data?.detail || e?.message, color: 'error', icon: 'i-lucide-circle-alert' })
  } finally {
    submitting.value = false
  }
}
const tierItems = [{ label: 'All', value: 'all' }, ...(Object.keys(TIER) as Tier[]).map(k => ({ label: TIER[k].label, value: k })), { label: 'Corrected', value: 'corrected' }]
const SOURCE: Record<string, string> = { bse: 'BSE filing', nse: 'NSE filing', deal_flow: 'Bulk/block deal' }
</script>

<template>
  <UDashboardPanel id="investors">
    <template #header>
      <PageNavbar title="Investors" icon="i-lucide-users" />
    </template>

    <template #body>
      <div class="mx-auto w-full max-w-7xl space-y-4">
        <p class="max-w-4xl text-sm text-muted">
          Every named investor from a capital-raise filing (preferential allotment, QIP, rights issue, warrants) or a bulk/block deal,
          sorted by how widely recognized the name is, <em>not</em> by how well they invest. The AI's tier is a starting point;
          correct it with <strong>Edit</strong> if it is wrong, and the correction always wins.
        </p>

        <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-circle-alert" title="Could not reach the investors API" :description="error.message" />
        <USkeleton v-else-if="!data && status === 'pending'" class="h-96" />
        <UEmpty v-else-if="data && !data.length" icon="i-lucide-users" title="No investors classified yet" class="py-12" />

        <template v-else-if="data">
          <div class="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <UCard v-for="k in (Object.keys(TIER) as Tier[])" :key="k" :ui="{ body: 'p-3 sm:p-4' }">
              <UTooltip :text="TIER[k].desc"><UBadge :color="TIER[k].color" variant="subtle" :label="TIER[k].label" /></UTooltip>
              <div class="num mt-2 text-2xl font-semibold text-highlighted">{{ num(counts[k]) }}</div>
              <div class="text-[11px] text-dimmed">{{ pct(counts[k] / data.length, 0) }} of {{ num(data.length) }} names</div>
            </UCard>
            <UCard :ui="{ body: 'p-3 sm:p-4' }">
              <UBadge color="warning" variant="subtle" icon="i-lucide-pencil" label="Corrected by hand" />
              <div class="num mt-2 text-2xl font-semibold text-highlighted">{{ num(counts.corrected) }}</div>
              <div class="text-[11px] text-dimmed">AI tier overridden</div>
            </UCard>
          </div>

          <UCard>
            <div class="mb-1 text-xs font-medium text-muted">Tier mix</div>
            <SegmentedBar :parts="parts" :height="10" />
          </UCard>

          <UCard :ui="{ body: 'p-0 sm:p-0', header: 'p-3 sm:px-4' }">
            <template #header>
              <div class="flex flex-wrap items-center gap-2">
                <UInput v-model="q" icon="i-lucide-search" placeholder="Find an investor" size="sm" :ui="{ root: 'w-full sm:w-64' }" />
                <UTabs v-model="tierFilter" :items="tierItems" size="xs" :content="false" class="hidden sm:flex" />
                <USelect v-model="tierFilter" :items="tierItems" size="sm" class="w-40 sm:hidden" />
                <span class="ml-auto text-xs text-muted">{{ num(filtered.length) }} names</span>
              </div>
            </template>
            <div>
              <div class="hidden border-b border-default px-4 py-2.5 text-xs font-medium text-muted md:grid md:grid-cols-[15rem_9rem_1fr_6.5rem_7rem] md:gap-4">
                <span>Investor</span><span>Tier</span><span>Why</span><span>Classified</span><span />
              </div>
              <ul class="divide-y divide-default">
                <li v-for="i in pageRows" :key="i.investor_key" class="grid gap-2 px-4 py-3 text-sm hover:bg-elevated/30 md:grid-cols-[15rem_9rem_1fr_6.5rem_7rem] md:gap-4" :class="editingKey === i.investor_key ? 'bg-elevated/40' : ''">
                  <div class="flex items-start justify-between gap-2 md:block">
                    <div class="min-w-0">
                      <div class="font-medium text-highlighted">{{ i.investor_name_display }}</div>
                      <div class="text-xs text-dimmed">
                        <template v-if="i.first_seen_source">first seen in {{ SOURCE[i.first_seen_source] || i.first_seen_source }}</template>
                        <span class="md:hidden"> · {{ dateShort(i.llm_classified_at) }}</span>
                      </div>
                    </div>
                  </div>
                  <div>
                    <USelect v-if="editingKey === i.investor_key" v-model="editTier" :items="(Object.keys(TIER) as Tier[]).map(k => ({ label: TIER[k].label, value: k }))" size="sm" class="w-36" />
                    <template v-else>
                      <UTooltip :text="TIER[effective(i)]?.desc"><UBadge :color="TIER[effective(i)]?.color || 'neutral'" variant="subtle" :label="TIER[effective(i)]?.label || effective(i)" /></UTooltip>
                      <div v-if="i.override_tier" class="mt-1 text-[11px] text-dimmed">corrected · AI said {{ i.llm_tier }}</div>
                    </template>
                  </div>
                  <div class="text-muted">
                    <UTextarea v-if="editingKey === i.investor_key" v-model="editNotes" :rows="2" autoresize placeholder="Why you are correcting it (optional)" class="w-full" size="sm" />
                    <template v-else>
                      <p>{{ i.override_notes || i.llm_reasoning || '—' }}</p>
                      <p v-if="!i.override_notes && i.llm_model" class="mt-0.5 text-[11px] text-dimmed">AI: {{ i.llm_model }}</p>
                    </template>
                  </div>
                  <div class="num hidden text-xs text-muted md:block">{{ dateShort(i.llm_classified_at) }}</div>
                  <div class="md:text-right">
                    <UButton v-if="editingKey !== i.investor_key" size="xs" color="neutral" variant="ghost" icon="i-lucide-pencil" label="Edit" class="-ml-2 md:ml-0" @click="startEdit(i)" />
                    <div v-else class="flex gap-1.5 md:justify-end">
                      <UButton size="xs" color="primary" :loading="submitting" label="Save" @click="save(i.investor_key)" />
                      <UButton size="xs" color="neutral" variant="ghost" label="Cancel" @click="editingKey = null" />
                    </div>
                  </div>
                </li>
              </ul>
              <UEmpty v-if="!filtered.length" icon="i-lucide-search-x" title="No investor matches" class="py-10" />
            </div>
            <template v-if="filtered.length > PAGE" #footer>
              <div class="flex flex-wrap items-center justify-between gap-2">
                <span class="text-xs text-muted">{{ (page - 1) * PAGE + 1 }}–{{ Math.min(page * PAGE, filtered.length) }} of {{ num(filtered.length) }}</span>
                <UPagination v-model:page="page" :total="filtered.length" :items-per-page="PAGE" size="xs" />
              </div>
            </template>
          </UCard>
        </template>
      </div>
    </template>
  </UDashboardPanel>
</template>
