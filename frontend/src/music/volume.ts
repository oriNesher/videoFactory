/**
 * Music timing and level (stage 4): pure functions of the props, shared by
 * the layer and its tests, so the preview and the export fade identically.
 */

import type { MusicProps } from './types'

/** The composition frame where the music stops: the video's end, or the track's. */
export function musicEndFrame(music: MusicProps, fps: number, durationInFrames: number): number {
  if (music.loop) return durationInFrames
  const remaining = Math.max(0, music.asset.duration_seconds - music.start_seconds)
  return Math.min(durationInFrames, Math.floor(remaining * fps))
}

/** The in-point in the track, in composition frames. */
export function musicStartFrame(music: MusicProps, fps: number): number {
  return Math.round(music.start_seconds * fps)
}

/**
 * The music's gain at composition frame `frame`: `volume`, faded in from the
 * first frame and out towards the end frame. Never above `volume`, never
 * below 0.
 */
export function musicVolumeAt(music: MusicProps, frame: number, fps: number, durationInFrames: number): number {
  const end = musicEndFrame(music, fps, durationInFrames)
  if (frame < 0 || frame >= end) return 0
  const fadeIn = music.fade_in_seconds * fps
  const fadeOut = music.fade_out_seconds * fps
  const rising = fadeIn > 0 ? Math.min(1, frame / fadeIn) : 1
  const falling = fadeOut > 0 ? Math.min(1, (end - frame) / fadeOut) : 1
  return Math.max(0, Math.min(1, music.volume * Math.min(rising, falling)))
}
