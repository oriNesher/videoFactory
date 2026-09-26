import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  getCuttingSettings,
  listCuttingRuns,
  outputUrl,
  saveCuttingSettings,
  startCuttingRun,
} from './api'
import type {
  CutClip,
  CutRun,
  CuttingCatalog,
  Project,
  SourceMedia,
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

function formatSize(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '—'
  const megabytes = bytes / (1024 * 1024)
  if (megabytes >= 1024) return `${(megabytes / 1024).toFixed(2)} GB`
  return `${megabytes.toFixed(1)} MB`
}

function formatTime(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('en-US')
}

/** One playable output with its own controls. Plain <video>: no Remotion yet. */
function OutputPlayer({
  projectId,
  runId,
  outputId,
  filename,
}: {
  projectId: string
  runId: string
  outputId: string
  filename: string
}) {
  return (
    <div className="output">
      {/* preload="metadata" keeps a long list of runs cheap to render while
          still letting the browser show a duration and seek immediately. */}
      <video
        controls
        preload="metadata"
        src={outputUrl(projectId, runId, outputId)}
      />
      <a
        className="download"
        href={outputUrl(projectId, runId, outputId, 'download')}
        download={filename}
      >
        Download
      </a>
    </div>
  )
}

function ClipRow({
  projectId,
  runId,
  clip,
}: {
  projectId: string
  runId: string
  clip: CutClip
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
          />
          <p className="hint small mono">
            {clip.filename} · {formatSize(clip.size_bytes)}
            {clip.video?.width ? ` · ${clip.video.width}×${clip.video.height}` : ''}
          </p>
        </>
      )}
    </li>
  )
}

