import type {
  AddDirectoryResult,
  AudioTrack,
  BrollDecision,
  BrollInputState,
  BrollLibraryAsset,
  BrollMoment,
  BrollNext,
  BrollPlan,
  BrollPlannerRun,
  BrollStatus,
  CaptionCatalog,
  CaptionCheck,
  CaptionGrouping,
  CaptionInputState,
  CaptionInputsResponse,
  CaptionRevision,
  EditableCaption,
  TranscriptionSettings,
  TranscriptSummary,
  CapabilityCatalog,
  CutRun,
  CutRunListing,
  CuttingForm,
  CuttingSettings,
  CuttingState,
  SampleRequest,
  Job,
  JobListing,
  LlmStatus,
  MusicDecision,
  MusicNext,
  MusicSettings,
  MusicState,
  PipelineState,
  PlanListing,
  PlanRevision,
  Project,
  ProjectListing,
  ResourceCatalog,
  SubtitleSettings,
  ToolStatus,
  ToolVersion,
  ZoomApproveResult,
  ZoomInputState,
  ZoomJobSummary,
  ZoomOverlayReference,
  ZoomRun,
  ZoomStatus,
} from './types'
import type { CaptionStyle } from './captions/types'

/** Every call goes through the Vite dev proxy to the local Python service. */
const API_BASE = '/api'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response

  try {
    response = await fetch(`${API_BASE}${path}`, {
      headers: init?.body ? { 'Content-Type': 'application/json' } : undefined,
      ...init,
    })
  } catch {
    throw new Error('No connection to the server. Check that the Python service is running.')
  }

  if (!response.ok) {
    throw new Error(await readError(response))
  }

  return (await response.json()) as T
}

/** FastAPI returns `detail` as a string for our errors, or an array for its own. */
async function readError(response: Response): Promise<string> {
  try {
    const body = await response.json()
    if (typeof body?.detail === 'string') {
      return body.detail
    }
    if (Array.isArray(body?.detail) && body.detail.length > 0) {
      return `The request was rejected: ${body.detail[0]?.msg ?? response.status}`
    }
  } catch {
    // fall through to the generic message
  }

  return `The action failed (error ${response.status}).`
}

export function listProjects() {
  return request<ProjectListing>('/projects')
}

export function createProject(name: string) {
  return request<Project>('/projects', {
    method: 'POST',
    body: JSON.stringify({ name }),
  })
}

export function loadProject(projectId: string) {
  return request<Project>(`/projects/${projectId}`)
}

export function saveProject(
  projectId: string,
  name: string,
  sourceIds: string[],
) {
  return request<Project>(`/projects/${projectId}`, {
    method: 'PUT',
    body: JSON.stringify({ name, source_ids: sourceIds }),
  })
}

export function addSource(projectId: string, path: string) {
  return request<Project>(`/projects/${projectId}/sources`, {
    method: 'POST',
    body: JSON.stringify({ path }),
  })
}

/**
 * Add a whole folder of clips, ordered by file name.
 *
 * With no `path` the backend opens the machine's own folder dialog, because a
 * browser never tells a page where a folder really is on disk. That request
 * stays open for as long as the dialog is on screen.
 */
export function addSourceDirectory(projectId: string, path?: string) {
  return request<AddDirectoryResult>(`/projects/${projectId}/sources/directory`, {
    method: 'POST',
    body: JSON.stringify({ path: path ?? null }),
  })
}

export function openSourceInVlc(projectId: string, sourceId: string) {
  return request<{ opened: boolean }>(`/projects/${projectId}/sources/${sourceId}/open`, {
    method: 'POST',
  })
}

export function checkTools() {
  return request<Record<string, ToolStatus>>('/tools')
}

export function checkToolVersions() {
  return request<Record<string, ToolVersion>>('/tools/versions')
}

// --- jobs -------------------------------------------------------------------

export function listJobs(projectId: string) {
  return request<JobListing>(`/projects/${projectId}/jobs`)
}

export function submitJob(
  projectId: string,
  type: string,
  input?: unknown,
) {
  return request<Job>(`/projects/${projectId}/jobs`, {
    method: 'POST',
    body: JSON.stringify({ type, input: input ?? null }),
  })
}

