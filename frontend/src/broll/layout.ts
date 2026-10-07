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
import type { PipCorner } from './types.ts'

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
export const PIP_MAX_HEIGHT_FRACTION = 0.45

export type Box = { left: number; top: number; width: number; height: number }

/**
 * The picture-in-picture box: `size_percent` of the frame width, the asset's
 * own aspect ratio (so nothing is cropped or stretched), at most 45 % of the
 * frame height, `margin_percent` of the shorter side from the two edges of
 * its corner.
 */
export function pipBox(
  frameWidth: number,
  frameHeight: number,
  assetWidth: number,
  assetHeight: number,
  settings: { position: PipCorner; size_percent: number; margin_percent: number },
): Box {
  const aspect = assetWidth > 0 && assetHeight > 0 ? assetWidth / assetHeight : 16 / 9
  const margin = (Math.min(frameWidth, frameHeight) * settings.margin_percent) / 100
  let width = (frameWidth * settings.size_percent) / 100
  let height = width / aspect
  const maxHeight = frameHeight * PIP_MAX_HEIGHT_FRACTION
  if (height > maxHeight) {
    height = maxHeight
    width = height * aspect
  }
  width = Math.round(width)
  height = Math.round(height)
  const right = settings.position.endsWith('right')
  const bottom = settings.position.startsWith('bottom')
  return {
    left: Math.round(right ? frameWidth - margin - width : margin),
    top: Math.round(bottom ? frameHeight - margin - height : margin),
    width,
    height,
  }
}
