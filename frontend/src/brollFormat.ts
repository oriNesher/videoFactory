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
