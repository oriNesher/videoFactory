import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  getCuttingSettings,
  getSubtitleSettings,
  listCuttingRuns,
  openRunFolder,
  outputUrl,
  saveCuttingSettings,
  saveSubtitleSettings,
  startCuttingRun,
  startSubtitles,
  subtitleUrl,
} from './api'
import type {
  ClipSubtitles,
  CutClip,
  CutRun,
  CuttingCatalog,
  Project,
  SubtitleCatalog,
} from './types'

/**
 * The manual cutting module.
 *
 * It runs from its own button. There is no prompt to write and no plan to
 * approve: the user picks takes, sets five numbers and presses run. The AI
 * panel is a separate, optional route to the same capability.
 */

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
type Section = 'cut' | 'subtitles'

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
          {clip.source_filename}
          <span className={CLIP_STATUS_CLASS[clip.status] ?? 'badge'}>
            {CLIP_STATUS_LABELS[clip.status] ?? clip.status}
          </span>
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

  // Subtitles are made for the run's merged video. A run without one (cut
  // before merging was automatic) gets it merged first, inside the same job.
  const merged = run.subtitles?.combined
  const working = (run.active_jobs ?? []).length > 0
  const attemptStatus = working ? undefined : merged?.attempt?.status
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
          <p className="hint small mono">
            threshold {run.settings.audio_threshold} · margins{' '}
            {run.settings.margin_before_seconds}s/{run.settings.margin_after_seconds}s
            · min silence {run.settings.min_silence_seconds}s · min speech{' '}
            {run.settings.min_speech_seconds}s
          </p>
          <h4>Input loudness</h4>
          <ul className="choices">
            {run.sources.map((source) => {
              const peak = source.audio_level?.peak_ratio ?? null
              const tooQuiet =
                peak !== null && peak < run.settings.audio_threshold
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
                      below the threshold of {run.settings.audio_threshold}
                    </span>
                  )}
                </li>
              )
            })}
          </ul>

          <p className="hint small mono">
            run {run.run_id} · job {run.job_id} ·{' '}
            {run.tool_versions.auto_editor ?? 'Auto-Editor version unknown'}
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
  const [settings, setSettings] = useState<Record<string, number>>({})

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

  const loadSettings = useCallback(async () => {
    try {
      const [loaded, subtitles] = await Promise.all([
        getCuttingSettings(project.id),
        getSubtitleSettings(project.id),
      ])
      setCatalog(loaded.catalog)
      setSettings(loaded.settings)
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

  useEffect(() => {
    void loadSettings()
  }, [loadSettings])

  useEffect(() => {
    void refreshRuns()
  }, [refreshRuns, refreshToken])

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

  function setParameter(name: string, raw: string) {
    const value = Number(raw)
    setSettings((current) => ({
      ...current,
      [name]: Number.isFinite(value) ? value : current[name],
    }))
  }

  function restoreDefaults() {
    if (!catalog) return
    setSettings({ ...catalog.defaults })
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

  function renderNotice(section: Section) {
    if (!notice || notice.section !== section) return null
    return <p className={`message ${notice.kind}`}>{notice.text}</p>
  }

  return (
    <>
    <section className="panel subpanel">
      <div className="editor-header">
        <h3>Silence cutting</h3>
        <div className="row">
          <button
            type="button"
            className="primary"
            disabled={busy || available.length === 0}
            title="Trim every clip, then merge them into one video"
            onClick={() =>
              void act(
                'cut',
                async () => {
                  // Always both: the trimmed clips and one merged video.
                  const job = await startCuttingRun(
                    project.id,
                    available,
                    settings,
                    'both',
                  )
                  setWatching({ id: job.id, kind: 'cut' })
                  notify.current()
                  await refreshRuns()
                },
                'Run started. It appears under Cut runs in the Subtitles step.',
              )
            }
          >
            Run cut
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() =>
              void act(
                'cut',
                async () => {
                  const saved = await saveCuttingSettings(
                    project.id,
                    settings,
                    'both',
                    available,
                  )
                  setSettings(saved.settings)
                },
                'Settings saved.',
              )
            }
          >
            Save settings
          </button>
        </div>
      </div>

      <p className="hint small">
        Each run trims every clip and then merges them, in order, into one video.
      </p>

      {renderNotice('cut')}

      <div className="editor-header">
        <h4>Cut settings</h4>
        <button type="button" onClick={restoreDefaults} disabled={busy || !catalog}>
          Restore defaults
        </button>
      </div>

      <div className="settings-grid">
        {catalog?.parameters.map((parameter) => (
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
              value={settings[parameter.name] ?? parameter.default}
              onChange={(event) => setParameter(parameter.name, event.target.value)}
            />
          </label>
        ))}
      </div>

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
