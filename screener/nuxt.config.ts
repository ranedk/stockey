// The research platform's web app (PLATFORM_PRD.md). Replaces the old screener pages, which
// live in legacy/ until each one is rebuilt here.
export default defineNuxtConfig({
  compatibilityDate: '2025-07-15',
  modules: ['@nuxt/ui'],
  css: ['~/assets/css/main.css'],
  devtools: { enabled: false },
  ignore: ['legacy/**'],
  // Not .output: the live service runs the OLD app from .output until the switch-over
  // (deploy/deploy.sh). Building this app there replaced the live site once (2026-11-13).
  nitro: { output: { dir: '.output-next' } },
  components: [{ path: '~/components', pathPrefix: false }],

  app: {
    head: {
      titleTemplate: '%s · Stockey',
      htmlAttrs: { lang: 'en' },
      meta: [{ name: 'viewport', content: 'width=device-width, initial-scale=1' }],
    },
  },

  // Same-origin proxy: the browser and SSR both call /_api/..., so the API is never exposed
  // directly and CORS never matters (same reasoning as legacy/nuxt.config.ts).
  routeRules: {
    '/_api/**': { proxy: `${process.env.PLATFORM_API_ORIGIN || 'http://127.0.0.1:8110'}/api/v1/**` },
  },

  vite: {
    optimizeDeps: { include: ['echarts/core', 'echarts/charts', 'echarts/components', 'echarts/renderers', 'vue-echarts'] },
  },

  typescript: { tsConfig: { exclude: ['../legacy/**'] } },
})
