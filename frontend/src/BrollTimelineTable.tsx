import { useEffect, useMemo, useRef } from 'react'
import { formatTime } from './captionFormat'
import type { Caption } from './captions/types'
import type { BrollStoredPlacement } from './types'
import { usePreviewHeight } from './usePreviewHeight'

/**
 * The B-roll plan beside the preview, for looking only: one row per caption
 * (its text and time), and three layer columns in which each B-roll is a
 * rectangle. Time runs down the table: a row covers from its caption's start
 * to the next caption's start, and a rectangle's top and bottom sit at the
 * placement's real start and end inside those rows, so its height is how
 * long it is on screen.
 *
 * Placements are laid out greedily: each goes into the first layer that is
 * free at its start. The renderer shows one B-roll at a time today, so a
 * valid plan only fills layer 1; layers 2 and 3 are for overlapping B-roll
 * should that ever exist.
 */
const LAYERS = 3
/** Rows for a video without captions: one per this many seconds. */
const EMPTY_ROW_SECONDS = 2

type Props = {
  captions: Caption[]
  placements: BrollStoredPlacement[]
  duration: number
  /** The player's position, in seconds. */
  playhead: number
  nameOf: (assetId: string) => string
  onSeek: (seconds: number) => void
}

type Row = { id: string; text: string; start: number; end: number; rowStart: number; rowEnd: number }

function rowsOf(captions: Caption[], duration: number): Row[] {
  if (captions.length === 0) {
    const count = Math.max(1, Math.ceil(duration / EMPTY_ROW_SECONDS))
    return Array.from({ length: count }, (_, index) => {
      const start = index * EMPTY_ROW_SECONDS
      const end = Math.min(duration, start + EMPTY_ROW_SECONDS)
      return { id: `t${index}`, text: '', start, end, rowStart: start, rowEnd: end }
    })
  }
  const ordered = [...captions].sort((a, b) => a.start - b.start)
  return ordered.map((caption, index) => ({
    id: caption.id,
    text: caption.text,
    start: caption.start,
    end: caption.end,
    rowStart: index === 0 ? 0 : caption.start,
    rowEnd: index < ordered.length - 1 ? ordered[index + 1].start : Math.max(duration, caption.end),
  }))
}

function layersOf(placements: BrollStoredPlacement[]): BrollStoredPlacement[][] {
  const layers: BrollStoredPlacement[][] = Array.from({ length: LAYERS }, () => [])
  for (const placement of [...placements].sort((a, b) => a.start - b.start)) {
    const free = layers.find((layer) => layer.length === 0 || layer[layer.length - 1].end <= placement.start)
    ;(free ?? layers[LAYERS - 1]).push(placement)
  }
  return layers
}

/** A steady colour per asset, so the same asset looks the same everywhere. */
function hueOf(assetId: string): number {
  let hash = 0
  for (const char of assetId) hash = (hash * 31 + char.charCodeAt(0)) % 360
  return hash
}

export default function BrollTimelineTable({ captions, placements, duration, playhead, nameOf, onSeek }: Props) {
  const box = useRef<HTMLDivElement>(null)
  const height = usePreviewHeight(box)
  const rows = useMemo(() => rowsOf(captions, duration), [captions, duration])
  const layers = useMemo(() => layersOf(placements), [placements])
  const activeIndex = rows.findIndex((row) => playhead >= row.rowStart && playhead < row.rowEnd)
  const activeId = activeIndex >= 0 ? rows[activeIndex].id : null

  // Follow the playhead inside the box, never scrolling the page.
  useEffect(() => {
    const container = box.current
    if (!container || !activeId) return
    const row = container.querySelector<HTMLElement>(`tr[data-id="${CSS.escape(activeId)}"]`)
    if (!row) return
    const header = container.querySelector('thead')?.getBoundingClientRect().height ?? 0
    if (row.offsetTop - header < container.scrollTop) container.scrollTop = row.offsetTop - header
    else if (row.offsetTop + row.offsetHeight > container.scrollTop + container.clientHeight)
      container.scrollTop = row.offsetTop + row.offsetHeight - container.clientHeight
  }, [activeId])

  return (
    <div className="caption-table-box broll-timeline-box" ref={box} style={height ? { height } : undefined}>
      <table className="caption-table broll-timeline">
        <thead>
          <tr>
            <th>Text</th>
            <th>Time</th>
            {Array.from({ length: LAYERS }, (_, index) => (
              <th key={index} className="broll-layer-head" title={`B-roll layer ${index + 1}`}>
                L{index + 1}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={row.id}
              data-id={row.id}
              className={row.id === activeId ? 'active' : undefined}
              onClick={() => onSeek(row.start)}
            >
              <td className="broll-timeline-text" dir="auto">
                {row.text}
              </td>
              <td className="mono time">
                {formatTime(row.start)}–{formatTime(row.end)}
              </td>
              {layers.map((layer, layerIndex) => (
                <td key={layerIndex} className="broll-layer">
                  {layer
                    .filter((placement) => placement.start < row.rowEnd && placement.end > row.rowStart)
                    .map((placement) => {
                      const span = row.rowEnd - row.rowStart
                      if (span <= 0) return null
                      const top = ((Math.max(placement.start, row.rowStart) - row.rowStart) / span) * 100
                      const bottom = ((Math.min(placement.end, row.rowEnd) - row.rowStart) / span) * 100
                      const starts = placement.start >= row.rowStart
                      const ends = placement.end <= row.rowEnd
                      const name = nameOf(placement.asset_id)
                      return (
                        <span
                          key={placement.id}
                          className={['broll-block', starts ? 'starts' : '', ends ? 'ends' : ''].join(' ')}
                          style={{
                            top: `${top}%`,
                            height: `${Math.max(bottom - top, 4)}%`,
                            background: `hsl(${hueOf(placement.asset_id)} 65% 55%)`,
                          }}
                          title={`${name} · ${placement.template_id === 'pip' ? 'picture-in-picture' : 'full frame'} · ${formatTime(placement.start)}–${formatTime(placement.end)}`}
                          onClick={(event) => {
                            event.stopPropagation()
                            onSeek(placement.start)
                          }}
                        />
                      )
                    })}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
