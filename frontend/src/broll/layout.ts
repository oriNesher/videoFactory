/**
 * Pure timing and geometry for B-roll placements, shared by the preview and
 * the render (both run the same composition) and tested in tests/broll.test.ts.
 *
 * Timing uses the captions' rule (../captions/timing.ts): frame `f` shows the
 * instant `f / fps`, so a placement `[start, end)` is on screen on frames
 * `ceil(start·fps)` … `ceil(end·fps) − 1`, computed from absolute seconds.
 * Back-to-back placements meet on one frame boundary.
 */

import { secondsToFrame } from '../captions/timing.ts'
import type { CaptionLayout, PipPosition } from './types.ts'

export function placementFrameRange(start: number, end: number, fps: number): { from: number; to: number } {
  const from = secondsToFrame(start, fps)
  const to = Math.max(from + 1, secondsToFrame(end, fps))
  return { from, to }
}

/**
 * The asset frame shown first: the in-point rounded to the nearest
 * composition frame. Within the placement, composition frame `from + n`
 * shows the asset at `trim / fps + n / fps` seconds.
 */
export function sourceStartFrame(sourceStart: number | null, fps: number): number {
  return sourceStart === null ? 0 : Math.max(0, Math.round(sourceStart * fps))
}

/** Mirrors PIP_MAX_HEIGHT_FRACTION in backend/broll_templates.py. */
export const PIP_MAX_HEIGHT_FRACTION = 0.4
/** Up to this size the height limit is fixed; a larger (manual) size raises it in proportion. */
export const PIP_LIMIT_FIXED_UP_TO_SIZE = 60

/** The tallest the box may be, as a share of the frame height, for a size. */
export function pipMaxHeightFraction(sizePercent: number): number {
  return PIP_MAX_HEIGHT_FRACTION * Math.max(1, sizePercent / PIP_LIMIT_FIXED_UP_TO_SIZE)
}

export type Box = { left: number; top: number; width: number; height: number }

/**
 * Where a one-line caption sits, in pixels, exactly as CaptionLayer places
 * it: its anchor point `position_percent` of the way down the safe area, the
 * block shifted up by the same share of its own height (line height 1.25).
 */
export function captionLine(frameWidth: number, frameHeight: number, layout: CaptionLayout): { top: number; bottom: number } {
  const fontSize = (Math.min(frameWidth, frameHeight) * layout.font_size_percent) / 100
  const marginY = (frameHeight * layout.safe_margin_percent) / 100
  const share = Math.min(100, Math.max(0, layout.position_percent)) / 100
  const lineHeight = fontSize * 1.25
  const top = marginY + (frameHeight - 2 * marginY) * share - lineHeight * share
  return { top, bottom: top + lineHeight }
}

/** The gap between a box placed above or below the captions and the caption line. */
export const ABOVE_CAPTIONS_GAP_FRACTION = 0.015

/**
 * `below_captions` shrinks the box to the room under the caption line; with
 * less room than this share of the frame height it sits above the line instead.
 */
export const BELOW_CAPTIONS_MIN_ROOM_FRACTION = 0.15

/**
 * The picture-in-picture box: `size_percent` of the frame width, the asset's
 * own aspect ratio (so nothing is cropped or stretched), at most 40 % of the
 * frame height (more for a manual size above 60), `margin_percent` of the
 * shorter side from the two edges of its corner — or, with `custom`, centred
 * at `x_percent` / `y_percent` of the frame, kept inside it. Rotation is
 * applied around the box's centre by the renderer, not here.
 */
export function pipBox(
  frameWidth: number,
  frameHeight: number,
  assetWidth: number,
  assetHeight: number,
  settings: { position: PipPosition; size_percent: number; margin_percent: number; x_percent?: number; y_percent?: number },
  /** Where the caption line is; `above_captions` / `below_captions` without one sit above the bottom margin. */
  caption?: { top: number; bottom: number } | null,
): Box {
  const aspect = assetWidth > 0 && assetHeight > 0 ? assetWidth / assetHeight : 16 / 9
  const margin = (Math.min(frameWidth, frameHeight) * settings.margin_percent) / 100
  const gap = frameHeight * ABOVE_CAPTIONS_GAP_FRACTION
  const fit = (maxHeight: number) => {
    let width = (frameWidth * settings.size_percent) / 100
    let height = width / aspect
    if (height > maxHeight) {
      height = maxHeight
      width = height * aspect
    }
    return { width: Math.round(width), height: Math.round(height) }
  }
  const limit = frameHeight * pipMaxHeightFraction(settings.size_percent)
  if (settings.position === 'below_captions' && caption) {
    const top = caption.bottom + gap
    const room = frameHeight - margin - top
    if (room >= frameHeight * BELOW_CAPTIONS_MIN_ROOM_FRACTION) {
      const { width, height } = fit(Math.min(limit, room))
      return { left: Math.round((frameWidth - width) / 2), top: Math.round(top), width, height }
    }
  }
  const { width, height } = fit(limit)
  if (settings.position === 'above_captions' || settings.position === 'below_captions') {
    // below_captions lands here only without room under the line: it goes above.
    const captionsAtTop = caption ? caption.top + caption.bottom < frameHeight : false
    const top = !caption
      ? frameHeight - margin - height
      : captionsAtTop
        ? caption.bottom + gap
        : caption.top - gap - height
    return {
      left: Math.round((frameWidth - width) / 2),
      top: Math.round(Math.min(Math.max(top, 0), Math.max(0, frameHeight - height))),
      width,
      height,
    }
  }
  if (settings.position === 'custom') {
    const centreX = (frameWidth * (settings.x_percent ?? 50)) / 100
    const centreY = (frameHeight * (settings.y_percent ?? 30)) / 100
    const clamp = (value: number, size: number, frame: number) => Math.min(Math.max(value, 0), Math.max(0, frame - size))
    return {
      left: Math.round(clamp(centreX - width / 2, width, frameWidth)),
      top: Math.round(clamp(centreY - height / 2, height, frameHeight)),
      width,
      height,
    }
  }
  const right = settings.position.endsWith('right')
  const bottom = settings.position.startsWith('bottom')
  return {
    left: Math.round(right ? frameWidth - margin - width : margin),
    top: Math.round(bottom ? frameHeight - margin - height : margin),
    width,
    height,
  }
}
