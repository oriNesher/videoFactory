/** Seconds with two decimals: a boundary is judged in hundredths. */
export function formatSeconds(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `${value.toFixed(2)} s`
}

/** m:ss.s, for the length of a whole clip. */
export function formatLength(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  const minutes = Math.floor(value / 60)
  const seconds = value - minutes * 60
  return `${minutes}:${seconds.toFixed(1).padStart(4, '0')}`
}
