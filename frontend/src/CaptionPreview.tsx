import { Player, type PlayerRef } from '@remotion/player'
import { forwardRef, useEffect, useMemo } from 'react'
import { CaptionedVideo } from './captions/CaptionedVideo'
import type { Caption, CaptionStyle, CaptionedVideoProps } from './captions/types'
import type { CaptionComposition, EditableCaption } from './types'

/**
 * The in-app preview: Remotion's <Player> running the very composition the
 * export renders. Caption and style edits show up immediately — nothing is
 * rendered to a file.
 *
 * The draft may be mid-edit (a number being typed, two captions overlapping
 * for a moment). The preview shows what it can: captions with usable times,
 * in time order. Validation and saving are where the rules are enforced.
 */
type Props = {
  src: string
  composition: CaptionComposition
  captions: EditableCaption[]
  style: CaptionStyle
  direction: 'rtl' | 'ltr'
  onFrame: (frame: number) => void
}

const CaptionPreview = forwardRef<PlayerRef, Props>(function CaptionPreview(
  { src, composition, captions, style, direction, onFrame },
  ref,
) {
  const previewCaptions = useMemo<Caption[]>(
    () =>
      captions
        .filter(
          (caption) =>
            Number.isFinite(caption.start) &&
            Number.isFinite(caption.end) &&
            caption.end > caption.start &&
            caption.text.trim() !== '',
        )
        .map((caption) => ({
          id: caption.id,
          start: caption.start,
          end: caption.end,
          text: caption.text.trim().split(/\s+/).join(' '),
          words: caption.words,
        }))
        .sort((a, b) => a.start - b.start),
    [captions],
  )

  const inputProps = useMemo<CaptionedVideoProps>(
    () => ({
      video: {
        src,
        width: composition.width,
        height: composition.height,
        fps: composition.fps,
        duration_in_frames: composition.duration_in_frames,
      },
      captions: previewCaptions,
      style,
      direction,
    }),
    [src, composition, previewCaptions, style, direction],
  )

  useEffect(() => {
    const player = ref && typeof ref !== 'function' ? ref.current : null
    if (!player) return
    const listener = (event: { detail: { frame: number } }) => onFrame(event.detail.frame)
    player.addEventListener('frameupdate', listener)
    return () => player.removeEventListener('frameupdate', listener)
  }, [ref, onFrame])

  const ratio = composition.width / composition.height

  return (
    <div className="caption-preview" style={{ maxWidth: `calc(65vh * ${ratio})` }}>
      <Player
        ref={ref}
        component={CaptionedVideo}
        inputProps={inputProps}
        durationInFrames={composition.duration_in_frames}
        compositionWidth={composition.width}
        compositionHeight={composition.height}
        fps={composition.fps}
        controls
        clickToPlay
        spaceKeyToPlayOrPause
        acknowledgeRemotionLicense
        style={{ width: '100%' }}
      />
    </div>
  )
})

export default CaptionPreview
