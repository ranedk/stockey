<script setup lang="ts">
// Open identity issues (stocks we could not match to a data feed, with a reason) and recent
// fallback events (a job quietly fell back to a degraded path). Read-only: unlike the nightly
// digest, loading this never rechecks or closes anything.
const props = defineProps<{ data: OpsPlatformIssues }>()
const showIssues = ref(false)
const showEvents = ref(false)
const events = computed(() => props.data.fallback_events.rows ?? [])
const sevLevel = (s: string) => (s === 'error' ? 'error' : s === 'warn' || s === 'warning' ? 'warn' : 'ok')
const idTypes = computed(() => Object.entries(props.data.identity_issues.by_type ?? {}).sort((a, b) => b[1] - a[1]))
const fb = computed(() => props.data.fallback_events)
const fbTypes = computed(() => Object.entries(fb.value.counts_by_type ?? {}).sort((a, b) => b[1] - a[1]))
const fbModules = computed(() => Object.entries(fb.value.counts_by_module ?? {}).sort((a, b) => b[1] - a[1]))
const maxOf = (xs: [string, number][]) => Math.max(1, ...xs.map(x => x[1]))
const pretty = (s: string) => s.replaceAll('_', ' ')
</script>

<template>
  <div class="space-y-4">
    <div class="grid gap-3 md:grid-cols-2">
      <div class="rounded-lg border border-default p-3">
        <div class="flex items-center justify-between gap-2">
          <h4 class="text-sm font-medium text-highlighted">Stocks we cannot match, by reason</h4>
          <OpsStatusBadge :level="data.identity_issues.open_count ? 'warn' : 'ok'" :label="data.identity_issues.open_count ? `${data.identity_issues.open_count} open` : 'None open'" size="xs" />
        </div>
        <p class="mt-0.5 text-xs text-muted">The same stocks the "every stock covered" check counts, with a reason attached.</p>
        <p v-if="!idTypes.length" class="mt-3 text-xs text-muted">None open.</p>
        <ul v-else class="mt-3 space-y-2 text-xs">
          <li v-for="[t, n] in idTypes" :key="t">
            <div class="flex justify-between gap-3"><span class="text-default">{{ pretty(t) }}</span><span class="num font-medium text-highlighted">{{ num(n) }}</span></div>
            <div class="mt-1 h-1.5 rounded-full bg-elevated"><div class="h-full rounded-full" :style="{ width: `${n / maxOf(idTypes) * 100}%`, background: 'var(--series-4)' }" /></div>
          </li>
        </ul>
      </div>

      <div class="rounded-lg border border-default p-3">
        <div class="flex items-center justify-between gap-2">
          <h4 class="text-sm font-medium text-highlighted">Fallbacks in the last {{ fb.window_hours }} hours</h4>
          <div class="flex gap-1">
            <OpsStatusBadge :level="(fb.error_count ?? 0) ? 'error' : 'ok'" :label="`${fb.error_count ?? 0} error${(fb.error_count ?? 0) === 1 ? '' : 's'}`" size="xs" />
            <OpsStatusBadge :level="(fb.warn_count ?? 0) ? 'warn' : 'ok'" :label="`${fb.warn_count ?? 0} warning${(fb.warn_count ?? 0) === 1 ? '' : 's'}`" size="xs" />
          </div>
        </div>
        <p class="mt-0.5 text-xs text-muted">Times a job switched to a backup path instead of failing loudly.</p>
        <p v-if="!fbTypes.length" class="mt-3 text-xs text-muted">{{ fb.message || 'No fallbacks recorded in the window.' }}</p>
        <template v-else>
          <ul class="mt-3 space-y-2 text-xs">
            <li v-for="[t, n] in fbTypes" :key="t">
              <div class="flex justify-between gap-3"><span class="break-all text-default">{{ pretty(t) }}</span><span class="num font-medium text-highlighted">{{ num(n) }}</span></div>
              <div class="mt-1 h-1.5 rounded-full bg-elevated"><div class="h-full rounded-full" :style="{ width: `${n / maxOf(fbTypes) * 100}%`, background: 'var(--series-1)' }" /></div>
            </li>
          </ul>
          <p v-if="fbModules.length" class="mt-3 break-all text-xs text-muted">
            By job: <span v-for="([m, n], i) in fbModules" :key="m"><code class="text-default">{{ m }}</code> {{ num(n) }}<template v-if="i < fbModules.length - 1"> · </template></span>
          </p>
        </template>
      </div>
    </div>

    <div v-if="data.identity_issues.open_count || events.length" class="flex flex-wrap gap-x-6">
      <UButton
        v-if="data.identity_issues.open_count"
        :label="`${showIssues ? 'Hide' : 'Show'} the ${data.identity_issues.issues.length} open issue${data.identity_issues.issues.length === 1 ? '' : 's'}`"
        variant="link" color="neutral" size="xs" class="px-0"
        :trailing-icon="showIssues ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'"
        @click="showIssues = !showIssues"
      />
      <UButton
        v-if="events.length"
        :label="`${showEvents ? 'Hide' : 'Show'} the ${events.length} latest fallback events`"
        variant="link" color="neutral" size="xs" class="px-0"
        :trailing-icon="showEvents ? 'i-lucide-chevron-up' : 'i-lucide-chevron-down'"
        @click="showEvents = !showEvents"
      />
    </div>
    <div v-if="showIssues && data.identity_issues.open_count">
      <p v-if="data.identity_issues.issues.length >= data.identity_issues.limit" class="text-xs text-muted">Showing the first {{ data.identity_issues.limit }}.</p>
      <div class="mt-2 max-h-[28rem] overflow-auto rounded-lg border border-default">
        <table class="w-full min-w-[560px] text-xs">
          <thead class="sticky top-0 bg-elevated text-left text-muted">
            <tr>
              <th class="px-3 py-2 font-medium">Stock</th>
              <th class="px-3 py-2 font-medium">Reason</th>
              <th class="px-3 py-2 text-right font-medium">Times tried</th>
              <th class="px-3 py-2 font-medium">First seen</th>
              <th class="px-3 py-2 font-medium">Last seen</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-default">
            <tr v-for="i in data.identity_issues.issues" :key="i.issue_key">
              <td class="px-3 py-1.5 font-medium text-highlighted">
                <UTooltip :text="i.error_text || ''" :disabled="!i.error_text"><span>{{ i.symbol || '—' }}</span></UTooltip>
              </td>
              <td class="px-3 py-1.5 text-default">
                <UTooltip :text="i.suggested_action || ''" :disabled="!i.suggested_action" :content="{ class: 'max-w-sm' }">
                  <span :class="i.suggested_action ? 'underline decoration-dotted underline-offset-2' : ''">{{ pretty(i.issue_type) }}</span>
                </UTooltip>
              </td>
              <td class="num px-3 py-1.5 text-right text-muted">{{ num(i.attempt_count) }}</td>
              <td class="num whitespace-nowrap px-3 py-1.5 text-muted">{{ opsDate(i.first_seen_at) }}</td>
              <td class="num whitespace-nowrap px-3 py-1.5 text-muted">{{ opsDate(i.last_seen_at) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div v-if="showEvents && events.length" class="max-h-[28rem] overflow-auto rounded-lg border border-default">
      <table class="w-full min-w-[960px] text-xs">
        <thead class="sticky top-0 bg-elevated text-left text-muted">
          <tr>
            <th class="px-3 py-2 font-medium">When</th>
            <th class="px-3 py-2 font-medium">Level</th>
            <th class="px-3 py-2 font-medium">Kind</th>
            <th class="px-3 py-2 font-medium">What happened</th>
            <th class="px-3 py-2 font-medium">Job</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-default">
          <tr v-for="e in events" :key="e.event_id" class="align-top">
            <td class="num whitespace-nowrap px-3 py-1.5 text-muted">{{ opsDateTime(e.observed_at) }}</td>
            <td class="px-3 py-1.5"><OpsStatusBadge :level="sevLevel(e.severity)" :label="e.severity === 'warn' ? 'Warning' : e.severity === 'error' ? 'Error' : e.severity" size="xs" /></td>
            <td class="whitespace-nowrap px-3 py-1.5 text-default">{{ pretty(e.fallback_type) }}</td>
            <td class="px-3 py-1.5 text-default">{{ e.reason || e.error_message || '—' }}<span v-if="e.symbol" class="text-muted"> · {{ e.symbol }}</span></td>
            <td class="px-3 py-1.5 font-mono text-muted">{{ e.module || e.source || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
