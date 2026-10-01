import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  applyCutRecommendation,
  cutMapUrl,
  getCuttingSettings,
  getCuttingState,
  getLlmStatus,
  getPlan,
  getSubtitleSettings,
  listCuttingRuns,
  openRunFolder,
  outputUrl,
  requestCutRecommendation,
  saveCuttingSettings,
  savePlanRevision,
  saveSubtitleSettings,
  startCuttingAnalysis,
  startCuttingRun,
  startCuttingSamples,
  startSubtitles,
  subtitleUrl,
} from './api'
import CutAdvice from './CutAdvice'
import CutClips from './CutClips'
import { formatLength, formatSeconds } from './cutFormat'
import type {
  AppliedPlan,
  BoundaryOverride,
  ClipSubtitles,
  CutClip,
  CutMode,
  CutRecommendation,
  CutRun,
  CuttingCatalog,
  CuttingForm,
  CuttingSettings,
  CuttingState,
  Job,
  LlmStatus,
  Project,
  SampleRequest,
  SubtitleCatalog,
} from './types'

/**
 * The cutting module.
 *
 * Two modes. Boundary-only trimming (the default) removes the dead time around
 * each take and keeps the inside of it as recorded; full-clip silence removal
 * is the 1A Auto-Editor cut. Either runs from its own button — no prompt, no
 * plan, no approval. The AI recommendation below the settings is optional and
 * only ever fills in the form.
 */

/** How long after the last keystroke the backend is asked for the new state. */
const STATE_DEBOUNCE_MS = 300
const POLL_JOBS_MS = 1200

const POLL_ACTIVE_MS = 1500
const POLL_IDLE_MS = 10000

const RUN_STATUS_LABELS: Record<string, string> = {
  running: 'Running',
  succeeded: 'Finished',
  failed: 'Failed',
  cancelled: 'Cancelled',
}

const RUN_STATUS_CLASS: Record<string, string> = {
  running: 'badge warn',
  succeeded: 'badge ok',
  failed: 'badge bad',
  cancelled: 'badge',
}

const CLIP_STATUS_LABELS: Record<string, string> = {
  succeeded: 'Produced',
  empty: 'Nothing left',
  failed: 'Failed',
  skipped: 'Not processed',
  cancelled: 'Cancelled',
}

/** How one side of a boundary was decided, in the run's own record. */
const ORIGIN_LABELS: Record<string, string> = {
  detected: 'detected',
  manual: 'set by hand',
  kept_whole: 'whole clip kept',
  source_limit: 'no boundary found, nothing removed',
}

const CLIP_STATUS_CLASS: Record<string, string> = {
  succeeded: 'badge ok',
  empty: 'badge warn',
  failed: 'badge bad',
  skipped: 'badge',
  cancelled: 'badge',
}

function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  const whole = Math.round(seconds)
  const minutes = Math.floor(whole / 60)
  return `${minutes}:${String(whole % 60).padStart(2, '0')}`
}


/** Which section of the panel an action's outcome is shown in. */
type Section = 'cut' | 'advice' | 'subtitles'

/**
 * Starts Whisper on one run: its merged video when `outputIds` is null (the
 * job merges the clips first if needed), or the named clips. Undefined while
 * it cannot.
 */
type RequestSubtitles =
  | ((runId: string, outputIds: string[] | null) => void)
  | undefined

const SUBTITLE_STATUS: Record<string, { label: string; className: string }> = {
  running: { label: 'Transcribing…', className: 'badge warn' },
  failed: { label: 'Subtitles failed', className: 'badge bad' },
  cancelled: { label: 'Subtitles cancelled', className: 'badge' },
  interrupted: { label: 'Subtitles interrupted', className: 'badge bad' },
}

/** One playable output with its own controls. Plain <video>: no Remotion yet. */
function OutputPlayer({
  projectId,
  runId,
  outputId,
  filename,
  subtitles,
}: {
  projectId: string
  runId: string
  outputId: string
  filename: string
  subtitles?: ClipSubtitles
}) {
  const current = subtitles?.available ? subtitles.current : null

  return (
    <div className="output">
      {/* preload="metadata" keeps a long list of runs cheap to render while
          still letting the browser show a duration and seek immediately.
          Keyed on the subtitles' creation time: a browser does not reliably
          reload a <track> whose src changes, so new subtitles remount it. */}
      <video
        key={current?.created_at ?? 'none'}
        controls
        preload="metadata"
        src={outputUrl(projectId, runId, outputId)}
      >
        {current && (
          <track
            kind="subtitles"
            default
            label="Subtitles"
            srcLang={current.detected_language ?? current.language}
            src={`${subtitleUrl(projectId, runId, outputId, 'vtt')}?v=${encodeURIComponent(current.created_at)}`}
          />
        )}
      </video>
      <a
        className="download"
        href={outputUrl(projectId, runId, outputId, 'download')}
        download={filename}
      >
        Download
      </a>
      {current && (
        <a
          className="download"
          href={subtitleUrl(projectId, runId, outputId, 'srt')}
          download={current.filename}
        >
          Download SRT
        </a>
      )}
    </div>
  )
}

