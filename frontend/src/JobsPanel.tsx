import { useCallback, useEffect, useRef, useState } from 'react'
import { cancelJob, listJobs, retryJob, submitJob } from './api'
import type { Job, JobStatus, ToolCheckResult } from './types'

const STATUS_LABELS: Record<JobStatus, string> = {
  queued: 'Queued',
  running: 'Running',
  succeeded: 'Finished',
  failed: 'Failed',
  cancelled: 'Cancelled',
  interrupted: 'Interrupted',
}

const STATUS_CLASS: Record<JobStatus, string> = {
  queued: 'badge',
  running: 'badge warn',
  succeeded: 'badge ok',
  failed: 'badge bad',
  cancelled: 'badge',
  interrupted: 'badge bad',
}

const ACTIVE: JobStatus[] = ['queued', 'running']

/** How often the interface asks the backend for job state. */
const POLL_ACTIVE_MS = 1500
const POLL_IDLE_MS = 8000

function formatTime(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString('en-US')
}

function isToolCheckResult(result: unknown): result is ToolCheckResult {
  return (
    typeof result === 'object' &&
    result !== null &&
    Array.isArray((result as ToolCheckResult).tools)
  )
}

/** Pull every tool-check report out of a job result, whatever produced it. */
function toolReports(job: Job): ToolCheckResult[] {
  const result = job.result
  if (!result) return []

  if (isToolCheckResult(result)) return [result]

  const actionResults = (result as { action_results?: unknown }).action_results
  if (!Array.isArray(actionResults)) return []

  return actionResults
    .map((entry) => (entry as { output?: unknown })?.output)
    .filter(isToolCheckResult)
}

function ResultDetails({ job }: { job: Job }) {
  const reports = toolReports(job)
  const result = job.result as Record<string, unknown> | null

  if (reports.length > 0) {
    return (
      <div className="job-result">
        {reports.map((report, index) => (
          <table className="tools" key={index}>
            <thead>
              <tr>
                <th>Tool</th>
                <th>Available</th>
                <th>Version</th>
                <th>Path</th>
              </tr>
            </thead>
            <tbody>
              {report.tools.map((tool) => (
                <tr key={tool.tool}>
                  <td>{tool.label}</td>
                  <td className={tool.available && tool.working ? 'ok' : 'bad'}>
                    {tool.available ? 'Yes' : 'No'}
                  </td>
                  <td className="mono small">
                    {tool.working ? tool.version : (tool.error ?? '—')}
                  </td>
                  <td className="mono small">{tool.path ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ))}
      </div>
    )
  }

  if (result && typeof result.explanation === 'string') {
    return (
      <p className="message">
        <strong>No matching capability: </strong>
        {result.explanation}
      </p>
    )
  }

  if (result && typeof result.summary === 'string') {
    return <p className="hint small">{result.summary}</p>
  }

  return null
}

type Props = {
  projectId: string
  /** Bumped by the parent to force an immediate refresh. */
  refreshToken: number
  onJobFinished: () => void
}

export default function JobsPanel({
  projectId,
  refreshToken,
  onJobFinished,
}: Props) {
  const [jobs, setJobs] = useState<Job[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  // Which jobs were already finished last time we looked, so the parent is
  // notified once per transition rather than on every poll.
  const finishedIds = useRef<Set<string>>(new Set())
  const notify = useRef(onJobFinished)

  useEffect(() => {
    notify.current = onJobFinished
  }, [onJobFinished])

  const refresh = useCallback(async () => {
    try {
      const listing = await listJobs(projectId)
      setJobs(listing.jobs)
      setError('')

      let transitioned = false
      for (const job of listing.jobs) {
        const done = !ACTIVE.includes(job.status)
        if (done && !finishedIds.current.has(job.id)) {
          finishedIds.current.add(job.id)
          transitioned = true
        }
      }
      if (transitioned) notify.current()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Loading the jobs failed.')
    }
  }, [projectId])

  useEffect(() => {
    finishedIds.current = new Set()
  }, [projectId])

  useEffect(() => {
    void refresh()
  }, [refresh, refreshToken])

  // Polling: fast while something is running, slow when everything is idle.
  const hasActive = jobs.some((job) => ACTIVE.includes(job.status))

  useEffect(() => {
    const interval = window.setInterval(
      () => void refresh(),
      hasActive ? POLL_ACTIVE_MS : POLL_IDLE_MS,
    )
    return () => window.clearInterval(interval)
  }, [refresh, hasActive])

  async function run(action: () => Promise<unknown>) {
    setBusy(true)
    try {
      await action()
      await refresh()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'The action failed.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="panel subpanel">
      <div className="editor-header">
        <h3>Background jobs</h3>
        <div className="row">
          <button
            type="button"
            onClick={() => void run(() => submitJob(projectId, 'tool_check'))}
            disabled={busy}
          >
            Run tool check
          </button>
          <button type="button" onClick={() => void refresh()} disabled={busy}>
            Refresh
          </button>
        </div>
      </div>

      <p className="hint small">
        Jobs run in the background on a single queue. You can keep working meanwhile;
        their state is saved and survives a server restart.
      </p>

      {error && <p className="message error">{error}</p>}

      {jobs.length === 0 && <p className="hint">No jobs in this project yet.</p>}

      <ul className="jobs">
        {jobs.map((job) => {
          const active = ACTIVE.includes(job.status)
          return (
            <li key={job.id}>
              <div className="job-header">
                <span className="job-title">
                  {job.type_label}
                  <span className={STATUS_CLASS[job.status]}>
                    {STATUS_LABELS[job.status]}
                  </span>
                  {job.cancel_requested && active && (
                    <span className="badge warn">Cancellation requested</span>
                  )}
                  {job.retry_of && <span className="badge">Re-run</span>}
                </span>
                <span className="row">
                  {active && (
                    <button
                      type="button"
                      onClick={() => void run(() => cancelJob(projectId, job.id))}
                      disabled={busy || job.cancel_requested}
                    >
                      Cancel
                    </button>
                  )}
                  {!active && (
                    <button
                      type="button"
                      onClick={() => void run(() => retryJob(projectId, job.id))}
                      disabled={busy}
                    >
                      Run again
                    </button>
                  )}
                </span>
              </div>

              <p className="hint small">
                {job.progress_message}
                {job.progress_percent !== null && ` · ${job.progress_percent}%`}
              </p>

              {job.progress_percent !== null && active && (
                <div className="progress">
                  <div
                    className="progress-bar"
                    style={{ width: `${job.progress_percent}%` }}
                  />
                </div>
              )}

              {job.error && <p className="message error">{job.error}</p>}

              <ResultDetails job={job} />

              <p className="hint small">
                Created {formatTime(job.created_at)} · started{' '}
                {formatTime(job.started_at)} · finished{' '}
                {formatTime(job.finished_at)}
              </p>
            </li>
          )
        })}
      </ul>
    </section>
  )
}
