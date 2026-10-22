import { useCallback, useEffect, useState } from 'react'
import {
  addBrollPlacement,
  applyBrollPlanningRun,
  brollBaseVideoUrl,
  brollExportUrl,
  cancelJob,
  captionExportUrl,
  decideBroll,
  getBrollInput,
  getBrollLibrary,
  getBrollStatus,
  importCaptionVideo,
  removeBrollPlacement,
  restoreBrollRevision,
  retryJob,
  saveBrollSettings,
  startBrollPlanning,
  updateBrollPlacement,
} from './api'
import BrollTimelineTable from './BrollTimelineTable'
import LibraryWindow from './LibraryWindow'
import { usePreviewBridge } from './previewBridge'
import { describeAsset, fitsFullFrame } from './brollFormat'
import { formatTime } from './captionFormat'
import type {
  BrollInputState,
  BrollLibraryAsset,
  BrollNext,
  BrollPlannerRun,
  BrollStatus,
  BrollStoredPlacement,
  Project,
} from './types'

type Message = { kind: 'ok' | 'error' | 'warn'; text: string } | null

function errorText(caught: unknown, fallback: string): string {
  return caught instanceof Error ? caught.message : fallback
}

const ORIGIN_LABEL = { planner: 'automatic plan', manual: 'your correction', restore: 'restored' } as const

