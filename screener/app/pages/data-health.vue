<script setup lang="ts">
// Data health (rebuilt from legacy pages/data-health): is the data actually there across the
// whole universe, are the collectors and the scheduler working, what is open. Read-only;
// repairs live in `data_readiness --fix` and the collectors themselves.
useHead({ title: 'Data health' })

const windowDays = ref(30)
const lazy = { lazy: true, server: false }
// The minute-data checks scan a large compressed table and take ~20 s cold; the API caches
// for 5 minutes, and "Recheck" forces a fresh run (refresh=true) on click only.
const dh = useStockey<OpsDataHealth>(() => `/api/data-health?window_days=${windowDays.value}`, lazy)
const sched = useStockey<OpsScheduler>('/api/scheduler-health', lazy)
const coll = useStockey<OpsCollectors>('/api/collectors', lazy)
const issues = useStockey<OpsPlatformIssues>('/api/platform-issues', lazy)
const cov = useStockey<OpsCoverage>('/api/coverage-report', lazy)

const rechecking = ref(false)
const recheckError = ref<string | null>(null)
async function recheck() {
  if (rechecking.value) return
  rechecking.value = true
  recheckError.value = null
  try {
    dh.data.value = await stockeyApi<OpsDataHealth>(`/api/data-health?window_days=${windowDays.value}&refresh=true`)
    await Promise.all([sched.refresh(), coll.refresh(), cov.refresh()])
  }
  catch (e: any) { recheckError.value = e?.message || 'Recheck failed' }
  finally { rechecking.value = false }
}

