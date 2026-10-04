/**
 * Caption seconds → composition frames. Pure functions, shared by the in-app
 * preview and the final render (both run the same composition).
 *
 * The rule: frame `f` covers the instant `f / fps` (its start time). A caption
 * `[start, end)` is visible on frame `f` exactly when `start <= f / fps < end`.
 * So its frames are `[ceil(start·fps), ceil(end·fps))`.
 *
 * Every conversion starts again from the caption's absolute seconds — nothing
 * is added up frame by frame — so there is no accumulated drift however long
 * the video is. A tiny epsilon absorbs floating-point noise such as
 * 0.1 × 30 = 3.0000000000000004, so a caption that starts exactly on a frame
 * boundary starts on that frame.
 *
 * Back-to-back captions (`a.end === b.start`) meet on one frame boundary:
 * no frame shows both and no frame shows neither. A caption shorter than one
 * frame interval that would get no frame at all is given one frame, so
 * nothing the user kept silently disappears.
 */

export const FRAME_EPSILON = 1e-6

/** The first frame whose start time is at or after `seconds`. */
export function secondsToFrame(seconds: number, fps: number): number {
  return Math.max(0, Math.ceil(seconds * fps - FRAME_EPSILON))
}

export function frameToSeconds(frame: number, fps: number): number {
  return frame / fps
}

/** Half-open frame range `[from, to)` in which a caption is on screen. */
export function captionFrameRange(
  start: number,
  end: number,
  fps: number,
): { from: number; to: number } {
  const from = secondsToFrame(start, fps)
  const to = Math.max(from + 1, secondsToFrame(end, fps))
  return { from, to }
}

/**
 * Whole frames for a video of `durationSeconds`, rounded to the nearest frame
 * (never zero). The rendered length therefore differs from the source by at
 * most half a frame, before the encoder's own audio padding.
 */
export function durationInFrames(durationSeconds: number, fps: number): number {
  return Math.max(1, Math.round(durationSeconds * fps))
}

export type FrameRange = { from: number; to: number }

/**
 * Frame ranges for a sorted, non-overlapping caption list.
 *
 * The one-frame minimum above can make two captions that both fall inside a
 * single frame interval claim the same frame. Ranges are therefore laid out
 * in order, each starting no earlier than the previous one ends, so the
 * result is always sorted and disjoint — which `activeIndex` relies on.
 */
export function captionFrameRanges(
  captions: { start: number; end: number }[],
  fps: number,
): FrameRange[] {
  const ranges: FrameRange[] = []
  let previousEnd = 0
  for (const caption of captions) {
    const natural = captionFrameRange(caption.start, caption.end, fps)
    const from = Math.max(natural.from, previousEnd)
    const to = Math.max(natural.to, from + 1)
    ranges.push({ from, to })
    previousEnd = to
  }
  return ranges
}

/**
 * The index of the caption on screen at `frame`, or -1. Ranges must be sorted
 * and non-overlapping (the backend refuses overlapping captions), which makes
 * a binary search exact.
 */
export function activeIndex(ranges: FrameRange[], frame: number): number {
  let low = 0
  let high = ranges.length - 1
  while (low <= high) {
    const middle = (low + high) >> 1
    const range = ranges[middle]
    if (frame < range.from) high = middle - 1
    else if (frame >= range.to) low = middle + 1
    else return middle
  }
  return -1
}
