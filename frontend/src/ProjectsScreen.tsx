import { useCallback, useEffect, useState } from 'react'
import { addSource, createProject, listProjects, loadProject, saveProject } from './api'
import CuttingPanel from './CuttingPanel'
import JobsPanel from './JobsPanel'
import PlanPanel from './PlanPanel'
import type { Project, ProjectListing, SourceMedia } from './types'

type Message = { kind: 'ok' | 'error'; text: string } | null

function formatDate(value: string): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('he-IL')
}

function formatSize(bytes: number | null): string {
  if (bytes === null) return ''
  const megabytes = bytes / (1024 * 1024)
  if (megabytes >= 1024) return `${(megabytes / 1024).toFixed(2)} GB`
  return `${megabytes.toFixed(1)} MB`
}

function sameOrder(a: SourceMedia[], b: SourceMedia[]): boolean {
  return a.length === b.length && a.every((item, index) => item.id === b[index].id)
}

export default function ProjectsScreen() {
  const [listing, setListing] = useState<ProjectListing | null>(null)
  const [listError, setListError] = useState('')

  const [newName, setNewName] = useState('')
  const [createMessage, setCreateMessage] = useState<Message>(null)

  const [project, setProject] = useState<Project | null>(null)
  const [draftName, setDraftName] = useState('')
  const [draftSources, setDraftSources] = useState<SourceMedia[]>([])

  const [pathInput, setPathInput] = useState('')
  const [message, setMessage] = useState<Message>(null)
  const [busy, setBusy] = useState(false)

  // Two counters, each bumped to tell the other panel to refetch now instead
  // of waiting for its next poll: a finished job may have produced a plan, and
  // a new plan job should appear in the jobs list immediately.
  const [jobsRefresh, setJobsRefresh] = useState(0)
  const [plansRefresh, setPlansRefresh] = useState(0)

  const dirty =
    project !== null &&
    (draftName !== project.name || !sameOrder(draftSources, project.sources))

  const refreshListing = useCallback(async () => {
    try {
      setListing(await listProjects())
      setListError('')
    } catch (caught) {
      setListError(caught instanceof Error ? caught.message : 'טעינת הרשימה נכשלה.')
    }
  }, [])

  useEffect(() => {
    void refreshListing()
  }, [refreshListing])

  // Browser-level guard, on top of the in-app confirmation below.
  useEffect(() => {
    if (!dirty) return

    function warn(event: BeforeUnloadEvent) {
      event.preventDefault()
    }

    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])

  function adopt(loaded: Project) {
    setProject(loaded)
    setDraftName(loaded.name)
    setDraftSources(loaded.sources)
  }

  function confirmDiscard(): boolean {
    if (!dirty) return true
    return window.confirm(
      'יש שינויים שלא נשמרו בפרויקט הנוכחי. לעזוב בלי לשמור?',
    )
  }

  async function handleCreate(event: React.FormEvent) {
    event.preventDefault()
    if (!confirmDiscard()) return

    setBusy(true)
    try {
      const created = await createProject(newName)
      adopt(created)
      setNewName('')
      setCreateMessage({ kind: 'ok', text: `הפרויקט "${created.name}" נוצר.` })
      setMessage(null)
      await refreshListing()
    } catch (caught) {
      setCreateMessage({
        kind: 'error',
        text: caught instanceof Error ? caught.message : 'יצירת הפרויקט נכשלה.',
      })
    } finally {
      setBusy(false)
    }
  }

  async function handleOpen(projectId: string) {
    if (projectId === project?.id) return
    if (!confirmDiscard()) return

    setBusy(true)
    try {
      adopt(await loadProject(projectId))
      setMessage(null)
      setPathInput('')
    } catch (caught) {
      setMessage({
        kind: 'error',
        text: caught instanceof Error ? caught.message : 'פתיחת הפרויקט נכשלה.',
      })
    } finally {
      setBusy(false)
    }
  }

  async function handleAddSource(event: React.FormEvent) {
    event.preventDefault()
    if (!project) return

    setBusy(true)
    try {
      const updated = await addSource(project.id, pathInput)
      setProject(updated)

      // Keep the unsaved order and name: re-apply the draft order to the
      // server's list and append whatever is new (the file just added).
      const draftIds = new Set(draftSources.map((source) => source.id))
      const byId = new Map(updated.sources.map((source) => [source.id, source]))
      setDraftSources([
        ...draftSources.flatMap((source) => {
          const current = byId.get(source.id)
          return current ? [current] : []
        }),
        ...updated.sources.filter((source) => !draftIds.has(source.id)),
      ])

      setPathInput('')
      setMessage({ kind: 'ok', text: 'הקובץ נוסף לפרויקט ונשמר.' })
      await refreshListing()
    } catch (caught) {
      setMessage({
        kind: 'error',
        text: caught instanceof Error ? caught.message : 'הוספת הקובץ נכשלה.',
      })
    } finally {
      setBusy(false)
    }
  }

  function move(index: number, delta: number) {
    const target = index + delta
    if (target < 0 || target >= draftSources.length) return

    const next = [...draftSources]
    ;[next[index], next[target]] = [next[target], next[index]]
    setDraftSources(next)
  }

  function remove(sourceId: string) {
    setDraftSources(draftSources.filter((source) => source.id !== sourceId))
  }

  async function handleSave() {
    if (!project) return

    setBusy(true)
    try {
      const saved = await saveProject(
        project.id,
        draftName,
        draftSources.map((source) => source.id),
      )
      adopt(saved)
      setMessage({ kind: 'ok', text: `נשמר בהצלחה ב־${formatDate(saved.updated_at)}.` })
      await refreshListing()
    } catch (caught) {
      setMessage({
        kind: 'error',
        text: caught instanceof Error ? caught.message : 'השמירה נכשלה.',
      })
    } finally {
      setBusy(false)
    }
  }

  function handleRevert() {
    if (!project) return
    if (!window.confirm('לבטל את כל השינויים שלא נשמרו?')) return
    adopt(project)
    setMessage(null)
  }

  const missingCount = draftSources.filter((source) => !source.exists).length

  return (
    <div className="projects">
      <section className="panel sidebar">
        <h2>פרויקטים</h2>

        <form className="row" onSubmit={handleCreate}>
          <input
            type="text"
            value={newName}
            placeholder="שם פרויקט חדש"
            onChange={(event) => setNewName(event.target.value)}
          />
          <button type="submit" className="primary" disabled={busy}>
            צור
          </button>
        </form>

        {createMessage && (
          <p className={`message ${createMessage.kind === 'ok' ? 'ok' : 'error'}`}>
            {createMessage.text}
          </p>
        )}

        {listError && <p className="message error">{listError}</p>}

        <ul className="project-list">
          {listing?.projects.map((entry) => (
            <li key={entry.id}>
              <button
                type="button"
                className={entry.id === project?.id ? 'entry active' : 'entry'}
                onClick={() => void handleOpen(entry.id)}
                disabled={busy || entry.error !== null}
              >
                <span className="entry-name">{entry.name}</span>
                <span className="small">
                  {entry.error
                    ? `קובץ פרויקט פגום: ${entry.error}`
                    : `${entry.source_count} מקורות · עודכן ${formatDate(entry.updated_at)}`}
                </span>
                {entry.missing_source_count > 0 && (
                  <span className="badge bad">
                    {entry.missing_source_count} קבצים חסרים
                  </span>
                )}
              </button>
            </li>
          ))}
        </ul>

        {listing && listing.projects.length === 0 && (
          <p className="hint">אין עדיין פרויקטים. צור פרויקט חדש כדי להתחיל.</p>
        )}

        {listing && (
          <p className="hint small">
            תיקיית העבודה: <span className="mono">{listing.workspace}</span>
          </p>
        )}
      </section>

      <section className="panel editor">
        {!project && <p className="hint">בחר פרויקט מהרשימה או צור פרויקט חדש.</p>}

        {project && (
          <>
            <div className="editor-header">
              <h2>
                עריכת פרויקט
                {dirty && <span className="badge warn">שינויים לא שמורים</span>}
              </h2>
              <div className="row">
                <button
                  type="button"
                  className="primary"
                  onClick={() => void handleSave()}
                  disabled={busy || !dirty}
                >
                  שמור שינויים
                </button>
                <button type="button" onClick={handleRevert} disabled={busy || !dirty}>
                  בטל שינויים
                </button>
              </div>
            </div>

            {message && (
              <p className={`message ${message.kind === 'ok' ? 'ok' : 'error'}`}>
                {message.text}
              </p>
            )}

            <label className="field">
              <span>שם הפרויקט</span>
              <input
                type="text"
                value={draftName}
                onChange={(event) => setDraftName(event.target.value)}
              />
            </label>

            <p className="hint small">
              נוצר {formatDate(project.created_at)} · נשמר לאחרונה{' '}
              {formatDate(project.updated_at)} · תיקייה:{' '}
              <span className="mono">{project.directory}</span>
            </p>

            <h3>חומרי גלם ({draftSources.length})</h3>
            <p className="hint">
              קובצי הווידאו נשארים במיקומם המקורי ומקושרים לפי נתיב מלא. Video Factory
              לא מעתיק, מזיז או משנה אותם. הדבק נתיב מלא, למשל{' '}
              <span className="mono">C:\Videos\take 1.mp4</span> (אפשר גם עם מרכאות,
              כפי ש־Explorer מעתיק).
            </p>

            <form className="row" onSubmit={handleAddSource}>
              <input
                type="text"
                value={pathInput}
                placeholder="C:\Videos\take 1.mp4"
                onChange={(event) => setPathInput(event.target.value)}
              />
              <button type="submit" disabled={busy}>
                הוסף קובץ
              </button>
            </form>

            {missingCount > 0 && (
              <p className="message error">
                {missingCount} קבצים לא נמצאו במיקומם. הפרויקט נפתח כרגיל; אפשר להסיר
                את ההפניה או להחזיר את הקובץ למקומו.
              </p>
            )}

            <ol className="sources">
              {draftSources.map((source, index) => (
                <li key={source.id} className={source.exists ? '' : 'missing'}>
                  <span className="index">{index + 1}</span>
                  <span className="details">
                    <span className="filename">
                      {source.filename}
                      {!source.exists && <span className="badge bad">קובץ חסר</span>}
                      {source.exists && source.size_bytes !== null && (
                        <span className="small"> · {formatSize(source.size_bytes)}</span>
                      )}
                    </span>
                    <span className="mono small path">{source.path}</span>
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
                      disabled={index === draftSources.length - 1}
                      title="העבר למטה"
                    >
                      ↓
                    </button>
                    <button type="button" onClick={() => remove(source.id)}>
                      הסר
                    </button>
                  </span>
                </li>
              ))}
            </ol>

            {draftSources.length === 0 && (
              <p className="hint">אין עדיין חומרי גלם בפרויקט.</p>
            )}

            <CuttingPanel
              key={project.id}
              project={project}
              refreshToken={plansRefresh}
              onJobSubmitted={() => setJobsRefresh((value) => value + 1)}
            />

            <JobsPanel
              projectId={project.id}
              refreshToken={jobsRefresh}
              onJobFinished={() => setPlansRefresh((value) => value + 1)}
            />

            <PlanPanel
              key={project.id}
              project={project}
              refreshToken={plansRefresh}
              onJobSubmitted={() => setJobsRefresh((value) => value + 1)}
            />
          </>
        )}
      </section>
    </div>
  )
}
