import { Player, type PlayerRef } from '@remotion/player'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  addBrollPlacement,
  brollAssetMediaUrl,
  brollBaseVideoUrl,
  brollExportUrl,
  cancelJob,
  captionExportUrl,
  captionInputStreamUrl,
  decideBroll,
  getBrollInput,
  getBrollLibrary,
  getBrollStatus,
  importCaptionVideo,
  removeBrollPlacement,
  saveBrollSettings,
  updateBrollPlacement,
} from './api'
import BrollLibrary from './BrollLibrary'
import { describeAsset } from './brollFormat'
import { formatTime } from './captionFormat'
import { CaptionedVideo } from './captions/CaptionedVideo'
import type { CaptionedVideoProps } from './captions/types'
import type { BrollInputState, BrollLibraryAsset, BrollNext, BrollStatus, BrollStoredPlacement, Project } from './types'

type Message = { kind: 'ok' | 'error' | 'warn'; text: string } | null

function errorText(caught: unknown, fallback: string): string {
  return caught instanceof Error ? caught.message : fallback
}

/**
 * B-roll (stage 3A): a reusable asset library, a minimal way to place an
 * asset at the playhead, and the composed result — the speaker's video, the
 * B-roll, the captions — previewed with the very composition the export
 * renders, then approved (and exported) or skipped.
 *
 * Automatic planning is the next stage. Nothing here inserts B-roll on its
 * own: with no placements the panel says so and offers to skip.
 */