/** Where one output's subtitles stand, and the button that (re)makes them. */
function SubtitleControls({
  runId,
  outputId,
  subtitles,
  onSubtitles,
}: {
  runId: string
  outputId: string
  subtitles?: ClipSubtitles
  onSubtitles: RequestSubtitles
}) {
  const attempt = subtitles?.attempt
  const running = attempt?.status === 'running'
  const status = attempt ? SUBTITLE_STATUS[attempt.status] : undefined
  const current = subtitles?.available ? subtitles.current : null

  return (
    <>
      <div className="row subtitle-row">
        {current && (
          <span className="badge ok">
            Subtitles · {current.line_count} lines
          </span>
        )}
        {status && <span className={status.className}>{status.label}</span>}
        <button
          type="button"
          disabled={!onSubtitles || running}
          onClick={() => onSubtitles?.(runId, [outputId])}
        >
          {current ? 'Redo subtitles' : 'Create subtitles'}
        </button>
      </div>
      {attempt?.error && attempt.status === 'failed' && (
        <p className="message error">{attempt.error}</p>
      )}
    </>
  )
}

function ClipRow({
  projectId,
  runId,
  clip,
  subtitles,
  onSubtitles,
}: {
  projectId: string
  runId: string
  clip: CutClip
  subtitles?: ClipSubtitles
  onSubtitles: RequestSubtitles
}) {
  const saved =
    clip.source_duration_seconds && clip.duration_seconds !== null
      ? clip.source_duration_seconds - clip.duration_seconds
      : null

  return (
    <li className={clip.status === 'succeeded' ? '' : 'muted'}>
      <div className="job-header">
        <span className="job-title">
          <span className="index">{clip.order}</span>
          <bdi>{clip.source_filename}</bdi>
          <span className={CLIP_STATUS_CLASS[clip.status] ?? 'badge'}>
            {CLIP_STATUS_LABELS[clip.status] ?? clip.status}
          </span>
          {clip.boundary?.needs_review && <span className="badge warn">Needs a look</span>}
        </span>
        <span className="small mono">
          {formatDuration(clip.source_duration_seconds)} ←{' '}
          {formatDuration(clip.duration_seconds)}
          {saved !== null && saved > 0 && ` (saved ${formatDuration(saved)})`}
        </span>
      </div>

      {clip.error && (
        <p className={clip.status === 'empty' ? 'message warn' : 'message error'}>
          {clip.error}
        </p>
      )}

      {clip.boundary && (
        <p className="hint small">
          Kept <span className="mono">{formatSeconds(clip.boundary.start_seconds)}</span> to{' '}
          <span className="mono">{formatSeconds(clip.boundary.end_seconds)}</span> of the
          source (start: {ORIGIN_LABELS[clip.boundary.start_origin]}; end:{' '}
          {ORIGIN_LABELS[clip.boundary.end_origin]}).
          {clip.audio_synthesised && ' The source had no audio, so a silent track was added.'}
        </p>
      )}

      {clip.boundary?.warnings
        .filter((warning) => warning.severity === 'warn')
        .map((warning) => (
          <p className="message warn" key={warning.code}>
            {warning.message}
          </p>
        ))}

      {clip.cut_map && clip.cut_map.available && (clip.cut_map.retained?.length ?? 0) > 1 && (
        <p className="hint small mono">
          {clip.cut_map.retained?.length} kept sections:{' '}
          {clip.cut_map.retained
            ?.map(
              (interval) =>
                `${interval.source_start.toFixed(2)}–${interval.source_end.toFixed(2)}`,
            )
            .join(', ')}{' '}
          s
        </p>
      )}
      {clip.cut_map && !clip.cut_map.available && clip.status === 'succeeded' && (
        <p className="hint small">No cut map for this clip: {clip.cut_map.reason}</p>
      )}

      {clip.playable && clip.filename && (
        <>
          <OutputPlayer
            projectId={projectId}
            runId={runId}
            outputId={clip.output_id}
            filename={clip.filename}
            subtitles={subtitles}
          />
          <SubtitleControls
            runId={runId}
            outputId={clip.output_id}
            subtitles={subtitles}
            onSubtitles={onSubtitles}
          />
        </>
      )}
    </li>
  )
}

