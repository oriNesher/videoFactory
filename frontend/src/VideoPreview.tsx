import { Player } from '@remotion/player'
import { useMemo } from 'react'
import { brollAssetMediaUrl, captionInputStreamUrl } from './api'
import { CaptionedVideo } from './captions/CaptionedVideo'
import type { CaptionedVideoProps } from './captions/types'
import type { PipelinePreview, PipelineStage } from './types'

/**
 * The video being made, beside every stage.
 *
 * Always the furthest result reached (first clip → merged cut → zoomed video)
 * with the current captions and B-roll drawn live by the very composition the
 * export renders. Nothing is rendered to a file for it. Saved edits appear on
 * the next refresh of the stage pipeline.
 */
type Props = {
  projectId: string
  preview: PipelinePreview | null
  stages: PipelineStage[]
}

export default function VideoPreview({ projectId, preview, stages }: Props) {
  const props = preview?.props ?? null
  const inputProps = useMemo<CaptionedVideoProps | null>(() => {
    if (!preview || !props) return null
    return {
      ...props,
      video: { ...props.video, src: captionInputStreamUrl(projectId, preview.key) },
      broll: props.broll
        ? {
            ...props.broll,
            placements: props.broll.placements.map((placement) => ({
              ...placement,
              asset: {
                ...placement.asset,
                src: brollAssetMediaUrl(placement.asset.id, preview.assets?.[placement.asset.id]?.digest ?? '', projectId),
              },
            })),
          }
        : null,
    } as CaptionedVideoProps
  }, [preview, props, projectId])

  const stageLabel = stages.find((stage) => stage.id === preview?.stage)?.label
  const captionCount = props?.captions.length ?? 0
  const brollCount = props?.broll?.placements.length ?? 0

  return (
    <aside className="video-preview" aria-label="Video preview">
      <div className="video-preview-header">
        <h3>Preview</h3>
        {stageLabel && <span className="badge">{stageLabel}</span>}
      </div>

      {!preview && <p className="hint">The video appears here once it has clips.</p>}
      {preview?.error && <p className="message error">{preview.error}</p>}

      {inputProps && props && (
        <>
          <div
            className="video-preview-player"
            style={{ maxWidth: `calc(70vh * ${props.video.width / props.video.height})` }}
          >
            {/* Remounted per video: the player keeps its own timeline state. */}
            <Player
              key={preview!.key}
              component={CaptionedVideo}
              inputProps={inputProps}
              durationInFrames={props.video.duration_in_frames}
              compositionWidth={props.video.width}
              compositionHeight={props.video.height}
              fps={props.video.fps}
              controls
              clickToPlay
              acknowledgeRemotionLicense
              style={{ width: '100%' }}
            />
          </div>
          <p className="hint small">
            <bdi>{preview!.label}</bdi>
            {' · '}
            {captionCount > 0 ? `${captionCount} captions` : 'no captions yet'}
            {brollCount > 0 && ` · ${brollCount} B-roll`}
          </p>
        </>
      )}
    </aside>
  )
}
