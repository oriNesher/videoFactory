/**
 * The B-roll layer of the composition: above the speaker's video, below the
 * captions. One placement is on screen at a time (the backend refuses
 * overlaps); each lives in its own <Sequence>, so a video asset starts from
 * its in-point when the placement starts, whatever the base video is doing.
 *
 * The speaker's video is never paused, trimmed or restarted by this layer —
 * it keeps playing underneath — so speech continues once, and when a
 * full-frame cutaway ends the speaker's picture (zoomed or not, as the base
 * video has it at that moment) is simply visible again.
 *
 * Asset audio is muted unless the placement's `muted` setting is false.
 */

import { AbsoluteFill, Img, Sequence, useVideoConfig } from 'remotion'
import { Video } from '@remotion/media'
import { pipBox, placementFrameRange, sourceStartFrame } from './layout'
import type { BrollAsset, BrollPlacement, BrollProps } from './types'

export function BrollLayer({ broll }: { broll: BrollProps }) {
  const { fps } = useVideoConfig()
  return (
    <>
      {broll.placements.map((placement) => {
        const { from, to } = placementFrameRange(placement.start, placement.end, fps)
        return (
          <Sequence
            key={placement.id}
            from={from}
            durationInFrames={to - from}
            // Loads the asset a second early in the preview so the cut is clean.
            premountFor={Math.round(fps)}
            name={`B-roll ${placement.template_id}`}
          >
            <Placement placement={placement} captionBand={broll.caption_band} />
          </Sequence>
        )
      })}
    </>
  )
}

function Placement({ placement, captionBand }: { placement: BrollPlacement; captionBand: BrollProps['caption_band'] }) {
  const { width, height, fps } = useVideoConfig()

  if (placement.template_id === 'fullframe') {
    const { fit, caption_scrim, muted } = placement.settings
    return (
      <AbsoluteFill style={{ backgroundColor: 'black' }}>
        <AssetMedia asset={placement.asset} fit={fit} muted={muted} trimBefore={sourceStartFrame(placement.source_start, fps)} />
        {caption_scrim && captionBand && (
          <AbsoluteFill
            style={{
              background: `linear-gradient(to ${captionBand === 'bottom' ? 'top' : 'bottom'}, rgba(0,0,0,0.55) 0%, rgba(0,0,0,0.25) 22%, rgba(0,0,0,0) 40%)`,
            }}
          />
        )}
      </AbsoluteFill>
    )
  }

  const box = pipBox(width, height, placement.asset.width, placement.asset.height, placement.settings)
  const shorter = Math.min(width, height)
  return (
    <AbsoluteFill>
      <div
        style={{
          position: 'absolute',
          ...box,
          overflow: 'hidden',
          borderRadius: shorter * 0.02,
          backgroundColor: 'black',
          boxShadow: `0 0 0 ${Math.max(1, shorter * 0.003)}px rgba(255,255,255,0.85), 0 ${shorter * 0.01}px ${shorter * 0.03}px rgba(0,0,0,0.45)`,
        }}
      >
        <AssetMedia
          asset={placement.asset}
          fit="contain"
          muted={placement.settings.muted}
          trimBefore={sourceStartFrame(placement.source_start, fps)}
        />
      </div>
    </AbsoluteFill>
  )
}

function AssetMedia({
  asset,
  fit,
  muted,
  trimBefore,
}: {
  asset: BrollAsset
  fit: 'contain' | 'cover'
  muted: boolean
  trimBefore: number
}) {
  const fill: React.CSSProperties = { position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: fit }
  if (!asset.src) return null
  if (asset.media_type === 'image') return <Img src={asset.src} style={fill} />
  return (
    <Video
      src={asset.src}
      trimBefore={trimBefore || undefined}
      muted={muted || !asset.has_audio}
      objectFit={fit}
      style={fill}
    />
  )
}
