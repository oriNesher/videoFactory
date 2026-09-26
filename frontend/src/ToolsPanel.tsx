import { useState } from 'react'
import { checkTools, checkToolVersions } from './api'
import type { ToolStatus, ToolVersion } from './types'

const TOOL_LABELS: Record<string, string> = {
  ffmpeg: 'FFmpeg',
  ffprobe: 'FFprobe',
  auto_editor: 'Auto-Editor',
}

export default function ToolsPanel() {
  const [statuses, setStatuses] = useState<Record<string, ToolStatus> | null>(
    null,
  )
  const [versions, setVersions] = useState<Record<string, ToolVersion> | null>(
    null,
  )
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function run() {
    setBusy(true)
    setError('')

    try {
      const [toolStatuses, toolVersions] = await Promise.all([
        checkTools(),
        checkToolVersions(),
      ])
      setStatuses(toolStatuses)
      setVersions(toolVersions)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'The check failed.')
      setStatuses(null)
      setVersions(null)
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="panel">
      <h2>Processing tools</h2>
      <p className="hint">
        Checks that the external tools are reachable by Video Factory through the system PATH.
      </p>

      <button type="button" className="primary" onClick={run} disabled={busy}>
        {busy ? 'Checking…' : 'Check tools'}
      </button>

      {error && <p className="message error">{error}</p>}

      {statuses && (
        <table className="tools">
          <thead>
            <tr>
              <th>Tool</th>
              <th>Available</th>
              <th>Version</th>
              <th>Path</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(statuses).map(([key, status]) => {
              const version = versions?.[key]
              return (
                <tr key={key}>
                  <td>{TOOL_LABELS[key] ?? key}</td>
                  <td className={status.available ? 'ok' : 'bad'}>
                    {status.available ? 'Yes' : 'No'}
                  </td>
                  <td className="mono small">
                    {version?.working ? version.version : (version?.error ?? '—')}
                  </td>
                  <td className="mono small">{status.path ?? '—'}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </section>
  )
}
