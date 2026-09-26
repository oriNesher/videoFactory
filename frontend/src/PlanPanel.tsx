import { useCallback, useEffect, useRef, useState } from 'react'
import {
  approvePlanRevision,
  executePlanRevision,
  generatePlan,
  getCapabilities,
  getLlmStatus,
  getPlanRevision,
  getResources,
  listPlans,
  savePlanRevision,
} from './api'
import type {
  Capability,
  CapabilityCatalog,
  LlmStatus,
  ParameterSpec,
  PlanAction,
  PlanRevision,
  PlanSummary,
  Project,
  ProjectResource,
} from './types'

type Message = { kind: 'ok' | 'error'; text: string } | null

function formatDate(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('en-US')
}

function sameActions(a: PlanAction[], b: PlanAction[]): boolean {
  return JSON.stringify(a) === JSON.stringify(b)
}

/** One parameter, rendered from the capability's own specification. */
function ParameterField({
  name,
  spec,
  value,
  onChange,
}: {
  name: string
  spec: ParameterSpec
  value: unknown
  onChange: (next: unknown) => void
}) {
  if (spec.type === 'string_list' && spec.choices) {
    const selected = Array.isArray(value) ? (value as string[]) : []
    return (
      <div className="field">
        <span>
          {name}
          {spec.description && (
            <span className="hint small"> — {spec.description}</span>
          )}
        </span>
        <div className="row">
          {spec.choices.map((choice) => (
            <label key={choice} className="choice">
              <input
                type="checkbox"
                checked={selected.includes(choice)}
                onChange={(event) =>
                  onChange(
                    event.target.checked
                      ? [...selected, choice]
                      : selected.filter((entry) => entry !== choice),
                  )
                }
              />
              <span>{choice}</span>
            </label>
          ))}
        </div>
      </div>
    )
  }

  if (spec.type === 'boolean') {
    return (
      <label className="choice">
        <input
          type="checkbox"
          checked={value === true}
          onChange={(event) => onChange(event.target.checked)}
        />
        <span>{name}</span>
      </label>
    )
  }

  if (spec.type === 'string' && spec.choices) {
    return (
      <label className="field">
        <span>{name}</span>
        <select
          value={typeof value === 'string' ? value : ''}
          onChange={(event) => onChange(event.target.value)}
        >
          {spec.choices.map((choice) => (
            <option key={choice} value={choice}>
              {choice}
            </option>
          ))}
        </select>
      </label>
    )
  }

  if (spec.type === 'integer' || spec.type === 'number') {
    return (
      <label className="field">
        <span>{name}</span>
        <input
          type="number"
          value={typeof value === 'number' ? value : ''}
          min={spec.min}
          max={spec.max}
          onChange={(event) =>
            onChange(
              event.target.value === '' ? null : Number(event.target.value),
            )
          }
        />
      </label>
    )
  }

  return (
    <label className="field">
      <span>{name}</span>
      <input
        type="text"
        value={typeof value === 'string' ? value : ''}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  )
}

/** A parameter and its value on one read-only line, e.g. `audio_threshold 0.04`. */
function summariseParameters(
  capability: Capability,
  action: PlanAction,
): string {
  return Object.entries(capability.parameters)
    .map(([name, spec]) => {
      const value = action.parameters[name] ?? spec.default
      return `${name} ${Array.isArray(value) ? value.join('+') : String(value)}`
    })
    .join(' · ')
}

/**
 * One action of a plan.
 *
 * The parameters start collapsed behind a read-only summary, the same way a
 * past cut run shows the settings it used. A capability's settings are edited
 * in one place — its own panel — and a plan that happens to reuse the same
 * five numbers does not stamp a second copy of that form onto the page.
 */
