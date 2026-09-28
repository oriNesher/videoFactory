export type SourceMedia = {
  id: string
  path: string
  filename: string
  added_at: string
  exists: boolean
  size_bytes: number | null
  duration_seconds: number | null
}

/** What came of adding a folder: partial success is normal and is reported. */
export type AddDirectoryResult = {
  /** True when the folder dialog was dismissed; nothing else is set. */
  cancelled: boolean
  project?: Project
  directory?: string
  /** File names added, in the order they will be cut. */
  added?: string[]
  /** Already in the project, so left alone. */
  duplicates?: string[]
  /** Not video files, so skipped. */
  ignored?: string[]
  failed?: { filename: string; error: string }[]
}

export type Project = {
  schema_version: number
  id: string
  name: string
  created_at: string
  updated_at: string
  settings: Record<string, unknown>
  sources: SourceMedia[]
  directory: string
}

export type ProjectSummary = {
  id: string
  name: string
  created_at: string
  updated_at: string
  source_count: number
  missing_source_count: number
  error: string | null
}

export type ProjectListing = {
  workspace: string
  projects: ProjectSummary[]
}

export type ToolStatus = {
  available: boolean
  path: string | null
}

export type ToolVersion = {
  working: boolean
  version?: string
  exit_code?: number
  error?: string
}

/** One tool as reported by a tool-check job or plan execution. */
export type ToolCheckEntry = {
  tool: string
  label: string
  available: boolean
  path: string | null
  working: boolean
  version: string
  error: string | null
}

export type ToolCheckResult = {
  tools: ToolCheckEntry[]
  available_count: number
  checked_count: number
  summary: string
}

// --- jobs -------------------------------------------------------------------

export type JobStatus =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'interrupted'

export type Job = {
  schema_version: number
  id: string
  project_id: string
  type: string
  type_label: string
  status: JobStatus
  input: Record<string, unknown>
  created_at: string
  started_at: string | null
  finished_at: string | null
  progress_message: string
  /** Only present when the job can actually measure its progress. */
  progress_percent: number | null
  result: Record<string, unknown> | null
  error: string | null
  cancel_requested: boolean
  retry_of: string | null
}

export type JobListing = {
  project_id: string
  job_types: { type: string; label: string }[]
  jobs: Job[]
}

// --- catalogs ---------------------------------------------------------------

export type ParameterSpec = {
  type: 'string' | 'integer' | 'number' | 'boolean' | 'string_list'
  required?: boolean
  choices?: string[]
  default?: unknown
  min?: number
  max?: number
  min_items?: number
  max_items?: number
  description?: string
}

export type Capability = {
  id: string
  version: number
  kind: string
  title: string
  purpose: string
  executable: boolean
  resource_types: string[]
  max_resources: number
  parameters: Record<string, ParameterSpec>
}

export type CapabilityCatalog = {
  catalog_version: number
  capabilities: Capability[]
  not_yet_supported: string[]
  kind_labels: Record<string, string>
}

export type ProjectResource = {
  id: string
  filename: string
  media_type: string
  order: number
  available: boolean
  size_bytes: number | null
  added_at: string
  description: string
}

export type ResourceCatalog = {
  project_id: string
  catalog_version: number
  note: string
  resources: ProjectResource[]
  /** Files this application produced. Never offered as cutting inputs. */
  generated: GeneratedResource[]
  input_fingerprint: string
}

export type ProviderInfo = {
  id: string
  label: string
  model: string
  is_mock: boolean
}

export type LlmStatus = {
  provider: string
  label: string
  model: string
  is_mock: boolean
  ready: boolean
  message: string
  data_sent: string[]
  data_not_sent: string[]
}

// --- plans ------------------------------------------------------------------

export type PlanAction = {
  id: string
  capability_id: string
  capability_version: number
  resource_ids: string[]
  parameters: Record<string, unknown>
  note: string
}

export type PlanRevision = {
  schema_version: number
  plan_id: string
  project_id: string
  revision: number
  created_at: string
  revised_at: string
  origin: 'ai' | 'user'
  instruction: string
  provider: ProviderInfo
  capability_catalog_version: number
  resource_catalog_version: number
  input_fingerprint: string
  summary: string
  actions: PlanAction[]
  /** Derived server-side, never stored. */
  approved: boolean
  approved_at: string | null
  status: 'proposed' | 'approved'
  outdated: boolean
  outdated_reason: string | null
  is_latest: boolean
  latest_revision: number
  revisions: number[]
  executable: boolean
  blocked_reason: string | null
}

export type PlanSummary = {
  plan_id: string
  revision: number
  revisions: number[]
  created_at: string
  revised_at: string
  summary: string
  instruction: string
  origin: 'ai' | 'user'
  provider: ProviderInfo
  status: 'proposed' | 'approved'
  approved: boolean
  outdated: boolean
  executable: boolean
  action_count: number
}

export type PlanListing = {
  project_id: string
  plans: PlanSummary[]
}

// --- cutting (milestone 1A) --------------------------------------------------