export default function BrollPanel({ project, onJobSubmitted }: { project: Project; onJobSubmitted: () => void }) {
  const projectId = project.id
  const [status, setStatus] = useState<BrollStatus | null>(null)
  const [library, setLibrary] = useState<{ assets: BrollLibraryAsset[]; storage_note: string }>({ assets: [], storage_note: '' })
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [state, setState] = useState<BrollInputState | null>(null)
  const [message, setMessage] = useState<Message>(null)
  const [busy, setBusy] = useState(false)
  const [frame, setFrame] = useState(0)
  const [assetChoice, setAssetChoice] = useState('')
  const [templateChoice, setTemplateChoice] = useState('fullframe')
  const playerRef = useRef<PlayerRef>(null)

  const loadStatus = useCallback(async () => {
    const loaded = await getBrollStatus(projectId)
    setStatus(loaded)
    return loaded
  }, [projectId])

  const loadLibrary = useCallback(async () => {
    setLibrary(await getBrollLibrary())
  }, [])

  useEffect(() => {
    let cancelled = false
    Promise.all([loadStatus(), loadLibrary()])
      .then(([loaded]) => {
        if (!cancelled) setSelectedKey(loaded.selected_input)
      })
      .catch((caught) => setMessage({ kind: 'error', text: errorText(caught, 'Loading B-roll failed.') }))
    return () => {
      cancelled = true
    }
  }, [loadStatus, loadLibrary])

  const refresh = useCallback(async () => {
    if (!selectedKey) return
    try {
      setState(await getBrollInput(projectId, selectedKey))
    } catch (caught) {
      setMessage({ kind: 'error', text: errorText(caught, 'Loading the video failed.') })
    }
  }, [projectId, selectedKey])

  useEffect(() => {
    setState(null)
    setFrame(0)
    void refresh()
  }, [selectedKey, refresh])

  // A zoom approval hands its result over to this panel (it saves the
  // selection); pick that up without a reload.
  useEffect(() => {
    const interval = window.setInterval(() => {
      void loadStatus().then((loaded) => {
        if (loaded.settings.selected_input && loaded.settings.selected_input !== selectedKey)
          setSelectedKey(loaded.settings.selected_input)
      })
    }, 8000)
    return () => window.clearInterval(interval)
  }, [loadStatus, selectedKey])

  const exporting =
    !!state?.active_job ||
    (state?.continuation &&
      'job' in state.continuation &&
      (state.continuation.job !== null || state.continuation.export?.status === 'running'))
  useEffect(() => {
    const interval = window.setInterval(() => void refresh(), exporting ? 1500 : 8000)
    return () => window.clearInterval(interval)
  }, [refresh, exporting])

  useEffect(() => {
    const player = playerRef.current
    if (!player) return
    const listener = (event: { detail: { frame: number } }) => setFrame(event.detail.frame)
    player.addEventListener('frameupdate', listener)
    player.addEventListener('seeked', listener)
    return () => {
      player.removeEventListener('frameupdate', listener)
      player.removeEventListener('seeked', listener)
    }
  }, [state?.composition_digest])

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

  const props = state?.props ?? null
  const digest = state?.input.fingerprint?.digest ?? ''
  const inputProps = useMemo<CaptionedVideoProps | null>(() => {
    if (!props || !selectedKey || !state) return null
    return {
      ...props,
      video: { ...props.video, src: `${captionInputStreamUrl(projectId, selectedKey)}?v=${digest.slice(7, 19)}` },
      broll: props.broll
        ? {
            ...props.broll,
            placements: props.broll.placements.map((placement) => ({
              ...placement,
              asset: {
                ...placement.asset,
                src: brollAssetMediaUrl(placement.asset.id, state.assets[placement.asset.id]?.fingerprint.digest ?? ''),
              },
            })),
          }
        : null,
    } as CaptionedVideoProps
  }, [props, selectedKey, state, projectId, digest])

  if (!status) {
    return (
      <section className="panel subpanel broll">
        <h3>B-roll</h3>
        {message ? <p className={`message ${message.kind}`}>{message.text}</p> : <p className="hint">Loading…</p>}
      </section>
    )
  }

  const plan = state?.plan ?? null
  const placements = plan?.placements ?? []
  const revision = plan?.revision ?? 0
  const fps = props?.video.fps ?? 30
  const playhead = frame / fps
  const usable = library.assets.filter((asset) => asset.status === 'available')
  const chosenAsset = usable.find((asset) => asset.id === assetChoice) ?? usable[0] ?? null
  const review = state?.review ?? null
  const nameOf = (assetId: string) =>
    state?.assets[assetId]?.display_name ?? library.assets.find((asset) => asset.id === assetId)?.display_name ?? 'Missing asset'
  const issueFor = (placementId: string) => state?.issues.find((issue) => issue.placement_id === placementId)

  async function choose(key: string) {
    await act(async () => {
      await saveBrollSettings(projectId, { selected_input: key })
      setSelectedKey(key)
    }, 'Selecting the video failed.')
  }

  async function importBase() {
    await act(async () => {
      const result = await importCaptionVideo(projectId)
      if (result.cancelled || !result.input_key) return
      await saveBrollSettings(projectId, { selected_input: result.input_key })
      await loadStatus()
      setSelectedKey(result.input_key)
    }, 'Importing the video failed.')
  }

  async function addAtPlayhead() {
    if (!selectedKey || !chosenAsset) return
    await act(async () => {
      await addBrollPlacement(projectId, selectedKey, {
        asset_id: chosenAsset.id,
        at_seconds: playhead,
        template_id: templateChoice,
        base_revision: revision,
      })
      await refresh()
    }, 'Adding B-roll failed.')
  }

  async function change(placement: BrollStoredPlacement, changes: Record<string, unknown>) {
    if (!selectedKey) return
    await act(async () => {
      await updateBrollPlacement(projectId, selectedKey, placement.id, changes, revision)
      await refresh()
    }, 'Changing the placement failed.')
  }

  async function remove(placement: BrollStoredPlacement) {
    if (!selectedKey) return
    await act(async () => {
      await removeBrollPlacement(projectId, selectedKey, placement.id, revision)
      await refresh()
    }, 'Removing the placement failed.')
  }

  async function decide(kind: 'approve' | 'skip') {
    if (!selectedKey || !state?.composition_digest) return
    await act(async () => {
      const result = await decideBroll(projectId, selectedKey, kind, state.composition_digest!)
      const next = result.next
      setMessage(
        next.kind === 'final' || next.kind === 'blocked'
          ? { kind: next.kind === 'blocked' ? 'warn' : 'ok', text: next.message }
          : {
              kind: 'ok',
              text:
                kind === 'approve'
                  ? 'Approved. Exporting the video with its B-roll and captions…'
                  : 'B-roll skipped. Exporting the video with its captions…',
            },
      )
      onJobSubmitted()
      await refresh()
    }, kind === 'approve' ? 'Approving failed.' : 'Skipping failed.')
  }

  function seek(seconds: number) {
    playerRef.current?.seekTo(Math.round(seconds * fps))
  }

  const composition = state?.input.composition ?? null

  return (
    <section className="panel subpanel broll">
      <div className="editor-header">
        <h3>B-roll</h3>
        <span className="badge">Manual placement · automatic planning comes next</span>
      </div>
      {message && <p className={`message ${message.kind}`}>{message.text}</p>}

      <div className="row">
        <select
          value={selectedKey ?? ''}
          onChange={(event) => void choose(event.target.value)}
          disabled={busy || status.inputs.length === 0}
          aria-label="Video to add B-roll to"
        >
          {status.inputs.length === 0 && <option value="">No videos yet</option>}
          {(['zoom', 'cut', 'source', 'import'] as const).map((kind) => {
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
        <button type="button" onClick={() => void importBase()} disabled={busy}>
          Import a video…
        </button>
      </div>
      <p className="hint">
        B-roll is drawn over the video{state?.input.kind === 'zoom' ? ' (already zoomed — B-roll and captions are not zoomed)' : ''},
        under the captions. The speaker&apos;s audio plays once, unchanged; B-roll is muted unless you unmute it.
      </p>

      {state && !state.available && <p className="message error">{state.unavailable_reason}</p>}
      {state && state.stale_plans.length > 0 && (
        <p className="message warn">
          This video changed since B-roll was planned on it ({state.stale_plans.length} earlier plan
          {state.stale_plans.length > 1 ? 's' : ''} kept, not applied). Place B-roll again.
        </p>
      )}

      {state?.available && inputProps && composition && (
        <>
          <div className="caption-preview" style={{ maxWidth: `calc(60vh * ${composition.width / composition.height})` }}>
            <Player
              ref={playerRef}
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
          <p className="small hint">
            This preview is the export&apos;s own composition.{' '}
            {state.captions ? `Captions: revision ${state.captions.revision}, ${state.captions.caption_count} captions.` : 'No captions for this video.'}
          </p>

          <h4>Add B-roll</h4>
          {usable.length === 0 ? (
            <p className="hint">Import an image or a video into the library below first.</p>
          ) : (
            <div className="row">
              <select value={chosenAsset?.id ?? ''} onChange={(event) => setAssetChoice(event.target.value)} aria-label="Asset">
                {usable.map((asset) => (
                  <option key={asset.id} value={asset.id}>
                    {asset.display_name} — {describeAsset(asset)}
                  </option>
                ))}
              </select>
              <select value={templateChoice} onChange={(event) => setTemplateChoice(event.target.value)} aria-label="Template">
                {status.templates.map((template) => (
                  <option key={template.id} value={template.id}>
                    {template.label}
                  </option>
                ))}
              </select>
              <button type="button" className="primary" onClick={() => void addAtPlayhead()} disabled={busy || !chosenAsset}>
                Add at {formatTime(playhead)}
              </button>
            </div>
          )}
          <p className="small hint">
            Pause the preview where the B-roll should start. Its length is 3 s for an image, up to 4 s for a video,
            shortened to fit the asset, the video and the next B-roll.
          </p>

          {placements.length === 0 ? (
            <div className="message warn">
              <p>{state.planner.message}</p>
            </div>
          ) : (
            <ul className="broll-placements">
              {placements.map((placement) => (
                <PlacementRow
                  key={placement.id}
                  placement={placement}
                  name={nameOf(placement.asset_id)}
                  templates={status.templates}
                  issue={issueFor(placement.id)?.message ?? null}
                  busy={busy}
                  onSeek={() => seek(placement.start)}
                  onChange={(changes) => void change(placement, changes)}
                  onRemove={() => void remove(placement)}
                />
              ))}
            </ul>
          )}
          {state.issues
            .filter((issue) => issue.placement_id === null || !placements.some((placement) => placement.id === issue.placement_id))
            .map((issue, index) => (
              <p key={index} className="message error">
                {issue.message}
              </p>
            ))}

          <Review
            state={state}
            busy={busy}
            projectId={projectId}
            inputKey={selectedKey!}
            onApprove={() => void decide('approve')}
            onSkip={() => void decide('skip')}
            onCancel={(jobId) => void act(() => cancelJob(projectId, jobId).then(refresh), 'Cancel failed.')}
          />
          {review?.decision && !review.approved && !review.skipped && review.outdated_reason && (
            <p className="message warn">{review.outdated_reason}</p>
          )}
          {!status.renderer.ready && <p className="message warn">{status.renderer.problems.join(' ')}</p>}
        </>
      )}

      <details className="broll-library-section">
        <summary>
          Asset library ({library.assets.length}) — shared by all projects
        </summary>
        <BrollLibrary
          assets={library.assets}
          storageNote={library.storage_note}
          onChanged={async () => {
            await loadLibrary()
            await refresh()
          }}
        />
      </details>

      <details>
        <summary>Settings and templates</summary>
        <label className="choice">
          <input
            type="checkbox"
            checked={status.settings.enabled}
            disabled={busy}
            onChange={(event) =>
              void act(async () => {
                await saveBrollSettings(projectId, { enabled: event.target.checked })
                await loadStatus()
              }, 'Saving failed.')
            }
          />
          Continue to B-roll after a zoom result is approved (off: approving zooms exports right away)
        </label>
        {status.templates.map((template) => (
          <p key={template.id} className="small">
            <strong>{template.label}:</strong> {template.description}
          </p>
        ))}
        <p className="small">{status.constraints.overlap_rule}</p>
      </details>
    </section>
  )
}

function PlacementRow({
  placement,
  name,
  templates,
  issue,
  busy,
  onSeek,
  onChange,
  onRemove,
}: {
  placement: BrollStoredPlacement
  name: string
  templates: BrollStatus['templates']
  issue: string | null
  busy: boolean
  onSeek: () => void
  onChange: (changes: Record<string, unknown>) => void
  onRemove: () => void
}) {
  const [start, setStart] = useState(String(placement.start))
  const [end, setEnd] = useState(String(placement.end))
  const [inPoint, setInPoint] = useState(String(placement.source_start ?? 0))
  const template = templates.find((item) => item.id === placement.template_id)

  return (
    <li className={issue ? 'invalid' : ''}>
      <div className="row">
        <button type="button" onClick={onSeek} title="Show in the preview">
          ▶ {formatTime(placement.start)}–{formatTime(placement.end)}
        </button>
        <strong>{name}</strong>
        <select
          value={placement.template_id}
          onChange={(event) => onChange({ template_id: event.target.value })}
          disabled={busy}
          aria-label="Template"
        >
          {templates.map((item) => (
            <option key={item.id} value={item.id}>
              {item.label}
            </option>
          ))}
        </select>
        <button type="button" onClick={onRemove} disabled={busy}>
          Remove
        </button>
      </div>
      {issue && <p className="small message error">{issue}</p>}
      <details className="small">
        <summary>Adjust</summary>
        <div className="row">
          <label className="field">
            <span>Start (s)</span>
            <input type="number" step="0.1" min="0" value={start} onChange={(event) => setStart(event.target.value)} />
          </label>
          <label className="field">
            <span>End (s)</span>
            <input type="number" step="0.1" min="0" value={end} onChange={(event) => setEnd(event.target.value)} />
          </label>
          {placement.source_start !== null && (
            <label className="field">
              <span>From the asset at (s)</span>
              <input type="number" step="0.1" min="0" value={inPoint} onChange={(event) => setInPoint(event.target.value)} />
            </label>
          )}
          <button
            type="button"
            disabled={busy}
            onClick={() =>
              onChange({
                start: Number(start),
                end: Number(end),
                ...(placement.source_start !== null ? { source_start: Number(inPoint) } : {}),
              })
            }
          >
            Apply
          </button>
        </div>
        {template &&
          Object.entries(template.settings).map(([key, spec]) => {
            const value = placement.settings[key]
            if (spec.type === 'boolean')
              return (
                <label key={key} className="choice" title={spec.description}>
                  <input
                    type="checkbox"
                    checked={value === true}
                    disabled={busy}
                    onChange={(event) => onChange({ settings: { ...placement.settings, [key]: event.target.checked } })}
                  />
                  {spec.label}
                </label>
              )
            if (spec.type === 'choice')
              return (
                <label key={key} className="field" title={spec.description}>
                  <span>{spec.label}</span>
                  <select
                    value={String(value)}
                    disabled={busy}
                    onChange={(event) => onChange({ settings: { ...placement.settings, [key]: event.target.value } })}
                  >
                    {spec.choices?.map((choice) => (
                      <option key={choice} value={choice}>
                        {choice.replace('_', ' ')}
                      </option>
                    ))}
                  </select>
                </label>
              )
            return (
              <label key={key} className="field" title={spec.description}>
                <span>
                  {spec.label} ({spec.unit})
                </span>
                <input
                  type="number"
                  min={spec.min}
                  max={spec.max}
                  step={spec.step}
                  defaultValue={Number(value)}
                  disabled={busy}
                  onBlur={(event) => {
                    const number = Number(event.target.value)
                    if (number !== value) onChange({ settings: { ...placement.settings, [key]: number } })
                  }}
                />
              </label>
            )
          })}
      </details>
    </li>
  )
}

function Review({
  state,
  busy,
  projectId,
  inputKey,
  onApprove,
  onSkip,
  onCancel,
}: {
  state: BrollInputState
  busy: boolean
  projectId: string
  inputKey: string
  onApprove: () => void
  onSkip: () => void
  onCancel: (jobId: string) => void
}) {
  const review = state.review
  const next = state.continuation
  const decided = review?.approved || review?.skipped

  return (
    <div className="broll-review">
      {!decided || (next && 'export' in next && next.export?.status === 'failed') ? (
        <div className="row">
          {(state.plan?.placements.length ?? 0) > 0 && (
            <button type="button" className="primary" onClick={onApprove} disabled={busy || !review?.can_approve}>
              {review?.approved ? 'Export again' : 'Approve and continue'}
            </button>
          )}
          <button type="button" onClick={onSkip} disabled={busy}>
            {(state.plan?.placements.length ?? 0) > 0 ? 'Skip B-roll instead' : 'Skip B-roll'}
          </button>
        </div>
      ) : (
        <p className="message ok">
          {review?.approved ? 'Approved' : 'B-roll skipped'} {review?.decision?.decided_at.slice(0, 16).replace('T', ' ')} UTC.
        </p>
      )}
      {decided && <Continuation next={next} projectId={projectId} inputKey={inputKey} onCancel={onCancel} />}
    </div>
  )
}

function Continuation({
  next,
  projectId,
  inputKey,
  onCancel,
}: {
  next: BrollNext | null
  projectId: string
  inputKey: string
  onCancel: (jobId: string) => void
}) {
  if (!next) return null
  if (next.kind === 'final')
    return (
      <div className="row">
        <span className="small">{next.message}</span>
        <a className="button primary" href={brollBaseVideoUrl(projectId, inputKey, true)}>
          Download the video
        </a>
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
  if (record.status !== 'succeeded')
    return <p className="message error">The export {record.status}: {record.error}</p>
  const url = next.kind === 'broll_export' ? brollExportUrl : captionExportUrl
  return (
    <div className="row">
      <a className="button" href={url(projectId, record.export_id, false)} target="_blank" rel="noreferrer">
        Play
      </a>
      <a className="button primary" href={url(projectId, record.export_id, true)}>
        Download the finished video
      </a>
    </div>
  )
}
