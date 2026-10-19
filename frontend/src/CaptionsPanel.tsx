import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  cancelJob,
  captionExportUrl,
  downloadWhisperModel,
  getCaptionCatalog,
  getCaptionInput,
  getCaptionRevision,
  getPipeline,
  listCaptionInputs,
  prepareCaptionRenderer,
  regroupCaptions,
  saveCaptionRevision,
  saveCaptionStyle,
  selectCaptionInput,
  startCaptionExport,
  startTranscription,
  validateCaptions,
} from './api'
import { toPreviewCaptions } from './captionFormat'
import CaptionStyleForm from './CaptionStyleForm'
import CaptionTextTable from './CaptionTextTable'
import type { CaptionStyle } from './captions/types'
import { activeIndex, captionFrameRanges } from './captions/timing'
import { usePreviewBridge } from './previewBridge'
import type {
  CaptionCatalog,
  CaptionCheck,
  CaptionGrouping,
  CaptionInputState,
  CaptionInputsResponse,
  CaptionJobSummary,
  CaptionRevision,
  EditableCaption,
  Project,
  TranscriptionSettings,
} from './types'

type Message = { kind: 'ok' | 'error' | 'warn'; text: string } | null

const RTL_LANGUAGES = new Set(['he', 'ar', 'fa', 'ur', 'yi'])

function errorText(caught: unknown, fallback: string): string {
  return caught instanceof Error ? caught.message : fallback
}

function sameJson(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b)
}

function JobLine({ job, onCancel }: { job: CaptionJobSummary; onCancel: (id: string) => void }) {
  return (
    <li className="caption-job">
      <div className="job-header">
        <strong>{job.type_label}</strong>
        <span className="badge">{job.cancel_requested ? 'Stopping…' : job.status}</span>
        <button type="button" onClick={() => onCancel(job.id)} disabled={job.cancel_requested}>
          Cancel
        </button>
      </div>
      <p className="small">{job.progress_message}</p>
      {job.progress_percent !== null && (
        <div className="progress" aria-label="progress">
          <div className="progress-bar" style={{ width: `${job.progress_percent}%` }} />
        </div>
      )}
    </li>
  )
}

