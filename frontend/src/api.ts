import type {
  CapabilityCatalog,
  Job,
  JobListing,
  LlmStatus,
  PlanListing,
  PlanRevision,
  Project,
  ProjectListing,
  ResourceCatalog,
  ToolStatus,
  ToolVersion,
} from './types'

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
    throw new Error('אין חיבור לשרת. ודא ששירות הפייתון פועל.')
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
      return `הבקשה נדחתה: ${body.detail[0]?.msg ?? response.status}`
    }
  } catch {
    // fall through to the generic message
  }

  return `הפעולה נכשלה (שגיאה ${response.status}).`
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
