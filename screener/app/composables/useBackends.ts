// The two older backends, through the same-origin proxies in nuxt.config.ts.
// Paths are the backends' own, e.g. useStockey('/api/themes'), useSystrader('/api/rotation').
type P = string | (() => string)
const resolve = (p: P) => (typeof p === 'function' ? p() : p)

export function useStockey<T>(path: P, opts: Record<string, any> = {}) {
  return useFetch<T>(() => `/_stockey${resolve(path)}`, { key: typeof path === 'string' ? `stockey:${path}` : undefined, ...opts })
}
export function useSystrader<T>(path: P, opts: Record<string, any> = {}) {
  return useFetch<T>(() => `/_systrader${resolve(path)}`, { key: typeof path === 'string' ? `systrader:${path}` : undefined, ...opts })
}
export const stockeyApi = <T>(path: string, opts: Record<string, any> = {}) => $fetch<T>(`/_stockey${path}`, opts)
export const systraderApi = <T>(path: string, opts: Record<string, any> = {}) => $fetch<T>(`/_systrader${path}`, opts)
