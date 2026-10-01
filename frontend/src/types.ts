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

export type CutMode = 'boundary' | 'full_clip'

export type CutModeInfo = {
  id: CutMode
  label: string
  description: string
  /** Which tool does the work in this mode. */
  tool: string
}

/** A numeric setting as the backend describes it: range, unit, meaning. */
export type NumberSpec = {
  name: string
  type: 'number'
  default: number
  min: number
  max: number
  step: number
  unit: string
  label: string
  description: string
}

export type CuttingCatalog = {
  /** The five full-clip (Auto-Editor) settings. */
  defaults: Record<string, number>
  default_output_mode: string
  parameters: CuttingParameterSpec[]
  max_sources_per_run: number
  modes: CutModeInfo[]
  default_mode: CutMode
  /** The boundary-only settings: a separate set, never shared with the five. */
  boundary: {
    defaults: Record<string, number>
    parameters: NumberSpec[]
    bridge_seconds: number
    initial_window_seconds: number
  }
  sample: NumberSpec
  previews: { kinds: SampleKind[]; source_context_seconds: number }
  /** What an AI recommendation request does and does not contain. */
  advice: { data_sent: string[]; data_not_sent: string[] }
}

/** One clip's manual boundary, in seconds of the source. */
export type BoundaryOverride = {
  start_seconds?: number
  end_seconds?: number
  keep_whole?: boolean
}

export type AppliedPlan = { plan_id: string; revision: number }

export type CuttingSettings = {
  project_id: string
  catalog: CuttingCatalog
  mode: CutMode
  /** False for a project that has never saved a cutting configuration. */
  mode_is_explicit: boolean
  settings: Record<string, number>
  boundary_settings: Record<string, number>
  overrides: Record<string, BoundaryOverride>
  sample_seconds: number
  output_mode: string
  source_ids: string[]
  /** Goes up whenever a saved change would move a cut. */
  revision: number
  applied_plan: AppliedPlan | null
}

/** The cutting form as it is sent with every request. */
export type CuttingForm = {
  mode: CutMode
  settings: Record<string, number>
  boundary_settings: Record<string, number>
  overrides: Record<string, BoundaryOverride>
  sample_seconds: number
  output_mode: string
  source_ids: string[]
  applied_plan: AppliedPlan | null
}

export type BoundaryWarning = {
  code: string
  severity: 'info' | 'warn'
  message: string
}

export type BoundaryOrigin = 'detected' | 'manual' | 'kept_whole' | 'source_limit'

/** Where one clip is cut: one continuous interval of its source. */
export type ClipBoundary = {
  status: 'detected' | 'no_activity' | 'no_audio'
  start_seconds: number
  end_seconds: number
  start_origin: BoundaryOrigin
  end_origin: BoundaryOrigin
  source_duration_seconds: number
  retained_seconds: number
  removed_leading_seconds: number
  removed_trailing_seconds: number
  activity_start_seconds: number | null
  activity_end_seconds: number | null
  leading_padding_seconds: number
  trailing_padding_seconds: number
  override: BoundaryOverride | null
  warnings: BoundaryWarning[]
  /** True when no confident boundary was found on a side left to detection. */
  needs_review: boolean
  stats: {
    peak: number | null
    background_level: number | null
    activity_level: number | null
  }
}

export type CutStateClip = {
  source_id: string
  order: number
  filename: string
  available: boolean
  duration_seconds: number | null
  override: BoundaryOverride | null
  /** False until this clip was analysed with the current detection settings. */
  analysed: boolean
  boundary: ClipBoundary | null
  error: string | null
}

export type SampleKind = 'opening' | 'ending' | 'join'

export type SampleRequest = {
  kind: SampleKind
  source_id: string
  next_source_id?: string
}

export type SampleFile = {
  filename: string
  duration_seconds: number
  size_bytes: number
}

