/**
 * Described moments of a video asset (stage 3B), as typed in the library:
 * one per line, `0:05-0:12 what it shows`. Pure functions; the backend
 * checks each range against the video's measured length.
 */

export type Moment = { start: number; end: number; description: string }

/** `75.5` → `1:15.5`; whole seconds without a fraction. */
function clock(seconds: number): string {
  const minutes = Math.floor(seconds / 60)
  const rest = Math.round((seconds - minutes * 60) * 10) / 10
  return `${minutes}:${rest < 10 ? '0' : ''}${rest}`
}

export function formatMoment(moment: Moment): string {
  return `${clock(moment.start)}-${clock(moment.end)} ${moment.description}`
}

/** `1:15.5`, `75.5` or `0:05` → seconds; null if it is not a time. */
export function parseClock(text: string): number | null {
  const match = /^(?:(\d+):)?(\d+(?:\.\d+)?)$/.exec(text.trim())
  if (!match) return null
  const minutes = match[1] ? Number(match[1]) : 0
  const seconds = Number(match[2])
  if (match[1] && seconds >= 60) return null
  return minutes * 60 + seconds
}

/** The moments, or a message naming the first line that is wrong. Blank lines are ignored. */
export function parseMoments(text: string): Moment[] | string {
  const moments: Moment[] = []
  const lines = text.split('\n')
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index].trim()
    if (!line) continue
    const match = /^([\d:.]+)\s*[-–]\s*([\d:.]+)\s+(.+)$/.exec(line)
    const start = match ? parseClock(match[1]) : null
    const end = match ? parseClock(match[2]) : null
    if (!match || start === null || end === null) return `Line ${index + 1}: write a moment as "0:05-0:12 what it shows".`
    if (end <= start) return `Line ${index + 1}: the end must come after the start.`
    moments.push({ start, end, description: match[3].trim() })
  }
  return moments
}
