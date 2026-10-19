/**
 * Background music under the speaker (stage 4): one more audio layer in the
 * captioned-video composition. It draws nothing.
 *
 * The speaker's own track is untouched and still plays exactly once; the
 * music is mixed under it at `volume`, faded in and out (./volume.ts).
 */

import { useCallback } from 'react'
import { useVideoConfig } from 'remotion'
import { Audio } from '@remotion/media'
import type { MusicProps } from './types'
import { musicEndFrame, musicStartFrame, musicVolumeAt } from './volume'

export function MusicLayer({ music }: { music: MusicProps }) {
  const { fps, durationInFrames } = useVideoConfig()
  // With `loopVolumeCurveBehavior="extend"` the frame keeps counting across
  // loops, so it is the composition frame and the fade-out lands on the end.
  const volume = useCallback(
    (frame: number) => musicVolumeAt(music, frame, fps, durationInFrames),
    [music, fps, durationInFrames],
  )
  if (!music.asset.src || musicEndFrame(music, fps, durationInFrames) <= 0) return null
  return (
    <Audio
      src={music.asset.src}
      trimBefore={musicStartFrame(music, fps)}
      loop={music.loop}
      loopVolumeCurveBehavior="extend"
      volume={volume}
    />
  )
}