export function cancelJob(projectId: string, jobId: string) {
  return request<Job>(`/projects/${projectId}/jobs/${jobId}/cancel`, {
    method: 'POST',
  })
}

export function retryJob(projectId: string, jobId: string) {
  return request<Job>(`/projects/${projectId}/jobs/${jobId}/retry`, {
    method: 'POST',
  })
}

// --- catalogs and provider --------------------------------------------------

export function getCapabilities() {
  return request<CapabilityCatalog>('/capabilities')
}

export function getLlmStatus() {
  return request<LlmStatus>('/llm')
}

export function getResources(projectId: string) {
  return request<ResourceCatalog>(`/projects/${projectId}/resources`)
}

// --- plans ------------------------------------------------------------------

export function listPlans(projectId: string) {
  return request<PlanListing>(`/projects/${projectId}/plans`)
}

/** Returns the queued generation *job*: the model never answers inline. */
export function generatePlan(projectId: string, instruction: string) {
  return request<Job>(`/projects/${projectId}/plans/generate`, {
    method: 'POST',
    body: JSON.stringify({ instruction }),
  })
}

export function getPlan(projectId: string, planId: string) {
  return request<PlanRevision>(`/projects/${projectId}/plans/${planId}`)
}

export function getPlanRevision(
  projectId: string,
  planId: string,
  revision: number,
) {
  return request<PlanRevision>(
    `/projects/${projectId}/plans/${planId}/revisions/${revision}`,
  )
}

export function savePlanRevision(
  projectId: string,
  planId: string,
  summary: string,
  actions: unknown[],
) {
  return request<PlanRevision>(
    `/projects/${projectId}/plans/${planId}/revisions`,
    { method: 'POST', body: JSON.stringify({ summary, actions }) },
  )
}

export function approvePlanRevision(
  projectId: string,
  planId: string,
  revision: number,
) {
  return request<PlanRevision>(
    `/projects/${projectId}/plans/${planId}/revisions/${revision}/approve`,
    { method: 'POST' },
  )
}

export function executePlanRevision(
  projectId: string,
  planId: string,
  revision: number,
) {
  return request<Job>(
    `/projects/${projectId}/plans/${planId}/revisions/${revision}/execute`,
    { method: 'POST' },
  )
}

// --- cutting (milestone 1A) --------------------------------------------------

export function getCuttingSettings(projectId: string) {
  return request<CuttingSettings>(`/projects/${projectId}/cutting/settings`)
}

export function saveCuttingSettings(projectId: string, form: CuttingForm) {
  return request<CuttingSettings>(`/projects/${projectId}/cutting/settings`, {
    method: 'PUT',
    body: JSON.stringify(form),
  })
}

/** Returns the queued *job*: cutting always runs through the job queue. */
export function startCuttingRun(projectId: string, form: CuttingForm) {
  return request<Job>(`/projects/${projectId}/cutting/runs`, {
    method: 'POST',
    body: JSON.stringify(form),
  })
}

/**
 * Where each clip would be cut for the form as it stands, and which previews
 * and recommendation are still about that form. Reads caches; renders nothing.
 */
export function getCuttingState(projectId: string, form: CuttingForm) {
  return request<CuttingState>(`/projects/${projectId}/cutting/state`, {
    method: 'POST',
    body: JSON.stringify(form),
  })
}

/** Queues boundary detection for the selected clips. */
export function startCuttingAnalysis(projectId: string, form: CuttingForm) {
  return request<Job>(`/projects/${projectId}/cutting/analysis`, {
    method: 'POST',
    body: JSON.stringify(form),
  })
}

/** Queues preview samples; without `samples`, every preview the selection allows. */
export function startCuttingSamples(
  projectId: string,
  form: CuttingForm,
  samples?: SampleRequest[],
) {
  return request<Job>(`/projects/${projectId}/cutting/samples`, {
    method: 'POST',
    body: JSON.stringify({ ...form, samples: samples ?? null }),
  })
}

/** A preview file by id. `source` does not exist for a join preview. */
export function sampleUrl(
  projectId: string,
  sampleId: string,
  which: 'edited' | 'source',
) {
  return `/api/projects/${projectId}/cutting/samples/${sampleId}/${which}/stream`
}

