import { useState } from 'react'
import type { CaptionValidationError, EditableCaption } from './types'

/**
 * The caption list: correct words, adjust times, split, merge, seek.
 *
 * Edits change the draft only. Split, merge and "fix overlaps" are done by
 * the backend on the draft (one tested implementation) and come back as a new
 * draft; nothing is saved until "Save captions".
 */

type Props = {
  captions: EditableCaption[]
  errors: CaptionValidationError[]
  /** Timing state after normalisation, by caption id, from the last check. */
  timing: Record<string, { word_timing?: string; timing_note?: string | null }>
  activeId: string | null
  readOnly: boolean
  busy: boolean
  onChange: (captions: EditableCaption[]) => void
  onSeek: (seconds: number) => void
  onSplit: (captionId: string, wordIndex: number) => void
  onMerge: (captionId: string) => void
}

const NUDGE = 0.05

export default function CaptionEditor({
  captions,
  errors,
  timing,
  activeId,
  readOnly,
  busy,
  onChange,
  onSeek,
  onSplit,
  onMerge,
}: Props) {
  const [splitting, setSplitting] = useState<string | null>(null)

  const errorsById = new Map<string, CaptionValidationError[]>()
  for (const error of errors) {
    if (!error.caption_id) continue
    errorsById.set(error.caption_id, [...(errorsById.get(error.caption_id) ?? []), error])
  }

  function update(index: number, changes: Partial<EditableCaption>) {
    onChange(captions.map((caption, at) => (at === index ? { ...caption, ...changes } : caption)))
  }

  function setTime(index: number, field: 'start' | 'end', raw: string) {
    // An empty or half-typed number stays NaN in the draft; validation says so.
    update(index, { [field]: raw === '' ? Number.NaN : Number(raw) })
  }

  function nudge(index: number, field: 'start' | 'end', delta: number) {
    const value = captions[index][field]
    if (!Number.isFinite(value)) return
    update(index, { [field]: Math.max(0, Math.round((value + delta) * 1000) / 1000) })
  }

  function remove(index: number) {
    onChange(captions.filter((_, at) => at !== index))
  }

  return (
    <ol className="caption-list">
      {captions.map((caption, index) => {
        const problems = errorsById.get(caption.id) ?? []
        const state = timing[caption.id] ?? caption
        const words = caption.text.trim().split(/\s+/).filter(Boolean)
        const groupTiming = state.word_timing === 'group'
        const fieldError = (field: string) => problems.some((problem) => problem.field === field)

        return (
          <li
            key={caption.id}
            className={[
              'caption-row',
              caption.id === activeId ? 'active' : '',
              problems.length > 0 ? 'invalid' : '',
            ].join(' ')}
          >
            <div className="caption-row-main">
              <button
                type="button"
                className="seek"
                title="Play from this caption"
                onClick={() => onSeek(caption.start)}
                disabled={!Number.isFinite(caption.start)}
              >
                {index + 1} ▶
              </button>

              {(['start', 'end'] as const).map((field) => (
                <span key={field} className="caption-time">
                  <button
                    type="button"
                    onClick={() => nudge(index, field, -NUDGE)}
                    disabled={readOnly}
                    title={`${field} − ${NUDGE} s`}
                  >
                    −
                  </button>
                  <input
                    type="number"
                    step="0.01"
                    min="0"
                    aria-label={`Caption ${index + 1} ${field} (seconds)`}
                    className={fieldError(field) ? 'invalid' : ''}
                    value={Number.isFinite(caption[field]) ? caption[field] : ''}
                    disabled={readOnly}
                    onChange={(event) => setTime(index, field, event.target.value)}
                  />
                  <button
                    type="button"
                    onClick={() => nudge(index, field, NUDGE)}
                    disabled={readOnly}
                    title={`${field} + ${NUDGE} s`}
                  >
                    +
                  </button>
                </span>
              ))}

              <input
                type="text"
                dir="auto"
                className={`caption-text ${fieldError('text') ? 'invalid' : ''}`}
                aria-label={`Caption ${index + 1} text`}
                value={caption.text}
                disabled={readOnly}
                onChange={(event) => update(index, { text: event.target.value })}
              />

              {groupTiming && (
                <span className="badge warn" title={state.timing_note ?? 'No word timing.'}>
                  Group timing
                </span>
              )}

              {!readOnly && (
                <span className="row">
                  <button
                    type="button"
                    disabled={busy || words.length < 2}
                    onClick={() => setSplitting(splitting === caption.id ? null : caption.id)}
                  >
                    Split…
                  </button>
                  <button
                    type="button"
                    disabled={busy || index === captions.length - 1}
                    onClick={() => onMerge(caption.id)}
                    title="Merge with the next caption"
                  >
                    Merge ↓
                  </button>
                  <button type="button" disabled={busy} onClick={() => remove(index)}>
                    Remove
                  </button>
                </span>
              )}
            </div>

            {splitting === caption.id && !readOnly && (
              <div className="split-picker" dir="auto">
                <span className="small">Split before:</span>
                {words.slice(1).map((word, offset) => (
                  <button
                    key={offset}
                    type="button"
                    disabled={busy}
                    onClick={() => {
                      setSplitting(null)
                      onSplit(caption.id, offset + 1)
                    }}
                  >
                    {word}
                  </button>
                ))}
                <span className="small">
                  {groupTiming
                    ? 'No word timing: the split time is the player position if it is inside this caption, otherwise an estimate to check.'
                    : 'Word timing is kept: each part keeps its words’ real times.'}
                </span>
              </div>
            )}

            {problems.map((problem, at) => (
              <p key={at} className="caption-error">
                {problem.message}
              </p>
            ))}
            {groupTiming && state.timing_note && <p className="small">{state.timing_note}</p>}
          </li>
        )
      })}
    </ol>
  )
}
