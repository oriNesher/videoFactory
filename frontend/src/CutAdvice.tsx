import { useState } from 'react'
import type {
  CutMode,
  CutRecommendation,
  CuttingCatalog,
  CuttingJobSummary,
  LlmStatus,
  NumberSpec,
} from './types'

/**
 * AI-assisted cutting settings.
 *
 * Optional, and never in the way: the manual form above works without it.
 * The model is given measurements — never media — and returns a proposal that
 * is stored as a plan. It changes nothing until the user approves it here, and
 * approving it only fills in the form; rendering stays a separate press.
 */

const EXAMPLES = [
  {
    label: 'Natural delivery',
    text: 'Remove the dead time around each take, but keep my delivery natural.',
  },
  {
    label: 'Tighter joins',
    text: 'Make the joins slightly tighter without clipping the first or last word.',
  },
  {
    label: 'More breathing room',
    text: 'Leave a little more breathing room after each section.',
  },
]

type Props = {
  llm: LlmStatus | null
  catalog: CuttingCatalog
  /** The mode selected in the form right now. */
  mode: CutMode
  recommendation: CutRecommendation | null
  /** How the last recommendation job ended, to show a refusal or a failure. */
  lastJob: CuttingJobSummary | undefined
  /** True while a recommendation job is queued or running. */
  working: boolean
  disabled: boolean
  onAsk: (request: string, feedback?: string, revisePlanId?: string) => void
  onSaveEdits: (recommendation: CutRecommendation, settings: Record<string, number>) => void
  onApply: (recommendation: CutRecommendation, confirmModeChange: boolean) => void
}

function specsFor(catalog: CuttingCatalog, mode: CutMode): NumberSpec[] {
  return mode === 'boundary' ? catalog.boundary.parameters : catalog.parameters
}

function modeLabel(catalog: CuttingCatalog, mode: CutMode): string {
  return catalog.modes.find((entry) => entry.id === mode)?.label ?? mode
}

function sameSettings(a: Record<string, number>, b: Record<string, number>): boolean {
  return Object.keys(a).every((name) => a[name] === b[name])
}

/** The proposal itself: what it would change, editable before it applies. */
function Proposal({
  catalog,
  mode,
  recommendation,
  working,
  disabled,
  onAsk,
  onSaveEdits,
  onApply,
}: Omit<Props, 'llm' | 'lastJob' | 'recommendation'> & {
  recommendation: CutRecommendation
}) {
  const [draft, setDraft] = useState<Record<string, number>>(recommendation.settings)
  const [confirmed, setConfirmed] = useState(false)
  const [feedback, setFeedback] = useState('')

  const dirty = !sameSettings(draft, recommendation.settings)
  const before = recommendation.based_on_settings
  const blocked =
    recommendation.stale || !recommendation.is_latest || recommendation.applied

  return (
    <div className="plan-detail">
      <div className="job-header">
        <span className="job-title">
          Proposal · revision {recommendation.revision}
          <span className="badge">{modeLabel(catalog, recommendation.mode)}</span>
          {recommendation.provider.is_mock && <span className="badge warn">Demo, not AI</span>}
          {recommendation.origin === 'user' && <span className="badge">Edited by you</span>}
          {recommendation.applied && <span className="badge ok">Applied</span>}
          {!recommendation.applied && recommendation.approved && (
            <span className="badge ok">Approved</span>
          )}
          {recommendation.stale && <span className="badge bad">Out of date</span>}
          {dirty && <span className="badge warn">Unsaved edits</span>}
        </span>
      </div>

      <p className="hint small">
        Asked: “{recommendation.request}”
        {recommendation.feedback && ` · feedback: “${recommendation.feedback}”`}
      </p>

      {recommendation.mode_changed && (
        <p className="message warn">
          This proposal switches the cutting mode from{' '}
          <strong>{modeLabel(catalog, recommendation.requested_in_mode)}</strong> to{' '}
          <strong>{modeLabel(catalog, recommendation.mode)}</strong>.
          {recommendation.mode === 'full_clip' &&
            ' That removes pauses inside each clip, not only around it.'}
        </p>
      )}

      <p>{recommendation.explanation}</p>

      {recommendation.limitations.length > 0 && (
        <>
          <h4>Limitations and uncertainty</h4>
          <ul className="choices">
            {recommendation.limitations.map((limitation) => (
              <li key={limitation} className="hint small">
                {limitation}
              </li>
            ))}
          </ul>
        </>
      )}

      <div className="settings-grid advice-grid">
        {specsFor(catalog, recommendation.mode).map((spec) => {
          const was = !recommendation.mode_changed ? before?.[spec.name] : undefined
          const value = draft[spec.name] ?? spec.default
          return (
            <label className="field wide" key={spec.name}>
              <span className="parameter-label">
                {spec.label}
                <span
                  className="info"
                  tabIndex={0}
                  aria-label={`${spec.description} (${spec.unit})`}
                  data-tip={`${spec.description} (${spec.unit})`}
                >
                  i
                </span>
              </span>
              <input
                type="number"
                step={spec.step}
                min={spec.min}
                max={spec.max}
                value={value}
                disabled={disabled || recommendation.applied}
                onChange={(event) => {
                  const next = Number(event.target.value)
                  if (Number.isFinite(next)) setDraft({ ...draft, [spec.name]: next })
                }}
              />
              <span className="hint small mono">
                {was === undefined
                  ? spec.unit
                  : was === value
                    ? `unchanged (${spec.unit})`
                    : `now ${was} → ${value}`}
              </span>
            </label>
          )
        })}
      </div>

      {recommendation.stale && (
        <p className="message error">{recommendation.stale_reason}</p>
      )}
      {!recommendation.is_latest && (
        <p className="message">A newer revision of this proposal exists.</p>
      )}

      {recommendation.mode_changed && !blocked && (
        <label className="choice">
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(event) => setConfirmed(event.target.checked)}
          />
          <span>
            Yes, switch to {modeLabel(catalog, recommendation.mode)}
            {mode !== recommendation.requested_in_mode && ' (the form has changed mode since)'}
          </span>
        </label>
      )}

      <div className="row">
        <button
          type="button"
          disabled={disabled || !dirty || recommendation.applied}
          onClick={() => onSaveEdits(recommendation, draft)}
        >
          Save my edits as a new revision
        </button>
        <button
          type="button"
          disabled={disabled || !dirty}
          onClick={() => setDraft(recommendation.settings)}
        >
          Discard edits
        </button>
        <button
          type="button"
          className="primary"
          disabled={
            disabled || dirty || blocked || (recommendation.mode_changed && !confirmed)
          }
          title="Approves this revision and copies its settings into the form. Nothing is rendered."
          onClick={() => onApply(recommendation, confirmed)}
        >
          Approve and apply to the form
        </button>
      </div>

      {dirty && (
        <p className="hint small">
          Save your edits as a new revision before approving: what is approved is
          exactly what is saved.
        </p>
      )}
      {recommendation.applied && (
        <p className="hint small">
          These settings are in the form. Nothing was rendered — render previews or
          the clips when you are ready.
        </p>
      )}

      <label className="field wide">
        <span>Ask for one revision, based on what you heard</span>
        <textarea
          rows={2}
          value={feedback}
          placeholder="For example: the first word of clip 2 still sounds clipped"
          onChange={(event) => setFeedback(event.target.value)}
        />
      </label>
      <div className="row">
        <button
          type="button"
          disabled={disabled || working || !feedback.trim()}
          onClick={() => {
            onAsk(recommendation.request, feedback, recommendation.plan_id)
            setFeedback('')
          }}
        >
          Ask for a revision
        </button>
      </div>
    </div>
  )
}

