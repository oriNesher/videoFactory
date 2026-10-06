import type { PlayerRef } from '@remotion/player'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  cancelJob,
  captionExportUrl,
  captionInputStreamUrl,
  captionOperation,
  downloadWhisperModel,
  getCaptionCatalog,
  getCaptionInput,
  getCaptionRevision,
  importCaptionVideo,
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
import CaptionEditor from './CaptionEditor'
import { formatTime } from './captionFormat'
import CaptionPreview from './CaptionPreview'
import CaptionStyleForm from './CaptionStyleForm'
import type { CaptionStyle } from './captions/types'
import { activeIndex, captionFrameRanges, secondsToFrame } from './captions/timing'
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

  const playerRef = useRef<PlayerRef>(null)
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
        const keys = loadedInputs.inputs.map((input) => input.key)
        setSelectedKey(
          loadedInputs.selected && keys.includes(loadedInputs.selected)
            ? loadedInputs.selected
            : (keys[0] ?? null),
        )
      })
      .catch((caught) => setMessage({ kind: 'error', text: errorText(caught, 'Loading captions failed.') }))
    return () => {
      cancelled = true
    }
  }, [loadInputs])

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
  const timing = useMemo(
    () => Object.fromEntries((check?.captions ?? []).map((caption) => [caption.id, caption])),
    [check],
  )
  const fps = composition?.fps ?? 30
  const activeId = useMemo(() => {
    const ordered = draft.filter((c) => Number.isFinite(c.start) && Number.isFinite(c.end))
    const index = activeIndex(captionFrameRanges(ordered, fps), frame)
    return index >= 0 ? ordered[index].id : null
  }, [draft, fps, frame])

  const model = catalog?.models.find((entry) => entry.id === settings?.model) ?? null
  const cuda = catalog?.devices.find((device) => device.id === 'cuda')
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

  async function chooseInput(key: string) {
    if (key === selectedKey || !confirmLeaveDraft()) return
    await run(async () => {
      await selectCaptionInput(projectId, key)
      setSelectedKey(key)
    }, 'Selecting the video failed.')
  }

  async function importVideo() {
    if (!confirmLeaveDraft()) return
    await run(async () => {
      const result = await importCaptionVideo(projectId)
      if (result.cancelled || !result.input_key) return
      await loadInputs()
      await selectCaptionInput(projectId, result.input_key)
      setSelectedKey(result.input_key)
      setMessage({
        kind: 'ok',
        text: result.duplicate ? 'That video was already imported; it is selected.' : 'Video imported for captions.',
      })
    }, 'Importing the video failed.')
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

  async function operate(
    operation: 'split' | 'merge' | 'fix_overlaps',
    options: { captionId?: string; wordIndex?: number; atSeconds?: number } = {},
  ) {
    if (!transcriptId) return
    await run(async () => {
      const result = await captionOperation(projectId, transcriptId, draft, operation, options)
      setDraft(result.captions)
      setCheck(result)
    }, 'The caption change failed.')
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

  const seek = useCallback(
    (seconds: number) => {
      const player = playerRef.current
      if (!player || !Number.isFinite(seconds)) return
      player.seekTo(secondsToFrame(seconds, fps))
    },
    [fps],
  )

  const onFrame = useCallback((value: number) => setFrame(value), [])

  // --- render ----------------------------------------------------------------

  if (!catalog || !inputs || !settings || !style || !grouping) {
    return (
      <section className="panel subpanel captions">
        <h3>Captions</h3>
        {message ? <p className={`message ${message.kind}`}>{message.text}</p> : <p className="hint">Loading…</p>}
      </section>
    )
  }

  const streamUrl =
    selectedKey && detail?.fingerprint
      ? `${captionInputStreamUrl(projectId, selectedKey)}?v=${detail.fingerprint.digest.slice(7, 19)}`
      : null

  return (
    <section className="panel subpanel captions">
      <div className="editor-header">
        <h3>Captions</h3>
        <span className="small">Whisper runs on this computer. Nothing here needs an AI key.</span>
      </div>

      {message && <p className={`message ${message.kind}`}>{message.text}</p>}

      {/* 1. Select a video */}
      <h4>1. Video</h4>
      <div className="row">
        <select
          value={selectedKey ?? ''}
          onChange={(event) => void chooseInput(event.target.value)}
          disabled={busy || inputs.inputs.length === 0}
          aria-label="Video to caption"
        >
          {inputs.inputs.length === 0 && <option value="">No videos yet</option>}
          {(['cut', 'source', 'import', 'zoom'] as const).map((kind) => {
            const group = inputs.inputs.filter((input) => input.kind === kind)
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
        <button type="button" onClick={() => void importVideo()} disabled={busy}>
          Import a video…
        </button>
      </div>
      <p className="hint">
        Captions use the selected video&apos;s own audio and timing. Switching videos keeps every earlier transcript
        and export.
      </p>

      {state?.timing_source?.via_zoom && (
        <p className="message ok">
          A zoomed video keeps every frame at its original time, so it uses the captions of the clean video it was
          made from. They are drawn over the zoomed picture, never zoomed with it.
        </p>
      )}
      {detail?.error && <p className="message error">{detail.error}</p>}
      {detail?.metadata && composition && (
        <p className="small">
          {composition.width}×{composition.height} · {Number(composition.fps.toFixed(3))} fps ·{' '}
          {formatTime(detail.metadata.duration_seconds)}
          {detail.metadata.rotation ? ` · rotated ${detail.metadata.rotation}°` : ''}
          {!detail.metadata.has_audio && <span className="badge bad">No audio</span>}
        </p>
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

      {/* 2. Transcribe */}
      <h4>2. Transcription</h4>
      <div className="caption-settings">
        <label className="field">
          <span>Model</span>
          <select value={settings.model} onChange={(event) => setSettings({ ...settings, model: event.target.value })}>
            {catalog.models.map((entry) => (
              <option key={entry.id} value={entry.id}>
                {entry.label}
                {entry.downloaded ? '' : ' — not downloaded'}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Language</span>
          <select
            value={settings.language}
            onChange={(event) => setSettings({ ...settings, language: event.target.value })}
          >
            {catalog.languages.map((language) => (
              <option key={language.code} value={language.code}>
                {language.label}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Device</span>
          <select
            value={settings.device}
            onChange={(event) => {
              const device = event.target.value as 'cpu' | 'cuda'
              setSettings({ ...settings, device, compute_type: catalog.compute_types[device][0] })
            }}
          >
            {catalog.devices.map((device) => (
              <option key={device.id} value={device.id} disabled={!device.available}>
                {device.label}
                {device.available ? '' : ' — not available'}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Precision</span>
          <select
            value={settings.compute_type}
            onChange={(event) => setSettings({ ...settings, compute_type: event.target.value })}
          >
            {catalog.compute_types[settings.device].map((type) => (
              <option key={type} value={type}>
                {type}
              </option>
            ))}
          </select>
        </label>
      </div>
      {cuda && !cuda.available && <p className="small">GPU: {cuda.reason}</p>}

      {model && !model.downloaded && (
        <div className="message warn">
          <p>
            <strong>{model.label}</strong> is not on this computer yet. Downloading it needs internet once ({model.download}
            ) and saves it in <span className="mono">{catalog.model_cache}</span>. After that, transcription runs offline.
          </p>
          <button type="button" className="primary" onClick={() => void downloadModel()} disabled={busy || downloading}>
            {downloading ? 'Downloading…' : 'Download model'}
          </button>
        </div>
      )}

      <div className="row">
        <button
          type="button"
          className="primary"
          disabled={busy || transcribing || !selectedKey || !model?.downloaded || !detail?.metadata?.has_audio}
          onClick={() => void transcribe(false)}
        >
          {transcribing ? 'Transcribing…' : 'Transcribe'}
        </button>
        {(state?.transcripts.length ?? 0) > 0 && (
          <button
            type="button"
            disabled={busy || transcribing || !model?.downloaded}
            onClick={() => void transcribe(true)}
            title="Make a new transcript even if one with these settings exists. The current one is kept."
          >
            Transcribe again
          </button>
        )}
        <span className="small">
          Progress is the position of the last recognised phrase, so it moves in steps. The first run loads the model (a
          few seconds); later runs reuse it.
        </span>
      </div>

      {state && state.transcripts.length > 0 && (
        <div className="row">
          <label className="field wide">
            <span>Transcript</span>
            <select
              value={transcriptId ?? ''}
              onChange={(event) => {
                if (confirmLeaveDraft()) setTranscriptId(event.target.value)
              }}
            >
              {state.transcripts.map((entry) => (
                <option key={entry.id} value={entry.id}>
                  {new Date(entry.created_at).toLocaleString()} · {entry.model_label.split(' — ')[0]} · {entry.language}
                  {' · '}
                  {entry.word_count ?? 0} words{entry.stale ? ' · OUT OF DATE' : ''}
                </option>
              ))}
            </select>
          </label>
        </div>
      )}
      {transcript?.stale && <p className="message error">{transcript.reason}</p>}

      {/* 3–5. Review, group, style, preview */}
      {transcript && revision && composition && streamUrl && (
        <>
          <h4>3. Review and correct</h4>
          <div className="caption-workspace">
            <div className="caption-preview-column">
              <CaptionPreview
                ref={playerRef}
                src={streamUrl}
                composition={composition}
                captions={stale ? [] : draft}
                style={style}
                direction={direction}
                onFrame={onFrame}
              />
              <p className="small">
                Preview of the same composition the export renders. Edits and style changes appear at once.
              </p>
            </div>

            <div className="caption-edit-column">
              <div className="row">
                <label className="field">
                  <span>Revision</span>
                  <select
                    value={revision.revision}
                    onChange={(event) => {
                      if (confirmLeaveDraft()) void openRevision(Number(event.target.value))
                    }}
                  >
                    {transcript.revisions
                      .slice()
                      .reverse()
                      .map((number) => (
                        <option key={number} value={number}>
                          Revision {number}
                          {number === transcriptLatest ? ' (latest)' : ''}
                        </option>
                      ))}
                  </select>
                </label>
                {dirty && <span className="badge warn">Unsaved edits</span>}
                {revision.has_manual_edits && !dirty && <span className="badge">Edited by hand</span>}
                {errors.length > 0 && <span className="badge bad">{errors.length} to fix</span>}
              </div>

              {viewingOld && !stale && (
                <p className="message warn">
                  Revision {revision.revision} is an older revision and is read-only.{' '}
                  <button type="button" onClick={() => void makeLatest()} disabled={busy}>
                    Make it the latest revision
                  </button>
                </p>
              )}

              <div className="row">
                <button
                  type="button"
                  className="primary"
                  disabled={busy || readOnly || !dirty || errors.length > 0}
                  onClick={() => void save()}
                >
                  Save captions
                </button>
                <button
                  type="button"
                  disabled={busy || !dirty}
                  onClick={() => {
                    if (window.confirm('Discard your unsaved caption edits?')) setDraft(revision.captions)
                  }}
                >
                  Discard edits
                </button>
                {errors.some((error) => error.message.includes('Fix overlaps')) && !readOnly && (
                  <button type="button" disabled={busy} onClick={() => void operate('fix_overlaps')}>
                    Fix overlaps
                  </button>
                )}
              </div>

              {errors.filter((error) => !error.caption_id).map((error) => (
                <p key={error.message} className="message error">
                  {error.message}
                </p>
              ))}

              <CaptionEditor
                captions={draft}
                errors={errors}
                timing={timing}
                activeId={activeId}
                readOnly={readOnly}
                busy={busy}
                onChange={setDraft}
                onSeek={seek}
                onSplit={(captionId, wordIndex) =>
                  void operate('split', { captionId, wordIndex, atSeconds: frame / fps })
                }
                onMerge={(captionId) => void operate('merge', { captionId })}
              />
              {draft.length === 0 && <p className="hint">No speech was recognised, so there are no captions.</p>}
            </div>
          </div>

          <h4>4. Grouping and style</h4>
          <details className="caption-grouping" open>
            <summary>Grouping — how words are split into captions</summary>
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
            <div className="row">
              <button type="button" disabled={busy || stale} onClick={() => void regroup()}>
                Regroup from the transcript
              </button>
              <span className="small">
                Creates a new revision; the current one stays saved. Grouping never transcribes again.
              </span>
            </div>
          </details>

          <details className="caption-style" open>
            <summary>
              Style {styleDirty && <span className="badge warn">Unsaved</span>}
            </summary>
            <CaptionStyleForm catalog={catalog} style={style} onChange={setStyle} />
            <div className="row">
              <button type="button" className="primary" disabled={busy || !styleDirty} onClick={() => void storeStyle()}>
                Save style
              </button>
              <button type="button" disabled={busy || !styleDirty} onClick={() => savedStyle && setStyle(savedStyle)}>
                Discard style changes
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => setStyle(catalog.style_defaults)}
                title="Back to the template's defaults (not saved until you press Save style)"
              >
                Template defaults
              </button>
            </div>
            <p className="small">
              Fonts are bundled ({catalog.fonts.map((font) => font.label).join(', ')}; {catalog.fonts[0]?.license}) and
              never fetched from the internet. Word highlighting only applies to captions that still have word timing.
            </p>
          </details>

          {/* 6. Export */}
          <h4>5. Export</h4>
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
              className="primary"
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
          <p className="small">
            The MP4 has the captions burned in, at the video&apos;s own size, with its original audio. The SRT carries
            only text and timing: players and editors show it in their own style, not this one.
          </p>
        </>
      )}

      {state && state.exports.length > 0 && (
        <ul className="caption-exports">
          {state.exports.map((record) => (
            <li key={record.export_id}>
              <div className="job-header">
                <strong>{record.format.toUpperCase()}</strong>
                <span className={`badge ${record.status === 'succeeded' ? 'ok' : record.status === 'running' ? '' : 'bad'}`}>
                  {record.status}
                </span>
                {record.stale && <span className="badge warn">Video changed since</span>}
                <span className="small">
                  {new Date(record.created_at).toLocaleString()} · revision {record.revision} · style{' '}
                  {record.style_revision} · {record.caption_count} captions
                </span>
              </div>
              {record.verification && record.format === 'mp4' && (
                <p className="small">
                  Checked with FFprobe: {String(record.verification.width)}×{String(record.verification.height)}, H.264 +
                  AAC, {String(record.verification.duration_seconds)} s (source {String(record.verification.source_seconds)}{' '}
                  s, tolerance ±{String(record.verification.tolerance_seconds)} s).
                </p>
              )}
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
    </section>
  )
}
