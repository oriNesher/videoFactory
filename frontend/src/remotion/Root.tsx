/**
 * Remotion's entry for server-side rendering. The bundle built from
 * `index.ts` holds exactly one composition, the one the editor previews.
 *
 * Dimensions, frame rate and length are not fixed here: they come from the
 * export snapshot's `video` block through `calculateMetadata`, so every
 * render is explicit about all three and matches the source video.
 */

import { Composition } from 'remotion'
import { CaptionedVideo } from '../captions/CaptionedVideo'
import { COMPOSITION_ID, type CaptionedVideoProps } from '../captions/types'

const placeholder: CaptionedVideoProps = {
  video: { src: '', width: 1080, height: 1920, fps: 30, duration_in_frames: 30 },
  captions: [],
  style: {
    font_family: 'heebo',
    font_weight: 700,
    font_size_percent: 6.5,
    text_color: '#ffffff',
    background: 'outline',
    outline_color: '#000000',
    outline_width_percent: 8,
    box_color: '#000000',
    box_opacity: 0.6,
    vertical_position_percent: 60,
    safe_margin_percent: 6,
    max_width_percent: 90,
    highlight_active_word: false,
    highlight_color: '#ffd400',
  },
  direction: 'rtl',
}

export function RemotionRoot() {
  return (
    <Composition
      id={COMPOSITION_ID}
      component={CaptionedVideo}
      defaultProps={placeholder}
      width={placeholder.video.width}
      height={placeholder.video.height}
      fps={placeholder.video.fps}
      durationInFrames={placeholder.video.duration_in_frames}
      calculateMetadata={({ props }) => ({
        width: props.video.width,
        height: props.video.height,
        fps: props.video.fps,
        durationInFrames: props.video.duration_in_frames,
      })}
    />
  )
}