/** Queues one AI recommendation; `revisePlanId` asks for a revision with feedback. */
export function requestCutRecommendation(
  projectId: string,
  form: CuttingForm,
  requestText: string,
  feedback?: string,
  revisePlanId?: string,
) {
  return request<Job>(`/projects/${projectId}/cutting/recommendations`, {
    method: 'POST',
    body: JSON.stringify({
      ...form,
      request: requestText,
      feedback: feedback ?? null,
      revise_plan_id: revisePlanId ?? null,
    }),
  })
}

/** Approves a recommendation and saves its settings into the form. */
export function applyCutRecommendation(
  projectId: string,
  planId: string,
  revision: number,
  form: CuttingForm,
  confirmModeChange: boolean,
) {
  return request<CuttingSettings>(
    `/projects/${projectId}/cutting/recommendations/${planId}/revisions/${revision}/apply`,
    {
      method: 'POST',
      body: JSON.stringify({ ...form, confirm_mode_change: confirmModeChange }),
    },
  )
}

export function cutMapUrl(projectId: string, runId: string) {
  return `/api/projects/${projectId}/cutting/runs/${runId}/cut-map`
}

export function listCuttingRuns(projectId: string) {
  return request<CutRunListing>(`/projects/${projectId}/cutting/runs`)
}

export function openRunFolder(projectId: string, runId: string) {
  return request<{ opened: boolean }>(
    `/projects/${projectId}/cutting/runs/${runId}/open-folder`,
    { method: 'POST' },
  )
}

export function getCuttingRun(projectId: string, runId: string) {
  return request<CutRun>(`/projects/${projectId}/cutting/runs/${runId}`)
}

/**
 * The URL a <video> element plays from, and the one a download link points at.
 *
 * Both address a run id and an output id that the backend resolves inside that
 * run's own directory. There is no endpoint that takes a filesystem path.
 */
export function outputUrl(
  projectId: string,
  runId: string,
  outputId: string,
  kind: 'stream' | 'download' = 'stream',
) {
  return `/api/projects/${projectId}/cutting/runs/${runId}/outputs/${outputId}/${kind}`
}

// --- subtitles (Whisper) ----------------------------------------------------

export function getSubtitleSettings(projectId: string) {
  return request<SubtitleSettings>(`/projects/${projectId}/subtitles/settings`)
}

export function saveSubtitleSettings(
  projectId: string,
  settings: Record<string, number>,
) {
  return request<SubtitleSettings>(`/projects/${projectId}/subtitles/settings`, {
    method: 'PUT',
    body: JSON.stringify({ settings }),
  })
}

/**
 * Returns the queued *job*: Whisper always runs through the job queue. The
 * model and the language (Hebrew) are fixed by the backend.
 *
 * Without `outputIds` it transcribes the run's merged video, and the job
 * merges the run's clips first when the run has no merged video yet.
 */
export function startSubtitles(
  projectId: string,
  runId: string,
  outputIds: string[] | null,
  settings: Record<string, number>,
) {
  return request<Job>(`/projects/${projectId}/subtitles/jobs`, {
    method: 'POST',
    body: JSON.stringify({ run_id: runId, output_ids: outputIds, settings }),
  })
}

/** `vtt` feeds the player's <track>; `srt` is the download for Premiere. */
export function subtitleUrl(
  projectId: string,
  runId: string,
  outputId: string,
  kind: 'vtt' | 'srt',
) {
  return `/api/projects/${projectId}/subtitles/runs/${runId}/outputs/${outputId}/${kind}`
}

// --- captions (stage 2A) -----------------------------------------------------

const captionsBase = (projectId: string) => `/projects/${projectId}/captions`

export function getCaptionCatalog(projectId: string) {
  return request<CaptionCatalog>(`${captionsBase(projectId)}/catalog`)
}

export function listCaptionInputs(projectId: string) {
  return request<CaptionInputsResponse>(`${captionsBase(projectId)}/inputs`)
}

