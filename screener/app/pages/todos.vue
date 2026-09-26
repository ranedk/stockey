<script setup lang="ts">
import type { Todos } from '~/types/api'

const api = useApi()
const { data, status, error } = await useAsyncData('todos', () => api.get<Todos>('/api/todos'))

const ratingAgencies = computed(() => data.value?.rating_agencies ?? [])
</script>

<template>
  <div>
    <h1 class="text-xl font-semibold">Development Todos</h1>
    <p class="mt-1 text-sm text-slate-600">
      Gaps the pipeline has flagged from real data, not guesses. Self-clearing --
      an item drops off automatically once it's built.
    </p>

    <div v-if="error" class="mt-6 rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
      Could not reach the API ({{ error.message }}).
    </div>

    <template v-else-if="data">
      <section class="mt-6">
        <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-500">Rating agencies without a plugin</h2>
        <p class="mt-1 text-xs text-slate-400">
          Detected in real filings but no enrichment plugin exists yet -- ranked by how often they've actually shown up.
        </p>

        <div v-if="ratingAgencies.length === 0" class="mt-3 text-sm text-slate-500">
          Nothing flagged -- every agency seen in real data so far has a plugin.
        </div>

        <div v-else class="mt-3 overflow-x-auto rounded-lg border border-slate-200 bg-white">
          <table class="w-full text-sm">
            <thead class="border-b border-slate-200 bg-slate-50 text-left text-xs uppercase text-slate-500">
              <tr>
                <th class="px-4 py-2">Agency</th>
                <th class="px-4 py-2 text-right">Times seen</th>
                <th class="px-4 py-2">First seen</th>
                <th class="px-4 py-2">Last seen</th>
                <th class="px-4 py-2">Example</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="a in ratingAgencies" :key="a.agency_name" class="border-b border-slate-100 last:border-0">
                <td class="px-4 py-2 font-medium capitalize">{{ a.agency_name }}</td>
                <td class="px-4 py-2 text-right">
                  <BadgePill :label="String(a.occurrence_count)" :tone="a.occurrence_count >= 5 ? 'warn' : 'neutral'" />
                </td>
                <td class="px-4 py-2 text-xs text-slate-500">{{ formatDate(a.first_seen_at) }}</td>
                <td class="px-4 py-2 text-xs text-slate-500">{{ formatDate(a.last_seen_at) }}</td>
                <td class="px-4 py-2 max-w-sm text-xs text-slate-500">
                  {{ a.example_company_master_id }}
                  <span v-if="a.example_headline">-- {{ a.example_headline }}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>

    <p v-else-if="status === 'pending'" class="mt-6 text-sm text-slate-500">Loading…</p>
  </div>
</template>