function ActionCard({
  action,
  capability,
  kindLabel,
  resourceName,
  busy,
  onChange,
  onRemove,
}: {
  action: PlanAction
  capability: Capability | undefined
  kindLabel: Record<string, string>
  resourceName: (id: string) => string
  busy: boolean
  onChange: (changes: Partial<PlanAction>) => void
  onRemove: () => void
}) {
  const [open, setOpen] = useState(false)
  const parameterCount = capability
    ? Object.keys(capability.parameters).length
    : 0

  return (
    <li>
      <div className="job-header">
        <span className="job-title">
          <span className="mono small">{action.id}</span>
          {capability?.title ?? action.capability_id}
          {capability ? (
            <span className="badge">
              {kindLabel[capability.kind] ?? capability.kind}
            </span>
          ) : (
            <span className="badge bad">Unsupported capability</span>
          )}
        </span>
        <span className="row">
          {parameterCount > 0 && (
            <button type="button" onClick={() => setOpen(!open)}>
              {open ? 'Hide settings' : `Edit settings (${parameterCount})`}
            </button>
          )}
          <button type="button" onClick={onRemove} disabled={busy}>
            Remove action
          </button>
        </span>
      </div>

      {capability ? (
        <p className="hint small">{capability.purpose}</p>
      ) : (
        <p className="message error">
          Capability "{action.capability_id}" does not exist in this application,
          so the plan cannot be run. Remove the action or create a new plan.
        </p>
      )}

      {capability && parameterCount > 0 && !open && (
        <p className="hint small mono">
          {summariseParameters(capability, action)}
        </p>
      )}

      {capability &&
        open &&
        Object.entries(capability.parameters).map(([name, spec]) => (
          <ParameterField
            key={name}
            name={name}
            spec={spec}
            value={action.parameters[name]}
            onChange={(next) =>
              onChange({ parameters: { ...action.parameters, [name]: next } })
            }
          />
        ))}

      {action.resource_ids.length > 0 && (
        <p className="hint small">
          Footage: {action.resource_ids.map(resourceName).join(', ')}
        </p>
      )}

      <label className="field wide">
        <span>Note</span>
        <input
          type="text"
          value={action.note}
          onChange={(event) => onChange({ note: event.target.value })}
        />
      </label>
    </li>
  )
}

type Props = {
  project: Project
  refreshToken: number
  onJobSubmitted: () => void
}

