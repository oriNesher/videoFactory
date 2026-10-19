import { formatTime } from './captionFormat'
import type { BrollLibraryAsset } from './types'

/** One line of measured facts: type, size, length, audio. */
export function describeAsset(asset: BrollLibraryAsset): string {
  const { measured } = asset
  const parts = [asset.media_type === 'image' ? 'Image' : 'Video', `${measured.width}×${measured.height}`]
  if (measured.duration_seconds !== null) parts.push(formatTime(measured.duration_seconds))
  if (asset.media_type === 'video') parts.push(measured.has_audio ? 'has audio' : 'no audio')
  return parts.join(' · ')
}

/** Mirrors FULLFRAME_ASPECT_TOLERANCE in backend/broll_templates.py. */
export const FULLFRAME_ASPECT_TOLERANCE = 1.25

/** Whether an asset is shaped like the video, so it may be shown full-frame. */
export function fitsFullFrame(asset: BrollLibraryAsset | undefined, frameWidth: number, frameHeight: number): boolean {
  if (!asset || asset.measured.width <= 0 || asset.measured.height <= 0 || frameWidth <= 0 || frameHeight <= 0) return false
  const ratio = asset.measured.width / asset.measured.height / (frameWidth / frameHeight)
  return ratio >= 1 / FULLFRAME_ASPECT_TOLERANCE && ratio <= FULLFRAME_ASPECT_TOLERANCE
}
