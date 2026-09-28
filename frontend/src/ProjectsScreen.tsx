import { useCallback, useEffect, useState } from 'react'
import {
  addSourceDirectory,
  createProject,
  listProjects,
  loadProject,
  openSourceInVlc,
  saveProject,
} from './api'
import CuttingPanel from './CuttingPanel'
import JobsPanel from './JobsPanel'
import PlanPanel from './PlanPanel'
import type { Project, ProjectListing, SourceMedia } from './types'

type Message = { kind: 'ok' | 'error'; text: string } | null

function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return ''
  const whole = Math.round(seconds)
  const minutes = Math.floor(whole / 60)
  return `${minutes}:${String(whole % 60).padStart(2, '0')}`
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
  const [draftSources, setDraftSources] = useState<SourceMedia[]>([])

  // Only used by the fallback below, which appears if the native folder dialog
  // cannot be opened on this machine.
  const [pathInput, setPathInput] = useState('')
  const [needsPathFallback, setNeedsPathFallback] = useState(false)

  const [message, setMessage] = useState<Message>(null)
  const [busy, setBusy] = useState(false)

  // Two counters, each bumped to tell the other panel to refetch now instead
  // of waiting for its next poll: a finished job may have produced a plan, and
  // a new plan job should appear in the jobs list immediately.
  const [jobsRefresh, setJobsRefresh] = useState(0)
  const [plansRefresh, setPlansRefresh] = useState(0)

  const dirty =
    project !== null &&
    !sameOrder(draftSources, project.sources)

  const refreshListing = useCallback(async () => {
    try {
      setListing(await listProjects())
      setListError('')
    } catch (caught) {
      setListError(caught instanceof Error ? caught.message : 'Loading the video list failed.')
    }
  }, [])

  useEffect(() => {
    void refreshListing()
  }, [refreshListing])

  useEffect(() => {
    document.title = project ? `${project.name} video` : 'Video Factory'
  }, [project])

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
    setDraftSources(loaded.sources)
  }

  function confirmDiscard(): boolean {
    if (!dirty) return true
    return window.confirm(
      'There are unsaved changes in the current video. Leave without saving?',
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
      setCreateMessage({ kind: 'ok', text: `Video "${created.name}" was created.` })
      setMessage(null)
      await refreshListing()
    } catch (caught) {
      setCreateMessage({
        kind: 'error',
        text: caught instanceof Error ? caught.message : 'Creating the video failed.',
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
        text: caught instanceof Error ? caught.message : 'Opening the video failed.',
      })
    } finally {
      setBusy(false)
    }
  }

  /**
   * Fold the server's new source list into the draft.
   *
   * Keeps the unsaved order and name: the draft order is re-applied to the
   * server's list, and whatever is new is appended in the order it arrived.
   */
  function adoptNewSources(updated: Project) {
    setProject(updated)

    const draftIds = new Set(draftSources.map((source) => source.id))
    const byId = new Map(updated.sources.map((source) => [source.id, source]))
    setDraftSources([
      ...draftSources.flatMap((source) => {
        const current = byId.get(source.id)
        return current ? [current] : []
      }),
      ...updated.sources.filter((source) => !draftIds.has(source.id)),
    ])
  }

  /**
   * Add a folder of clips.
   *
   * With no path the backend opens the machine's own folder dialog, so this
   * request stays open for as long as that dialog is on screen. `path` is only
   * passed by the fallback field, which appears if the dialog is unavailable.
   */
  async function handleAddFolder(path?: string) {
    if (!project) return

    setBusy(true)
    setMessage(null)
    try {
      const result = await addSourceDirectory(project.id, path)
      if (result.cancelled || !result.project) {
        setBusy(false)
        return
      }

      adoptNewSources(result.project)
      setPathInput('')
      setNeedsPathFallback(false)

      const added = result.added ?? []
      const notes: string[] = []
      if (result.duplicates?.length) {
        notes.push(`${result.duplicates.length} already in the video`)
      }
      if (result.ignored?.length) {
        notes.push(`${result.ignored.length} non-video files ignored`)
      }
      for (const failure of result.failed ?? []) {
        notes.push(`${failure.filename}: ${failure.error}`)
      }

      setMessage({
        kind: added.length > 0 ? 'ok' : 'error',
        text:
          added.length > 0
            ? `Added ${added.length} clips from ${result.directory}, ordered by name.` +
              (notes.length > 0 ? ` (${notes.join('; ')})` : '')
            : `Nothing new was added from ${result.directory}.` +
              (notes.length > 0 ? ` ${notes.join('; ')}.` : ''),
      })
      await refreshListing()
    } catch (caught) {
      const text =
        caught instanceof Error ? caught.message : 'Adding the folder failed.'
      // The dialog needs a desktop session and Tk. Where it cannot open, the
      // path field is the only way left to add footage, so reveal it.
      if (text.includes('Paste the folder path instead')) {
        setNeedsPathFallback(true)
      }
      setMessage({ kind: 'error', text })
    } finally {
      setBusy(false)
    }
  }

  async function handleOpenInVlc(sourceId: string) {
    try {
      await openSourceInVlc(project!.id, sourceId)
    } catch (caught) {
      setMessage({
        kind: 'error',
        text: caught instanceof Error ? caught.message : 'Opening VLC failed.',
      })
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
        project.name,
        draftSources.map((source) => source.id),
      )
      adopt(saved)
      setMessage({ kind: 'ok', text: 'Saved.' })
      await refreshListing()
    } catch (caught) {
      setMessage({
        kind: 'error',
        text: caught instanceof Error ? caught.message : 'Saving failed.',
      })
    } finally {
      setBusy(false)
    }
  }

  function handleRevert() {
    if (!project) return
    if (!window.confirm('Discard all unsaved changes?')) return
    adopt(project)
    setMessage(null)
  }

  const missingCount = draftSources.filter((source) => !source.exists).length

  return (
    <div className="projects">
      <section className="panel sidebar">
        <h2>Videos</h2>

        <form className="row" onSubmit={handleCreate}>
          <input
            type="text"
            value={newName}
            placeholder="New video name"
            onChange={(event) => setNewName(event.target.value)}
          />
          <button type="submit" className="primary" disabled={busy}>
            Create
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
                    ? `Corrupt video file: ${entry.error}`
                    : `${entry.source_count} sources`}
                </span>
                {entry.missing_source_count > 0 && (
                  <span className="badge bad">
                    {entry.missing_source_count} files missing
                  </span>
                )}
              </button>
            </li>
          ))}
        </ul>

        {listing && listing.projects.length === 0 && (
          <p className="hint">No videos yet. Create one to get started.</p>
        )}
      </section>

      <section className="panel editor">
        {!project && <p className="hint">Pick a video from the list, or create a new one.</p>}

        {project && (
          <>
            <div className="editor-header">
              <h2>
                {project.name} video
                {dirty && <span className="badge warn">Unsaved changes</span>}
              </h2>
              <div className="row">
                <button
                  type="button"
                  className="primary"
                  onClick={() => void handleSave()}
                  disabled={busy || !dirty}
                >
                  Save changes
                </button>
                <button type="button" onClick={handleRevert} disabled={busy || !dirty}>
                  Discard changes
                </button>
              </div>
            </div>

            {message && (
              <p className={`message ${message.kind === 'ok' ? 'ok' : 'error'}`}>
                {message.text}
              </p>
            )}

            <div className="editor-header">
              <h3>Footage ({draftSources.length})</h3>
              <button
                type="button"
                className="primary"
                disabled={busy}
                onClick={() => void handleAddFolder()}
              >
                {busy ? 'Working…' : 'Upload folder…'}
              </button>
            </div>

            {needsPathFallback && (
              <form
                className="row"
                onSubmit={(event) => {
                  event.preventDefault()
                  void handleAddFolder(pathInput)
                }}
              >
                <input
                  type="text"
                  value={pathInput}
                  placeholder="C:\Videos\shoot-01"
                  onChange={(event) => setPathInput(event.target.value)}
                />
                <button type="submit" disabled={busy || !pathInput.trim()}>
                  Add this folder
                </button>
              </form>
            )}

            {missingCount > 0 && (
              <p className="message error">
                {missingCount} files are missing. Remove them or put them back.
              </p>
            )}

            <ol className="sources">
              {draftSources.map((source, index) => (
                <li key={source.id} className={source.exists ? '' : 'missing'}>
                  <span className="index">{index + 1}</span>
                  <span className="details">
                    <span className="filename">
                      {source.filename}
                      {!source.exists && <span className="badge bad">File missing</span>}
                      {source.exists && source.duration_seconds != null && (
                        <span className="small mono">
                          {' '}
                          · {formatDuration(source.duration_seconds)}
                        </span>
                      )}
                    </span>
                  </span>
                  <span className="row">
                    <button
                      type="button"
                      className="vlc"
                      onClick={() => void handleOpenInVlc(source.id)}
                      disabled={!source.exists}
                      title="Open in VLC"
                    >
                      VLC
                    </button>
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
                      disabled={index === draftSources.length - 1}
                      title="Move down"
                    >
                      ↓
                    </button>
                    <button type="button" onClick={() => remove(source.id)}>
                      Remove
                    </button>
                  </span>
                </li>
              ))}
            </ol>

            {draftSources.length === 0 && (
              <p className="hint">No footage yet.</p>
            )}

            <CuttingPanel
              key={`cutting-${project.id}`}
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
              key={`plan-${project.id}`}
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
