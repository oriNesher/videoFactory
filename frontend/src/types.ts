export type SourceMedia = {
  id: string
  path: string
  filename: string
  added_at: string
  exists: boolean
  size_bytes: number | null
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
