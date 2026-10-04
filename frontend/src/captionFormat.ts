/** `m:ss.cc` for caption times; a dash for a value that is not a number. */
export function formatTime(seconds: number): string {
  if (!Number.isFinite(seconds)) return '–'
  const sign = seconds < 0 ? '-' : ''
  const value = Math.abs(seconds)
  const minutes = Math.floor(value / 60)
  return `${sign}${minutes}:${(value - minutes * 60).toFixed(2).padStart(5, '0')}`
}
