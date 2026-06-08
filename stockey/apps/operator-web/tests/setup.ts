import {
  computed,
  nextTick,
  onMounted,
  reactive,
  ref,
  watch
} from 'vue'
import { vi } from 'vitest'

Object.assign(globalThis, {
  computed,
  nextTick,
  onMounted,
  reactive,
  ref,
  watch,
  useRoute: () => ({ query: {}, hash: '' }),
  useRouter: () => ({ replace: vi.fn(async () => undefined) })
})
