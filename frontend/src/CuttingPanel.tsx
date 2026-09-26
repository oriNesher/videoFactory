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
  running: 'פועלת',
  succeeded: 'הסתיימה',
  failed: 'נכשלה',
  cancelled: 'בוטלה',
}

const RUN_STATUS_CLASS: Record<string, string> = {
  running: 'badge warn',
  succeeded: 'badge ok',
  failed: 'badge bad',
  cancelled: 'badge',
}

const CLIP_STATUS_LABELS: Record<string, string> = {
  succeeded: 'הופק',
  empty: 'לא נותר תוכן',
  failed: 'נכשל',
  skipped: 'לא עובד',
  cancelled: 'בוטל',
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
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('he-IL')
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
        הורד
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
          {saved !== null && saved > 0 && ` (נחסכו ${formatDuration(saved)})`}
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
          הרצת חיתוך
          <span className={RUN_STATUS_CLASS[run.status] ?? 'badge'}>
            {RUN_STATUS_LABELS[run.status] ?? run.status}
          </span>
          <span className="small">{formatTime(run.created_at)}</span>
        </span>
        <button type="button" onClick={() => setOpen(!open)}>
          {open ? 'הסתר פרטים' : 'הצג פרטים'}
        </button>
      </div>

      <p className="hint small">
        {run.sources.length} מקורות · אורך מקורי{' '}
        {formatDuration(run.source_duration_seconds)} · אחרי חיתוך{' '}
        {formatDuration(run.clip_duration_seconds)} · נחסכו{' '}
        {formatDuration(run.removed_duration_seconds)}
      </p>

      {run.error && <p className="message error">{run.error}</p>}

      {run.status !== 'succeeded' && (
        <p className="hint small">
          ההרצה הזו אינה תוצאה מלאה. קבצים שכבר הופקו נשמרו ומסומנים למטה.
        </p>
      )}

      {run.combined && run.combined.status === 'succeeded' && (
        <div className="combined">
          <h4>
            סרטון מאוחד
            {run.combined.complete === false && (
              <span className="badge warn">חסרים קטעים</span>
            )}
            <span className="badge">
              {run.combined.strategy === 'stream_copy'
                ? 'ללא קידוד מחדש'
                : 'עם קידוד מחדש'}
            </span>
          </h4>

          {run.combined.complete === false && (
            <p className="message warn">
              הסרטון המאוחד אינו כולל את הקבצים שלא נותר בהם תוכן אחרי החיתוך:{' '}
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
          <h4>קטעים ({run.clips.length})</h4>
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

          <h4>ההגדרות של ההרצה הזו</h4>
          <p className="hint small mono">
            סף {run.settings.audio_threshold} · שוליים{' '}
            {run.settings.margin_before_seconds}s/{run.settings.margin_after_seconds}s
            · שתיקה מינ' {run.settings.min_silence_seconds}s · דיבור מינ'{' '}
            {run.settings.min_speech_seconds}s
          </p>
          <p className="hint small mono">
            מזהה הרצה {run.run_id} · משימה {run.job_id} ·{' '}
            {run.tool_versions.auto_editor ?? 'Auto-Editor לא ידוע'}
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
      setError(caught instanceof Error ? caught.message : 'טעינת ההגדרות נכשלה.')
    }
  }, [project.id])

  const refreshRuns = useCallback(async () => {
    try {
      const listing = await listCuttingRuns(project.id)
      setRuns(listing.runs)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'טעינת ההרצות נכשלה.')
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
    setMessage('הוחזרו ערכי ברירת המחדל. לחץ "שמור הגדרות" כדי לשמור אותם.')
  }

  async function act(action: () => Promise<unknown>, success: string) {
    setBusy(true)
    setError('')
    setMessage('')
    try {
      await action()
      setMessage(success)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'הפעולה נכשלה.')
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
        <h3>חיתוך שתיקות</h3>
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
              }, 'ההרצה נוספה לתור. אפשר להמשיך לעבוד בינתיים.')
            }
          >
            הרץ חיתוך
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
              }, 'ההגדרות נשמרו עם הפרויקט.')
            }
          >
            שמור הגדרות
          </button>
        </div>
      </div>

      <p className="hint small">
        חותך שתיקות לפי עוצמת האודיו עם Auto-Editor, ולפי הבחירה גם מחבר את
        הקטעים ל־MP4 אחד עם FFmpeg. קובצי המקור לא משתנים, וכל הרצה נכתבת
        לתיקייה חדשה משלה. ביטול ושחזור נמצאים בלוח <strong>משימות רקע</strong>.
      </p>

      {error && <p className="message error">{error}</p>}
      {message && <p className="message ok">{message}</p>}

      <h4>בחירת חומרי גלם וסדר עיבוד</h4>

      {available.length === 0 && (
        <p className="hint">
          אין חומרי גלם זמינים בפרויקט. הוסף קובץ למעלה כדי להתחיל.
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
            {!source.exists && <span className="badge bad">קובץ חסר</span>}
          </li>
        ))}
      </ul>

      {orderedSelection.length > 0 && (
        <>
          <p className="hint small">
            סדר העיבוד והחיבור. אפשר לשנות אותו בלי לשנות את סדר חומרי הגלם
            בפרויקט.
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
                    title="העבר למעלה"
                  >
                    ↑
                  </button>
                  <button
                    type="button"
                    onClick={() => move(index, 1)}
                    disabled={index === orderedSelection.length - 1}
                    title="העבר למטה"
                  >
                    ↓
                  </button>
                  <button type="button" onClick={() => toggle(source.id)}>
                    הסר
                  </button>
                </span>
              </li>
            ))}
          </ol>
        </>
      )}

      <div className="editor-header">
        <h4>הגדרות חיתוך</h4>
        <button type="button" onClick={restoreDefaults} disabled={busy || !catalog}>
          החזר ברירות מחדל
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
              {parameter.maps_to} · טווח {parameter.min}–{parameter.max} · ברירת
              מחדל {parameter.default}
            </span>
          </label>
        ))}
      </div>

      <h4>מה להפיק</h4>
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

      <h4>הרצות קודמות ({runs.length})</h4>

      {runs.length === 0 && (
        <p className="hint">עדיין לא בוצעו הרצות חיתוך בפרויקט הזה.</p>
      )}

      <ul className="jobs">
        {runs.map((run) => (
          <RunCard key={run.run_id} projectId={project.id} run={run} />
        ))}
      </ul>
    </section>
  )
}
