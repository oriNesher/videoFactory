import { Player, type PlayerRef } from '@remotion/player'
import { useEffect, useMemo, useRef, useState } from 'react'
import { brollAssetMediaUrl, captionInputStreamUrl, musicTrackMediaUrl, openInputInVlc } from './api'
import { CaptionedVideo } from './captions/CaptionedVideo'
import type { CaptionedVideoProps } from './captions/types'
import type { PreviewBridgeHost, PreviewOverride } from './previewBridge'
import type { PipelinePreview, PipelineStage } from './types'

/**
 * The video being made, beside every stage.
 *
 * Always the furthest result reached (first clip → merged cut → zoomed video)
 * with the current captions and B-roll drawn live, and from the music stage
 * on the background music mixed in, by the very composition the export
 * renders. Nothing is rendered to a file for it. Saved edits appear on
 * the next refresh of the stage pipeline.
 */
type Props = {
  projectId: string
  preview: PipelinePreview | null
  stages: PipelineStage[]
  /** A slide's unsaved captions and style, drawn instead of the saved ones. */
  override: PreviewOverride | null
  bridge: PreviewBridgeHost
}

export default function VideoPreview({ projectId, preview, stages, override, bridge }: Props) {
  const playerRef = useRef<PlayerRef>(null)
  const applied = override && preview?.props && override.key === preview.key ? override : null
  const props = useMemo(
    () =>
      preview?.props && applied
        ? { ...preview.props, captions: applied.captions, style: applied.style, direction: applied.direction }
        : (preview?.props ?? null),
    [preview, applied],
  )
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
      music:
        props.music && preview.music
          ? { ...props.music, asset: { ...props.music.asset, src: musicTrackMediaUrl(props.music.asset.id, preview.music.digest) } }
          : null,
    } as CaptionedVideoProps
  }, [preview, props, projectId])

  // Let slides move the player and follow its position.
  const fps = props?.video.fps ?? 30
  const playerKey = preview?.key
  useEffect(() => {
    const player = playerRef.current
    bridge.setSeeker((seconds) => {
      if (Number.isFinite(seconds)) playerRef.current?.seekTo(Math.round(seconds * fps))
    })
    if (!player) return () => bridge.setSeeker(null)
    const listener = (event: { detail: { frame: number } }) => bridge.emitFrame(event.detail.frame, fps)
    player.addEventListener('frameupdate', listener)
    player.addEventListener('seeked', listener)
    return () => {
      player.removeEventListener('frameupdate', listener)
      player.removeEventListener('seeked', listener)
      bridge.setSeeker(null)
    }
  }, [bridge, fps, playerKey, inputProps])

  const [vlcError, setVlcError] = useState('')

  async function openInVlc(inputKey: string) {
    setVlcError('')
    try {
      await openInputInVlc(projectId, inputKey)
    } catch (caught) {
      setVlcError(caught instanceof Error ? caught.message : 'Opening VLC failed.')
    }
  }

  const stageLabel = stages.find((stage) => stage.id === preview?.stage)?.label
  const captionCount = props?.captions.length ?? 0
  const brollCount = props?.broll?.placements.length ?? 0

  return (
    <aside className="video-preview" aria-label="Video preview">
      <div className="video-preview-header">
        <h3>Preview</h3>
        {stageLabel && <span className="badge">{stageLabel}</span>}
        {preview && !preview.error && (
          <button
            type="button"
            className="vlc"
            title="Open this video in VLC (the file itself: no captions or B-roll drawn on it)"
            onClick={() => void openInVlc(preview.key)}
          >
            VLC
          </button>
        )}
      </div>
      {vlcError && <p className="message error">{vlcError}</p>}

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
              ref={playerRef}
              key={preview!.key}
              component={CaptionedVideo}
              inputProps={inputProps}
              durationInFrames={props.video.duration_in_frames}
              compositionWidth={props.video.width}
              compositionHeight={props.video.height}
              fps={props.video.fps}
              controls
              clickToPlay
              spaceKeyToPlayOrPause
              acknowledgeRemotionLicense
              style={{ width: '100%' }}
            />
          </div>
          <p className="hint small">
            <bdi>{preview!.label}</bdi>
            {' · '}
            {captionCount > 0 ? `${captionCount} captions` : 'no captions yet'}
            {applied && ' · showing unsaved edits'}
            {brollCount > 0 && ` · ${brollCount} B-roll`}
            {props.music && ' · music'}
          </p>
        </>
      )}
    </aside>
  )
}
