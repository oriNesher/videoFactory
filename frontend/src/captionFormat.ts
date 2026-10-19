import type { Caption } from './captions/types'
import type { EditableCaption } from './types'

/** `m:ss.cc` for caption times; a dash for a value that is not a number. */
export function formatTime(seconds: number): string {
  if (!Number.isFinite(seconds)) return '–'
  const sign = seconds < 0 ? '-' : ''
  const value = Math.abs(seconds)
  const minutes = Math.floor(value / 60)
  return `${sign}${minutes}:${(value - minutes * 60).toFixed(2).padStart(5, '0')}`
}

/**
 * A caption draft as the composition draws it. The draft may be mid-edit, so
 * only captions with usable times and text are kept, in time order.
 * Validation and saving are where the rules are enforced.
 */
export function toPreviewCaptions(captions: EditableCaption[]): Caption[] {
  return captions
    .filter(
      (caption) =>
        Number.isFinite(caption.start) &&
        Number.isFinite(caption.end) &&
        caption.end > caption.start &&
        caption.text.trim() !== '',
    )
    .map((caption) => ({
      id: caption.id,
      start: caption.start,
      end: caption.end,
      text: caption.text.trim().split(/\s+/).join(' '),
      words: caption.words,
    }))
    .sort((a, b) => a.start - b.start)
}