function RunCard({
  projectId,
  run,
  onSubtitles,
}: {
  projectId: string
  run: CutRun
  onSubtitles: RequestSubtitles
}) {
  const [open, setOpen] = useState(false)
  const [folderError, setFolderError] = useState('')
  // Only a full-clip run has these; a boundary-only run never used them.
  const fullSettings = run.mode === 'full_clip' ? run.settings : null

  // Subtitles are made for the run's merged video. A run without one (cut
  // before merging was automatic) gets it merged first, inside the same job.
  const merged = run.subtitles?.combined
  const working = (run.active_jobs ?? []).length > 0
  const attemptStatus = working ? undefined : merged?.attempt?.status
  const needsReview = run.clips.filter((clip) => clip.boundary?.needs_review).length
  const canTranscribe =
    run.status !== 'running' &&
    (run.combined?.playable || run.clips.some((clip) => clip.playable))

  async function showFolder() {
    setFolderError('')
    try {
      await openRunFolder(projectId, run.run_id)
    } catch (caught) {
      setFolderError(
        caught instanceof Error ? caught.message : 'Opening the folder failed.',
      )
    }
  }

  return (
    <li>
      <div className="job-header">
        <span className="job-title">
          Cut run
          <span className={RUN_STATUS_CLASS[run.status] ?? 'badge'}>
            {RUN_STATUS_LABELS[run.status] ?? run.status}
          </span>
          <span className="badge">
            {run.mode === 'boundary' ? 'Boundary-only' : 'Full-clip silence removal'}
          </span>
          {needsReview > 0 && (
            <span className="badge warn">{needsReview} need a look</span>
          )}
          {working && <span className="badge warn">Creating subtitles…</span>}
          {!working && merged?.available && merged.current && (
            <span className="badge ok">
              Subtitles · {merged.current.line_count} lines
            </span>
          )}
          {attemptStatus && attemptStatus !== 'succeeded' && (
            <span className={SUBTITLE_STATUS[attemptStatus]?.className ?? 'badge'}>
              {SUBTITLE_STATUS[attemptStatus]?.label ?? attemptStatus}
            </span>
          )}
        </span>
        <span className="row">
          {canTranscribe && (
            <button
              type="button"
              className="primary"
              disabled={!onSubtitles || working}
              title="Transcribe this run's merged video with Whisper"
              onClick={() => onSubtitles?.(run.run_id, null)}
            >
              {merged?.available ? 'Redo subtitles' : 'Create subtitles'}
            </button>
          )}
          <button type="button" onClick={() => void showFolder()}>
            Open folder
          </button>
          <button type="button" onClick={() => setOpen(!open)}>
            {open ? 'Hide details' : 'Show details'}
          </button>
        </span>
      </div>

      {folderError && <p className="message error">{folderError}</p>}

      <p className="hint small">
        {run.sources.length} sources · original length{' '}
        {formatDuration(run.source_duration_seconds)} · after cutting{' '}
        {formatDuration(run.clip_duration_seconds)} · saved{' '}
        {formatDuration(run.removed_duration_seconds)}
      </p>

      {run.error && <p className="message error">{run.error}</p>}

      {run.status !== 'succeeded' && (
        <p className="hint small">
          Incomplete run. Finished clips are kept below.
        </p>
      )}

      {run.combined && run.combined.status === 'succeeded' && (
        <div className="combined">
          <h4>
            Merged video
            {run.combined.complete === false && (
              <span className="badge warn">Clips missing</span>
            )}
            <span className="badge">
              {run.combined.strategy === 'stream_copy'
                ? 'no re-encoding'
                : 're-encoded'}
            </span>
          </h4>

          {run.combined.complete === false && (
            <p className="message warn">
              The merged video leaves out the files that had nothing left after
              cutting:{' '}
              {run.combined.excluded_clips
                .map((entry) => entry.source_filename)
                .join(', ')}
              .
            </p>
          )}

          <OutputPlayer
            projectId={projectId}
            runId={run.run_id}
            outputId={run.combined.output_id}
            filename={run.combined.filename}
            subtitles={merged}
          />
          <p className="hint small mono">
            {formatDuration(run.combined.duration_seconds)}
          </p>
        </div>
      )}

      {run.combined && run.combined.status !== 'succeeded' && run.combined.error && (
        <p className="message error">{run.combined.error}</p>
      )}

      {attemptStatus === 'failed' && merged?.attempt?.error && (
        <p className="message error">{merged.attempt.error}</p>
      )}

      {!run.combined?.playable && canTranscribe && (
        <p className="hint small">
          This run has no merged video yet. Creating subtitles merges its clips
          first.
        </p>
      )}

      {open && (
        <>
          <h4>Clips ({run.clips.length})</h4>
          <ul className="clips">
            {run.clips.map((clip) => (
              <ClipRow
                key={clip.output_id}
                projectId={projectId}
                runId={run.run_id}
                clip={clip}
                subtitles={run.subtitles?.[clip.output_id]}
                onSubtitles={onSubtitles}
              />
            ))}
          </ul>

          <h4>Settings used by this run</h4>
          {run.boundary_settings && (
            <p className="hint small mono">
              boundary-only · threshold {run.boundary_settings.detection_threshold} · padding{' '}
              {run.boundary_settings.leading_padding_seconds}s before /{' '}
              {run.boundary_settings.trailing_padding_seconds}s after · ignores sounds under{' '}
              {run.boundary_settings.min_activity_seconds}s
            </p>
          )}
          {fullSettings && (
            <p className="hint small mono">
              full-clip · threshold {fullSettings.audio_threshold} · margins{' '}
              {fullSettings.margin_before_seconds}s/{fullSettings.margin_after_seconds}s ·
              min silence {fullSettings.min_silence_seconds}s · min speech{' '}
              {fullSettings.min_speech_seconds}s
            </p>
          )}
          <p className="hint small">
            {run.settings_revision === null
              ? 'Made from settings that were not saved.'
              : `Settings revision ${run.settings_revision}.`}
            {run.applied_plan &&
              ` The settings came from AI proposal revision ${run.applied_plan.revision}.`}
            {run.plan && ` Run from plan revision ${run.plan.revision}.`}
          </p>

          <h4>Cut map</h4>
          {run.cut_map.available ? (
            <p className="hint small">
              Source-to-output times are recorded for all {run.cut_map.clip_count} clips
              {run.cut_map.sequence_rendered
                ? ', with their positions in the merged video. '
                : '; positions in the sequence are nominal until the clips are merged. '}
              <a href={cutMapUrl(projectId, run.run_id)} target="_blank" rel="noreferrer">
                Open cut-map.json
              </a>
            </p>
          ) : (
            <p className="hint small">Not available for this run. {run.cut_map.reason}</p>
          )}

          {fullSettings && <h4>Input loudness</h4>}
          <ul className="choices">
            {(fullSettings ? run.sources : []).map((source) => {
              const peak = source.audio_level?.peak_ratio ?? null
              const tooQuiet =
                peak !== null && peak < fullSettings!.audio_threshold
              return (
                <li key={source.source_id}>
                  <span className="mono small">
                    {source.filename}: peak{' '}
                    {peak === null ? 'unknown' : peak.toFixed(3)}
                    {source.audio_level?.max_db !== null &&
                      source.audio_level !== null &&
                      ` (${source.audio_level.max_db?.toFixed(1)} dB)`}
                  </span>
                  {tooQuiet && (
                    <span className="badge bad">
                      below the threshold of {fullSettings!.audio_threshold}
                    </span>
                  )}
                </li>
              )
            })}
          </ul>

          <p className="hint small mono">
            run {run.run_id} · job {run.job_id} ·{' '}
            {run.mode === 'boundary'
              ? (run.tool_versions.ffmpeg ?? 'FFmpeg version unknown')
              : (run.tool_versions.auto_editor ?? 'Auto-Editor version unknown')}
          </p>
        </>
      )}
    </li>
  )
}

