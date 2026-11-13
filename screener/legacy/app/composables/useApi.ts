// Thin fetch wrapper over the stockey fundamentals API (fundamentals/api/app.py).
// Base URL is a public runtime config value so it can be pointed at a different host
// without a rebuild (see nuxt.config.ts's NUXT_PUBLIC_API_BASE).
export function useApi() {
  const config = useRuntimeConfig()
  const base = config.public.apiBase as string

  async function get<T>(path: string): Promise<T> {
    return await $fetch<T>(`${base}${path}`)
  }

  async function post<T>(path: string, body: object): Promise<T> {
    return await $fetch<T>(`${base}${path}`, { method: 'POST', body })
  }

  return { get, post }
}