export default function CutAdvice({
  llm,
  catalog,
  mode,
  recommendation,
  lastJob,
  working,
  disabled,
  onAsk,
  onSaveEdits,
  onApply,
}: Props) {
  const [request, setRequest] = useState('')

  const ready = llm ? llm.ready : true
  // A job that ended after the proposal on screen was made is news about it.
  const jobIsNewer =
    lastJob !== undefined &&
    (recommendation === null || (lastJob.finished_at ?? '') > recommendation.revised_at)
  const declined =
    jobIsNewer && lastJob?.status === 'succeeded' && lastJob.result?.supported === false

  return (
    <div className="advice">
      <div className="editor-header">
        <h4>AI recommendation (optional)</h4>
        {llm && (
          <span className={llm.is_mock ? 'badge warn' : 'badge ok'}>
            {llm.is_mock ? llm.label : `${llm.label} · ${llm.model}`}
          </span>
        )}
      </div>

      <p className="hint small">
        Describe the pacing you want and get proposed settings for{' '}
        <strong>{modeLabel(catalog, mode)}</strong>. The proposal is based on
        measurements of your clips, not on listening to them, and changes nothing
        until you approve it.
        {llm && !llm.is_mock && llm.ready && (
          <>
            {' '}
            Sent to the provider: {catalog.advice.data_sent.join('; ')}. Not sent:{' '}
            {catalog.advice.data_not_sent.join('; ')}.
          </>
        )}
      </p>

      {llm && !llm.ready && <p className="message error">{llm.message}</p>}

      <label className="field wide">
        <span>What should the cut feel like?</span>
        <textarea
          rows={2}
          value={request}
          placeholder={EXAMPLES[1].text}
          onChange={(event) => setRequest(event.target.value)}
        />
      </label>
      <div className="row">
        <button
          type="button"
          disabled={disabled || working || !ready || !request.trim()}
          onClick={() => onAsk(request)}
        >
          {working ? 'Asking…' : 'Ask for a recommendation'}
        </button>
        {EXAMPLES.map((example) => (
          <button
            key={example.label}
            type="button"
            className="example"
            disabled={disabled}
            title={example.text}
            onClick={() => setRequest(example.text)}
          >
            {example.label}
          </button>
        ))}
      </div>

      {jobIsNewer && lastJob?.status === 'failed' && (
        <p className="message error">{lastJob.error}</p>
      )}
      {declined && (
        <p className="message">
          <strong>No proposal: </strong>
          {String(lastJob?.result?.explanation ?? '')}
        </p>
      )}

      {recommendation && (
        <Proposal
          // A new revision starts from its own values, not from stale edits.
          key={`${recommendation.plan_id}-${recommendation.revision}`}
          catalog={catalog}
          mode={mode}
          recommendation={recommendation}
          working={working}
          disabled={disabled}
          onAsk={onAsk}
          onSaveEdits={onSaveEdits}
          onApply={onApply}
        />
      )}
    </div>
  )
}