/** Without a path the backend opens the machine's own file dialog. */
export function importCaptionVideo(projectId: string, path?: string) {
  return request<{ cancelled: boolean; input_key: string | null; duplicate?: boolean }>(
    `${captionsBase(projectId)}/imports`,
    { method: 'POST', body: JSON.stringify({ path: path ?? null }) },
  )
}

export function selectCaptionInput(projectId: string, inputKey: string | null) {
  return request<{ selected: string | null }>(`${captionsBase(projectId)}/selection`, {
    method: 'PUT',
    body: JSON.stringify({ input_key: inputKey }),
  })
}

export function getCaptionInput(projectId: string, inputKey: string) {
  return request<CaptionInputState>(`${captionsBase(projectId)}/inputs/${inputKey}`)
}

/** The selected video for the preview player. A key, never a path. */
export function captionInputStreamUrl(projectId: string, inputKey: string) {
  return `${API_BASE}${captionsBase(projectId)}/inputs/${inputKey}/stream`
}

/** Open the video behind an input key (cut, zoomed, footage…) in VLC. */
export function openInputInVlc(projectId: string, inputKey: string) {
  return request<{ opened: boolean }>(`${captionsBase(projectId)}/inputs/${inputKey}/open`, {
    method: 'POST',
  })
}

/** Returns either a reused transcript or the queued job. */
export function startTranscription(
  projectId: string,
  inputKey: string,
  settings: TranscriptionSettings,
  force: boolean,
) {
  return request<{ reused: TranscriptSummary | null; job: Job | null }>(
    `${captionsBase(projectId)}/transcriptions`,
    { method: 'POST', body: JSON.stringify({ input_key: inputKey, settings, force }) },
  )
}

export function downloadWhisperModel(projectId: string, model: string) {
  return request<Job>(`${captionsBase(projectId)}/model-downloads`, {
    method: 'POST',
    body: JSON.stringify({ model }),
  })
}

export function prepareCaptionRenderer(projectId: string) {
  return request<Job>(`${captionsBase(projectId)}/renderer-setup`, { method: 'POST' })
}

export function getCaptionRevision(projectId: string, transcriptId: string, revision: number) {
  return request<CaptionRevision>(
    `${captionsBase(projectId)}/transcripts/${transcriptId}/revisions/${revision}`,
  )
}

export function validateCaptions(projectId: string, transcriptId: string, captions: EditableCaption[]) {
  return request<CaptionCheck>(`${captionsBase(projectId)}/transcripts/${transcriptId}/validate`, {
    method: 'POST',
    body: JSON.stringify({ captions }),
  })
}

export function captionOperation(
  projectId: string,
  transcriptId: string,
  captions: EditableCaption[],
  operation: 'split' | 'merge' | 'fix_overlaps',
  options: { captionId?: string; wordIndex?: number; atSeconds?: number } = {},
) {
  return request<CaptionCheck>(`${captionsBase(projectId)}/transcripts/${transcriptId}/operations`, {
    method: 'POST',
    body: JSON.stringify({
      captions,
      operation,
      caption_id: options.captionId ?? null,
      word_index: options.wordIndex ?? null,
      at_seconds: options.atSeconds ?? null,
    }),
  })
}

export function saveCaptionRevision(
  projectId: string,
  transcriptId: string,
  baseRevision: number,
  captions: EditableCaption[],
) {
  return request<CaptionRevision>(`${captionsBase(projectId)}/transcripts/${transcriptId}/revisions`, {
    method: 'POST',
    body: JSON.stringify({ base_revision: baseRevision, captions }),
  })
}

export function regroupCaptions(
  projectId: string,
  transcriptId: string,
  baseRevision: number | null,
  grouping: CaptionGrouping,
  confirmDiscardEdits: boolean,
) {
  return request<CaptionRevision>(`${captionsBase(projectId)}/transcripts/${transcriptId}/regroup`, {
    method: 'POST',
    body: JSON.stringify({
      base_revision: baseRevision,
      grouping,
      confirm_discard_edits: confirmDiscardEdits,
    }),
  })
}

export function saveCaptionStyle(projectId: string, style: CaptionStyle) {
  return request<{ style: CaptionStyle; style_revision: number }>(`${captionsBase(projectId)}/style`, {
    method: 'PUT',
    body: JSON.stringify({ style }),
  })
}

