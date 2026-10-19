import { useEffect, useRef } from 'react'
import { formatTime } from './captionFormat'
import type { CaptionCheck, EditableCaption } from './types'
import { usePreviewHeight } from './usePreviewHeight'

/**
 * The captions as a compact two-column table: the text (editable, for fixing
 * the transcription's spelling) and when it is on screen (read-only). Timing,
 * splitting and merging are deliberately not offered here.
 *
 * The table scrolls inside a box as tall as the preview panel beside it, and
 * follows the caption on screen. ↑ / ↓ move between rows (and the player
 * with them).
 */
type Props = {
  captions: EditableCaption[]
  errors: CaptionCheck['errors']
  activeId: string | null
  direction: 'rtl' | 'ltr'
  readOnly: boolean
  onChange: (captions: EditableCaption[]) => void
  onSeek: (seconds: number) => void
}

export default function CaptionTextTable({
  captions,
  errors,
  activeId,
  direction,
  readOnly,
  onChange,
  onSeek,
}: Props) {
  const box = useRef<HTMLDivElement>(null)
  const height = usePreviewHeight(box)

  function setText(index: number, text: string) {
    onChange(captions.map((caption, at) => (at === index ? { ...caption, text } : caption)))
  }

  function focusRow(index: number) {
    box.current?.querySelector<HTMLInputElement>(`input[data-row="${index}"]`)?.focus()
  }

  /** Scroll the box (never the page) so a row is visible. */
  function reveal(row: HTMLElement) {
    const container = box.current
    if (!container) return
    const header = container.querySelector('thead')?.getBoundingClientRect().height ?? 0
    const top = row.offsetTop - header
    const bottom = row.offsetTop + row.offsetHeight
    if (top < container.scrollTop) container.scrollTop = top
    else if (bottom > container.scrollTop + container.clientHeight) {
      container.scrollTop = bottom - container.clientHeight
    }
  }

  // Follow the caption on screen while playing, unless a row is being edited.
  useEffect(() => {
    const container = box.current
    if (!container || !activeId || container.contains(document.activeElement)) return
    const row = container.querySelector<HTMLElement>(`tr[data-id="${CSS.escape(activeId)}"]`)
    if (row) reveal(row)
  }, [activeId])

  return (
    <div className="caption-table-box" ref={box} style={height ? { height } : undefined}>
      <table className="caption-table">
        <thead>
          <tr>
            <th>Text</th>
            <th>Time</th>
          </tr>
        </thead>
        <tbody>
          {captions.map((caption, index) => {
            const problem = errors.find((error) => error.caption_id === caption.id)
            return (
              <tr key={caption.id} data-id={caption.id} className={caption.id === activeId ? 'active' : undefined}>
                <td>
                  <input
                    type="text"
                    dir={direction}
                    data-row={index}
                    value={caption.text}
                    readOnly={readOnly}
                    className={problem ? 'invalid' : undefined}
                    title={problem?.message}
                    aria-label={`Caption ${index + 1} text`}
                    onFocus={(event) => {
                      const row = event.currentTarget.closest('tr')
                      if (row) reveal(row)
                      onSeek(caption.start)
                    }}
                    onKeyDown={(event) => {
                      if (event.key === 'ArrowDown' && index < captions.length - 1) {
                        event.preventDefault()
                        focusRow(index + 1)
                      } else if (event.key === 'ArrowUp' && index > 0) {
                        event.preventDefault()
                        focusRow(index - 1)
                      }
                    }}
                    onChange={(event) => setText(index, event.target.value)}
                  />
                </td>
                <td className="mono time">
                  <button type="button" className="link" title="Jump here" onClick={() => focusRow(index)}>
                    {formatTime(caption.start)}–{formatTime(caption.end)}
                  </button>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
