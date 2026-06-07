import type { Config } from 'tailwindcss'

export default {
  content: [
    './app.vue',
    './components/**/*.{vue,ts}',
    './composables/**/*.ts',
    './pages/**/*.vue'
  ],
  theme: {
    extend: {
      colors: {
        ink: '#17211d',
        paper: '#f6f1e7',
        moss: '#0f4c5c',
        ember: '#b45309',
        rust: '#9a3412',
        sun: '#d6a84f',
        sky: '#316f92',
        danger: '#a12a2a'
      },
      boxShadow: {
        soft: '0 22px 70px rgba(31, 32, 24, 0.10)'
      }
    }
  }
} satisfies Config