export function startCaptionExport(
  projectId: string,
  transcriptId: string,
  revision: number,
  format: 'mp4' | 'srt',
  /** Draw on a zoomed version of the transcript's video instead (lineage-checked). */
  inputKey: string | null = null,
) {
  return request<Job>(`${captionsBase(projectId)}/exports`, {
    method: 'POST',
    body: JSON.stringify({ transcript_id: transcriptId, revision, format, input_key: inputKey }),
  })
}

export function captionExportUrl(projectId: string, exportId: string, download: boolean) {
  return `${API_BASE}${captionsBase(projectId)}/exports/${exportId}/file${download ? '?download=true' : ''}`
}

// --- zooms (stage 2B) ---------------------------------------------------------

const zoomsBase = (projectId: string) => `/projects/${projectId}/zooms`

/** Provider readiness (never the key), presets, inputs and settings. */
export function getZoomStatus(projectId: string) {
  return request<ZoomStatus>(`${zoomsBase(projectId)}/status`)
}

export function saveZoomSettings(projectId: string, changes: { preference?: string; selected_input?: string }) {
  return request<ZoomStatus['settings']>(`${zoomsBase(projectId)}/settings`, {
    method: 'PUT',
    body: JSON.stringify(changes),
  })
}

/** State only: reading never calls the AI provider or renders anything. */
export function getZoomInput(projectId: string, inputKey: string) {
  return request<ZoomInputState>(`${zoomsBase(projectId)}/inputs/${inputKey}`)
}

/** Idempotent: returns the running job, a reused result, or a new job. */
export function startZooms(
  projectId: string,
  inputKey: string,
  options: { forceReplan?: boolean; planId?: string } = {},
) {
  return request<{ job: Job | null; reused: ZoomRun | null }>(`${zoomsBase(projectId)}/runs`, {
    method: 'POST',
    body: JSON.stringify({
      input_key: inputKey,
      force_replan: options.forceReplan ?? false,
      plan_id: options.planId ?? null,
    }),
  })
}

export function approveZoomRun(projectId: string, runId: string, outputDigest: string, captions: ZoomOverlayReference) {
  return request<ZoomApproveResult>(`${zoomsBase(projectId)}/runs/${runId}/approve`, {
    method: 'POST',
    body: JSON.stringify({ output_digest: outputDigest, captions }),
  })
}

/** The actual FFmpeg output, by run id. */
export function zoomVideoUrl(projectId: string, runId: string, download = false) {
  return `${API_BASE}${zoomsBase(projectId)}/runs/${runId}/video${download ? '?download=true' : ''}`
}

// --- B-roll (stage 3A) ----------------------------------------------------------

/**
 * Asset libraries. Without a project: the general library, shared by every
 * video. With one: what that video sees — its own assets and the general
 * ones. An asset is reached through a video that may see it.
 */
const libraryBase = (projectId: string | null = null) =>
  projectId ? `/projects/${projectId}/broll/library` : '/broll/library'

/** With `ownOnly`, just the video's own assets. */
export function getBrollLibrary(projectId: string | null = null, ownOnly = false) {
  return request<{ assets: BrollLibraryAsset[]; storage_note: string }>(
    `${libraryBase(projectId)}${projectId && ownOnly ? '?own_only=true' : ''}`,
  )
}

/** Into the video's own library, or the general one. No path opens the machine's file dialog on the backend. */
export function importBrollAsset(path: string | null, projectId: string | null = null) {
  return request<{ cancelled: boolean; asset: BrollLibraryAsset | null; duplicate: boolean }>(`${libraryBase(projectId)}/imports`, {
    method: 'POST',
    body: JSON.stringify({ path }),
  })
}

export function updateBrollAsset(
  assetId: string,
  changes: { display_name?: string | null; description?: string | null; tags?: string[]; moments?: BrollMoment[] },
  projectId: string | null = null,
) {
  return request<BrollLibraryAsset>(`${libraryBase(projectId)}/${assetId}`, { method: 'PATCH', body: JSON.stringify(changes) })
}

