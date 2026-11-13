// All data comes from the platform API through the same-origin /_api proxy (nuxt.config.ts).
export function useApi<T>(path: string | (() => string), opts: Record<string, any> = {}) {
  return useFetch<T>(() => `/_api/${typeof path === 'function' ? path() : path}`, {
    key: typeof path === 'string' ? path : undefined,
    ...opts,
  })
}

export const api = <T>(path: string, query?: Record<string, any>) => $fetch<T>(`/_api/${path}`, { query })
