import { useCallback, useEffect, useState } from 'react'
import {
  brollBaseVideoUrl,
  cancelJob,
  decideMusic,
  finalExportUrl,
  getMusicState,
  openFinalExportInVlc,
  revealFinalExport,
  saveMusicSettings,
} from './api'
import { formatTime } from './captionFormat'
import MusicLibrary from './MusicLibrary'
import type { AudioTrack, MusicNext, MusicSettings, MusicState, Project } from './types'

type Message = { kind: 'ok' | 'error' | 'warn'; text: string } | null

function errorText(caught: unknown, fallback: string): string {
  return caught instanceof Error ? caught.message : fallback
}

const BROLL_LABEL = {
  approved: 'with the approved B-roll',
  skipped: 'without B-roll (skipped)',
  off: 'without B-roll (switched off)',
  pending: 'B-roll not decided yet',
} as const

type NumberSetting = 'volume_percent' | 'start_seconds' | 'fade_in_seconds' | 'fade_out_seconds'

/**
 * Background music (stage 4), on the result the B-roll stage decided on.
 *
 * Choose a track from the shared music library, set its level and fades,
 * and listen to it in the preview beside this slide — the very composition
 * the export renders. Approving (or continuing without music) exports the
 * finished video. Sound effects will be added to this stage later.
 */