export default function PlanPanel({
  project,
  refreshToken,
  onJobSubmitted,
}: Props) {
  const [llm, setLlm] = useState<LlmStatus | null>(null)
  const [catalog, setCatalog] = useState<CapabilityCatalog | null>(null)
  const [resources, setResources] = useState<ProjectResource[]>([])

  const [plans, setPlans] = useState<PlanSummary[]>([])
  const [plan, setPlan] = useState<PlanRevision | null>(null)

  const [draftSummary, setDraftSummary] = useState('')
  const [draftActions, setDraftActions] = useState<PlanAction[]>([])

  const [instruction, setInstruction] = useState('')
  const [message, setMessage] = useState<Message>(null)
  const [busy, setBusy] = useState(false)

  const projectId = project.id
  const dirty =
    plan !== null &&
    (draftSummary !== plan.summary || !sameActions(draftActions, plan.actions))

  useEffect(() => {
    void (async () => {
      try {
        const [status, capabilities] = await Promise.all([
          getLlmStatus(),
          getCapabilities(),
        ])
        setLlm(status)
        setCatalog(capabilities)
      } catch {
        // The panel still works for reading; errors surface on use.
      }
    })()
  }, [])

  // Which revision is open, readable from `refresh` without making the plan
  // itself a dependency (which would restart the effect on every poll).
  const opened = useRef<{ planId: string; revision: number } | null>(null)

  // A refresh must never overwrite edits the user has not saved yet.
  const hasUnsavedEdits = useRef(false)

  useEffect(() => {
    hasUnsavedEdits.current = dirty
  }, [dirty])

  const adopt = useCallback((revision: PlanRevision) => {
    opened.current = { planId: revision.plan_id, revision: revision.revision }
    setPlan(revision)
    setDraftSummary(revision.summary)
    setDraftActions(revision.actions)
  }, [])

  const openRevision = useCallback(
    async (planId: string, revision: number) => {
      try {
        adopt(await getPlanRevision(projectId, planId, revision))
      } catch (caught) {
        setMessage({
          kind: 'error',
          text: caught instanceof Error ? caught.message : 'Loading the plan failed.',
        })
      }
    },
    [adopt, projectId],
  )

  const refresh = useCallback(async () => {
    try {
      const [listing, catalogue] = await Promise.all([
        listPlans(projectId),
        getResources(projectId),
      ])
      setPlans(listing.plans)
      setResources(catalogue.resources)

      // Keep the open plan in sync: approval, staleness and the revision list
      // all change server-side.
      const current = opened.current
      if (
        current &&
        !hasUnsavedEdits.current &&
        listing.plans.some((summary) => summary.plan_id === current.planId)
      ) {
        await openRevision(current.planId, current.revision)
      }
    } catch (caught) {
      setMessage({
        kind: 'error',
        text: caught instanceof Error ? caught.message : 'Loading the plans failed.',
      })
    }
  }, [openRevision, projectId])

  useEffect(() => {
    void refresh()
    // `project.updated_at` matters: changing sources can make a plan outdated.
  }, [refresh, refreshToken, project.updated_at])

  async function handleGenerate(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    try {
      await generatePlan(projectId, instruction)
      setMessage({
        kind: 'ok',
        text: 'The request was queued as a background job. Progress and result are in the jobs panel above.',
      })
      onJobSubmitted()
    } catch (caught) {
      setMessage({
        kind: 'error',
        text: caught instanceof Error ? caught.message : 'Generating the plan failed.',
      })
    } finally {
      setBusy(false)
    }
  }

  async function handleSaveRevision() {
    if (!plan) return
    setBusy(true)
    try {
      const saved = await savePlanRevision(
        projectId,
        plan.plan_id,
        draftSummary,
        draftActions,
      )
      adopt(saved)
      setMessage({
        kind: 'ok',
        text: `Saved revision ${saved.revision}. The previous revision is kept as it was, and the new one is waiting for approval.`,
      })
      await refresh()
    } catch (caught) {
      setMessage({
        kind: 'error',
        text: caught instanceof Error ? caught.message : 'Saving the revision failed.',
      })
    } finally {
      setBusy(false)
    }
  }

  async function handleApprove() {
    if (!plan) return
    setBusy(true)
    try {
      adopt(await approvePlanRevision(projectId, plan.plan_id, plan.revision))
      setMessage({ kind: 'ok', text: 'The plan is approved and can be run.' })
      await refresh()
    } catch (caught) {
      setMessage({
        kind: 'error',
        text: caught instanceof Error ? caught.message : 'Approval failed.',
      })
    } finally {
      setBusy(false)
    }
  }

  async function handleExecute() {
    if (!plan) return
    setBusy(true)
    try {
      await executePlanRevision(projectId, plan.plan_id, plan.revision)
      setMessage({
        kind: 'ok',
        text: 'The run was added to the job queue. The result will appear in the jobs panel.',
      })
      onJobSubmitted()
    } catch (caught) {
      setMessage({
        kind: 'error',
        text: caught instanceof Error ? caught.message : 'Running failed.',
      })
    } finally {
      setBusy(false)
    }
  }

  function updateAction(index: number, changes: Partial<PlanAction>) {
    setDraftActions(
      draftActions.map((action, position) =>
        position === index ? { ...action, ...changes } : action,
      ),
    )
  }

  function capabilityOf(id: string): Capability | undefined {
    return catalog?.capabilities.find((capability) => capability.id === id)
  }

  function resourceName(id: string): string {
    return resources.find((resource) => resource.id === id)?.filename ?? id
  }

  return (
    <section className="panel subpanel">
      <div className="editor-header">
        <h3>Editing plan (AI)</h3>
        {llm && (
          <span className={llm.is_mock ? 'badge warn' : 'badge ok'}>
            {llm.is_mock ? llm.label : `${llm.label} · ${llm.model}`}
          </span>
        )}
      </div>

      {llm && <p className="hint small">{llm.message}</p>}
      {llm && !llm.is_mock && llm.ready && (
        <p className="hint small">
          Sent to the provider: {llm.data_sent.join(', ')}. Not sent:{' '}
          {llm.data_not_sent.join(', ')}.
        </p>
      )}
      {llm && !llm.ready && (
        <p className="message error">{llm.message}</p>
      )}

      <form className="field" onSubmit={handleGenerate}>
        <span>What needs doing in this video?</span>
        <textarea
          rows={3}
          value={instruction}
          placeholder="For example: check that all the processing tools are installed and working"
          onChange={(event) => setInstruction(event.target.value)}
        />
        <div className="row">
          <button
            type="submit"
            className="primary"
            disabled={busy || !instruction.trim() || (llm ? !llm.ready : false)}
          >
            Ask for a plan
          </button>
        </div>
      </form>

      {catalog && (
        <p className="hint small">
          Capabilities available now: {catalog.capabilities.map((c) => c.title).join(', ')}.
          Not supported yet: {catalog.not_yet_supported.join(', ')}.
        </p>
      )}

      {message && (
        <p className={`message ${message.kind === 'ok' ? 'ok' : 'error'}`}>
          {message.text}
        </p>
      )}

      {plans.length === 0 && (
        <p className="hint">No plans in this project yet.</p>
      )}

      {plans.length > 0 && (
        <ul className="plan-list">
          {plans.map((entry) => (
            <li key={entry.plan_id}>
              <button
                type="button"
                className={
                  entry.plan_id === plan?.plan_id ? 'entry active' : 'entry'
                }
                onClick={() => void openRevision(entry.plan_id, entry.revision)}
                disabled={busy}
              >
                <span className="entry-name">{entry.summary}</span>
                <span className="small">
                  revision {entry.revision} · {entry.action_count} actions · updated{' '}
                  {formatDate(entry.revised_at)}
                </span>
                <span className="row">
                  <span className={entry.approved ? 'badge ok' : 'badge'}>
                    {entry.approved ? 'Approved' : 'Proposal'}
                  </span>
                  {entry.outdated && (
                    <span className="badge bad">Out of date</span>
                  )}
                  {entry.provider.is_mock && (
                    <span className="badge warn">Demo</span>
                  )}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {plan && (
        <div className="plan-detail">
          <div className="editor-header">
            <h3>
              Plan · revision {plan.revision}
              {dirty && <span className="badge warn">Unsaved changes</span>}
            </h3>
            <div className="row">
              {plan.revisions.map((revision) => (
                <button
                  key={revision}
                  type="button"
                  className={revision === plan.revision ? 'tab active' : 'tab'}
                  onClick={() => void openRevision(plan.plan_id, revision)}
                  disabled={busy}
                >
                  Revision {revision}
                </button>
              ))}
            </div>
          </div>

          <p className="hint small">
            Instruction: {plan.instruction} · created by{' '}
            {plan.origin === 'ai' ? plan.provider.label : 'manual editing'} ·{' '}
            {formatDate(plan.revised_at)}
          </p>

          {plan.provider.is_mock && (
            <p className="message">
              This plan was produced in demo mode by fixed rules on this machine,
              not by an AI model.
            </p>
          )}

          {plan.outdated && (
            <p className="message error">
              {plan.outdated_reason} Create a new plan before approving or running.
            </p>
          )}

          {!plan.is_latest && (
            <p className="message">
              This is not the latest revision (the latest is {plan.latest_revision}).
            </p>
          )}

          <label className="field wide">
            <span>Summary</span>
            <textarea
              rows={3}
              value={draftSummary}
              onChange={(event) => setDraftSummary(event.target.value)}
            />
          </label>

          <h4>Actions ({draftActions.length})</h4>

          <ol className="plan-actions">
            {draftActions.map((action, index) => {
              return (
                <ActionCard
                  key={action.id}
                  action={action}
                  capability={capabilityOf(action.capability_id)}
                  kindLabel={catalog?.kind_labels ?? {}}
                  resourceName={resourceName}
                  busy={busy}
                  onChange={(changes) => updateAction(index, changes)}
                  onRemove={() =>
                    setDraftActions(
                      draftActions.filter((_, position) => position !== index),
                    )
                  }
                />
              )
            })}
          </ol>

          <div className="row">
            <button
              type="button"
              onClick={() => void handleSaveRevision()}
              disabled={busy || !dirty}
            >
              Save as a new revision
            </button>
            <button
              type="button"
              onClick={() => plan && adopt(plan)}
              disabled={busy || !dirty}
            >
              Discard changes
            </button>
            <button
              type="button"
              className="primary"
              onClick={() => void handleApprove()}
              disabled={
                busy || dirty || plan.approved || plan.outdated || !plan.is_latest
              }
            >
              {plan.approved ? 'Approved' : 'Approve plan'}
            </button>
            <button
              type="button"
              className="primary"
              onClick={() => void handleExecute()}
              disabled={busy || dirty || !plan.executable}
            >
              Run approved plan
            </button>
          </div>

          {dirty && (
            <p className="hint small">
              Save your changes as a new revision before approving or running.
            </p>
          )}
          {!dirty && plan.blocked_reason && (
            <p className="message">{plan.blocked_reason}</p>
          )}
          {plan.approved && (
            <p className="hint small">Approved on {formatDate(plan.approved_at)}.</p>
          )}
        </div>
      )}
    </section>
  )
}
