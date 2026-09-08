// https://nuxt.com/docs/api/configuration/nuxt-config
export default defineNuxtConfig({
  compatibilityDate: '2025-07-15',
  devtools: { enabled: true },
  modules: ['@nuxtjs/tailwindcss'],

  // Both backends are proxied THROUGH this app rather than called directly, so every
  // request the browser makes is same-origin. Added 2026-09-02 when putting the site
  // behind nginx on a public IP, and it is the only arrangement that works everywhere:
  //
  //   * the previous defaults were absolute http://localhost:8000 / :8090, which are
  //     baked into the CLIENT bundle -- a remote browser would resolve them against its
  //     OWN machine and every call would fail;
  //   * pointing them at the public IP instead would trip CORS (app.py allows only
  //     http://localhost:3000) and would expose both APIs directly, outside whatever
  //     auth the reverse proxy applies;
  //   * a relative path alone breaks SERVER-side rendering, where $fetch has no origin
  //     to resolve against -- hence these Nitro route rules, which make the same path
  //     work from the browser and from SSR.
  //
  // Same-origin also means CORS stops mattering at all, in dev and behind a proxy alike.
  routeRules: {
    '/_stockey/**': { proxy: `${process.env.STOCKEY_API_ORIGIN || 'http://127.0.0.1:8000'}/**` },
    '/_systrader/**': { proxy: `${process.env.SYSTRADER_API_ORIGIN || 'http://127.0.0.1:8090'}/**` },
  },

  runtimeConfig: {
    public: {
      // Relative on purpose -- see routeRules above. Override with NUXT_PUBLIC_API_BASE
      // only if you are pointing the browser straight at a remote API host.
      apiBase: process.env.NUXT_PUBLIC_API_BASE || '/_stockey',
      // systrader's API (systrade/cmd/api) -- a SEPARATE backend, not a proxy through
      // stockey's: systrader owns all research/signal outputs per the two repos' own
      // boundary contract. The shared origin here is transport, not a merge of the two.
      systraderApiBase: process.env.NUXT_PUBLIC_SYSTRADER_API_BASE || '/_systrader',
    },
  },
})
