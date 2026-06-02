<template>
  <div class="min-h-screen">
    <header class="mx-auto flex max-w-7xl items-center justify-between px-6 py-6">
      <NuxtLink to="/" class="text-xl font-black tracking-tight text-ink">Stockey Operator</NuxtLink>
      <nav class="flex gap-2 text-sm font-semibold">
        <NuxtLink class="rounded-full px-4 py-2 text-ink/65 hover:bg-white/70" to="/">Overview</NuxtLink>
        <NuxtLink class="rounded-full px-4 py-2 text-ink/65 hover:bg-white/70" to="/events">Event Inbox</NuxtLink>
        <NuxtLink class="rounded-full px-4 py-2 text-ink/65 hover:bg-white/70" to="/manual-review">Manual Review</NuxtLink>
        <NuxtLink class="rounded-full px-4 py-2 text-ink/65 hover:bg-white/70" to="/decision-trace">Decision Trace</NuxtLink>
        <NuxtLink class="rounded-full px-4 py-2 text-ink/65 hover:bg-white/70" to="/hypotheses">Playbooks</NuxtLink>
        <NuxtLink class="rounded-full px-4 py-2 text-ink/65 hover:bg-white/70" to="/health">Data Health</NuxtLink>
        <NuxtLink class="rounded-full px-4 py-2 text-ink/65 hover:bg-white/70" to="/operations">Operations</NuxtLink>
      </nav>
    </header>
    <main class="mx-auto max-w-7xl px-6 pb-16">
      <slot />
    </main>
  </div>
</template>
