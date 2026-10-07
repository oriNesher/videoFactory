/**
 * The B-roll part of the composition's props (stage 3A).
 *
 * Written by the backend (`backend/broll.py` → `compose`) from the saved plan,
 * the library and the caption style, with every template setting resolved —
 * `auto` already turned into a corner — so the in-app <Player> and the export
 * render receive identical values. Only `asset.src` differs: the browser
 * points it at the backend, the render script at its own local server.
 */

export type BrollAsset = {
  id: string
  media_type: 'image' | 'video'
  /** Measured display size, used to keep the aspect ratio. */
  width: number
  height: number
  has_audio: boolean
  /** Filled in by whoever renders: never stored. */
  src?: string
}

export type FullframeSettings = {
  fit: 'contain' | 'cover'
  caption_scrim: boolean
  muted: boolean
}

export type PipCorner = 'top_left' | 'top_right' | 'bottom_left' | 'bottom_right'

export type PipSettings = {
  position: PipCorner
  size_percent: number
  margin_percent: number
  muted: boolean
}

export type BrollPlacement =
  | BrollPlacementBase<'fullframe', FullframeSettings>
  | BrollPlacementBase<'pip', PipSettings>

type BrollPlacementBase<T extends string, S> = {
  id: string
  template_id: T
  /** Seconds on the base video's timeline, half-open [start, end). */
  start: number
  end: number
  /** Video assets: the in-point in the asset, in seconds. Images: null. */
  source_start: number | null
  settings: S
  asset: BrollAsset
}

export type BrollProps = {
  /** Sorted, non-overlapping: the backend refuses overlaps. */
  placements: BrollPlacement[]
  /** Where captions sit, for the full-frame scrim; null without captions. */
  caption_band: 'top' | 'bottom' | null
}
