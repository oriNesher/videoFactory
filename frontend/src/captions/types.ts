/**
 * The data the caption composition is rendered from.
 *
 * One shape for both consumers: the in-app <Player> builds it from the draft
 * in the editor, and the backend writes it — validated, from a saved caption
 * revision and the saved style — into every export's snapshot, which the Node
 * render script hands to the same composition. Field names follow the
 * backend's JSON, hence snake_case.
 */

import type { BrollProps } from '../broll/types'
import type { MusicProps } from '../music/types'

export type CaptionWord = {
  text: string
  start: number
  end: number
}

export type Caption = {
  id: string
  /** Seconds, relative to the first frame of the selected video. */
  start: number
  end: number
  text: string
  /**
   * Per-word timing from Whisper, kept only while it still matches the text
   * word for word. Null means group-level timing: the caption is shown whole
   * and no word is highlighted.
   */
  words: CaptionWord[] | null
}

export type CaptionBackground = 'outline' | 'box' | 'none'

export type CaptionStyle = {
  font_family: string
  font_weight: number
  /** Percent of the video's shorter side, so portrait and landscape match. */
  font_size_percent: number
  text_color: string
  background: CaptionBackground
  outline_color: string
  /** Percent of the font size. */
  outline_width_percent: number
  box_color: string
  /** 0–1. */
  box_opacity: number
  /**
   * 0 puts the caption block at the top of the safe area, 100 at the bottom;
   * in between, the block's own matching point sits on that line, so it can
   * never cross the safe margin whatever its height.
   */
  vertical_position_percent: number
  /** Percent of each dimension kept clear at every edge. */
  safe_margin_percent: number
  /** Percent of the safe area's width a line may use before wrapping. */
  max_width_percent: number
  highlight_active_word: boolean
  highlight_color: string
}

export type CompositionVideo = {
  /** A URL the browser (preview) or the headless renderer can fetch. */
  src: string
  width: number
  height: number
  fps: number
  duration_in_frames: number
}

export type CaptionedVideoProps = {
  video: CompositionVideo
  captions: Caption[]
  style: CaptionStyle
  direction: 'rtl' | 'ltr'
  /** Stage 3A: B-roll between the video and the captions. Absent = none. */
  broll?: BrollProps | null
  /** Stage 4: background music under the speaker. Absent = none. */
  music?: MusicProps | null
}

export const COMPOSITION_ID = 'CaptionedVideo'