export type CutSample = {
  sample_id: string
  job_id: string
  created_at: string
  kind: SampleKind
  source_ids: string[]
  sample_seconds: number
  sources: {
    source_id: string
    filename: string
    duration_seconds: number
    override: BoundaryOverride | null
    boundary: {
      start_seconds: number
      end_seconds: number
      start_origin: BoundaryOrigin
      end_origin: BoundaryOrigin
      retained_seconds: number
      needs_review: boolean
    }
  }[]
  /** Exactly what decided where this sample was cut. */
  basis: {
    mode: CutMode
    boundary_settings: Record<string, number>
    settings_revision: number | null
    applied_plan: AppliedPlan | null
  }
  edited: SampleFile & {
    segments: {
      source_id: string
      source_start_seconds: number
      source_end_seconds: number
    }[]
    /** Join previews: where the second clip takes over. */
    join_at_seconds?: number
  }
  /** The same region of the source plus context; absent for a join. */
  source_context:
    | (SampleFile & {
        source_start_seconds: number
        source_end_seconds: number
        /** Where, inside this excerpt, the edit starts (opening) or ends. */
        cut_at_seconds: number
        removed_shown_seconds: number
      })
    | null
  notes: string[]
  /** Derived server-side against the form as it stands now. */
  stale: boolean
  stale_reason: string | null
}

export type CutRecommendation = {
  plan_id: string
  revision: number
  latest_revision: number
  is_latest: boolean
  revised_at: string
  origin: 'ai' | 'user'
  provider: ProviderInfo
  request: string
  feedback: string | null
  mode: CutMode
  requested_in_mode: CutMode
  /** True when the proposal is for a different mode than the one selected. */
  mode_changed: boolean
  capability_id: string
  settings: Record<string, number>
  explanation: string
  limitations: string[]
  based_on_settings: Record<string, number> | null
  based_on_settings_revision: number | null
  approved: boolean
  applied: boolean
  stale: boolean
  stale_reason: string | null
}

export type CuttingJobSummary = {
  id: string
  type: string
  status: JobStatus
  progress_message: string
  progress_percent: number | null
  finished_at: string | null
  error: string | null
  result: Record<string, unknown> | null
}

/** The cutting screen's live state for the form as it stands. */
export type CuttingState = {
  project_id: string
  mode: CutMode
  config_fingerprint: string | null
  /** Null while the form holds unsaved changes. */
  settings_revision: number | null
  applied_plan: AppliedPlan | null
  clips: CutStateClip[]
  analysis_needed: boolean
  needs_review_count: number
  invalid_count: number
  original_seconds: number
  retained_seconds: number | null
  samples: CutSample[]
  recommendation: CutRecommendation | null
  active_jobs: CuttingJobSummary[]
  /** The newest finished job of each kind. */
  recent_jobs: Record<string, CuttingJobSummary>
}

export type CutMapInterval = {
  source_start: number
  source_end: number
  output_start: number
  output_end: number
}

/** A clip's source-to-output mapping, or why there is none. */
export type ClipCutMap = {
  available: boolean
  reason?: string
  origin?: string
  retained?: CutMapInterval[]
  removed?: { source_start: number; source_end: number; position: string }[]
  nominal_output_seconds?: number
  measured_output_seconds?: number
  difference_seconds?: number
  tolerance_seconds?: number
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
  /** Boundary-only runs: where this clip was cut, and how that was decided. */
  boundary?: ClipBoundary
  /** True when the source had no audio and a silent track was added. */
  audio_synthesised?: boolean
  cut_map?: ClipCutMap
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
  /** Runs made before modes existed are reported as `full_clip`. */
  mode: CutMode
  /** The five full-clip values; null for a boundary-only run. */
  settings: Record<string, number> | null
  /** The boundary-only values; null for a full-clip run. */
  boundary_settings: Record<string, number> | null
  /** The saved settings revision the run used; null for an unsaved form. */
  settings_revision: number | null
  applied_plan: AppliedPlan | null
  /** The plan that ran this, when it was run from the plan panel. */
  plan: AppliedPlan | null
  cut_map: {
    available: boolean
    reason?: string
    mapped_clip_count?: number
    clip_count?: number
    sequence_rendered?: boolean
  }
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