const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? '' : 's'}`

// One tile per area; each knows whether it is still loading, failed to load, or its status.
interface Tile { id: string, title: string, icon: string, level: string | null, value: string, sub: string, loading: boolean, failed: boolean }
const tiles = computed<Tile[]>(() => {
  const d = dh.data.value, s = sched.data.value, c = coll.data.value, i = issues.data.value, v = cov.data.value
  const fbErr = i?.fallback_events?.error_count ?? 0
  return [
    {
      id: 'checks', title: 'Data checks', icon: 'i-lucide-database',
      level: d?.status ?? null,
      value: d ? (d.status === 'ok' ? 'All passing' : [d.error_count && `${d.error_count} failing`, d.warn_count && plural(d.warn_count, 'warning')].filter(Boolean).join(', ')) : '',
      sub: d ? `${d.findings.length} checks over ${d.window_days} days` : '',
      loading: !d && !dh.error.value, failed: !!dh.error.value && !d,
    },
    {
      id: 'collectors', title: 'Collectors', icon: 'i-lucide-download',
      level: c ? (c.failing_count ? 'error' : 'ok') : null,
      value: c ? (c.failing_count ? `${c.failing_count} failing` : 'All healthy') : '',
      sub: c ? `${c.counts?.ok ?? 0} running OK · ${c.registry_module_count} modules` : '',
      loading: !c && !coll.error.value, failed: !!coll.error.value && !c,
    },
    {
      id: 'scheduler', title: 'Scheduler', icon: 'i-lucide-alarm-clock',
      level: s?.status ?? null,
      value: s ? (s.status === 'ok' ? 'Firing' : [s.error_count && `${s.error_count} failing`, s.warn_count && plural(s.warn_count, 'warning')].filter(Boolean).join(', ')) : '',
      sub: s ? `${s.findings.length} checks on jobs and services` : '',
      loading: !s && !sched.error.value, failed: !!sched.error.value && !s,
    },
    {
      id: 'issues', title: 'Open issues', icon: 'i-lucide-circle-dot',
      // Warning at most: the routine unmatched stocks log fallback errors every night.
      level: i ? (fbErr || i.identity_issues.open_count ? 'warn' : 'ok') : null,
      value: i ? (i.identity_issues.open_count ? `${i.identity_issues.open_count} open` : 'None open') : '',
      sub: i ? `${fbErr} fallback error${fbErr === 1 ? '' : 's'} in ${i.fallback_events.window_hours} h` : '',
      loading: !i && !issues.error.value, failed: !!issues.error.value && !i,
    },
    {
      id: 'tables', title: 'Tables', icon: 'i-lucide-table-2',
      level: v ? (v.not_ok_count ? 'warn' : 'ok') : null,
      value: v ? (v.not_ok_count ? `${v.not_ok_count} with problems` : 'All OK') : '',
      sub: v ? `${v.tables.length} tables tracked nightly` : '',
      loading: !v && !cov.error.value, failed: !!cov.error.value && !v,
    },
  ]
})

const overall = computed(() => {
  const t = tiles.value
  const loaded = t.filter(x => x.level)
  const bad = t.filter(x => x.level === 'error')
  const warn = t.filter(x => x.level === 'warn')
  const failed = t.filter(x => x.failed)
  const loading = t.filter(x => x.loading)
  if (bad.length) return { level: 'error', title: `${plural(bad.length, 'area')} failing`, text: `Look at ${[...bad, ...warn].map(x => x.title.toLowerCase()).join(', ')} below.` }
  if (warn.length) return { level: 'warn', title: `${plural(warn.length, 'area')} need${warn.length === 1 ? 's' : ''} a look`, text: `Warnings in ${warn.map(x => x.title.toLowerCase()).join(', ')}. Nothing is failing.` }
  if (failed.length && !loaded.length) return { level: 'error', title: 'Cannot reach the data API', text: 'None of the checks could be loaded. Is the stockey API on port 8000 running?' }
  if (loading.length) return { level: null, title: 'Running checks…', text: `${t.length - loading.length} of ${t.length} areas loaded${loaded.length ? ', all OK so far' : ''}. The data checks take about 20 seconds when nothing is cached; open issues can take a minute or two.` }
  return { level: 'ok', title: 'All systems healthy', text: failed.length ? `Everything that loaded is OK; ${failed.map(x => x.title.toLowerCase()).join(', ')} could not be loaded.` : 'Every check, collector and scheduled job is OK.' }
})
const bannerTone = computed(() => ({
  error: 'border-error/40 bg-error/5', warn: 'border-warning/40 bg-warning/5', ok: 'border-success/40 bg-success/5',
} as Record<string, string>)[overall.value.level ?? ''] ?? 'border-default bg-elevated/40')
const bannerIcon = computed(() => (overall.value.level ? opsLevel(overall.value.level) : { icon: 'i-lucide-loader-circle' }))

const windowItems = [{ label: 'Last 7 days', value: 7 }, { label: 'Last 30 days', value: 30 }, { label: 'Last 60 days', value: 60 }]
const checkedMeta = computed(() => {
  const d = dh.data.value
  if (!d) return ''
  return `Checked ${opsDateTime(d.checked_at)}${d.cached ? ` · from cache, ${Math.round((d.age_seconds ?? 0) / 60)} min old` : ''}`
})
</script>

<template>
  <UDashboardPanel id="data-health">
    <template #header>
      <PageNavbar title="Data health" icon="i-lucide-activity" />
    </template>

    <template #body>
      <div class="mx-auto w-full max-w-7xl space-y-6">
        <!-- overall banner -->
        <div class="rounded-xl border p-4 sm:p-5" :class="bannerTone">
          <div class="flex flex-wrap items-start justify-between gap-4">
            <div class="flex min-w-0 items-start gap-3">
              <div class="flex size-10 shrink-0 items-center justify-center rounded-full bg-default ring ring-default">
                <UIcon :name="bannerIcon.icon" class="size-6" :class="overall.level ? opsText(overall.level) : 'animate-spin text-muted'" />
              </div>
              <div class="min-w-0">
                <h2 class="text-lg font-semibold text-highlighted">{{ overall.title }}</h2>
                <p class="text-sm text-muted">{{ overall.text }}</p>
                <p v-if="checkedMeta" class="mt-1 text-xs text-dimmed">{{ checkedMeta }}</p>
              </div>
            </div>
            <div class="flex w-full items-center gap-2 sm:w-auto">
              <USelect v-model="windowDays" :items="windowItems" size="sm" class="w-36" aria-label="Window for the data checks" />
              <UButton :loading="rechecking" icon="i-lucide-refresh-cw" label="Recheck now" color="neutral" variant="outline" size="sm" @click="recheck" />
            </div>
          </div>
          <p v-if="recheckError" class="mt-3 text-xs text-error">Recheck failed: {{ recheckError }}</p>
        </div>

        <!-- one tile per area -->
        <div class="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
          <a v-for="t in tiles" :key="t.id" :href="`#${t.id}`" class="group last:col-span-2 md:last:col-span-1">
            <UCard class="h-full transition group-hover:ring-primary/40" :ui="{ body: 'p-3 sm:p-4' }">
              <div class="flex items-center justify-between gap-2">
                <span class="flex items-center gap-1.5 text-xs font-medium text-muted"><UIcon :name="t.icon" class="size-3.5" />{{ t.title }}</span>
                <UIcon v-if="t.level" :name="opsLevel(t.level).icon" class="size-4" :class="opsText(t.level)" :aria-label="opsLevel(t.level).label" />
              </div>
              <template v-if="t.loading"><USkeleton class="mt-2 h-6 w-24" /><USkeleton class="mt-1.5 h-3 w-32" /></template>
              <template v-else-if="t.failed">
                <div class="mt-1.5 text-base font-semibold text-muted">Not loaded</div>
                <div class="text-xs text-dimmed">API did not answer</div>
              </template>
              <template v-else>
                <div class="mt-1.5 text-base font-semibold" :class="t.level === 'ok' ? 'text-highlighted' : opsText(t.level)">{{ t.value }}</div>
                <div class="text-xs text-muted">{{ t.sub }}</div>
              </template>
            </UCard>
          </a>
        </div>

        <OpsPanel
          id="checks" title="Data checks" icon="i-lucide-database"
          :level="dh.data.value?.status" :meta="dh.data.value ? `${dh.data.value.window_days}-day window` : ''"
          note="Is the data really there for the whole market, not just recent? Each check compares how many stocks got data against the recent normal, because a feed can look up to date while most stocks quietly stopped. It is the same code as the nightly gate, so this page and the gate cannot disagree."
        >
          <OpsDataChecks v-if="dh.data.value" :findings="dh.data.value.findings" />
          <OpsLoadState v-else :pending="dh.status.value === 'pending'" :error="dh.error.value" what="the data checks" slow="Running checks… about 20 seconds when nothing is cached." height="12rem" />
        </OpsPanel>

        <OpsPanel
          id="collectors" title="Collectors" icon="i-lucide-download"
          :level="coll.data.value ? (coll.data.value.failing_count ? 'error' : 'ok') : null"
          :level-label="coll.data.value ? (coll.data.value.failing_count ? `${coll.data.value.failing_count} failing` : 'All healthy') : undefined"
          :meta="coll.data.value ? `${coll.data.value.registry_module_count} modules in the live registry` : ''"
          note="Last run of every job that downloads or loads data, checked against the list of jobs that still exist. Jobs that were paused on purpose, or deleted and renamed, keep their old status forever; they are folded away instead of counted as failures."
        >
          <OpsCollectors v-if="coll.data.value" :data="coll.data.value" />
          <OpsLoadState v-else :pending="coll.status.value === 'pending'" :error="coll.error.value" what="collector status" />
        </OpsPanel>

        <OpsPanel
          id="scheduler" title="Scheduler and host" icon="i-lucide-alarm-clock"
          :level="sched.data.value?.status"
          :level-label="sched.data.value ? (sched.data.value.status === 'ok' ? 'Healthy' : undefined) : undefined"
          :meta="sched.data.value ? `Checked ${opsDateTime(sched.data.value.checked_at)}` : ''"
        >
          <template #note>
            Whether the job scheduler (go-crond) is actually <span class="font-medium text-default">starting jobs</span>, not just running:
            twice it stayed alive while starting nothing. Also covers stuck job locks, schedule drift, the backup watchdog and the
            browser used for logins (Chrome CDP).
          </template>
          <OpsScheduler v-if="sched.data.value" :data="sched.data.value" />
          <OpsLoadState v-else :pending="sched.status.value === 'pending'" :error="sched.error.value" what="the scheduler check" />
          <p class="mt-3 flex items-start gap-1.5 text-xs text-muted">
            <UIcon name="i-lucide-info" class="mt-0.5 size-3.5 shrink-0" />
            This page is served by a process the scheduler itself manages, so it cannot fully report the scheduler's own death.
            The OS-level watchdog stays the backstop.
          </p>
        </OpsPanel>

        <OpsPanel
          id="issues" title="Open issues and fallbacks" icon="i-lucide-circle-dot"
          :level="tiles[3]!.level" :level-label="tiles[3]!.level ? tiles[3]!.value : undefined"
          note="Nothing on the platform is allowed to fail silently: stocks that cannot be matched to a feed, and jobs that fell back to a backup path, are recorded here. Read-only: loading this page never rechecks or closes anything."
        >
          <OpsPlatformIssues v-if="issues.data.value" :data="issues.data.value" />
          <OpsLoadState v-else :pending="issues.status.value === 'pending'" :error="issues.error.value" what="open issues" slow="Loading open issues… this one can take a minute." />
        </OpsPanel>

        <OpsPanel
          id="tables" title="Table coverage" icon="i-lucide-table-2"
          :level="cov.data.value ? (cov.data.value.not_ok_count ? 'warn' : 'ok') : null"
          :level-label="cov.data.value ? (cov.data.value.not_ok_count ? `${cov.data.value.not_ok_count} not OK` : 'All OK') : undefined"
          :meta="cov.data.value?.report_date ? `Nightly report of ${opsDate(cov.data.value.report_date)}` : ''"
          note="Size and freshness of every data table, from the nightly coverage job. The trend line is days behind over recent nights: a line creeping up is a feed degrading before it fails outright."
        >
          <OpsCoverage v-if="cov.data.value" :data="cov.data.value" />
          <OpsLoadState v-else :pending="cov.status.value === 'pending'" :error="cov.error.value" what="the coverage report" />
        </OpsPanel>

        <p class="pb-2 text-xs text-dimmed">
          Read-only. This page never repairs anything; repairs live in <code>data_readiness --fix</code> and in the collectors themselves.
        </p>
      </div>
    </template>
  </UDashboardPanel>
</template>
