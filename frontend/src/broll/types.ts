/**
 * The B-roll part of the composition's props (stage 3A).
 *
 * Written by the backend (`backend/broll.py` → `compose`) from the saved plan,
 * the library and the caption style, with every template setting resolved —
 * `auto` already turned into a corner — so the in-app <Player> and the export
 * render receive identical values. Only `asset.src` differs: the browser
 * points it at the backend, the render script at its own local server.
 */

import type { Entrance } from './entrance.ts'

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

/**
 * Both templates' entrance. The backend leaves both keys out when there is
 * none (a plain cut), and older plans never have them.
 */
export type EntranceSettings = {
  entrance?: Entrance
  entrance_seconds?: number
}

export type FullframeSettings = EntranceSettings & {
  fit: 'contain' | 'cover'
  caption_scrim: boolean
  muted: boolean
}

export type PipCorner = 'top_left' | 'top_right' | 'bottom_left' | 'bottom_right'

/**
 * A corner; `above_captions` (centred, just above the caption line, or just
 * below it when captions sit at the top); or `custom`: the box's centre at
 * x_percent / y_percent of the frame.
 */
export type PipPosition = PipCorner | 'above_captions' | 'custom'

export type PipSettings = EntranceSettings & {
  position: PipPosition
  size_percent: number
  margin_percent: number
  /** Plans saved before these settings existed do not have them. */
  x_percent?: number
  y_percent?: number
  rotation_degrees?: number
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
  /** The caption style's placement, to put a box just above the captions. Absent in older props. */
  caption_layout?: CaptionLayout | null
}

export type CaptionLayout = {
  position_percent: number
  safe_margin_percent: number
  font_size_percent: number
}