export function remeasureBrollAsset(assetId: string, projectId: string | null = null) {
  return request<BrollLibraryAsset>(`${libraryBase(projectId)}/${assetId}/remeasure`, { method: 'POST' })
}

export function removeBrollAsset(assetId: string, projectId: string | null = null) {
  return request<{ removed: string }>(`${libraryBase(projectId)}/${assetId}`, { method: 'DELETE' })
}

/** `general`: share with every video. `project`: this video's alone. */
export function moveBrollAsset(assetId: string, projectId: string, to: BrollLibraryAsset['library']) {
  return request<BrollLibraryAsset>(`${libraryBase(projectId)}/${assetId}/move`, {
    method: 'POST',
    body: JSON.stringify({ to }),
  })
}

/** Versioned by fingerprint so a re-measured file is never served from cache. */
export function brollAssetMediaUrl(assetId: string, digest: string, projectId: string | null = null) {
  return `${API_BASE}${libraryBase(projectId)}/${assetId}/media?v=${digest.slice(7, 19)}`
}

export function brollAssetThumbnailUrl(asset: BrollLibraryAsset, projectId: string | null = null) {
  return `${API_BASE}${libraryBase(projectId)}/${asset.id}/thumbnail?v=${asset.fingerprint.digest.slice(7, 19)}`
}

const brollBase = (projectId: string) => `/projects/${projectId}/broll`

export function getBrollStatus(projectId: string) {
  return request<BrollStatus>(`${brollBase(projectId)}/status`)
}

export function saveBrollSettings(
  projectId: string,
  changes: { enabled?: boolean; selected_input?: string; preference?: string },
) {
  return request<BrollStatus['settings']>(`${brollBase(projectId)}/settings`, {
    method: 'PUT',
    body: JSON.stringify(changes),
  })
}

/** State only: reading never renders anything. */
export function getBrollInput(projectId: string, inputKey: string) {
  return request<BrollInputState>(`${brollBase(projectId)}/inputs/${inputKey}`)
}

