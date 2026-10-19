import { createContext, useContext } from 'react'
import type { Caption, CaptionStyle } from './captions/types'

/**
 * How a stage's slide talks to the shared preview beside it.
 *
 * - A slide can show unsaved work on it: `setOverride` replaces the preview's
 *   captions and style while the preview plays that video (`key`).
 * - A slide can move it: `seek(seconds)`.
 * - A slide can follow it: `subscribeFrame` reports the frame on screen.
 *
 * Frames go through a listener rather than React state, so playback does not
 * re-render every slide 30 times a second.
 */
export type PreviewOverride = {
  key: string
  captions: Caption[]
  style: CaptionStyle
  direction: 'rtl' | 'ltr'
}

export type PreviewBridge = {
  setOverride: (override: PreviewOverride | null) => void
  seek: (seconds: number) => void
  subscribeFrame: (listener: (frame: number, fps: number) => void) => () => void
}

export type PreviewBridgeHost = PreviewBridge & {
  /** Set by the preview: how to move its player. */
  setSeeker: (seeker: ((seconds: number) => void) | null) => void
  /** Called by the preview on every frame update. */
  emitFrame: (frame: number, fps: number) => void
}

export function createPreviewBridge(setOverride: (override: PreviewOverride | null) => void): PreviewBridgeHost {
  let seeker: ((seconds: number) => void) | null = null
  const listeners = new Set<(frame: number, fps: number) => void>()
  return {
    setOverride,
    seek: (seconds) => seeker?.(seconds),
    subscribeFrame: (listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    setSeeker: (next) => {
      seeker = next
    },
    emitFrame: (frame, fps) => listeners.forEach((listener) => listener(frame, fps)),
  }
}

const noop: PreviewBridge = {
  setOverride: () => undefined,
  seek: () => undefined,
  subscribeFrame: () => () => undefined,
}

export const PreviewBridgeContext = createContext<PreviewBridge>(noop)

export function usePreviewBridge(): PreviewBridge {
  return useContext(PreviewBridgeContext)
}
