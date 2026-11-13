<script setup lang="ts">
import type { PlatformIssuesResponse } from '~/types/api'

// advisory_identity_issues and advisory_fallback_events implement CLAUDE.md's "no
// silent fallback" principle. Until now their only reader was a nightly log.
const api = useApi()
const { data, status, error } = await useAsyncData(
  'platform-issues',
  () => api.get<PlatformIssuesResponse>('/api/platform-issues'),
)

const showIssues = ref(false)

const fallbackTypes = computed(() =>
  Object.entries(data.value?.fallback_events?.counts_by_type ?? {}).sort((a, b) => b[1] - a[1]),
)
</script>

<template>
  <section class="mt-6">
    <div class="flex flex-wrap items-center gap-3">
      <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-500">Open issues &amp; fallbacks</h2>
      <BadgePill
        v-if="data"
        :label="data.identity_issues.open_count === 0 ? 'no open issues' : `${data.identity_issues.open_count} open`"
        :tone="data.identity_issues.open_count === 0 ? 'good' : 'warn'"
      />
      <span v-if="data?.fallback_events" class="text-xs text-slate-400">
        {{ data.fallback_events.error_count ?? 0 }} error ·
        {{ data.fallback_events.warn_count ?? 0 }} warn
        in the last {{ data.fallback_events.window_hours }}h
      </span>
    </div>

    <p class="mt-1 text-xs text-slate-400">
      Identity issues are the same names the completeness check counts as "symbols with no
      intraday data" — here with a reason attached. Read-only: unlike the nightly digest,
      loading this page never rechecks or closes anything.
    </p>

    <div v-if="error" class="mt-3 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">
      Could not load platform issues ({{ error.message }}).
    </div>
    <div v-else-if="status === 'pending'" class="mt-3 text-sm text-slate-500">Loading…</div>

    <template v-else-if="data">
      <div class="mt-3 grid gap-4 md:grid-cols-2">
        <div class="rounded-lg border border-slate-200 bg-white p-3">
          <h3 class="text-xs font-medium text-slate-600">Identity issues by type</h3>
          <p v-if="Object.keys(data.identity_issues.by_type).length === 0" class="mt-2 text-xs text-slate-400">
            None open.
          </p>
          <ul v-else class="mt-2 space-y-1 text-xs">
            <li v-for="(count, type) in data.identity_issues.by_type" :key="type" class="flex justify-between gap-3">
              <span class="text-slate-700">{{ type }}</span>
              <span class="font-medium text-slate-900">{{ count }}</span>
            </li>
          </ul>
        </div>

        <div class="rounded-lg border border-slate-200 bg-white p-3">
          <h3 class="text-xs font-medium text-slate-600">Fallback events by type ({{ data.fallback_events.window_hours }}h)</h3>
          <p v-if="fallbackTypes.length === 0" class="mt-2 text-xs text-slate-400">
            {{ data.fallback_events.message || 'No degradations recorded in the window.' }}
          </p>
          <ul v-else class="mt-2 space-y-1 text-xs">
            <li v-for="[type, count] in fallbackTypes" :key="type" class="flex justify-between gap-3">
              <span class="text-slate-700">{{ type }}</span>
              <span class="font-medium text-slate-900">{{ count }}</span>
            </li>
          </ul>
        </div>
      </div>

      <button
        v-if="data.identity_issues.open_count"
        class="mt-3 text-xs font-medium text-slate-500 hover:text-slate-900"
        @click="showIssues = !showIssues"
      >
        {{ showIssues ? 'Hide' : 'Show' }} the {{ data.identity_issues.issues.length }} open issue(s)
      </button>

      <div v-if="showIssues" class="mt-2 overflow-x-auto rounded-lg border border-slate-200 bg-white">
        <table class="w-full text-xs">
          <thead class="border-b border-slate-200 bg-slate-50 text-left text-slate-500">
            <tr>
              <th class="px-3 py-2 font-medium">Symbol</th>
              <th class="px-3 py-2 font-medium">Type</th>
              <th class="px-3 py-2 font-medium">First seen</th>
              <th class="px-3 py-2 font-medium">Last seen</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="i in data.identity_issues.issues" :key="i.issue_key" class="border-b border-slate-100 last:border-0">
              <td class="px-3 py-1.5 font-medium text-slate-800">{{ i.symbol || '—' }}</td>
              <td class="px-3 py-1.5 text-slate-600">{{ i.issue_type }}</td>
              <td class="px-3 py-1.5 text-slate-400">{{ formatDate(i.first_seen_at) }}</td>
              <td class="px-3 py-1.5 text-slate-400">{{ formatDate(i.last_seen_at) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </section>
</template>