export function addBrollPlacement(
  projectId: string,
  inputKey: string,
  body: { asset_id: string; at_seconds: number; template_id?: string; base_revision: number },
) {
  return request<BrollPlan>(`${brollBase(projectId)}/inputs/${inputKey}/placements`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function updateBrollPlacement(
  projectId: string,
  inputKey: string,
  placementId: string,
  changes: Record<string, unknown>,
  baseRevision: number,
) {
  return request<BrollPlan>(`${brollBase(projectId)}/inputs/${inputKey}/placements/${placementId}`, {
    method: 'PATCH',
    body: JSON.stringify({ changes, base_revision: baseRevision }),
  })
}

export function removeBrollPlacement(projectId: string, inputKey: string, placementId: string, baseRevision: number) {
  return request<BrollPlan>(
    `${brollBase(projectId)}/inputs/${inputKey}/placements/${placementId}?base_revision=${baseRevision}`,
    { method: 'DELETE' },
  )
}

export function decideBroll(projectId: string, inputKey: string, decision: 'approve' | 'skip', compositionDigest: string) {
  return request<{ decision: BrollDecision; next: BrollNext }>(`${brollBase(projectId)}/inputs/${inputKey}/${decision}`, {
    method: 'POST',
    body: JSON.stringify({ composition_digest: compositionDigest }),
  })
}

/**
 * Plan B-roll automatically (stage 3B). Idempotent: a running job is
 * returned, and an unedited plan for exactly today's inputs comes back as
 * `reused` without asking the provider again.
 */
export function startBrollPlanning(
  projectId: string,
  inputKey: string,
  options: { forceReplan?: boolean; replaceCurrent?: boolean } = {},
) {
  return request<{ job: ZoomJobSummary | null; reused: BrollPlannerRun | null }>(
    `${brollBase(projectId)}/inputs/${inputKey}/planner/runs`,
    {
      method: 'POST',
      body: JSON.stringify({ force_replan: options.forceReplan ?? false, replace_current: options.replaceCurrent ?? false }),
    },
  )
}

export function applyBrollPlanningRun(projectId: string, inputKey: string, runId: string, baseRevision: number) {
  return request<BrollPlan>(`${brollBase(projectId)}/inputs/${inputKey}/planner/runs/${runId}/apply`, {
    method: 'POST',
    body: JSON.stringify({ base_revision: baseRevision }),
  })
}

export function restoreBrollRevision(projectId: string, inputKey: string, revision: number, baseRevision: number) {
  return request<BrollPlan>(`${brollBase(projectId)}/inputs/${inputKey}/plan/restore/${revision}`, {
    method: 'POST',
    body: JSON.stringify({ base_revision: baseRevision }),
  })
}

export function brollExportUrl(projectId: string, exportId: string, download = false) {
  return `${API_BASE}${brollBase(projectId)}/exports/${exportId}/file${download ? '?download=true' : ''}`
}

export function brollBaseVideoUrl(projectId: string, inputKey: string, download = false) {
  return `${API_BASE}${brollBase(projectId)}/inputs/${inputKey}/video${download ? '?download=true' : ''}`
}

// --- background music (stage 4) -------------------------------------------------------

export function getMusicLibrary() {
  return request<{ assets: AudioTrack[]; storage_note: string; rights_note: string }>('/audio/library?kind=music')
}

/** No path opens the machine's file dialog on the backend. */
export function importMusicTrack(path: string | null) {
  return request<{ cancelled: boolean; asset: AudioTrack | null; duplicate: boolean }>('/audio/library/imports', {
    method: 'POST',
    body: JSON.stringify({ path, kind: 'music' }),
  })
}

export function renameMusicTrack(trackId: string, displayName: string | null) {
  return request<AudioTrack>(`/audio/library/${trackId}`, {
    method: 'PATCH',
    body: JSON.stringify({ display_name: displayName }),
  })
}

export function remeasureMusicTrack(trackId: string) {
  return request<AudioTrack>(`/audio/library/${trackId}/remeasure`, { method: 'POST' })
}

/** Removes the library entry only; the file on disk is never touched. */
export function removeMusicTrack(trackId: string) {
  return request<{ removed: string }>(`/audio/library/${trackId}`, { method: 'DELETE' })
}

/** The digest busts the browser cache when the file is re-measured. */
export function musicTrackMediaUrl(trackId: string, digest: string) {
  return `${API_BASE}/audio/library/${trackId}/media?v=${digest.slice(7, 19)}`
}

const musicBase = (projectId: string) => `/projects/${projectId}/music`

export function getMusicState(projectId: string) {
  return request<MusicState>(musicBase(projectId))
}

export function saveMusicSettings(projectId: string, changes: Partial<MusicSettings>) {
  return request<MusicSettings>(`${musicBase(projectId)}/settings`, {
    method: 'PUT',
    body: JSON.stringify(changes),
  })
}

export function decideMusic(projectId: string, decision: 'approve' | 'skip', compositionDigest: string) {
  return request<{ decision: MusicDecision; next: MusicNext }>(`${musicBase(projectId)}/${decision}`, {
    method: 'POST',
    body: JSON.stringify({ composition_digest: compositionDigest }),
  })
}

/** Play the finished video in VLC on this machine. */
export function openFinalExportInVlc(projectId: string, exportId: string) {
  return request<{ opened: boolean }>(`${musicBase(projectId)}/exports/${exportId}/open`, { method: 'POST' })
}

/** Show the finished video, selected, in File Explorer on this machine. */
export function revealFinalExport(projectId: string, exportId: string) {
  return request<{ opened: boolean }>(`${musicBase(projectId)}/exports/${exportId}/reveal`, { method: 'POST' })
}

export function finalExportUrl(projectId: string, exportId: string, download = false) {
  return `${API_BASE}${musicBase(projectId)}/exports/${exportId}/file${download ? '?download=true' : ''}`
}

// --- the stage pipeline ----------------------------------------------------------------

/** Every stage's status and the live preview. Reading never renders anything. */
export function getPipeline(projectId: string) {
  return request<PipelineState>(`/projects/${projectId}/pipeline`)
}

/** Accept exactly the candidate the screen showed (by its token). */
export function acceptStage(projectId: string, stageId: string, token: string) {
  return request<PipelineState>(`/projects/${projectId}/pipeline/${stageId}/accept`, {
    method: 'POST',
    body: JSON.stringify({ token }),
  })
}