type Props = {
  project: Project
  /** Bumped by the parent when a job finishes, so results appear at once. */
  refreshToken: number
  onJobSubmitted: () => void
}

export default function CuttingPanel({
  project,
  refreshToken,
  onJobSubmitted,
}: Props) {
  const [catalog, setCatalog] = useState<CuttingCatalog | null>(null)
  const [llm, setLlm] = useState<LlmStatus | null>(null)

  // The form. `settings` are the five full-clip values and `boundarySettings`
  // the boundary-only ones; they are separate sets and only `mode`'s is used.
  const [mode, setMode] = useState<CutMode>('boundary')
  const [settings, setSettings] = useState<Record<string, number>>({})
  const [boundarySettings, setBoundarySettings] = useState<Record<string, number>>({})
  const [overrides, setOverrides] = useState<Record<string, BoundaryOverride>>({})
  const [sampleSeconds, setSampleSeconds] = useState(4)
  const [appliedPlan, setAppliedPlan] = useState<AppliedPlan | null>(null)

  // What the backend says about the form as it stands: boundaries, previews,
  // the recommendation, and which of them are stale.
  const [cutState, setCutState] = useState<CuttingState | null>(null)
  const [stateError, setStateError] = useState('')

  const [subtitleCatalog, setSubtitleCatalog] = useState<SubtitleCatalog | null>(null)
  const [subtitleSettings, setSubtitleSettings] = useState<Record<string, number>>({})

  const [runs, setRuns] = useState<CutRun[]>([])
  const [busy, setBusy] = useState(false)
  // Each section shows its own outcome, next to the button that caused it.
  const [notice, setNotice] = useState<{
    section: Section
    kind: 'ok' | 'error'
    text: string
  } | null>(null)

  // The job we just submitted. A queued cut has no run directory yet, so
  // without this the list would sit at the idle poll rate until its manifest
  // appeared. A merge or subtitle job is listed on its run while active.
  const [watching, setWatching] = useState<{ id: string; kind: 'cut' | 'run' } | null>(
    null,
  )
  // A cutting-screen job (analysis, previews, recommendation, render) that was
  // submitted and has not been seen finished yet.
  const [pendingJob, setPendingJob] = useState<string | null>(null)
  // Per-clip start/end adjustment is optional: hidden until asked for.
  const [adjusting, setAdjusting] = useState(false)

  const notify = useRef(onJobSubmitted)
  useEffect(() => {
    notify.current = onJobSubmitted
  }, [onJobSubmitted])

  // Every run cuts all of the project's footage that is still on disk, in the
  // project's order. Missing files are flagged in the footage list above.
  const available = useMemo(
    () =>
      project.sources
        .filter((source) => source.exists)
        .map((source) => source.id),
    [project.sources],
  )

  const form: CuttingForm | null = useMemo(
    () =>
      catalog
        ? {
            mode,
            settings,
            boundary_settings: boundarySettings,
            overrides,
            sample_seconds: sampleSeconds,
            // Always both: the trimmed clips and one merged video.
            output_mode: 'both',
            source_ids: available,
            applied_plan: appliedPlan,
          }
        : null,
    [catalog, mode, settings, boundarySettings, overrides, sampleSeconds, available, appliedPlan],
  )
  const formKey = form ? JSON.stringify(form) : ''

  const formRef = useRef(form)
  useEffect(() => {
    formRef.current = form
  }, [form])

  function adopt(loaded: CuttingSettings) {
    setCatalog(loaded.catalog)
    setMode(loaded.mode)
    setSettings(loaded.settings)
    setBoundarySettings(loaded.boundary_settings)
    setOverrides(loaded.overrides)
    setSampleSeconds(loaded.sample_seconds)
    setAppliedPlan(loaded.applied_plan)
  }

  const loadSettings = useCallback(async () => {
    try {
      const [loaded, subtitles] = await Promise.all([
        getCuttingSettings(project.id),
        getSubtitleSettings(project.id),
      ])
      adopt(loaded)
      setSubtitleCatalog(subtitles.catalog)
      setSubtitleSettings(subtitles.settings)
    } catch (caught) {
      setNotice({
        section: 'cut',
        kind: 'error',
        text: caught instanceof Error ? caught.message : 'Loading the settings failed.',
      })
    }
  }, [project.id])

  const refreshRuns = useCallback(async () => {
    try {
      const listing = await listCuttingRuns(project.id)
      setRuns(listing.runs)
    } catch (caught) {
      setNotice({
        section: 'subtitles',
        kind: 'error',
        text: caught instanceof Error ? caught.message : 'Loading the runs failed.',
      })
    }
  }, [project.id])

  // Answers can arrive out of order while the user types; only the answer to
  // the newest question is shown.
  const stateRequest = useRef(0)
  const pendingRef = useRef(pendingJob)
  useEffect(() => {
    pendingRef.current = pendingJob
  }, [pendingJob])

  const refreshState = useCallback(async () => {
    const current = formRef.current
    if (!current) return

    const ticket = ++stateRequest.current
    try {
      const next = await getCuttingState(project.id, current)
      if (ticket !== stateRequest.current) return
      setCutState(next)
      setStateError('')

      const pending = pendingRef.current
      if (pending && Object.values(next.recent_jobs).some((job) => job.id === pending)) {
        setPendingJob(null)
        // A finished render is a new run; a finished anything may have
        // changed what the runs list shows.
        void refreshRuns()
      }
    } catch (caught) {
      if (ticket !== stateRequest.current) return
      // Usually a value that is out of range while it is being typed: keep
      // the last good state on screen and say what is wrong.
      setStateError(caught instanceof Error ? caught.message : 'Loading the state failed.')
    }
  }, [project.id, refreshRuns])

  useEffect(() => {
    void loadSettings()
    void (async () => {
      try {
        setLlm(await getLlmStatus())
      } catch {
        // The recommendation box says so on use; cutting does not need it.
      }
    })()
  }, [loadSettings])

  useEffect(() => {
    void refreshRuns()
  }, [refreshRuns, refreshToken])

  // The form changed (or a job finished somewhere): ask for the state again,
  // once the typing has paused.
  useEffect(() => {
    if (!formKey) return
    const timer = window.setTimeout(() => void refreshState(), STATE_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [formKey, refreshState, refreshToken])

  const jobsActive = (cutState?.active_jobs.length ?? 0) > 0 || pendingJob !== null

  useEffect(() => {
    if (!jobsActive) return
    const interval = window.setInterval(() => void refreshState(), POLL_JOBS_MS)
    return () => window.clearInterval(interval)
  }, [jobsActive, refreshState])

  // Derived, not stored: once the watched job has finished this stays true,
  // so it can simply be left in place until the next submit.
  const settled =
    watching === null ||
    (watching.kind === 'cut'
      ? runs.some((run) => run.job_id === watching.id && run.status !== 'running')
      : !runs.some((run) => (run.active_jobs ?? []).some((job) => job.id === watching.id)))

  const hasActive =
    runs.some(
      (run) => run.status === 'running' || (run.active_jobs ?? []).length > 0,
    ) || !settled

  useEffect(() => {
    const interval = window.setInterval(
      () => void refreshRuns(),
      hasActive ? POLL_ACTIVE_MS : POLL_IDLE_MS,
    )
    return () => window.clearInterval(interval)
  }, [refreshRuns, hasActive])

  // Any edit that moves a cut means the form no longer holds the approved
  // proposal's settings, so it stops claiming to.
  function changeMode(next: CutMode) {
    setMode(next)
    setAppliedPlan(null)
  }

  function setParameter(name: string, raw: string) {
    const value = Number(raw)
    if (!Number.isFinite(value)) return
    if (mode === 'boundary') {
      setBoundarySettings((current) => ({ ...current, [name]: value }))
    } else {
      setSettings((current) => ({ ...current, [name]: value }))
    }
    setAppliedPlan(null)
  }

  function setOverride(sourceId: string, override: BoundaryOverride | null) {
    setOverrides((current) => {
      const next = { ...current }
      if (override === null) delete next[sourceId]
      else next[sourceId] = override
      return next
    })
  }

  function restoreDefaults() {
    if (!catalog) return
    if (mode === 'boundary') setBoundarySettings({ ...catalog.boundary.defaults })
    else setSettings({ ...catalog.defaults })
    setAppliedPlan(null)
    setNotice({
      section: 'cut',
      kind: 'ok',
      text: 'Defaults restored. Press "Save settings" to keep them.',
    })
  }

  function setSubtitleParameter(name: string, raw: string) {
    const value = Number(raw)
    setSubtitleSettings((current) => ({
      ...current,
      [name]: Number.isFinite(value) ? value : current[name],
    }))
  }

  function restoreSubtitleDefaults() {
    if (!subtitleCatalog) return
    setSubtitleSettings({ ...subtitleCatalog.defaults })
    setNotice({
      section: 'subtitles',
      kind: 'ok',
      text: 'Defaults restored. Press "Save settings" to keep them.',
    })
  }

  const model = subtitleCatalog?.model

  const requestSubtitles: RequestSubtitles =
    busy || !subtitleCatalog?.installed
      ? undefined
      : (runId, outputIds) =>
          void act(
            'subtitles',
            async () => {
              const job = await startSubtitles(
                project.id,
                runId,
                outputIds,
                subtitleSettings,
              )
              setWatching({ id: job.id, kind: 'run' })
              notify.current()
              await refreshRuns()
            },
            model && !model.downloaded
              ? `Subtitles started. The first time, the Hebrew model is downloaded (${model.download}) before transcribing begins.`
              : 'Subtitles started. Progress is in Background jobs.',
          )

  async function act(
    section: Section,
    action: () => Promise<unknown>,
    success: string,
  ) {
    setBusy(true)
    setNotice(null)
    try {
      await action()
      setNotice({ section, kind: 'ok', text: success })
    } catch (caught) {
      setNotice({
        section,
        kind: 'error',
        text: caught instanceof Error ? caught.message : 'The action failed.',
      })
    }
    setBusy(false)
  }

  /**
   * Save the form, then queue a job with exactly what was saved.
   *
   * Saving first is what lets every preview, recommendation and run say which
   * settings revision produced it.
   */
  async function saveThenSubmit(
    submit: (saved: CuttingForm) => Promise<Job>,
  ): Promise<Job> {
    if (!form) throw new Error('The settings are still loading.')
    const saved = await saveCuttingSettings(project.id, form)
    adopt(saved)
    const job = await submit({
      ...form,
      mode: saved.mode,
      settings: saved.settings,
      boundary_settings: saved.boundary_settings,
      overrides: saved.overrides,
      sample_seconds: saved.sample_seconds,
      applied_plan: saved.applied_plan,
    })
    setPendingJob(job.id)
    notify.current()
    return job
  }

  function renderSamples(requests?: SampleRequest[]) {
    void act(
      'cut',
      () => saveThenSubmit((saved) => startCuttingSamples(project.id, saved, requests)),
      requests
        ? 'Rendering the preview. It appears on its clip when it is ready.'
        : 'Rendering every preview. They appear on their clips as they finish.',
    )
  }

  function renderRun() {
    void act(
      'cut',
      async () => {
        const job = await saveThenSubmit((saved) => startCuttingRun(project.id, saved))
        setWatching({ id: job.id, kind: 'cut' })
        await refreshRuns()
      },
      'Render started. It appears under Cut runs in the Subtitles step.',
    )
  }

  function askForRecommendation(text: string, feedback?: string, revisePlanId?: string) {
    void act(
      'advice',
      () =>
        saveThenSubmit((saved) =>
          requestCutRecommendation(project.id, saved, text, feedback, revisePlanId),
        ),
      'Asked. The proposal appears here when it is ready; nothing changes until you approve it.',
    )
  }

  function saveProposalEdits(
    recommendation: CutRecommendation,
    edited: Record<string, number>,
  ) {
    void act(
      'advice',
      async () => {
        // The proposal is an ordinary plan: an edit is a new revision of it,
        // which nobody has approved yet.
        const plan = await getPlan(project.id, recommendation.plan_id)
        await savePlanRevision(
          project.id,
          plan.plan_id,
          plan.summary,
          plan.actions.map((action) => ({
            ...action,
            parameters: { ...action.parameters, ...edited },
          })),
        )
        await refreshState()
      },
      'Saved as a new revision. It needs approving before it applies.',
    )
  }

  function applyProposal(recommendation: CutRecommendation, confirmModeChange: boolean) {
    if (!form) return
    void act(
      'advice',
      async () => {
        adopt(
          await applyCutRecommendation(
            project.id,
            recommendation.plan_id,
            recommendation.revision,
            form,
            confirmModeChange,
          ),
        )
      },
      'Approved and applied to the form. Nothing was rendered: render previews or the clips when you are ready.',
    )
  }

  function renderNotice(section: Section) {
    if (!notice || notice.section !== section) return null
    return <p className={`message ${notice.kind}`}>{notice.text}</p>
  }

  const boundaryMode = mode === 'boundary'
  const parameters = boundaryMode ? catalog?.boundary.parameters : catalog?.parameters
  const values = boundaryMode ? boundarySettings : settings
  const currentMode = catalog?.modes.find((entry) => entry.id === mode)
  const recent = cutState?.recent_jobs ?? {}
  // The state on screen belongs to the mode on screen (it lags by a moment).
  const stateMatches = cutState !== null && cutState.mode === mode
  const blocked = busy || available.length === 0
  const adjustedCount = Object.keys(overrides).filter((id) => available.includes(id)).length

  return (
    <>
    <section className="panel subpanel">
      <div className="editor-header">
        <h3>Cutting</h3>
        <div className="row">
          <button
            type="button"
            className="primary"
            disabled={blocked || (boundaryMode && (cutState?.invalid_count ?? 0) > 0)}
            title={
              boundaryMode
                ? 'Trim every clip at the boundaries shown below, then merge them into one video'
                : 'Remove the silences from every clip, then merge them into one video'
            }
            onClick={renderRun}
          >
            Render clips and merged video
          </button>
          <button
            type="button"
            disabled={busy || !form}
            onClick={() =>
              void act(
                'cut',
                async () => {
                  if (form) adopt(await saveCuttingSettings(project.id, form))
                },
                'Settings saved.',
              )
            }
          >
            Save settings
          </button>
        </div>
      </div>

      <fieldset className="mode-choice">
        <legend>Cutting mode</legend>
        {catalog?.modes.map((entry) => (
          <label
            key={entry.id}
            className={entry.id === mode ? 'mode-option active' : 'mode-option'}
          >
            <input
              type="radio"
              name={`cut-mode-${project.id}`}
              checked={entry.id === mode}
              disabled={busy}
              onChange={() => changeMode(entry.id)}
            />
            <span>
              <strong>{entry.label}</strong>
              {entry.id === catalog.default_mode && <span className="badge">Default</span>}
              <span className="hint small">{entry.description}</span>
              <span className="hint small mono">{entry.tool}</span>
            </span>
          </label>
        ))}
      </fieldset>

      {boundaryMode ? (
        <p className="message">
          <strong>Pauses inside each clip are kept.</strong> Each clip is cut once at
          the start and once at the end, separately from the others, and then the
          clips are joined. Detection measures loudness: it finds the first and
          last <em>sound</em>, which is usually — not always — your first and last
          word.
        </p>
      ) : (
        <p className="message warn">
          <strong>This mode also removes the pauses inside each clip.</strong> It
          changes the pacing of your delivery, cannot be adjusted by hand, and has
          no previews — the result is what you get when the render finishes.
        </p>
      )}

      {renderNotice('cut')}

      <div className="editor-header">
        <h4>{currentMode?.label ?? 'Cut'} settings</h4>
        <button type="button" onClick={restoreDefaults} disabled={busy || !catalog}>
          Restore defaults
        </button>
      </div>

      <div className={boundaryMode ? 'settings-grid boundary-grid' : 'settings-grid'}>
        {parameters?.map((parameter) => (
          <label className="field wide" key={parameter.name}>
            <span className="parameter-label">
              {parameter.label}
              <span
                className="info"
                tabIndex={0}
                aria-label={`${parameter.description} (${parameter.unit})`}
                data-tip={`${parameter.description} (${parameter.unit})`}
              >
                i
              </span>
            </span>
            <input
              type="number"
              step={parameter.step}
              min={parameter.min}
              max={parameter.max}
              value={values[parameter.name] ?? parameter.default}
              onChange={(event) => setParameter(parameter.name, event.target.value)}
            />
            <span className="hint small">{parameter.unit}</span>
          </label>
        ))}
        {boundaryMode && catalog && (
          <label className="field wide">
            <span className="parameter-label">
              {catalog.sample.label}
              <span
                className="info"
                tabIndex={0}
                aria-label={catalog.sample.description}
                data-tip={catalog.sample.description}
              >
                i
              </span>
            </span>
            <input
              type="number"
              step={catalog.sample.step}
              min={catalog.sample.min}
              max={catalog.sample.max}
              value={sampleSeconds}
              onChange={(event) => {
                const value = Number(event.target.value)
                if (Number.isFinite(value)) setSampleSeconds(value)
              }}
            />
            <span className="hint small">{catalog.sample.unit}</span>
          </label>
        )}
      </div>

      {stateError && <p className="message error">{stateError}</p>}

      {cutState?.active_jobs.map((job) => (
        <p className="hint small" key={job.id}>
          <span className="badge warn">Working</span> {job.progress_message}
          {job.progress_percent !== null && ` (${Math.round(job.progress_percent)}%)`}
        </p>
      ))}

      {boundaryMode && stateMatches && cutState && (
        <>
          <div className="editor-header">
            <h4>
              Clips ({cutState.clips.length})
              {cutState.needs_review_count > 0 && (
                <span className="badge warn">{cutState.needs_review_count} need a look</span>
              )}
              {cutState.invalid_count > 0 && (
                <span className="badge bad">{cutState.invalid_count} invalid</span>
              )}
            </h4>
            <div className="row">
              <button
                type="button"
                aria-pressed={adjusting}
                title="Set the start and end of each clip by hand, or keep a clip whole"
                onClick={() => setAdjusting(!adjusting)}
              >
                {adjusting ? 'Hide individual adjustments' : 'Adjust clips individually'}
                {adjustedCount > 0 && ` (${adjustedCount} adjusted)`}
              </button>
              <button
                type="button"
                className={cutState.analysis_needed ? 'primary' : undefined}
                disabled={blocked || jobsActive}
                title="Decode the audio at each end of every clip and find where the sound starts and ends"
                onClick={() =>
                  void act(
                    'cut',
                    async () => {
                      if (!form) return
                      const job = await startCuttingAnalysis(project.id, form)
                      setPendingJob(job.id)
                      notify.current()
                    },
                    'Finding the boundaries. They appear on each clip when it is done.',
                  )
                }
              >
                Find boundaries
              </button>
              <button
                type="button"
                disabled={blocked || jobsActive || cutState.invalid_count > 0}
                title="Opening, ending and join previews for every clip"
                onClick={() => renderSamples()}
              >
                Render all previews
              </button>
            </div>
          </div>

          <p className="hint small">
            Original {formatLength(cutState.original_seconds)}
            {cutState.retained_seconds !== null &&
              ` → ${formatLength(cutState.retained_seconds)} after trimming (removes ${formatLength(
                cutState.original_seconds - cutState.retained_seconds,
              )})`}
            {' · '}
            {cutState.settings_revision === null
              ? 'unsaved settings'
              : `settings revision ${cutState.settings_revision}`}
            {cutState.applied_plan &&
              ` · from approved AI proposal revision ${cutState.applied_plan.revision}`}
            . Rendering a preview or the clips saves the settings first.
          </p>

          {cutState.analysis_needed && (
            <p className="message">
              The boundaries have not been found with these detection settings yet.
              Press <strong>Find boundaries</strong> — or render a preview, which
              finds them on the way.
            </p>
          )}

          {recent.cut_analysis?.status === 'failed' && (
            <p className="message error">{recent.cut_analysis.error}</p>
          )}
          {recent.cut_sample?.status === 'failed' && (
            <p className="message error">{recent.cut_sample.error}</p>
          )}

          <CutClips
            projectId={project.id}
            clips={cutState.clips}
            samples={cutState.samples}
            disabled={busy}
            showAdjustments={adjusting}
            onOverride={setOverride}
            onRender={renderSamples}
          />
        </>
      )}

      {catalog && (
        <>
          <CutAdvice
            llm={llm}
            catalog={catalog}
            mode={mode}
            recommendation={cutState?.recommendation ?? null}
            lastJob={recent.cut_recommendation}
            working={(cutState?.active_jobs ?? []).some(
              (job) => job.type === 'cut_recommendation',
            )}
            disabled={blocked}
            onAsk={askForRecommendation}
            onSaveEdits={saveProposalEdits}
            onApply={applyProposal}
          />
          {renderNotice('advice')}
        </>
      )}
    </section>

    <section className="panel subpanel">
      <div className="editor-header">
        <h3>Subtitles (Whisper)</h3>
        <div className="row">
          <button
            type="button"
            disabled={busy || !subtitleCatalog}
            onClick={() =>
              void act(
                'subtitles',
                async () => {
                  const saved = await saveSubtitleSettings(project.id, subtitleSettings)
                  setSubtitleSettings(saved.settings)
                },
                'Settings saved.',
              )
            }
          >
            Save settings
          </button>
          <button
            type="button"
            onClick={restoreSubtitleDefaults}
            disabled={busy || !subtitleCatalog}
          >
            Restore defaults
          </button>
        </div>
      </div>

      {model && (
        <p className="hint small">
          Hebrew, with {model.label}, on this computer.
          {!model.downloaded &&
            ` The first use downloads the model once (${model.download}).`}
        </p>
      )}

      {subtitleCatalog && !subtitleCatalog.installed && (
        <p className="message warn">
          Whisper is not installed in the backend's Python environment. Stop the
          backend, run <code>.venv\Scripts\python.exe -m pip install -r requirements.txt</code>,
          and start it again.
        </p>
      )}

      {renderNotice('subtitles')}

      <div className="settings-grid subtitle-grid">
        {subtitleCatalog?.parameters.map((parameter) => (
          <label className="field wide" key={parameter.name}>
            <span className="parameter-label">
              {parameter.label}
              <span
                className="info"
                tabIndex={0}
                aria-label={`${parameter.description} (${parameter.unit})`}
                data-tip={`${parameter.description} (${parameter.unit})`}
              >
                i
              </span>
            </span>
            <input
              type="number"
              step={parameter.step}
              min={parameter.min}
              max={parameter.max}
              value={subtitleSettings[parameter.name] ?? parameter.default}
              onChange={(event) =>
                setSubtitleParameter(parameter.name, event.target.value)
              }
            />
          </label>
        ))}
      </div>

      <h4>Cut runs ({runs.length})</h4>

      <p className="hint small">
        Press <strong>Create subtitles</strong> on a run to transcribe its merged
        video. The SRT is saved next to the video and shown on its player.
      </p>

      {runs.length === 0 && (
        <p className="hint">No runs yet. Run a cut above first.</p>
      )}

      <ul className="jobs">
        {runs.map((run) => (
          <RunCard
            key={run.run_id}
            projectId={project.id}
            run={run}
            onSubtitles={requestSubtitles}
          />
        ))}
      </ul>
    </section>
    </>
  )
}