export default function CaptionsPanel({
  project,
  onJobSubmitted,
}: {
  project: Project
  onJobSubmitted: () => void
}) {
  const projectId = project.id
  const [catalog, setCatalog] = useState<CaptionCatalog | null>(null)
  const [inputs, setInputs] = useState<CaptionInputsResponse | null>(null)
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [state, setState] = useState<CaptionInputState | null>(null)
  const [message, setMessage] = useState<Message>(null)
  const [busy, setBusy] = useState(false)

  const [settings, setSettings] = useState<TranscriptionSettings | null>(null)
  const [transcriptId, setTranscriptId] = useState<string | null>(null)
  const [revision, setRevision] = useState<CaptionRevision | null>(null)
  const [latestRevision, setLatestRevision] = useState<number | null>(null)
  const [draft, setDraft] = useState<EditableCaption[]>([])
  const [check, setCheck] = useState<CaptionCheck | null>(null)
  const [grouping, setGrouping] = useState<CaptionGrouping | null>(null)
  const [style, setStyle] = useState<CaptionStyle | null>(null)
  const [savedStyle, setSavedStyle] = useState<CaptionStyle | null>(null)
  const [frame, setFrame] = useState(0)

  // The shared preview beside the slide plays the video; this slide feeds it
  // the unsaved draft and follows its position.
  const bridge = usePreviewBridge()
  useEffect(() => bridge.subscribeFrame((value) => setFrame(value)), [bridge])
  const activeJobIds = useRef<string>('')

  const dirty = revision !== null && !sameJson(draft, revision.captions)
  const styleDirty = style !== null && savedStyle !== null && !sameJson(style, savedStyle)

  // --- loading ---------------------------------------------------------------

  const loadInputs = useCallback(async () => {
    const [loadedCatalog, loadedInputs] = await Promise.all([
      getCaptionCatalog(projectId),
      listCaptionInputs(projectId),
    ])
    setCatalog(loadedCatalog)
    setInputs(loadedInputs)
    return { loadedCatalog, loadedInputs }
  }, [projectId])

  useEffect(() => {
    let cancelled = false
    loadInputs()
      .then(({ loadedCatalog, loadedInputs }) => {
        if (cancelled) return
        setSettings(loadedCatalog.transcription_defaults)
        setStyle(loadedInputs.style)
        setSavedStyle(loadedInputs.style)
        setGrouping(loadedInputs.grouping)
      })
      .catch((caught) => setMessage({ kind: 'error', text: errorText(caught, 'Loading captions failed.') }))
    return () => {
      cancelled = true
    }
  }, [loadInputs])

  // The video is always the accepted cut's merged video: captions are a stage
  // of the factory, not a tool pointed at any file. It follows the pipeline,
  // so re-accepting a different cut moves the captions to it.
  const loadCutKey = useCallback(async () => {
    try {
      const pipeline = await getPipeline(projectId)
      const cutting = pipeline.stages.find((stage) => stage.id === 'cutting')
      const key = cutting?.done && typeof cutting.acceptance?.key === 'string' ? cutting.acceptance.key : null
      setSelectedKey((current) => (current === key ? current : key))
    } catch (caught) {
      setMessage({ kind: 'error', text: errorText(caught, 'Loading the accepted cut failed.') })
    }
  }, [projectId])

  useEffect(() => {
    void loadCutKey()
    const interval = window.setInterval(() => void loadCutKey(), 8000)
    return () => window.clearInterval(interval)
  }, [loadCutKey])

  // Keep the backend's "selected video" in step, for anything that reads it.
  useEffect(() => {
    if (selectedKey && inputs && inputs.selected !== selectedKey) {
      void selectCaptionInput(projectId, selectedKey).catch(() => undefined)
    }
  }, [projectId, selectedKey, inputs])

  const refreshState = useCallback(async () => {
    if (!selectedKey) return
    try {
      const loaded = await getCaptionInput(projectId, selectedKey)
      setState(loaded)

      // A caption job just finished: catalogs (model downloaded, renderer
      // ready) and the transcript list may have changed.
      const ids = loaded.active_jobs.map((job) => job.id).join(',')
      if (activeJobIds.current && ids !== activeJobIds.current) {
        const { loadedCatalog } = await loadInputs()
        setCatalog(loadedCatalog)
        const finished = Object.values(loaded.recent_jobs).find(
          (job) => job.type === 'caption_transcription' && job.status === 'succeeded',
        )
        const created = finished?.result?.transcript_id
        if (typeof created === 'string' && loaded.transcripts.some((t) => t.id === created)) {
          setTranscriptId(created)
        }
        for (const job of Object.values(loaded.recent_jobs)) {
          if (job.status === 'failed' && activeJobIds.current.includes(job.id)) {
            setMessage({ kind: 'error', text: `${job.type_label} failed: ${job.error}` })
          }
        }
      }
      activeJobIds.current = ids
    } catch (caught) {
      setMessage({ kind: 'error', text: errorText(caught, 'Loading the video failed.') })
    }
  }, [projectId, selectedKey, loadInputs])

  useEffect(() => {
    setState(null)
    setTranscriptId(null)
    setRevision(null)
    setDraft([])
    activeJobIds.current = ''
    void refreshState()
  }, [selectedKey, refreshState])

  const hasActiveJobs = (state?.active_jobs.length ?? 0) > 0
  useEffect(() => {
    const interval = window.setInterval(() => void refreshState(), hasActiveJobs ? 1500 : 8000)
    return () => window.clearInterval(interval)
  }, [refreshState, hasActiveJobs])

  // Pick a transcript: keep the chosen one, else the newest current one.
  useEffect(() => {
    if (!state) return
    if (transcriptId && state.transcripts.some((t) => t.id === transcriptId)) return
    const current = state.transcripts.find((t) => !t.stale) ?? state.transcripts[0]
    setTranscriptId(current?.id ?? null)
  }, [state, transcriptId])

  const transcript = state?.transcripts.find((t) => t.id === transcriptId) ?? null
  const transcriptLatest = transcript?.latest_revision ?? null

  const openRevision = useCallback(
    async (number: number) => {
      if (!transcriptId) return
      try {
        const loaded = await getCaptionRevision(projectId, transcriptId, number)
        setRevision(loaded)
        setDraft(loaded.captions)
        setGrouping(loaded.grouping)
      } catch (caught) {
        setMessage({ kind: 'error', text: errorText(caught, 'Opening the captions failed.') })
      }
    },
    [projectId, transcriptId],
  )

  // Open the latest revision when the transcript changes or gains a revision
  // (a regroup, a save), unless there are unsaved edits to protect.
  useEffect(() => {
    if (!transcriptId || transcriptLatest === null) {
      setRevision(null)
      setDraft([])
      return
    }
    if (transcriptLatest === latestRevision && revision?.transcript_id === transcriptId) return
    setLatestRevision(transcriptLatest)
    if (!dirty) void openRevision(transcriptLatest)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [transcriptId, transcriptLatest])

  // Validate the draft on the backend, shortly after typing stops.
  useEffect(() => {
    if (!transcriptId || !revision) {
      setCheck(null)
      return
    }
    const timer = window.setTimeout(() => {
      validateCaptions(projectId, transcriptId, draft)
        .then(setCheck)
        .catch(() => setCheck(null))
    }, 350)
    return () => window.clearTimeout(timer)
  }, [projectId, transcriptId, revision, draft])

  // --- derived ---------------------------------------------------------------

  const detail = state?.input ?? null
  const composition = detail?.composition ?? null
  const stale = transcript?.stale ?? false
  const viewingOld = revision !== null && transcriptLatest !== null && revision.revision !== transcriptLatest
  const readOnly = stale || viewingOld
  const direction = RTL_LANGUAGES.has(revision?.language ?? settings?.language ?? 'he') ? 'rtl' : 'ltr'
  const errors = check?.errors ?? []
  const fps = composition?.fps ?? 30
  const activeId = useMemo(() => {
    const ordered = draft.filter((c) => Number.isFinite(c.start) && Number.isFinite(c.end))
    const index = activeIndex(captionFrameRanges(ordered, fps), frame)
    return index >= 0 ? ordered[index].id : null
  }, [draft, fps, frame])

  const model = catalog?.models.find((entry) => entry.id === settings?.model) ?? null
  const activeJobs = state?.active_jobs ?? []
  const transcribing = activeJobs.some((job) => job.type === 'caption_transcription')
  const downloading = activeJobs.some((job) => job.type === 'caption_model_download')
  const preparing = activeJobs.some((job) => job.type === 'caption_renderer_setup')

  const exportBlocked = !transcript
    ? 'Transcribe the video first.'
    : stale
      ? 'The video changed since it was transcribed; transcribe it again.'
      : !revision
        ? 'No captions yet.'
        : viewingOld
          ? 'You are viewing an older revision. Make it the latest revision or open the latest one.'
          : dirty
            ? 'Save your caption edits first: an export uses a saved revision.'
            : errors.length > 0
              ? 'Fix the caption errors first.'
              : styleDirty
                ? 'Save the style first: an export uses the saved style.'
                : null

  // --- actions ---------------------------------------------------------------

  async function run(action: () => Promise<void>, fallback: string) {
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

  function confirmLeaveDraft(): boolean {
    return !dirty || window.confirm('Your caption edits are not saved. Discard them?')
  }

  async function transcribe(force: boolean) {
    if (!selectedKey || !settings) return
    if (force && !confirmLeaveDraft()) return
    await run(async () => {
      const result = await startTranscription(projectId, selectedKey, settings, force)
      if (result.reused) {
        setTranscriptId(result.reused.id)
        setMessage({
          kind: 'ok',
          text: 'This video was already transcribed with these settings, so that transcript is used. Use "Transcribe again" for a fresh one.',
        })
      } else {
        onJobSubmitted()
        setMessage({ kind: 'ok', text: 'Transcription started. It runs locally in the background.' })
      }
      await refreshState()
    }, 'Starting the transcription failed.')
  }

  async function downloadModel() {
    if (!settings) return
    await run(async () => {
      await downloadWhisperModel(projectId, settings.model)
      onJobSubmitted()
      await refreshState()
    }, 'Starting the download failed.')
  }

  async function prepareRenderer() {
    await run(async () => {
      await prepareCaptionRenderer(projectId)
      onJobSubmitted()
      await refreshState()
    }, 'Preparing the renderer failed.')
  }

  async function cancel(jobId: string) {
    await run(async () => {
      await cancelJob(projectId, jobId)
      await refreshState()
    }, 'Cancelling failed.')
  }

  async function save() {
    if (!transcriptId || !revision) return
    await run(async () => {
      const saved = await saveCaptionRevision(projectId, transcriptId, revision.revision, draft)
      setRevision(saved)
      setDraft(saved.captions)
      setLatestRevision(saved.revision)
      setMessage({ kind: 'ok', text: `Saved as revision ${saved.revision}. Earlier revisions are kept.` })
      await refreshState()
    }, 'Saving the captions failed.')
  }

  async function makeLatest() {
    if (!transcriptId || !revision || transcriptLatest === null) return
    await run(async () => {
      const saved = await saveCaptionRevision(projectId, transcriptId, transcriptLatest, draft)
      setRevision(saved)
      setDraft(saved.captions)
      setLatestRevision(saved.revision)
      setMessage({ kind: 'ok', text: `Revision ${revision.revision} was saved again as revision ${saved.revision}.` })
      await refreshState()
    }, 'Saving the captions failed.')
  }

  async function regroup() {
    if (!transcriptId || !grouping) return
    const edited = dirty || revision?.has_manual_edits
    if (
      edited &&
      !window.confirm(
        'Regrouping rebuilds the captions from the transcript, so your manual corrections will not be in the new revision. ' +
          (revision?.has_manual_edits ? `They stay saved in revision ${revision.revision}, which you can reopen. ` : '') +
          (dirty ? 'Unsaved edits will be lost. ' : '') +
          'Continue?',
      )
    ) {
      return
    }
    await run(async () => {
      const created = await regroupCaptions(projectId, transcriptId, revision?.revision ?? null, grouping, true)
      setRevision(created)
      setDraft(created.captions)
      setLatestRevision(created.revision)
      setMessage({ kind: 'ok', text: `Regrouped as revision ${created.revision}.` })
      await refreshState()
    }, 'Regrouping failed.')
  }

  async function storeStyle() {
    if (!style) return
    await run(async () => {
      const saved = await saveCaptionStyle(projectId, style)
      setStyle(saved.style)
      setSavedStyle(saved.style)
      setMessage({ kind: 'ok', text: 'Style saved. Nothing was transcribed again.' })
    }, 'Saving the style failed.')
  }

  async function exportAs(format: 'mp4' | 'srt') {
    if (!transcriptId || !revision) return
    await run(async () => {
      const zoomed = selectedKey?.startsWith('zoom.') ? selectedKey : null
      await startCaptionExport(projectId, transcriptId, revision.revision, format, zoomed)
      onJobSubmitted()
      setMessage({
        kind: 'ok',
        text:
          format === 'mp4'
            ? `Export of revision ${revision.revision} started. Editing now does not change it.`
            : `SRT of revision ${revision.revision} is being written.`,
      })
      await refreshState()
    }, 'Starting the export failed.')
  }

  const seek = useCallback((seconds: number) => bridge.seek(seconds), [bridge])

  // Show the draft (text fixes, style changes) on the preview as it is typed.
  const previewCaptions = useMemo(() => toPreviewCaptions(draft), [draft])
  useEffect(() => {
    if (!selectedKey || !revision || !style || stale) {
      bridge.setOverride(null)
      return
    }
    bridge.setOverride({ key: selectedKey, captions: previewCaptions, style, direction })
  }, [bridge, selectedKey, revision, style, stale, previewCaptions, direction])
  useEffect(() => () => bridge.setOverride(null), [bridge])

  // Transcribe by itself as soon as the accepted cut has no transcript, with
  // the default engine settings. Once per video: a failed or cancelled run
  // is not restarted in a loop (the Transcribe button is the retry).
  const autoTranscribed = useRef<string | null>(null)
  useEffect(() => {
    if (!selectedKey || !state || state.input.key !== selectedKey || busy) return
    if (state.transcripts.length > 0 || state.active_jobs.length > 0) return
    if (!model?.downloaded || !state.input.metadata?.has_audio) return
    if (autoTranscribed.current === selectedKey) return
    autoTranscribed.current = selectedKey
    void transcribe(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedKey, state, busy, model])

  // --- render ----------------------------------------------------------------

  if (!catalog || !inputs || !settings || !style || !grouping) {
    return (
      <section className="panel subpanel captions">
        <h3>Captions</h3>
        {message ? <p className={`message ${message.kind}`}>{message.text}</p> : <p className="hint">Loading…</p>}
      </section>
    )
  }

  const canTranscribe = selectedKey !== null && !transcribing && (!transcript || stale)
  const editing = transcript !== null && revision !== null && composition !== null

  return (
    <section className="panel subpanel captions">
      {/* Actions first, always at the top. */}
      <div className="row caption-actions">
        {canTranscribe && (
          <button
            type="button"
            className="primary"
            disabled={busy || !model?.downloaded || !detail?.metadata?.has_audio}
            onClick={() => void transcribe(stale)}
          >
            {stale ? 'Transcribe again' : 'Transcribe'}
          </button>
        )}
        {editing && (
          <>
            <button
              type="button"
              className="primary"
              disabled={busy || readOnly || !dirty || errors.length > 0}
              onClick={() => void save()}
            >
              Save text fixes
            </button>
            <button
              type="button"
              disabled={busy || !dirty}
              onClick={() => {
                if (window.confirm('Discard your unsaved text fixes?')) setDraft(revision.captions)
              }}
            >
              Discard
            </button>
            {dirty && <span className="badge warn">Unsaved</span>}
            {errors.length > 0 && <span className="badge bad">{errors.length} to fix</span>}
          </>
        )}
      </div>

      {message && <p className={`message ${message.kind}`}>{message.text}</p>}

      {!selectedKey && <p className="hint">Accept the cut first: captions are made on its merged video.</p>}

      {detail?.error && <p className="message error">{detail.error}</p>}
      {detail?.metadata && !detail.metadata.has_audio && (
        <p className="message error">The merged video has no audio, so there is nothing to transcribe.</p>
      )}
      {composition?.notes.map((note) => (
        <p key={note} className="message warn">
          {note}
        </p>
      ))}

      {activeJobs.length > 0 && (
        <ul className="jobs">
          {activeJobs.map((job) => (
            <JobLine key={job.id} job={job} onCancel={(id) => void cancel(id)} />
          ))}
        </ul>
      )}

      {model && !model.downloaded && (
        <div className="message warn">
          <p>
            The transcription model (<strong>{model.label}</strong>) is not on this computer yet. Downloading it needs
            internet once ({model.download}). After that, transcription runs offline.
          </p>
          <button type="button" className="primary" onClick={() => void downloadModel()} disabled={busy || downloading}>
            {downloading ? 'Downloading…' : 'Download model'}
          </button>
        </div>
      )}

      {transcript?.stale && <p className="message error">{transcript.reason}</p>}

      {editing && (
        <>
          {viewingOld && !stale && (
            <p className="message warn">
              A newer revision was saved meanwhile, so this one is read-only.{' '}
              <button type="button" onClick={() => void makeLatest()} disabled={busy}>
                Keep these fixes as the latest
              </button>
            </p>
          )}

          {errors.filter((error) => !error.caption_id).map((error) => (
            <p key={error.message} className="message error">
              {error.message}
            </p>
          ))}

          <CaptionTextTable
            captions={draft}
            errors={errors}
            activeId={activeId}
            direction={direction}
            readOnly={readOnly}
            onChange={setDraft}
            onSeek={seek}
          />
          {draft.length === 0 && <p className="hint">No speech was recognised, so there are no captions.</p>}

          <section className="caption-style">
            <h4>
              Caption style {styleDirty && <span className="badge warn">Unsaved</span>}
            </h4>
            <div className="row">
              <button type="button" className="primary" disabled={busy || !styleDirty} onClick={() => void storeStyle()}>
                Save style
              </button>
              <button type="button" disabled={busy || !styleDirty} onClick={() => savedStyle && setStyle(savedStyle)}>
                Discard style changes
              </button>
              <button type="button" disabled={busy} onClick={() => setStyle(catalog.style_defaults)}>
                Template defaults
              </button>
            </div>
            <CaptionStyleForm catalog={catalog} style={style} onChange={setStyle} />
          </section>

          <section className="caption-grouping">
            <h4>Grouping — how many words each caption holds</h4>
            <div className="row">
              <button type="button" disabled={busy || stale} onClick={() => void regroup()}>
                Regroup from the transcript
              </button>
              <span className="small">Creates a new revision. Text fixes made since are not carried over.</span>
            </div>
            <div className="caption-settings">
              {catalog.grouping.map((spec) => (
                <label key={spec.name} className="field" title={spec.description}>
                  <span>
                    {spec.label} <span className="small">({spec.unit})</span>
                  </span>
                  <input
                    type="number"
                    min={spec.min}
                    max={spec.max}
                    step={spec.step}
                    value={grouping[spec.name as keyof CaptionGrouping]}
                    onChange={(event) => setGrouping({ ...grouping, [spec.name]: Number(event.target.value) })}
                  />
                </label>
              ))}
            </div>
          </section>

          <details className="caption-export">
            <summary>Export the captions on their own (optional)</summary>
            {!catalog.renderer.ready && (
              <div className="message warn">
                {catalog.renderer.problems.map((problem) => (
                  <p key={problem}>{problem}</p>
                ))}
                {catalog.renderer.node && catalog.renderer.packages_installed && (
                  <button type="button" onClick={() => void prepareRenderer()} disabled={busy || preparing}>
                    {preparing ? 'Preparing…' : 'Prepare renderer'}
                  </button>
                )}
              </div>
            )}
            <div className="row">
              <button
                type="button"
                disabled={busy || exportBlocked !== null || !catalog.renderer.ready}
                onClick={() => void exportAs('mp4')}
              >
                Export MP4 with captions
              </button>
              <button type="button" disabled={busy || exportBlocked !== null} onClick={() => void exportAs('srt')}>
                Export SRT
              </button>
              {exportBlocked && <span className="small">{exportBlocked}</span>}
            </div>
            {state && state.exports.length > 0 && (
              <ul className="caption-exports">
                {state.exports.map((record) => (
                  <li key={record.export_id}>
                    <div className="job-header">
                      <strong>{record.format.toUpperCase()}</strong>
                      <span
                        className={`badge ${record.status === 'succeeded' ? 'ok' : record.status === 'running' ? '' : 'bad'}`}
                      >
                        {record.status}
                      </span>
                      {record.stale && <span className="badge warn">Video changed since</span>}
                      <span className="small">
                        {new Date(record.created_at).toLocaleString()} · revision {record.revision} ·{' '}
                        {record.caption_count} captions
                      </span>
                    </div>
                    {record.error && <p className="small">{record.error}</p>}
                    {record.downloadable && (
                      <span className="row">
                        {record.format === 'mp4' && (
                          <a href={captionExportUrl(projectId, record.export_id, false)} target="_blank" rel="noreferrer">
                            Play
                          </a>
                        )}
                        <a href={captionExportUrl(projectId, record.export_id, true)}>Download {record.output?.filename}</a>
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </details>
        </>
      )}
    </section>
  )
}
