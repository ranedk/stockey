// Diverging scale: red -> neutral grey at zero -> green. Clamped at +/- `cap`.
const hex = (h: string) => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16))
const mix = (a: number[], b: number[], k: number) => '#' + a.map((v, i) => Math.round(v + (b[i]! - v) * k).toString(16).padStart(2, '0')).join('')

export function diverging(v: number | null | undefined, cap: number, dark: boolean): string {
  const neg = hex(dark ? '#b42c2c' : '#e05a5a')
  const mid = hex(dark ? '#3a3a3d' : '#ecebe8')
  const pos = hex(dark ? '#15803d' : '#2fa866')
  if (v == null || !Number.isFinite(v)) return mix(mid, mid, 0)
  const k = Math.max(-1, Math.min(1, v / cap))
  return k >= 0 ? mix(mid, pos, k) : mix(mid, neg, -k)
}
