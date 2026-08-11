<template>
  <div class="min-h-screen bg-slate-50 text-slate-900">
    <header class="border-b border-slate-200 bg-white">
      <nav class="mx-auto flex max-w-6xl items-center gap-6 px-6 py-4">
        <NuxtLink to="/" class="text-lg font-semibold tracking-tight">
          Screener
        </NuxtLink>
        <div class="flex gap-4 text-sm font-medium text-slate-600">
          <NuxtLink to="/" class="hover:text-slate-900" active-class="text-slate-900">Universe</NuxtLink>
          <NuxtLink to="/watchlist" class="hover:text-slate-900" active-class="text-slate-900">Watchlist</NuxtLink>
          <NuxtLink to="/sectors" class="hover:text-slate-900" active-class="text-slate-900">Sectors</NuxtLink>
          <NuxtLink to="/portfolio" class="hover:text-slate-900" active-class="text-slate-900">Portfolio</NuxtLink>
        </div>
      </nav>
    </header>
    <main class="mx-auto max-w-6xl px-6 py-8">
      <NuxtRouteAnnouncer />
      <NuxtPage />
    </main>
  </div>
</template>