/**
 * B-roll (stages 3A + 3B), result first: B-roll is planned automatically
 * from the transcript and the described assets in the library, composed
 * with the speaker's video and the captions, and shown in the very
 * composition the export renders. The user reviews the result and approves
 * it (or skips B-roll). Placement-level corrections, plan history and the
 * library are secondary, collapsed controls.
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
  const [libraryWindow, setLibraryWindow] = useState<'video' | 'general' | null>(null)
  // The shared preview beside the slide plays the composed video (it is
  // the same composition as the export); this slide moves and follows it.
  const bridge = usePreviewBridge()
  useEffect(() => bridge.subscribeFrame((value) => setFrame(value)), [bridge])

  const loadStatus = useCallback(async () => {
    const loaded = await getBrollStatus(projectId)
    setStatus(loaded)
    return loaded
  }, [projectId])

  const loadLibrary = useCallback(async () => {
    setLibrary(await getBrollLibrary(projectId))
  }, [projectId])

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

  // The video always follows the zoom stage's newest output (and a zoom
  // approval hands its result over here); pick that up without a reload.
  useEffect(() => {
    const interval = window.setInterval(() => {
      void loadStatus().then((loaded) => {
        if (loaded.selected_input && loaded.selected_input !== selectedKey) setSelectedKey(loaded.selected_input)
      })
    }, 8000)
    return () => window.clearInterval(interval)
  }, [loadStatus, selectedKey])

  const planning = !!state?.planner.active_job
  const exporting =
    !!state?.active_job ||
    (state?.continuation &&
      'job' in state.continuation &&
      (state.continuation.job !== null || state.continuation.export?.status === 'running'))
  useEffect(() => {
    // Reading state never calls the planner, so polling is free.
    const interval = window.setInterval(() => void refresh(), planning || exporting ? 1500 : 8000)
    return () => window.clearInterval(interval)
  }, [refresh, planning, exporting])


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
  const planner = state?.planner ?? null
  const run = planner?.run ?? null
  const nameOf = (assetId: string) =>
    state?.assets[assetId]?.display_name ?? library.assets.find((asset) => asset.id === assetId)?.display_name ?? 'Missing asset'
  const issueFor = (placementId: string) => state?.issues.find((issue) => issue.placement_id === placementId)
  // Only an asset shaped like the video may be full-frame (the backend enforces it too).
  const frameSize = state?.input.composition ?? null
  const fitsFrame = (assetId: string | undefined) =>
    !!frameSize &&
    !!assetId &&
    fitsFullFrame(
      state?.assets[assetId] ?? library.assets.find((asset) => asset.id === assetId),
      frameSize.width,
      frameSize.height,
    )
  const chosenFits = fitsFrame(chosenAsset?.id)
  const effectiveTemplate = templateChoice === 'fullframe' && !chosenFits ? 'pip' : templateChoice
  const provider = status.provider

  /** Open the library that has assets waiting for a description. */
  function openLibrary() {
    const ownUndescribed = library.assets.some((asset) => asset.library === 'project' && !asset.described)
    setLibraryWindow(ownUndescribed ? 'video' : 'general')
  }

  async function closeLibrary() {
    setLibraryWindow(null)
    await loadLibrary()
    await refresh()
  }

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

  async function requestPlan(options: { forceReplan?: boolean; replaceCurrent?: boolean } = {}) {
    if (!selectedKey) return
    await act(async () => {
      const result = await startBrollPlanning(projectId, selectedKey, options)
      if (result.reused) setMessage({ kind: 'ok', text: 'This plan is already up to date; nothing was sent again.' })
      if (result.job) onJobSubmitted()
      await refresh()
    }, 'Planning could not start.')
  }

  function makeNewPlan() {
    const edited = plan && plan.origin !== 'planner'
    const text = edited
      ? 'Make a new automatic plan? Your corrections are kept as revision ' +
        revision +
        ' in the plan history (you can restore them), but the new plan replaces what is shown.'
      : 'Ask for a new automatic plan? The current one stays in the plan history.'
    if (window.confirm(text)) void requestPlan({ forceReplan: true, replaceCurrent: true })
  }

  async function addAtPlayhead() {
    if (!selectedKey || !chosenAsset) return
    await act(async () => {
      await addBrollPlacement(projectId, selectedKey, {
        asset_id: chosenAsset.id,
        at_seconds: playhead,
        template_id: effectiveTemplate,
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
        next.kind === 'final' || next.kind === 'blocked' || next.kind === 'music'
          ? { kind: next.kind === 'blocked' ? 'warn' : 'ok', text: next.message }
          : {
              kind: 'ok',
              text:
                next.kind === 'broll_export'
                  ? 'Approved. Exporting the video with its B-roll and captions…'
                  : 'Exporting the video with its captions…',
            },
      )
      onJobSubmitted()
      await refresh()
    }, kind === 'approve' ? 'Approving failed.' : 'Skipping failed.')
  }

  function seek(seconds: number) {
    bridge.seek(seconds)
  }

  const composition = state?.input.composition ?? null
  const lastJob = planner?.last_job ?? null
  const lastJobIsNewer = !!lastJob?.finished_at && (!plan || lastJob.finished_at > plan.created_at)
  const lastResult = (lastJob?.result ?? null) as { applied?: boolean; not_applied_reason?: string; run_id?: string } | null

  return (
    <section className="panel subpanel broll">
      <div className="editor-header">
        <h3>B-roll</h3>
        <span className={`badge ${provider.ready ? '' : 'warn'}`}>
          {provider.is_mock ? provider.label : `${provider.label} · ${provider.model}`}
          {provider.ready ? '' : ' — not configured'}
        </span>
      </div>
      <div className="row library-buttons">
        <button type="button" onClick={() => setLibraryWindow('video')}>
          This video&apos;s assets ({library.assets.filter((asset) => asset.library === 'project').length})
        </button>
        <button type="button" onClick={() => setLibraryWindow('general')}>
          General library ({library.assets.filter((asset) => asset.library === 'general').length})
        </button>
        {library.assets.some((asset) => !asset.described) && (
          <span className="badge warn">Some assets need a description</span>
        )}
      </div>
      {libraryWindow && (
        <LibraryWindow
          projectId={projectId}
          videoName={project.name}
          which={libraryWindow}
          onClose={() => void closeLibrary()}
        />
      )}

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

      {state && !state.available && <p className="message error">{state.unavailable_reason}</p>}
      {state && state.stale_plans.length > 0 && (
        <p className="message warn">
          This video changed since B-roll was planned on it ({state.stale_plans.length} earlier plan
          {state.stale_plans.length > 1 ? 's' : ''} kept, not applied). Plan again for the video as it is now.
        </p>
      )}

      {state?.available && planner && (
        <PlanningStatus
          state={state}
          busy={busy}
          providerReady={provider.ready}
          lastJobIsNewer={lastJobIsNewer}
          lastResult={lastResult}
          onPlan={() => void requestPlan({ replaceCurrent: !!plan })}
          onRetry={(jobId) =>
            void act(async () => {
              await retryJob(projectId, jobId)
              onJobSubmitted()
              await refresh()
            }, 'Retrying failed.')
          }
          onCancel={(jobId) => void act(() => cancelJob(projectId, jobId).then(refresh), 'Cancel failed.')}
          onApplyRun={(runId) =>
            void act(async () => {
              await applyBrollPlanningRun(projectId, selectedKey!, runId, revision)
              await refresh()
            }, 'Applying the plan failed.')
          }
          onOpenLibrary={openLibrary}
          onSkip={() => void decide('skip')}
        />
      )}

      {state?.available && props && composition && (
        <>
          <BrollTimelineTable
            captions={props.captions}
            placements={placements}
            duration={composition.duration_in_frames / composition.fps}
            playhead={playhead}
            nameOf={nameOf}
            onSeek={seek}
          />

          {plan && <ResultSummary plan={plan} run={run} editedSince={!!planner?.edited_since} duration={composition.duration_in_frames / composition.fps} />}

          {state.issues
            .filter((issue) => issue.placement_id === null || !placements.some((placement) => placement.id === issue.placement_id))
            .map((issue, index) => (
              <p key={index} className="message error">
                {issue.message}
              </p>
            ))}

          <Review
            state={state}
            busy={busy || planning}
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

          <details>
            <summary>Placements and corrections ({placements.length})</summary>
            {placements.length > 0 && (
              <ul className="broll-placements">
                {placements.map((placement) => (
                  <PlacementRow
                    key={placement.id}
                    placement={placement}
                    name={nameOf(placement.asset_id)}
                    explanation={run?.explanations[placement.id] ?? null}
                    templates={status.templates}
                    fullFrameAllowed={fitsFrame(placement.asset_id)}
                    issue={issueFor(placement.id)?.message ?? null}
                    busy={busy || planning}
                    onSeek={() => seek(placement.start)}
                    onChange={(changes) => void change(placement, changes)}
                    onRemove={() => void remove(placement)}
                  />
                ))}
              </ul>
            )}
            <h4>Add B-roll by hand</h4>
            {usable.length === 0 ? (
              <p className="hint">Import an image or a video into the library first.</p>
            ) : (
              <div className="row">
                <select value={chosenAsset?.id ?? ''} onChange={(event) => setAssetChoice(event.target.value)} aria-label="Asset">
                  {usable.map((asset) => (
                    <option key={asset.id} value={asset.id}>
                      {asset.display_name} — {describeAsset(asset)}
                    </option>
                  ))}
                </select>
                <select value={effectiveTemplate} onChange={(event) => setTemplateChoice(event.target.value)} aria-label="Template">
                  {status.templates.map((template) => (
                    <option
                      key={template.id}
                      value={template.id}
                      disabled={template.id === 'fullframe' && !chosenFits}
                      title={template.id === 'fullframe' && !chosenFits ? 'Only an asset shaped like the video can fill the frame' : undefined}
                    >
                      {template.label}
                    </option>
                  ))}
                </select>
                <button type="button" onClick={() => void addAtPlayhead()} disabled={busy || planning || !chosenAsset}>
                  Add at {formatTime(playhead)}
                </button>
              </div>
            )}
            <p className="small hint">
              Pause the preview where it should start. 3 s for an image, up to 4 s for a video, shortened to fit. A
              correction is saved as a new revision; the automatic plan stays in the history.
            </p>
          </details>

          <details>
            <summary>Plan history and details</summary>
            <div className="row">
              <button type="button" onClick={makeNewPlan} disabled={busy || planning || !provider.ready || !planner?.catalog.ready}>
                Make a new plan
              </button>
            </div>
            {state.revisions.length > 0 && (
              <ul className="small">
                {state.revisions.map((item) => (
                  <li key={item.revision}>
                    Revision {item.revision} · {ORIGIN_LABEL[item.origin] ?? item.origin} · {item.placement_count} placement
                    {item.placement_count === 1 ? '' : 's'} · {item.created_at.slice(0, 16).replace('T', ' ')} UTC
                    {item.revision === revision ? (
                      ' (shown)'
                    ) : (
                      <button
                        type="button"
                        className="link"
                        disabled={busy || planning}
                        onClick={() =>
                          void act(async () => {
                            await restoreBrollRevision(projectId, selectedKey!, item.revision, revision)
                            await refresh()
                          }, 'Restoring failed.')
                        }
                      >
                        Restore
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            )}
            {run && <RunDetails run={run} />}
            <p className="small">
              {provider.message} {provider.free_tier_note}
            </p>
            {provider.data_sent.length > 0 && (
              <p className="small">Sent per plan: {provider.data_sent.join('; ')}. Never sent: video, audio, pictures, file names, paths.</p>
            )}
          </details>
        </>
      )}


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
          Continue to B-roll after a zoom result is approved (off: approving zooms goes straight to the next step)
        </label>
        <label className="field">
          <span>How much B-roll to aim for (applies to the next plan)</span>
          <select
            value={status.settings.preference}
            disabled={busy}
            onChange={(event) =>
              void act(async () => {
                await saveBrollSettings(projectId, { preference: event.target.value })
                await loadStatus()
                setMessage({ kind: 'ok', text: 'Saved. It applies the next time a plan is made.' })
              }, 'Saving failed.')
            }
          >
            {status.preferences.map((preference) => (
              <option key={preference.id} value={preference.id} title={preference.description}>
                {preference.id}
              </option>
            ))}
          </select>
        </label>
        <p className="small">{status.preferences.find((item) => item.id === status.settings.preference)?.description}</p>
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

/** Progress, errors and the one action that matters right now. */
function PlanningStatus({
  state,
  busy,
  providerReady,
  lastJobIsNewer,
  lastResult,
  onPlan,
  onRetry,
  onCancel,
  onApplyRun,
  onOpenLibrary,
  onSkip,
}: {
  state: BrollInputState
  busy: boolean
  providerReady: boolean
  lastJobIsNewer: boolean
  lastResult: { applied?: boolean; not_applied_reason?: string; run_id?: string } | null
  onPlan: () => void
  onRetry: (jobId: string) => void
  onCancel: (jobId: string) => void
  onApplyRun: (runId: string) => void
  onOpenLibrary: () => void
  onSkip: () => void
}) {
  const planner = state.planner
  const active = planner.active_job
  const last = planner.last_job
  if (active)
    return (
      <div className="message ok">
        <p>
          <strong>Planning B-roll…</strong> {active.progress_message}
        </p>
        <button type="button" onClick={() => onCancel(active.id)} disabled={busy || active.cancel_requested}>
          Cancel
        </button>
      </div>
    )
  if (last && lastJobIsNewer && (last.status === 'failed' || last.status === 'interrupted'))
    return (
      <div className="message error">
        <p>Automatic B-roll planning failed: {last.error}</p>
        <div className="row">
          <button type="button" className="primary" onClick={() => onRetry(last.id)} disabled={busy}>
            Retry
          </button>
          {!state.plan && (
            <button type="button" onClick={onSkip} disabled={busy}>
              Skip B-roll
            </button>
          )}
        </div>
      </div>
    )
  if (last && lastJobIsNewer && last.status === 'succeeded' && lastResult && lastResult.applied === false && lastResult.run_id)
    return (
      <div className="message warn">
        <p>{lastResult.not_applied_reason}</p>
        <button type="button" onClick={() => onApplyRun(lastResult.run_id!)} disabled={busy}>
          Apply the new plan
        </button>
      </div>
    )
  if (state.plan) return null
  if (!planner.catalog.ready)
    return (
      <div className="message warn">
        <p>{planner.catalog.message}</p>
        <div className="row">
          <button type="button" className="primary" onClick={onOpenLibrary}>
            Describe assets in the library
          </button>
          <button type="button" onClick={onSkip} disabled={busy}>
            Skip B-roll
          </button>
        </div>
      </div>
    )
  if (!providerReady)
    return (
      <div className="message warn">
        <p>{planner.provider.message}</p>
        <button type="button" onClick={onSkip} disabled={busy}>
          Skip B-roll
        </button>
      </div>
    )
  return (
    <div className="row">
      <button type="button" className="primary" onClick={onPlan} disabled={busy}>
        Plan B-roll automatically
      </button>
      <span className="small hint">
        Chooses from {planner.catalog.described_count} described asset{planner.catalog.described_count === 1 ? '' : 's'}
        {planner.catalog.message ? `. ${planner.catalog.message}` : '.'}
      </span>
    </div>
  )
}

function ResultSummary({
  plan,
  run,
  editedSince,
  duration,
}: {
  plan: NonNullable<BrollInputState['plan']>
  run: BrollPlannerRun | null
  editedSince: boolean
  duration: number
}) {
  const covered = plan.placements.reduce((total, placement) => total + placement.end - placement.start, 0)
  const count = plan.placements.length
  return (
    <div className="broll-summary">
      {run ? (
        <p>
          <strong>{count === 0 ? 'No B-roll: ' : ''}</strong>
          {run.summary}
        </p>
      ) : (
        <p>{count === 0 ? 'No B-roll placed.' : 'B-roll placed by hand.'}</p>
      )}
      <p className="small">
        {count} placement{count === 1 ? '' : 's'} · {formatTime(covered)} of {formatTime(duration)} (
        {duration > 0 ? Math.round((100 * covered) / duration) : 0}%)
        {run ? ` · ${run.provider.is_mock ? run.provider.label : `planned by ${run.provider.model}`}` : ''}
        {editedSince ? ' · corrected by hand since' : ''}
        {count === 0 && run ? ' · approving keeps the video as it is' : ''}
      </p>
      {run && run.suggestions.length > 0 && (
        <details className="small">
          <summary>Ideas for assets you could add ({run.suggestions.length}) — notes only, nothing was added</summary>
          <ul>
            {run.suggestions.map((suggestion, index) => (
              <li key={index}>
                {suggestion.at_seconds !== null ? `${formatTime(suggestion.at_seconds)}: ` : ''}
                {suggestion.description}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}

function RunDetails({ run }: { run: BrollPlannerRun }) {
  return (
    <div className="small">
      <p>
        Plan {run.run_id} · {run.provider.label} {run.provider.model} · prompt {run.prompt_version} · preference{' '}
        {run.preferences.preference} · transcript revision {run.transcript.revision} ({run.transcript.segment_count}{' '}
        phrases) · {run.audit.calls.length} request{run.audit.calls.length === 1 ? '' : 's'}
        {run.audit.repair_used ? ' (one correction was needed)' : ''}.
      </p>
      <p>
        Library: {run.catalog.counts.described} described, {run.catalog.counts.undescribed} undescribed (not offered),{' '}
        {run.catalog.counts.unavailable} unavailable. {run.catalog.shortlist.note}
      </p>
      {run.normalisation.notes.length > 0 && <p>Adjustments made by the app: {run.normalisation.notes.join(' ')}</p>}
    </div>
  )
}

function PlacementRow({
  placement,
  name,
  explanation,
  templates,
  fullFrameAllowed,
  issue,
  busy,
  onSeek,
  onChange,
  onRemove,
}: {
  placement: BrollStoredPlacement
  name: string
  explanation: { rationale: string; spoken_text: string | null } | null
  templates: BrollStatus['templates']
  /** Whether this placement's asset is shaped like the video. */
  fullFrameAllowed: boolean
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
            <option
              key={item.id}
              value={item.id}
              disabled={item.id === 'fullframe' && !fullFrameAllowed && placement.template_id !== 'fullframe'}
            >
              {item.label}
            </option>
          ))}
        </select>
        <button type="button" onClick={onRemove} disabled={busy}>
          Remove
        </button>
      </div>
      {explanation && (
        <p className="small">
          {explanation.rationale}
          {explanation.spoken_text && (
            <>
              {' '}
              — <span dir="auto">“{explanation.spoken_text}”</span>
            </>
          )}
        </p>
      )}
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
            // Plans saved before a setting existed do not have it: show its default.
            const value = placement.settings[key] ?? spec.default
            // The free position's two numbers only matter for a custom position.
            if ((key === 'x_percent' || key === 'y_percent') && placement.settings.position !== 'custom') return null
            // Nor does the entrance's length without an entrance.
            if (key === 'entrance_seconds' && (placement.settings.entrance ?? 'none') === 'none') return null
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
                        {spec.choice_labels?.[choice] ?? choice.replace('_', ' ')}
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
  const hasPlan = !!state.plan
  const count = state.plan?.placements.length ?? 0

  return (
    <div className="broll-review">
      {!decided || (next && 'export' in next && next.export?.status === 'failed') ? (
        <div className="row">
          {hasPlan && (
            <button type="button" className="primary" onClick={onApprove} disabled={busy || !review?.can_approve}>
              {review?.approved ? 'Export again' : count === 0 ? 'Approve without B-roll and continue' : 'Approve and continue'}
            </button>
          )}
          {(count > 0 || !hasPlan) && (
            <button type="button" onClick={onSkip} disabled={busy}>
              {count > 0 ? 'Skip B-roll instead' : 'Skip B-roll'}
            </button>
          )}
        </div>
      ) : (
        <p className="message ok">
          {review?.approved ? (review.decision?.placement_count ? 'Approved' : 'Approved without B-roll') : 'B-roll skipped'}{' '}
          {review?.decision?.decided_at.slice(0, 16).replace('T', ' ')} UTC.
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
  if (next.kind === 'music') return <p className="small hint">{next.message}</p>
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