export default function MusicPanel({ project, onChanged }: { project: Project; onChanged: () => void }) {
  const projectId = project.id
  const [state, setState] = useState<MusicState | null>(null)
  const [message, setMessage] = useState<Message>(null)
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(async () => {
    try {
      setState(await getMusicState(projectId))
    } catch (caught) {
      setMessage({ kind: 'error', text: errorText(caught, 'Loading the music stage failed.') })
    }
  }, [projectId])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const next = state?.continuation ?? null
  const exporting = !!next && next.kind === 'final_export' && (next.job !== null || next.export?.status === 'running')
  useEffect(() => {
    // Reading state never renders anything, so polling is free.
    const interval = window.setInterval(() => void refresh(), exporting ? 1500 : 8000)
    return () => window.clearInterval(interval)
  }, [refresh, exporting])

  async function act(action: () => Promise<void>, fallback: string) {
    setBusy(true)
    setMessage(null)
    try {
      await action()
    } catch (caught) {
      setMessage({ kind: 'error', text: errorText(caught, fallback) })
      await refresh()
    } finally {
      setBusy(false)
    }
  }

  /** Save, then let the stage bar and the preview pick the change up at once. */
  async function save(changes: Partial<MusicSettings>) {
    await act(async () => {
      await saveMusicSettings(projectId, changes)
      await refresh()
      onChanged()
    }, 'Saving the music settings failed.')
  }

  async function decide(kind: 'approve' | 'skip') {
    if (!state?.composition_digest) return
    await act(async () => {
      const result = await decideMusic(projectId, kind, state.composition_digest!)
      const following = result.next
      setMessage(
        following.kind === 'final_export'
          ? { kind: 'ok', text: kind === 'approve' ? 'Approved. Exporting the finished video…' : 'Exporting the finished video without music…' }
          : { kind: following.kind === 'blocked' ? 'warn' : 'ok', text: following.message },
      )
      onChanged()
      await refresh()
    }, kind === 'approve' ? 'Approving failed.' : 'Continuing without music failed.')
  }

  /** A track added here is the one wanted for this video; a removed chosen one is unset. */
  async function libraryChanged(event: { added?: AudioTrack; removed?: string }) {
    if (!state) return
    if (event.added) await save({ track_id: event.added.id })
    else if (event.removed && event.removed === state.settings.track_id) await save({ track_id: null })
    else {
      await refresh()
      onChanged()
    }
  }

  if (!state) {
    // The library does not depend on this video's stage: keep it usable.
    return (
      <section className="panel subpanel music">
        <h3>Background music</h3>
        {message ? <p className={`message ${message.kind}`}>{message.text}</p> : <p className="hint">Loading…</p>}
        <h4>Music library</h4>
        <MusicLibrary />
      </section>
    )
  }

  const settings = state.settings
  const review = state.review
  const props = state.props
  const videoSeconds = props ? props.video.duration_in_frames / props.video.fps : null
  const track = state.track
  const decided = !!review && (review.approved || review.skipped)
  const playable = track ? track.measured.duration_seconds - settings.start_seconds : null

  return (
    <section className="panel subpanel music">
      <div className="editor-header">
        <h3>Background music</h3>
        {state.broll_state && <span className="badge">{BROLL_LABEL[state.broll_state]}</span>}
      </div>

      {message && <p className={`message ${message.kind}`}>{message.text}</p>}
      {!settings.enabled && (
        <p className="message warn">
          The music stage is switched off: B-roll&apos;s decision exports the video directly. Turn it on under Settings below.
        </p>
      )}
      {!state.available && state.unavailable_reason && <p className="hint">{state.unavailable_reason}</p>}
      {review?.blocked_reason && <p className="message warn">{review.blocked_reason}</p>}

      <h4>Track</h4>
      <MusicLibrary
        chosen={settings.track_id}
        onChoose={(trackId) => void save({ track_id: trackId })}
        onChanged={(event) => void libraryChanged(event)}
        disabled={busy}
      />
      {state.issues.map((issue, index) => (
        <p key={index} className="message error">
          {issue}
        </p>
      ))}

      {track && (
        <>
          <h4>Mix</h4>
          <div className="music-mix">
            <SliderSetting
              key={settings.volume_percent}
              label="Volume under the speaker"
              unit="%"
              value={settings.volume_percent}
              disabled={busy}
              onCommit={(value) => void save({ volume_percent: value })}
            />
            <NumberField name="start_seconds" label="Start in the track (s)" value={settings.start_seconds} max={Math.max(0, track.measured.duration_seconds - 0.5)} busy={busy} onCommit={save} />
            <NumberField name="fade_in_seconds" label="Fade in (s)" value={settings.fade_in_seconds} max={30} busy={busy} onCommit={save} />
            <NumberField name="fade_out_seconds" label="Fade out (s)" value={settings.fade_out_seconds} max={30} busy={busy} onCommit={save} />
          </div>
          <label className="choice">
            <input type="checkbox" checked={settings.loop} disabled={busy} onChange={(event) => void save({ loop: event.target.checked })} />
            Loop the track if it is shorter than the video
          </label>
          {videoSeconds !== null && playable !== null && playable < videoSeconds && (
            <p className="small hint">
              The track plays {formatTime(playable)} from this start point; the video is {formatTime(videoSeconds)}.{' '}
              {settings.loop ? 'It starts again from the start point until the video ends.' : 'The music stops early (fading out where it ends).'}
            </p>
          )}
          <p className="small hint">Play the preview beside this slide to hear the mix. The speaker&apos;s own sound is never changed.</p>
        </>
      )}

      {state.available && review && (
        <div className="broll-review">
          {!decided || (next?.kind === 'final_export' && next.export?.status === 'failed') ? (
            <div className="row">
              <button type="button" className="primary" onClick={() => void decide('approve')} disabled={busy || !review.can_approve}>
                {review.approved ? 'Export again' : 'Approve music and export'}
              </button>
              <button type="button" onClick={() => void decide('skip')} disabled={busy || !review.can_skip}>
                Continue without music
              </button>
            </div>
          ) : (
            <p className="message ok">
              {review.approved ? `Approved with "${track?.display_name ?? 'music'}"` : 'Continued without music'}{' '}
              {review.decision?.decided_at.slice(0, 16).replace('T', ' ')} UTC.
            </p>
          )}
          {review.decision && !decided && review.outdated_reason && <p className="message warn">{review.outdated_reason}</p>}
          {decided && (
            <Continuation
              next={next}
              projectId={projectId}
              inputKey={state.input_key}
              onCancel={(jobId) => void act(() => cancelJob(projectId, jobId).then(refresh), 'Cancel failed.')}
              onOpenInVlc={(exportId) =>
                void act(() => openFinalExportInVlc(projectId, exportId).then(() => undefined), 'Opening VLC failed.')
              }
              onReveal={(exportId) =>
                void act(() => revealFinalExport(projectId, exportId).then(() => undefined), 'Opening File Explorer failed.')
              }
            />
          )}
          {!state.renderer.ready && <p className="message warn">{state.renderer.problems.join(' ')}</p>}
        </div>
      )}

      <details>
        <summary>Settings</summary>
        <label className="choice">
          <input type="checkbox" checked={settings.enabled} disabled={busy} onChange={(event) => void save({ enabled: event.target.checked })} />
          Continue to background music after B-roll (off: B-roll&apos;s decision exports right away)
        </label>
        <p className="small">Sound effects will be added to this stage in a later version.</p>
      </details>
    </section>
  )
}