export type CuttingParameterSpec = {
  name: string
  type: 'number'
  default: number
  min: number
  max: number
  step: number
  unit: string
  label: string
  description: string
  /** Which Auto-Editor flag this value ends up in. Shown so the mapping is visible. */
  maps_to: string
}

export type CuttingOutputMode = {
  id: 'clips' | 'combined' | 'both'
  label: string
  description: string
}

export type CuttingCatalog = {
  defaults: Record<string, number>
  default_output_mode: string
  parameters: CuttingParameterSpec[]
  max_sources_per_run: number
}

export type CuttingSettings = {
  project_id: string
  catalog: CuttingCatalog
  settings: Record<string, number>
  output_mode: string
  source_ids: string[]
}

export type StreamInfo = {
  codec_name: string
  width?: number
  height?: number
  pix_fmt?: string
  frame_rate?: number | null
  sample_rate?: number | null
  channels?: number | null
} | null

export type CutRunSource = {
  source_id: string
  order: number
  filename: string
  duration_seconds: number | null
  fingerprint: { method: string; digest: string; size_bytes: number }
  video: StreamInfo
  audio: StreamInfo
  /** Measured loudness. `peak_ratio` is on the same 0–1 scale as the threshold. */
  audio_level: {
    max_db: number | null
    mean_db: number | null
    peak_ratio: number | null
  } | null
}

export type CutClipStatus =
  | 'succeeded'
  | 'empty'
  | 'failed'
  | 'skipped'
  | 'cancelled'

export type CutClip = {
  output_id: string
  source_id: string
  order: number
  source_filename: string
  source_duration_seconds: number | null
  filename: string | null
  relative_path: string | null
  status: CutClipStatus
  duration_seconds: number | null
  size_bytes: number | null
  removed_seconds: number | null
  video: StreamInfo
  audio: StreamInfo
  error: string | null
  exit_code?: number
  command?: string
  log_path: string | null
  /** Derived server-side: is there a file behind this entry right now? */
  playable: boolean
}

export type CutCombined = {
  output_id: string
  requested: boolean
  filename: string
  status: CutClipStatus
  strategy: string | null
  clip_output_ids: string[]
  /** False when clips the user selected were left out of the joined file. */
  complete: boolean
  excluded_clips: { output_id: string; source_filename: string }[]
  duration_seconds: number | null
  expected_duration_seconds: number
  size_bytes: number | null
  video: StreamInfo
  audio: StreamInfo
  error: string | null
  playable: boolean
}

export type CutRunStatus = 'running' | 'succeeded' | 'failed' | 'cancelled'

export type CutRun = {
  schema_version: number
  run_id: string
  project_id: string
  job_id: string
  created_at: string
  finished_at: string | null
  status: CutRunStatus
  settings: Record<string, number>
  output_mode: string
  tool_versions: Record<string, string | null>
  sources: CutRunSource[]
  clips: CutClip[]
  combined: CutCombined | null
  error: string | null
  notes: string[]
  /** Derived server-side. */
  source_duration_seconds: number
  clip_duration_seconds: number
  removed_duration_seconds: number
  is_complete_result: boolean
  /** Subtitles per output id; only clips that have some or had an attempt. */
  subtitles: Record<string, ClipSubtitles>
  /** Subtitle jobs on this run that are queued or running. */
  active_jobs: { id: string; type: 'subtitles'; status: string }[]
}

export type CutRunListing = {
  project_id: string
  runs: CutRun[]
}

// --- subtitles (Whisper) ----------------------------------------------------

export type SubtitleParameterSpec = {
  name: string
  type: 'integer' | 'number'
  default: number
  min: number
  max: number
  step: number
  unit: string
  label: string
  description: string
}

export type SubtitleCatalog = {
  /** Is faster-whisper importable by the backend at all? */
  installed: boolean
  defaults: Record<string, number>
  parameters: SubtitleParameterSpec[]
  /** The one model the backend uses; not a choice. */
  model: {
    id: string
    label: string
    /** Roughly what the first use downloads. */
    download: string
    downloaded: boolean
  }
  language: string
}

export type SubtitleSettings = {
  project_id: string
  catalog: SubtitleCatalog
  settings: Record<string, number>
}

export type SubtitleAttemptStatus =
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'interrupted'

export type ClipSubtitles = {
  output_id: string
  /** True when an SRT is on disk for this clip. */
  available: boolean
  current: {
    job_id: string
    created_at: string
    model: string
    language: string
    detected_language: string | null
    line_count: number
    filename: string
  } | null
  /** The most recent attempt, which may have failed after `current` was made. */
  attempt: {
    status: SubtitleAttemptStatus
    job_id: string
    model: string
    error: string | null
  } | null
}

export type GeneratedResource = {
  id: string
  run_id: string
  run_status: CutRunStatus
  output_id: string
  kind: 'trimmed_clip' | 'combined_video'
  filename: string
  media_type: string
  produced_by: string
  from_source_id: string | null
  duration_seconds: number | null
  size_bytes: number | null
  created_at: string
}
