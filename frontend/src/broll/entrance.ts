/**
 * Entrance animations for B-roll placements: how a placement appears during
 * its first `entrance_seconds`. Pure, so the preview and the render (the same
 * composition) agree frame for frame; tested in tests/broll.test.ts.
 *
 * The choices mirror ENTRANCES in backend/broll_templates.py. The style is
 * applied to the placement's own element — the whole frame for a full-frame
 * cutaway, the box for picture-in-picture — and transforms act around its
 * centre. After the entrance the placement is shown as it is without one.
 */

import type { CSSProperties } from 'react'

export type Entrance = 'none' | 'fade' | 'zoom' | 'slide' | 'pop' | 'blur' | 'spin' | 'wipe'

/** The number of frames the entrance takes: at least one, never past the placement. */
export function entranceFrames(seconds: number | undefined, fps: number, placementFrames: number): number {
  const frames = Math.round((seconds ?? 0) * fps)
  return Math.max(1, Math.min(frames, placementFrames))
}

const easeOutCubic = (t: number) => 1 - (1 - t) ** 3
/** Ends at 1 after overshooting it by about 10 %. */
const easeOutBack = (t: number) => {
  const c1 = 1.70158
  return 1 + (c1 + 1) * (t - 1) ** 3 + c1 * (t - 1) ** 2
}

/**
 * The extra style for `frame` (counted from the placement's first frame) of
 * an entrance lasting `durationFrames`. `frameHeight` is how far a slide
 * travels: from just below the frame. Empty once the entrance is over.
 */
export function entranceStyle(
  entrance: Entrance | undefined,
  frame: number,
  durationFrames: number,
  frameHeight: number,
): { opacity?: number; transform?: string; filter?: string; clipPath?: string } {
  if (!entrance || entrance === 'none' || frame >= durationFrames) return {}
  const t = Math.min(1, Math.max(0, frame / durationFrames))
  const eased = easeOutCubic(t)
  // Rounded so float noise never reaches the CSS (e.g. scale(2e-16)).
  const r = (value: number) => Math.round(value * 1e4) / 1e4
  switch (entrance) {
    case 'fade':
      return { opacity: r(eased) }
    case 'zoom':
      return { opacity: r(eased), transform: `scale(${r(0.5 + 0.5 * eased)})` }
    case 'slide':
      return { transform: `translateY(${r((1 - eased) * frameHeight)}px)` }
    case 'pop':
      return { opacity: r(Math.min(1, t * 3)), transform: `scale(${r(easeOutBack(t))})` }
    case 'blur':
      return { opacity: r(eased), filter: `blur(${r((1 - eased) * frameHeight * 0.02)}px)` }
    case 'spin':
      return { opacity: r(eased), transform: `rotate(${r((eased - 1) * 180)}deg) scale(${r(0.3 + 0.7 * eased)})` }
    case 'wipe':
      return { clipPath: `inset(0 ${r((1 - eased) * 100)}% 0 0)` }
  }
}

/** How long every picture-in-picture box takes to fade out at its end. Not a setting. */
export const PIP_EXIT_SECONDS = 0.3

/**
 * The opacity of a picture-in-picture box at `frame` of a placement lasting
 * `placementFrames`: 1 until its last PIP_EXIT_SECONDS (at most a third of the
 * placement), then falling linearly so the last frame is nearly gone and the
 * next one, past the placement, is the speaker alone.
 */
export function pipExitOpacity(frame: number, placementFrames: number, fps: number): number {
  const fadeFrames = Math.min(Math.round(PIP_EXIT_SECONDS * fps), Math.floor(placementFrames / 3))
  const remaining = placementFrames - frame
  if (fadeFrames < 1 || remaining > fadeFrames) return 1
  return Math.round((Math.max(0, remaining) / (fadeFrames + 1)) * 1e4) / 1e4
}

/** `transform` from an entrance followed by the element's own (e.g. its rotation). */
export function withTransform(entrance: ReturnType<typeof entranceStyle>, own: string | undefined): CSSProperties {
  const transform = [entrance.transform, own].filter(Boolean).join(' ')
  return { ...entrance, transform: transform || undefined }
}