function RunCard({ projectId, run }: { projectId: string; run: CutRun }) {
  const [open, setOpen] = useState(run.status === 'running')

  return (
    <li>
      <div className="job-header">
        <span className="job-title">
          Cut run
          <span className={RUN_STATUS_CLASS[run.status] ?? 'badge'}>
            {RUN_STATUS_LABELS[run.status] ?? run.status}
          </span>
          <span className="small">{formatTime(run.created_at)}</span>
        </span>
        <button type="button" onClick={() => setOpen(!open)}>
          {open ? 'Hide details' : 'Show details'}
        </button>
      </div>

      <p className="hint small">
        {run.sources.length} sources · original length{' '}
        {formatDuration(run.source_duration_seconds)} · after cutting{' '}
        {formatDuration(run.clip_duration_seconds)} · saved{' '}
        {formatDuration(run.removed_duration_seconds)}
      </p>

      {run.error && <p className="message error">{run.error}</p>}

      {run.status !== 'succeeded' && (
        <p className="hint small">
          This run is not a complete result. Files that were already produced are
          kept and marked below.
        </p>
      )}

      {run.combined && run.combined.status === 'succeeded' && (
        <div className="combined">
          <h4>
            Combined video
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
              The combined video leaves out the files that had nothing left after
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
          />
          <p className="hint small mono">
            {run.combined.filename} · {formatDuration(run.combined.duration_seconds)}{' '}
            · {formatSize(run.combined.size_bytes)}
          </p>
        </div>
      )}

      {run.combined && run.combined.status !== 'succeeded' && run.combined.error && (
        <p className="message error">{run.combined.error}</p>
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
  const [outputMode, setOutputMode] = useState('both')
  const [selected, setSelected] = useState<string[]>([])

  const [runs, setRuns] = useState<CutRun[]>([])
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)

  // The job we just submitted. A queued job has no run directory yet, so
  // without this the list would sit at the idle poll rate until its manifest
  // appeared. Cleared once that job's run exists and has stopped running.
  const [watching, setWatching] = useState<string | null>(null)

  const notify = useRef(onJobSubmitted)
  useEffect(() => {
    notify.current = onJobSubmitted
  }, [onJobSubmitted])

  // Sources that can actually be cut. A missing file is shown as unselectable
  // rather than hidden, so the reason is visible.
  const available = useMemo(
    () => project.sources.filter((source) => source.exists),
    [project.sources],
  )

  const loadSettings = useCallback(async () => {
    try {
      const loaded = await getCuttingSettings(project.id)
      setCatalog(loaded.catalog)
      setSettings(loaded.settings)
      setOutputMode(loaded.output_mode)
      setSelected(loaded.source_ids)
      setError('')
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Loading the settings failed.')
    }
  }, [project.id])

  const refreshRuns = useCallback(async () => {
    try {
      const listing = await listCuttingRuns(project.id)
      setRuns(listing.runs)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Loading the runs failed.')
    }
  }, [project.id])

  useEffect(() => {
    void loadSettings()
  }, [loadSettings])

  useEffect(() => {
    void refreshRuns()
  }, [refreshRuns, refreshToken])

  // Derived, not stored: once the watched job's run has stopped running this
  // stays true, so the id can simply be left in place until the next submit.
  const settled =
    watching === null ||
    runs.some((run) => run.job_id === watching && run.status !== 'running')

  const hasActive = runs.some((run) => run.status === 'running') || !settled

  useEffect(() => {
    const interval = window.setInterval(
      () => void refreshRuns(),
      hasActive ? POLL_ACTIVE_MS : POLL_IDLE_MS,
    )
    return () => window.clearInterval(interval)
  }, [refreshRuns, hasActive])

  function toggle(sourceId: string) {
    setSelected((current) =>
      current.includes(sourceId)
        ? current.filter((id) => id !== sourceId)
        : [...current, sourceId],
    )
  }

  function move(index: number, delta: number) {
    const target = index + delta
    if (target < 0 || target >= selected.length) return

    const next = [...selected]
    ;[next[index], next[target]] = [next[target], next[index]]
    setSelected(next)
  }

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
    setMessage('Defaults restored. Press "Save settings" to keep them.')
  }

  async function act(action: () => Promise<unknown>, success: string) {
    setBusy(true)
    setError('')
    setMessage('')
    try {
      await action()
      setMessage(success)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'The action failed.')
    } finally {
      setBusy(false)
    }
  }

  const byId = new Map(project.sources.map((source) => [source.id, source]))
  const orderedSelection = selected
    .map((id) => byId.get(id))
    .filter((source): source is SourceMedia => source !== undefined)

  return (
    <section className="panel subpanel">
      <div className="editor-header">
        <h3>Silence cutting</h3>
        <div className="row">
          <button
            type="button"
            className="primary"
            disabled={busy || selected.length === 0}
            onClick={() =>
              void act(async () => {
                const job = await startCuttingRun(
                  project.id,
                  selected,
                  settings,
                  outputMode,
                )
                setWatching(job.id)
                notify.current()
                await refreshRuns()
              }, 'The run was queued. You can keep working meanwhile.')
            }
          >
            Run cut
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() =>
              void act(async () => {
                const saved = await saveCuttingSettings(
                  project.id,
                  settings,
                  outputMode,
                  selected,
                )
                setSettings(saved.settings)
              }, 'The settings were saved with the project.')
            }
          >
            Save settings
          </button>
        </div>
      </div>

      <p className="hint small">
        Cuts silence by audio loudness with Auto-Editor and, if you ask for it,
        joins the clips into one MP4 with FFmpeg. Your source files are never
        changed, and every run writes to its own new directory. Cancel and re-run
        live in the <strong>Background jobs</strong> panel.
      </p>

      {error && <p className="message error">{error}</p>}
      {message && <p className="message ok">{message}</p>}

      <h4>Footage and processing order</h4>

      {available.length === 0 && (
        <p className="hint">
          No footage is available in this project. Add a folder above to get started.
        </p>
      )}

      <ul className="choices">
        {project.sources.map((source) => (
          <li key={source.id}>
            <label className="choice">
              <input
                type="checkbox"
                checked={selected.includes(source.id)}
                disabled={!source.exists}
                onChange={() => toggle(source.id)}
              />
              <span>{source.filename}</span>
            </label>
            {!source.exists && <span className="badge bad">File missing</span>}
          </li>
        ))}
      </ul>

      {orderedSelection.length > 0 && (
        <>
          <p className="hint small">
            The processing and join order. You can change it without changing the
            order of the footage in the project.
          </p>
          <ol className="sources compact">
            {orderedSelection.map((source, index) => (
              <li key={source.id}>
                <span className="index">{index + 1}</span>
                <span className="details">
                  <span className="filename">{source.filename}</span>
                </span>
                <span className="row">
                  <button
                    type="button"
                    onClick={() => move(index, -1)}
                    disabled={index === 0}
                    title="Move up"
                  >
                    ↑
                  </button>
                  <button
                    type="button"
                    onClick={() => move(index, 1)}
                    disabled={index === orderedSelection.length - 1}
                    title="Move down"
                  >
                    ↓
                  </button>
                  <button type="button" onClick={() => toggle(source.id)}>
                    Remove
                  </button>
                </span>
              </li>
            ))}
          </ol>
        </>
      )}

      <div className="editor-header">
        <h4>Cut settings</h4>
        <button type="button" onClick={restoreDefaults} disabled={busy || !catalog}>
          Restore defaults
        </button>
      </div>

      <div className="settings-grid">
        {catalog?.parameters.map((parameter) => (
          <label className="field wide" key={parameter.name}>
            <span>
              {parameter.label} <span className="small">({parameter.unit})</span>
            </span>
            <input
              type="number"
              step={parameter.step}
              min={parameter.min}
              max={parameter.max}
              value={settings[parameter.name] ?? parameter.default}
              onChange={(event) => setParameter(parameter.name, event.target.value)}
            />
            <span className="hint small">{parameter.description}</span>
            <span className="hint small mono">
              {parameter.maps_to} · range {parameter.min}–{parameter.max} ·
              default {parameter.default}
            </span>
          </label>
        ))}
      </div>

      <h4>What to produce</h4>
      <ul className="choices">
        {catalog?.output_modes.map((mode) => (
          <li key={mode.id}>
            <label className="choice">
              <input
                type="radio"
                name="output-mode"
                checked={outputMode === mode.id}
                onChange={() => setOutputMode(mode.id)}
              />
              <span>{mode.label}</span>
            </label>
            <span className="hint small">{mode.description}</span>
          </li>
        ))}
      </ul>

      <h4>Previous runs ({runs.length})</h4>

      {runs.length === 0 && (
        <p className="hint">No cut runs have been made in this project yet.</p>
      )}

      <ul className="jobs">
        {runs.map((run) => (
          <RunCard key={run.run_id} projectId={project.id} run={run} />
        ))}
      </ul>
    </section>
  )
}
