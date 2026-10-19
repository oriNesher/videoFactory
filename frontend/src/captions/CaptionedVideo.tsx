/**
 * The captioned-video composition: the selected video with its own audio,
 * and the caption overlay on top.
 *
 * This one component is what the editor's <Player> shows and what the Node
 * render script (frontend/scripts/render-captions.mjs) renders, so preview
 * and export cannot disagree about where, when or how a caption appears.
 *
 * Layers, bottom to top. Later stages add layers here; they do not need a
 * second caption implementation.
 *   1. the video, letterboxed (never cropped or stretched) into the frame —
 *      the clean or already-zoomed talking head (zooms are rendered into that
 *      file by FFmpeg beforehand, so nothing here zooms anything);
 *   2. B-roll (stage 3A), when the props carry any: ../broll/BrollLayer.tsx;
 *   3. the caption layer, always on top.
 * Background music (stage 4) draws nothing: ../music/MusicLayer.tsx.
 *
 * Audio: the video element's own track is the composition's audio, present
 * exactly once; it keeps playing under every B-roll placement. B-roll assets
 * are muted unless a placement turns its asset's sound on. Music, when the
 * props carry it, is mixed under the speaker at its own volume.
 */

import { useEffect, useMemo, useState } from 'react'
import { AbsoluteFill, continueRender, delayRender, useCurrentFrame, useVideoConfig } from 'remotion'
import { Video } from '@remotion/media'
import { BrollLayer } from '../broll/BrollLayer'
import { MusicLayer } from '../music/MusicLayer'
import { fontFamily, loadCaptionFont } from './fonts'
import { activeIndex, captionFrameRanges, frameToSeconds } from './timing'
import type { Caption, CaptionStyle, CaptionedVideoProps } from './types'

export function CaptionedVideo({ video, captions, style, direction, broll, music }: CaptionedVideoProps) {
  return (
    <AbsoluteFill style={{ backgroundColor: 'black' }}>
      <Video src={video.src} objectFit="contain" />
      {music && <MusicLayer music={music} />}
      {broll && broll.placements.length > 0 && <BrollLayer broll={broll} />}
      <CaptionLayer captions={captions} style={style} direction={direction} />
    </AbsoluteFill>
  )
}

function useFontReady(key: string): boolean {
  const [ready, setReady] = useState(false)
  // Blocks the frame from being captured until the font is in: a render must
  // never show the fallback font, and the preview simply waits a moment.
  const [pending] = useState(() => ({ handle: delayRender(`Loading caption font ${key}`), open: true }))

  useEffect(() => {
    let cancelled = false
    loadCaptionFont(key)
      .catch(() => undefined) // a font that fails to load still lets the frame render
      .finally(() => {
        if (!cancelled) setReady(true)
        if (pending.open) {
          pending.open = false
          continueRender(pending.handle)
        }
      })
    return () => {
      cancelled = true
    }
  }, [key, pending])

  return ready
}

function hexToRgba(hex: string, alpha: number): string {
  const match = /^#?([0-9a-f]{6})$/i.exec(hex)
  if (!match) return `rgba(0,0,0,${alpha})`
  const value = parseInt(match[1], 16)
  return `rgba(${(value >> 16) & 255},${(value >> 8) & 255},${value & 255},${alpha})`
}

export function CaptionLayer({
  captions,
  style,
  direction,
}: {
  captions: Caption[]
  style: CaptionStyle
  direction: 'rtl' | 'ltr'
}) {
  const frame = useCurrentFrame()
  const { fps, width, height } = useVideoConfig()
  useFontReady(style.font_family)

  const ranges = useMemo(() => captionFrameRanges(captions, fps), [captions, fps])
  const index = activeIndex(ranges, frame)
  if (index < 0) return null

  const caption = captions[index]
  const shorter = Math.min(width, height)
  const fontSize = (shorter * style.font_size_percent) / 100
  const marginX = (width * style.safe_margin_percent) / 100
  const marginY = (height * style.safe_margin_percent) / 100
  const now = frameToSeconds(frame, fps)

  const textStyle: React.CSSProperties = {
    fontFamily: fontFamily(style.font_family),
    fontWeight: style.font_weight,
    fontSize,
    lineHeight: 1.25,
    color: style.text_color,
    // Long words wrap rather than run off the frame; normal text breaks at
    // spaces as usual.
    overflowWrap: 'break-word',
    whiteSpace: 'pre-wrap',
  }

  if (style.background === 'outline') {
    // The stroke is painted under the fill, so only its outer half shows:
    // twice the requested width gives the requested visible outline.
    const stroke = (fontSize * style.outline_width_percent) / 100
    textStyle.WebkitTextStroke = `${stroke * 2}px ${style.outline_color}`
    textStyle.paintOrder = 'stroke fill'
  } else if (style.background === 'box') {
    textStyle.backgroundColor = hexToRgba(style.box_color, style.box_opacity)
    textStyle.padding = `${fontSize * 0.08}px ${fontSize * 0.3}px`
    textStyle.borderRadius = fontSize * 0.18
    textStyle.boxDecorationBreak = 'clone'
    textStyle.WebkitBoxDecorationBreak = 'clone'
  }

  const position = Math.min(100, Math.max(0, style.vertical_position_percent))

  return (
    <AbsoluteFill
      style={{ top: marginY, bottom: marginY, left: marginX, right: marginX, width: 'auto', height: 'auto' }}
    >
      <div
        // Direction comes from the transcript's language, not from the first
        // letter: a Hebrew line that starts with an English word is still a
        // right-to-left line. The browser's bidi algorithm then orders the
        // mixed runs; the text itself is never reversed.
        dir={direction}
        lang={direction === 'rtl' ? 'he' : undefined}
        style={{
          position: 'absolute',
          top: `${position}%`,
          transform: `translateY(-${position}%)`,
          left: 0,
          right: 0,
          display: 'flex',
          justifyContent: 'center',
        }}
      >
        <div style={{ maxWidth: `${style.max_width_percent}%`, textAlign: 'center' }}>
          <span style={textStyle}>
            <CaptionText caption={caption} now={now} style={style} />
          </span>
        </div>
      </div>
    </AbsoluteFill>
  )
}

function CaptionText({ caption, now, style }: { caption: Caption; now: number; style: CaptionStyle }) {
  const words = caption.words
  const tokens = caption.text.split(/\s+/).filter(Boolean)

  // Highlight only with trustworthy word timing: the same number of timed
  // words as words on screen. Otherwise the caption is shown whole.
  if (!style.highlight_active_word || !words || words.length !== tokens.length) {
    return <>{caption.text}</>
  }

  return (
    <>
      {tokens.map((token, index) => {
        const word = words[index]
        const active = now >= word.start && now < word.end
        return (
          <span key={index}>
            {index > 0 ? ' ' : ''}
            <span style={active ? { color: style.highlight_color } : undefined}>{token}</span>
          </span>
        )
      })}
    </>
  )
}
