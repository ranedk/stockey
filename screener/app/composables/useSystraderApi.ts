// Thin fetch wrapper over systrader's own API (systrade/cmd/api) -- a separate
// backend from stockey's fundamentals API (see useApi.ts). Base URL is a public
// runtime config value (nuxt.config.ts's NUXT_PUBLIC_SYSTRADER_API_BASE).
export function useSystraderApi() {
  const config = useRuntimeConfig()
  const base = config.public.systraderApiBase as string

  async function get<T>(path: string): Promise<T> {
    return await $fetch<T>(`${base}${path}`)
  }

  return { get }
}