function SliderSetting({
  label,
  unit,
  value,
  disabled,
  onCommit,
}: {
  label: string
  unit: string
  value: number
  disabled: boolean
  onCommit: (value: number) => void
}) {
  // The parent re-creates this when the saved value changes (`key`).
  const [draft, setDraft] = useState(value)
  const commit = () => {
    if (draft !== value) onCommit(draft)
  }
  return (
    <label className="field">
      <span>
        {label}: {Math.round(draft)}
        {unit}
      </span>
      <input
        type="range"
        min={0}
        max={100}
        step={1}
        value={draft}
        disabled={disabled}
        onChange={(event) => setDraft(Number(event.target.value))}
        onPointerUp={commit}
        onKeyUp={commit}
        onBlur={commit}
      />
    </label>
  )
}

function NumberField({
  name,
  label,
  value,
  max,
  busy,
  onCommit,
}: {
  name: NumberSetting
  label: string
  value: number
  max: number
  busy: boolean
  onCommit: (changes: Partial<MusicSettings>) => void
}) {
  return (
    <label className="field">
      <span>{label}</span>
      <input
        // Re-created when the saved value changes, so it always shows it.
        key={`${name}-${value}`}
        type="number"
        min={0}
        max={max}
        step={0.1}
        defaultValue={value}
        disabled={busy}
        onBlur={(event) => {
          const number = Number(event.target.value)
          if (Number.isFinite(number) && number !== value) onCommit({ [name]: number })
        }}
        onKeyDown={(event) => {
          if (event.key === 'Enter') event.currentTarget.blur()
        }}
      />
    </label>
  )
}

function Continuation({
  next,
  projectId,
  inputKey,
  onCancel,
  onOpenInVlc,
  onReveal,
}: {
  next: MusicNext | null
  projectId: string
  inputKey: string | null
  onCancel: (jobId: string) => void
  onOpenInVlc: (exportId: string) => void
  onReveal: (exportId: string) => void
}) {
  if (!next) return null
  if (next.kind === 'final')
    return (
      <div className="row">
        <span className="small">{next.message}</span>
        {inputKey && (
          <a className="button primary" href={brollBaseVideoUrl(projectId, inputKey, true)}>
            Download the video
          </a>
        )}
      </div>
    )
  if (next.kind === 'blocked') return <p className="message warn">{next.message}</p>
  const record = next.export
  if (next.job && !record)
    return (
      <div className="row">
        <span className="small">
          {next.job.progress_message}
          {next.job.progress_percent !== null ? ` (${Math.round(next.job.progress_percent)}%)` : ''}
        </span>
        <button type="button" onClick={() => onCancel(next.job!.id)} disabled={next.job.cancel_requested}>
          Cancel
        </button>
      </div>
    )
  if (!record) return null
  if (record.status === 'running') return <span className="small">Exporting… see Background jobs for progress.</span>
  if (record.status !== 'succeeded') return <p className="message error">The export {record.status}: {record.error}</p>
  return (
    <div className="row">
      <a className="button" href={finalExportUrl(projectId, record.export_id, false)} target="_blank" rel="noreferrer">
        Play
      </a>
      <a className="button primary" href={finalExportUrl(projectId, record.export_id, true)}>
        Download the finished video
      </a>
      <button type="button" onClick={() => onOpenInVlc(record.export_id)} title="Play the finished file in VLC">
        Open in VLC
      </button>
      <button type="button" onClick={() => onReveal(record.export_id)} title="Show the finished file in its folder">
        Show in File Explorer
      </button>
    </div>
  )
}
