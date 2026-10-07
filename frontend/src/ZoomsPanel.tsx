import { Player } from '@remotion/player'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  approveZoomRun,
  cancelJob,
  captionExportUrl,
  getZoomInput,
  getZoomStatus,
  retryJob,
  saveZoomSettings,
  startZooms,
  zoomVideoUrl,
} from './api'
import { formatTime } from './captionFormat'
import { CaptionedVideo } from './captions/CaptionedVideo'
import type { CaptionedVideoProps } from './captions/types'
import type { CaptionComposition, Project, ZoomInputState, ZoomRun, ZoomStatus } from './types'

type Message = { kind: 'ok' | 'error' | 'warn'; text: string } | null

function errorText(caught: unknown, fallback: string): string {
  return caught instanceof Error ? caught.message : fallback
}

/**
 * Review player: the actual FFmpeg zoom output, with the project's existing
 * captions drawn over it by the same Remotion composition the export uses.
 * The zoom itself is never simulated here.
 */
function ZoomReviewPlayer({ src, composition, run }: { src: string; composition: CaptionComposition; run: ZoomRun }) {
  const overlay = run.review?.overlay ?? null
  const inputProps = useMemo<CaptionedVideoProps | null>(
    () =>
      overlay
        ? {
            video: {
              src,
              width: composition.width,
              height: composition.height,
              fps: composition.fps,
              duration_in_frames: composition.duration_in_frames,
            },
            captions: overlay.captions,
            style: overlay.style,
            direction: overlay.direction,
          }
        : null,
    [src, composition, overlay],
  )
  const ratio = composition.width / composition.height

  if (!inputProps) {
    return (
      <div className="caption-preview" style={{ maxWidth: `calc(65vh * ${ratio})` }}>
        <video src={src} controls preload="metadata" style={{ width: '100%' }} />
      </div>
    )
  }
  return (
    <div className="caption-preview" style={{ maxWidth: `calc(65vh * ${ratio})` }}>
      <Player
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
}

function percentOf(seconds: number, total: number): string {
  return total > 0 ? `${Math.round((100 * seconds) / total)}%` : '–'
}

export default function ZoomsPanel({ project, onJobSubmitted }: { project: Project; onJobSubmitted: () => void }) {
  const projectId = project.id
  const [status, setStatus] = useState<ZoomStatus | null>(null)
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [state, setState] = useState<ZoomInputState | null>(null)
  const [message, setMessage] = useState<Message>(null)
  const [busy, setBusy] = useState(false)
  const lastActive = useRef<string | null>(null)

  const loadStatus = useCallback(async () => {
    const loaded = await getZoomStatus(projectId)
    setStatus(loaded)
    return loaded
  }, [projectId])

  useEffect(() => {
    let cancelled = false
    loadStatus()
      .then((loaded) => {
        if (!cancelled) setSelectedKey(loaded.selected_input)
      })
      .catch((caught) => setMessage({ kind: 'error', text: errorText(caught, 'Loading zooms failed.') }))
    return () => {
      cancelled = true
    }
  }, [loadStatus])

  const refresh = useCallback(async () => {
    if (!selectedKey) return
    try {
      const loaded = await getZoomInput(projectId, selectedKey)
      setState(loaded)
      // A job just finished: say so once, and refresh the input list (a new
      // zoomed video also appears in the Captions section).
      const activeId = loaded.active_job?.id ?? null
      if (lastActive.current && activeId !== lastActive.current) {
        const finished = loaded.last_job
        if (finished?.id === lastActive.current && finished.status === 'failed') {
          setMessage({ kind: 'error', text: finished.error ?? 'Making zooms failed.' })
        } else if (finished?.id === lastActive.current && finished.status === 'succeeded') {
          setMessage({ kind: 'ok', text: 'The zoomed video is ready for review.' })
        }
        void loadStatus()
      }
      lastActive.current = activeId
    } catch (caught) {
      setMessage({ kind: 'error', text: errorText(caught, 'Loading the video failed.') })
    }
  }, [projectId, selectedKey, loadStatus])

  useEffect(() => {
    setState(null)
    lastActive.current = null
    void refresh()
  }, [selectedKey, refresh])

  const run = state?.current_run ?? null
  const continuation = run?.continuation ?? null
  const exporting = continuation !== null && (continuation.status === 'queued' || continuation.status === 'running')
  const active = state?.active_job ?? null
  useEffect(() => {
    const interval = window.setInterval(() => void refresh(), active || exporting ? 1500 : 8000)
    return () => window.clearInterval(interval)
  }, [refresh, active, exporting])

  async function act(action: () => Promise<void>, fallback: string) {
    setBusy(true)
    setMessage(null)
    try {
      await action()
    } catch (caught) {
      setMessage({ kind: 'error', text: errorText(caught, fallback) })
    } finally {
      setBusy(false)
    }
  }

  async function choose(key: string) {
    await act(async () => {
      await saveZoomSettings(projectId, { selected_input: key })
      setSelectedKey(key)
    }, 'Selecting the video failed.')
  }

  async function create(forceReplan = false) {
    if (!selectedKey) return
    if (
      forceReplan &&
      !window.confirm('Ask Gemini for a new plan? This sends the transcript again and uses free-tier quota.')
    )
      return
    await act(async () => {
      const result = await startZooms(projectId, selectedKey, { forceReplan })
      if (result.reused) setMessage({ kind: 'ok', text: 'These zooms already exist; nothing was sent or rendered.' })
      else onJobSubmitted()
      await refresh()
    }, 'Starting zooms failed.')
  }

  async function retry() {
    const failed = state?.last_job
    if (!failed) return
    await act(async () => {
      // The same snapshot again. A plan made by the failed attempt is reused,
      // so a failed render does not call Gemini a second time.
      await retryJob(projectId, failed.id)
      onJobSubmitted()
      await refresh()
    }, 'Retrying failed.')
  }

  async function approve() {
    if (!run?.output || !run.review) return
    await act(async () => {
      const result = await approveZoomRun(
        projectId,
        run.run_id,
        run.output!.fingerprint.digest,
        run.review!.overlay_reference,
      )
      const next = result.next
      setMessage(
        next.kind === 'caption_export'
          ? { kind: 'ok', text: 'Approved. Exporting the zoomed video with its captions…' }
          : { kind: next.kind === 'blocked' ? 'warn' : 'ok', text: next.message },
      )
      if (next.kind === 'broll') document.querySelector('.broll')?.scrollIntoView({ behavior: 'smooth' })
      onJobSubmitted()
      await refresh()
    }, 'Approving failed.')
  }

  async function savePreference(preference: string) {
    await act(async () => {
      await saveZoomSettings(projectId, { preference })
      await loadStatus()
      setMessage({ kind: 'ok', text: 'Saved. It applies the next time a plan is made.' })
    }, 'Saving failed.')
  }

  if (!status) {
    return (
      <section className="panel subpanel zooms">
        <h3>Zooms</h3>
        {message ? <p className={`message ${message.kind}`}>{message.text}</p> : <p className="hint">Loading…</p>}
      </section>
    )
  }

  const provider = status.provider
  const composition = state?.input.composition ?? null
  const plan = run?.plan ?? null
  const review = run?.review ?? null
  const failedJob = !active && state?.last_job?.status === 'failed' ? state.last_job : null
  const duration = state?.input.metadata?.duration_seconds ?? 0
  const videoSrc = run?.output ? `${zoomVideoUrl(projectId, run.run_id)}?v=${run.output.fingerprint.digest.slice(7, 19)}` : null

  return (
    <section className="panel subpanel zooms">
      <div className="editor-header">
        <h3>Zooms</h3>
        <span className={`badge ${provider.ready ? '' : 'bad'}`}>
          {provider.is_mock ? provider.label : `${provider.label} · ${provider.model}`}
        </span>
      </div>
      {!provider.ready && <p className="message warn">{provider.message}</p>}
      {message && <p className={`message ${message.kind}`}>{message.text}</p>}

      <div className="row">
        <select
          value={selectedKey ?? ''}
          onChange={(event) => void choose(event.target.value)}
          disabled={busy || status.inputs.length === 0}
          aria-label="Video to zoom"
        >
          {status.inputs.length === 0 && <option value="">No videos yet — cut some takes first</option>}
          {(['cut', 'source', 'import'] as const).map((kind) => {
            const group = status.inputs.filter((input) => input.kind === kind)
            if (group.length === 0) return null
            return (
              <optgroup key={kind} label={group[0].kind_label}>
                {group.map((input) => (
                  <option key={input.key} value={input.key}>
                    {input.label}
                    {input.available ? '' : ' (missing)'}
                  </option>
                ))}
              </optgroup>
            )
          })}
        </select>
        {!run && !active && (
          <button
            type="button"
            className="primary"
            onClick={() => void create()}
            disabled={busy || !selectedKey || !provider.ready || !state?.input.available}
          >
            Create zooms
          </button>
        )}
      </div>
      <p className="hint">
        Zooms are planned from the transcript and applied to the clean video; captions stay a separate layer on top.
        {state && !state.transcript && !run && ' This video has no transcript yet; it is transcribed locally first.'}
      </p>

      {active && (
        <ul className="jobs">
          <li className="caption-job">
            <div className="job-header">
              <strong>Making zooms</strong>
              <span className="badge">{active.cancel_requested ? 'Stopping…' : active.status}</span>
              <button
                type="button"
                onClick={() => void act(() => cancelJob(projectId, active.id).then(refresh), 'Cancel failed.')}
                disabled={active.cancel_requested}
              >
                Cancel
              </button>
            </div>
            <p className="small">{active.progress_message}</p>
            {active.progress_percent !== null && (
              <div className="progress" aria-label="progress">
                <div className="progress-bar" style={{ width: `${active.progress_percent}%` }} />
              </div>
            )}
          </li>
        </ul>
      )}

      {failedJob && !run && (
        <div className="message error">
          <p>{failedJob.error}</p>
          <button type="button" onClick={() => void retry()} disabled={busy}>
            Retry
          </button>
        </div>
      )}

      {run && composition && videoSrc && (
        <>
          <ZoomReviewPlayer src={videoSrc} composition={composition} run={run} />
          {plan && (
            <p>
              <strong>Style applied:</strong> {plan.summary}
              {plan.provider.is_mock && <span className="badge bad">Demo rules, not AI</span>}
            </p>
          )}
          {review && !review.overlay && (
            <p className="hint">No captions for this video, so the zoomed video itself is the result.</p>
          )}

          {review?.approved ? (
            <p className="message ok">Approved {review.approval?.approved_at.slice(0, 16).replace('T', ' ')} UTC.</p>
          ) : (
            <>
              {review?.approval_outdated_reason && <p className="message warn">{review.approval_outdated_reason}</p>}
              <div className="row">
                <button
                  type="button"
                  className="primary"
                  onClick={() => void approve()}
                  disabled={busy || !review?.reviewable}
                >
                  Approve and continue
                </button>
                {failedJob && (
                  <button type="button" onClick={() => void retry()} disabled={busy}>
                    Retry
                  </button>
                )}
              </div>
            </>
          )}

          {review?.approved && !continuation && status.broll_enabled && (
            <p className="small hint">Next step: B-roll, below. Approve or skip it there to export.</p>
          )}
          {review?.approved && (
            <div className="row">
              {continuation && 'export_id' in continuation && continuation.downloadable ? (
                <a className="button primary" href={captionExportUrl(projectId, continuation.export_id, true)}>
                  Download the captioned, zoomed video
                </a>
              ) : continuation && (continuation.status === 'queued' || continuation.status === 'running') ? (
                <span className="small">Exporting with captions… see the Jobs panel for progress.</span>
              ) : continuation && continuation.status === 'failed' ? (
                <span className="message error">The captioned export failed: {'error' in continuation ? continuation.error : ''}</span>
              ) : (
                !review.overlay && (
                  <a className="button primary" href={zoomVideoUrl(projectId, run.run_id, true)}>
                    Download the zoomed video
                  </a>
                )
              )}
            </div>
          )}

          <details>
            <summary>Plan and technical details</summary>
            {plan && (
              <>
                <p className="small">
                  {plan.provider.label} · {plan.provider.model} · prompt {plan.prompt_version} · preference{' '}
                  {plan.settings.preference} · transcript revision {plan.transcript.revision} (
                  {plan.transcript.timing_precision}-level timing){plan.audit.repair_used ? ' · needed one correction' : ''}
                </p>
                <p className="small">
                  {plan.statistics.framing_changes} framing changes. Time per preset:{' '}
                  {Object.entries(plan.statistics.seconds_per_preset)
                    .filter(([, seconds]) => seconds > 0)
                    .map(([preset, seconds]) => `${preset} ${percentOf(seconds, duration)}`)
                    .join(' · ')}
                </p>
                <table className="small zoom-plan">
                  <thead>
                    <tr>
                      <th>From</th>
                      <th>To</th>
                      <th>Preset</th>
                      <th>Why</th>
                    </tr>
                  </thead>
                  <tbody>
                    {plan.intervals.map((interval) => (
                      <tr key={interval.start}>
                        <td>{formatTime(interval.start)}</td>
                        <td>{formatTime(interval.end)}</td>
                        <td>
                          {interval.preset} ({Math.round(interval.scale * 100)}%)
                        </td>
                        <td>{interval.reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <p className="small">Adjustments made by the app: {plan.normalisation.notes.join(' ')}</p>
              </>
            )}
            <p className="small">Renderer: {run.renderer.method}</p>
            {run.verification && (
              <p className="small">
                Checked with FFprobe: {run.verification.width}×{run.verification.height},{' '}
                {run.verification.duration_seconds} s (input {run.verification.input_duration_seconds} s),{' '}
                {run.verification.video_frames ?? '?'} of {run.verification.input_video_frames ?? '?'} frames, audio{' '}
                {run.verification.audio_codec ?? 'none'} (copied once).
              </p>
            )}
            <div className="row">
              <button type="button" onClick={() => void create(true)} disabled={busy || !!active || !provider.ready}>
                Make a new plan
              </button>
            </div>
          </details>
        </>
      )}

      <details>
        <summary>Settings and what is sent to Google</summary>
        <label className="field">
          Editing style
          <select
            value={status.settings.preference}
            onChange={(event) => void savePreference(event.target.value)}
            disabled={busy}
          >
            {status.preferences.map((preference) => (
              <option key={preference.id} value={preference.id} title={preference.description}>
                {preference.id}
              </option>
            ))}
          </select>
        </label>
        <p className="small">
          Presets: {status.presets.map((preset) => `${preset.id} ${Math.round(preset.scale * 100)}%`).join(' · ')}. The
          AI picks the preset and when each one starts and ends; there is no fixed length or rhythm.
        </p>
        {provider.data_sent.length > 0 && (
          <p className="small">
            Sent: {provider.data_sent.join('; ')}. Never sent: video, audio, file names or paths.
          </p>
        )}
        {provider.free_tier_note && <p className="small">{provider.free_tier_note}</p>}
        <p className="small">{status.renderer.method}</p>
      </details>
    </section>
  )
}
