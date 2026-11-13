<script setup lang="ts">
// To-dos the pipeline raises from real data (rebuilt from legacy pages/todos). Self-clearing:
// an item drops off once the missing piece is built.
useHead({ title: 'To-dos' })
const { data, error } = useStockey<OpsTodos>('/api/todos', { lazy: true, server: false })

const agencies = computed(() => [...(data.value?.rating_agencies ?? [])].sort((a, b) => b.occurrence_count - a.occurrence_count))
const total = computed(() => agencies.value.reduce((s, a) => s + (a.occurrence_count || 0), 0))
const maxCount = computed(() => Math.max(1, ...agencies.value.map(a => a.occurrence_count)))
const latest = computed(() => agencies.value.reduce<string | null>((m, a) => (a.last_seen_at && (!m || a.last_seen_at > m) ? a.last_seen_at : m), null))
const OFTEN = 5
const symbolOf = (id: string | null) => (id ? id.replace(/^nse:|^bse:/, '') : '')
const agencyName = (n: string) => (n === 'unnamed' ? 'Agency not named' : n.replace(/\b\w/g, c => c.toUpperCase()))
</script>

<template>
  <UDashboardPanel id="todos">
    <template #header>
      <PageNavbar title="To-dos" icon="i-lucide-square-check" />
    </template>

    <template #body>
      <div class="mx-auto w-full max-w-7xl space-y-6">
        <p class="max-w-3xl text-sm text-muted">
          Gaps the data pipeline has found in real data, not guesses. Each item clears itself once the missing piece is built.
        </p>

        <div v-if="error" class="flex items-start gap-3 rounded-lg border border-error/30 bg-error/5 p-4 text-sm">
          <UIcon name="i-lucide-plug-zap" class="mt-0.5 size-5 shrink-0 text-error" />
          <div>
            <div class="font-medium text-highlighted">Could not load the to-do list</div>
            <p class="mt-0.5 text-muted">The stockey data API (port 8000) did not answer. Reload the page to try again.</p>
          </div>
        </div>

        <template v-else-if="!data">
          <div class="grid grid-cols-2 gap-3 md:grid-cols-3"><USkeleton v-for="i in 3" :key="i" class="h-24 rounded-xl" /></div>
          <USkeleton class="h-64 rounded-xl" />
        </template>

        <template v-else>
          <div class="grid grid-cols-2 gap-3 md:grid-cols-3">
            <UCard :ui="{ body: 'p-3 sm:p-4' }">
              <div class="flex items-center justify-between text-xs font-medium text-muted">
                Open to-dos
                <UIcon :name="agencies.length ? 'i-lucide-list-todo' : 'i-lucide-circle-check'" class="size-4" :class="agencies.length ? 'text-warning' : 'text-success'" />
              </div>
              <div class="num mt-1.5 text-2xl font-semibold text-highlighted">{{ agencies.length }}</div>
              <div class="text-xs text-muted">{{ agencies.length ? 'rating agencies to add' : 'nothing flagged' }}</div>
            </UCard>
            <UCard :ui="{ body: 'p-3 sm:p-4' }">
              <div class="text-xs font-medium text-muted">Filings affected</div>
              <div class="num mt-1.5 text-2xl font-semibold text-highlighted">{{ num(total) }}</div>
              <div class="text-xs text-muted">rating filings we could not read</div>
            </UCard>
            <UCard class="col-span-2 md:col-span-1" :ui="{ body: 'p-3 sm:p-4' }">
              <div class="text-xs font-medium text-muted">Last seen</div>
              <div class="mt-1.5 text-2xl font-semibold text-highlighted">{{ latest ? opsAgo(latest) : '—' }}</div>
              <div class="text-xs text-muted">{{ latest ? opsDateTime(latest) : 'no filings yet' }}</div>
            </UCard>
          </div>

          <UCard :ui="{ body: 'p-0 sm:p-0' }">
            <template #header>
              <div class="flex flex-wrap items-center gap-2">
                <UIcon name="i-lucide-badge-check" class="size-5 text-muted" />
                <h3 class="font-semibold text-highlighted">Rating agencies we cannot read yet</h3>
                <UBadge
                  :color="agencies.length ? 'warning' : 'success'" variant="subtle"
                  :icon="agencies.length ? 'i-lucide-triangle-alert' : 'i-lucide-circle-check'"
                  :label="agencies.length ? `${agencies.length} to add` : 'All covered'"
                />
              </div>
              <p class="mt-1.5 max-w-3xl text-xs text-muted">
                Credit-rating agencies named in company filings for which there is no reader (plugin) yet, so their rating
                changes are not picked up. Ordered by how often they have actually appeared. "Agency not named" is filings where
                no agency could be identified at all.
              </p>
            </template>

            <div v-if="!agencies.length" class="flex items-center gap-2 p-6 text-sm text-muted">
              <UIcon name="i-lucide-circle-check" class="size-4 text-success" />
              Nothing flagged: every agency seen in real filings so far has a reader.
            </div>

            <template v-else>
              <!-- phones: one card per agency -->
              <ul class="divide-y divide-default sm:hidden">
                <li v-for="a in agencies" :key="a.agency_name" class="space-y-2 p-4">
                  <div class="flex items-center justify-between gap-2">
                    <span class="font-medium text-highlighted">{{ agencyName(a.agency_name) }}</span>
                    <UBadge :color="a.occurrence_count >= OFTEN ? 'warning' : 'neutral'" variant="subtle" :icon="a.occurrence_count >= OFTEN ? 'i-lucide-flame' : undefined" :label="`${num(a.occurrence_count)} times`" />
                  </div>
                  <div class="h-1.5 rounded-full bg-elevated"><div class="h-full rounded-full" :style="{ width: `${a.occurrence_count / maxCount * 100}%`, background: 'var(--series-1)' }" /></div>
                  <div class="text-xs text-muted">First {{ opsDate(a.first_seen_at) }} · last {{ opsDate(a.last_seen_at) }}</div>
                  <div v-if="a.example_company_master_id || a.example_headline" class="text-xs text-muted">
                    <NuxtLink v-if="a.example_company_master_id" :to="`/company/${encodeURIComponent(symbolOf(a.example_company_master_id))}`" class="font-medium text-primary hover:underline">{{ symbolOf(a.example_company_master_id) }}</NuxtLink>
                    <span v-if="a.example_headline"> · {{ a.example_headline }}</span>
                  </div>
                </li>
              </ul>

              <div class="hidden overflow-x-auto sm:block">
                <table class="w-full min-w-[760px] text-sm">
                  <thead class="bg-elevated/60 text-left text-xs text-muted">
                    <tr>
                      <th class="px-4 py-2 font-medium">Agency</th>
                      <th class="w-56 px-4 py-2 font-medium">Times seen</th>
                      <th class="px-4 py-2 font-medium">First seen</th>
                      <th class="px-4 py-2 font-medium">Last seen</th>
                      <th class="px-4 py-2 font-medium">Example filing</th>
                    </tr>
                  </thead>
                  <tbody class="divide-y divide-default">
                    <tr v-for="a in agencies" :key="a.agency_name" class="align-top">
                      <td class="whitespace-nowrap px-4 py-3 font-medium text-highlighted">{{ agencyName(a.agency_name) }}</td>
                      <td class="px-4 py-3">
                        <div class="flex items-center gap-2">
                          <div class="h-1.5 flex-1 rounded-full bg-elevated"><div class="h-full rounded-full" :style="{ width: `${a.occurrence_count / maxCount * 100}%`, background: 'var(--series-1)' }" /></div>
                          <div class="flex w-20 justify-end"><UTooltip :text="a.occurrence_count >= OFTEN ? `Seen ${OFTEN} or more times: worth building a reader` : ''" :disabled="a.occurrence_count < OFTEN">
                            <UBadge :color="a.occurrence_count >= OFTEN ? 'warning' : 'neutral'" variant="subtle" size="sm" :icon="a.occurrence_count >= OFTEN ? 'i-lucide-flame' : undefined" :label="num(a.occurrence_count)" class="num" />
                          </UTooltip></div>
                        </div>
                      </td>
                      <td class="num whitespace-nowrap px-4 py-3 text-xs text-muted">{{ opsDate(a.first_seen_at) }}</td>
                      <td class="num whitespace-nowrap px-4 py-3 text-xs text-muted">{{ opsDate(a.last_seen_at) }}</td>
                      <td class="max-w-md px-4 py-3 text-xs text-muted">
                        <NuxtLink v-if="a.example_company_master_id" :to="`/company/${encodeURIComponent(symbolOf(a.example_company_master_id))}`" class="font-medium text-primary hover:underline">{{ symbolOf(a.example_company_master_id) }}</NuxtLink>
                        <span v-if="a.example_headline" class="line-clamp-2"> {{ a.example_headline }}</span>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </template>
          </UCard>
        </template>
      </div>
    </template>
  </UDashboardPanel>
</template>
