import { sampleUrl } from './api'
import { formatLength, formatSeconds } from './cutFormat'
import type {
  BoundaryOrigin,
  BoundaryOverride,
  CutSample,
  CutStateClip,
  SampleKind,
  SampleRequest,
} from './types'

/**
 * The boundary-only clip list: for every selected clip, where it will be cut,
 * the means to move that by hand, and previews of the three places a cut can
 * be wrong — the opening, the ending and the join to the next clip.
 *
 * Everything shown here comes from the backend's state for the form as it
 * stands. The component decides nothing about staleness or boundaries itself.
 */

const ORIGIN_LABELS: Record<BoundaryOrigin, string> = {
  detected: 'detected',
  manual: 'set by hand',
  kept_whole: 'whole clip kept',
  source_limit: 'no boundary found',
}

const KIND_TITLES: Record<SampleKind, string> = {
  opening: 'Opening',
  ending: 'Ending',
  join: 'Join with the next clip',
}

const NUDGE_SECONDS = 0.1

function slotOf(kind: SampleKind, sourceId: string, nextId?: string): string {
  return [kind, sourceId, ...(nextId ? [nextId] : [])].join(':')
}

function sampleSlot(sample: CutSample): string {
  return [sample.kind, ...sample.source_ids].join(':')
}

/** Which settings a preview was rendered with, in one line. */
function describeBasis(sample: CutSample): string {
  const settings = sample.basis.boundary_settings
  const revision =
    sample.basis.settings_revision === null
      ? 'unsaved settings'
      : `settings revision ${sample.basis.settings_revision}`
  const plan = sample.basis.applied_plan
    ? ` · from AI proposal revision ${sample.basis.applied_plan.revision}`
    : ''
  const time = new Date(sample.created_at)
  const when = Number.isNaN(time.getTime())
    ? ''
    : ` · rendered ${time.toLocaleTimeString('en-US')}`

  return (
    `${revision}${plan} · threshold ${settings.detection_threshold} · padding ` +
    `${settings.leading_padding_seconds}s before / ${settings.trailing_padding_seconds}s after · ` +
    `ignores sounds under ${settings.min_activity_seconds}s${when}`
  )
}

function SamplePlayers({
  projectId,
  sample,
}: {
  projectId: string
  sample: CutSample
}) {
  const context = sample.source_context
  const source = sample.sources[0]

  return (
    <div className={sample.stale ? 'preview stale' : 'preview'}>
      <div className="preview-files">
        <figure>
          <figcaption>
            <strong>Edited</strong> ·{' '}
            {sample.kind === 'join'
              ? `the cut is ${formatSeconds(sample.edited.join_at_seconds)} in`
              : sample.kind === 'opening'
                ? `starts at ${formatSeconds(source.boundary.start_seconds)} of the source`
                : `ends at ${formatSeconds(source.boundary.end_seconds)} of the source`}
          </figcaption>
          <video
            controls
            preload="metadata"
            src={sampleUrl(projectId, sample.sample_id, 'edited')}
          />
        </figure>

        {context && (
          <figure>
            <figcaption>
              <strong>Source</strong> ·{' '}
              {sample.kind === 'opening'
                ? context.removed_shown_seconds > 0
                  ? `the first ${formatSeconds(context.cut_at_seconds)} here is what gets removed`
                  : 'nothing is removed before this'
                : context.removed_shown_seconds > 0
                  ? `everything after ${formatSeconds(context.cut_at_seconds)} here gets removed`
                  : 'nothing is removed after this'}
            </figcaption>
            <video
              controls
              preload="metadata"
              src={sampleUrl(projectId, sample.sample_id, 'source')}
            />
          </figure>
        )}
      </div>

      {sample.notes.map((note) => (
        <p className="hint small" key={note}>
          {note}
        </p>
      ))}
      <p className="hint small mono">{describeBasis(sample)}</p>
    </div>
  )
}

function PreviewSlot({
  projectId,
  kind,
  sample,
  request,
  disabled,
  onRender,
}: {
  projectId: string
  kind: SampleKind
  sample: CutSample | undefined
  request: SampleRequest
  disabled: boolean
  onRender: (requests: SampleRequest[]) => void
}) {
  return (
    <div className="preview-slot">
      <div className="job-header">
        <span className="job-title">
          {KIND_TITLES[kind]}
          {sample && !sample.stale && <span className="badge ok">Current</span>}
          {sample?.stale && <span className="badge bad">Out of date</span>}
        </span>
        <button
          type="button"
          className={kind === 'opening' && !sample ? 'primary' : undefined}
          disabled={disabled}
          onClick={() => onRender([request])}
        >
          {sample ? 'Render again' : 'Render preview'}
        </button>
      </div>

      {sample?.stale && (
        <p className="message warn">
          {sample.stale_reason} What you hear below is the earlier cut.
        </p>
      )}

      {sample && <SamplePlayers projectId={projectId} sample={sample} />}
    </div>
  )
}

function BoundaryInput({
  label,
  value,
  resolved,
  origin,
  disabled,
  onChange,
}: {
  label: string
  /** The manual value, when one is set. */
  value: number | undefined
  /** Where this side currently resolves to, manual or not. */
  resolved: number | undefined
  origin: BoundaryOrigin | undefined
  disabled: boolean
  onChange: (next: number | undefined) => void
}) {
  const base = value ?? resolved

  function nudge(delta: number) {
    if (base === undefined) return
    onChange(Math.max(0, Math.round((base + delta) * 100) / 100))
  }

  return (
    <label className="field boundary-field">
      <span>
        {label}
        <span className="hint small"> · {origin ? ORIGIN_LABELS[origin] : 'not analysed'}</span>
      </span>
      <span className="row">
        <input
          type="number"
          step={0.05}
          min={0}
          disabled={disabled}
          value={value ?? ''}
          placeholder={resolved === undefined ? 'seconds' : resolved.toFixed(2)}
          onChange={(event) =>
            onChange(event.target.value === '' ? undefined : Number(event.target.value))
          }
        />
        <button
          type="button"
          title={`${NUDGE_SECONDS} s earlier`}
          disabled={disabled || base === undefined}
          onClick={() => nudge(-NUDGE_SECONDS)}
        >
          −
        </button>
        <button
          type="button"
          title={`${NUDGE_SECONDS} s later`}
          disabled={disabled || base === undefined}
          onClick={() => nudge(NUDGE_SECONDS)}
        >
          +
        </button>
      </span>
    </label>
  )
}

function ClipRow({
  projectId,
  clip,
  next,
  samples,
  disabled,
  showAdjustments,
  onOverride,
  onRender,
}: {
  projectId: string
  clip: CutStateClip
  next: CutStateClip | undefined
  samples: Map<string, CutSample>
  disabled: boolean
  showAdjustments: boolean
  onOverride: (sourceId: string, override: BoundaryOverride | null) => void
  onRender: (requests: SampleRequest[]) => void
}) {
  const boundary = clip.boundary
  const override = clip.override ?? {}
  const keptWhole = override.keep_whole === true
  const manual = override.start_seconds !== undefined || override.end_seconds !== undefined

  function setSide(side: 'start_seconds' | 'end_seconds', value: number | undefined) {
    const changed: BoundaryOverride = { ...override, keep_whole: undefined, [side]: value }
    const empty = changed.start_seconds === undefined && changed.end_seconds === undefined
    onOverride(clip.source_id, empty ? null : changed)
  }

  return (
    <li className={clip.available ? '' : 'muted'}>
      <div className="job-header">
        <span className="job-title">
          <span className="index">{clip.order}</span>
          <bdi>{clip.filename}</bdi>
          {!clip.available && <span className="badge bad">File missing</span>}
          {clip.available && !clip.analysed && <span className="badge">Not analysed yet</span>}
          {boundary?.needs_review && <span className="badge warn">Needs a look</span>}
          {keptWhole && <span className="badge">Whole clip kept</span>}
          {manual && <span className="badge">Adjusted by hand</span>}
          {clip.error && <span className="badge bad">Invalid</span>}
        </span>
        <span className="small mono">
          {formatLength(clip.duration_seconds)} original
          {boundary && ` → ${formatLength(boundary.retained_seconds)} kept`}
        </span>
      </div>

      {boundary && (
        <p className="hint small">
          Keeps <span className="mono">{formatSeconds(boundary.start_seconds)}</span> to{' '}
          <span className="mono">{formatSeconds(boundary.end_seconds)}</span> of the source as one
          piece. Removes {formatSeconds(boundary.removed_leading_seconds)} before it and{' '}
          {formatSeconds(boundary.removed_trailing_seconds)} after it.
          {boundary.activity_start_seconds !== null &&
            ` Sound detected from ${formatSeconds(boundary.activity_start_seconds)} to ${formatSeconds(boundary.activity_end_seconds)}.`}
        </p>
      )}

      {clip.error && <p className="message error">{clip.error}</p>}

      {boundary?.warnings.map((warning) => (
        <p
          className={warning.severity === 'warn' ? 'message warn' : 'hint small'}
          key={warning.code}
        >
          {warning.message}
        </p>
      ))}

      {showAdjustments && (
        <div className="boundary-controls">
          <BoundaryInput
            label="Start"
            value={override.start_seconds}
            resolved={boundary?.start_seconds}
            origin={boundary?.start_origin}
            disabled={disabled || keptWhole || !clip.available}
            onChange={(value) => setSide('start_seconds', value)}
          />
          <BoundaryInput
            label="End"
            value={override.end_seconds}
            resolved={boundary?.end_seconds}
            origin={boundary?.end_origin}
            disabled={disabled || keptWhole || !clip.available}
            onChange={(value) => setSide('end_seconds', value)}
          />
          <label className="choice">
            <input
              type="checkbox"
              checked={keptWhole}
              disabled={disabled || !clip.available}
              onChange={(event) =>
                onOverride(clip.source_id, event.target.checked ? { keep_whole: true } : null)
              }
            />
            <span>Keep the whole clip</span>
          </label>
          <button
            type="button"
            disabled={disabled || clip.override === null}
            onClick={() => onOverride(clip.source_id, null)}
          >
            Back to detected
          </button>
        </div>
      )}

      {clip.available && (
        <div className="previews">
          <PreviewSlot
            projectId={projectId}
            kind="opening"
            sample={samples.get(slotOf('opening', clip.source_id))}
            request={{ kind: 'opening', source_id: clip.source_id }}
            disabled={disabled || clip.error !== null}
            onRender={onRender}
          />
          <PreviewSlot
            projectId={projectId}
            kind="ending"
            sample={samples.get(slotOf('ending', clip.source_id))}
            request={{ kind: 'ending', source_id: clip.source_id }}
            disabled={disabled || clip.error !== null}
            onRender={onRender}
          />
          {next?.available && (
            <PreviewSlot
              projectId={projectId}
              kind="join"
              sample={samples.get(slotOf('join', clip.source_id, next.source_id))}
              request={{
                kind: 'join',
                source_id: clip.source_id,
                next_source_id: next.source_id,
              }}
              disabled={disabled || clip.error !== null || next.error !== null}
              onRender={onRender}
            />
          )}
        </div>
      )}
    </li>
  )
}

type Props = {
  projectId: string
  clips: CutStateClip[]
  samples: CutSample[]
  disabled: boolean
  /** Per-clip start/end controls are optional and hidden unless asked for. */
  showAdjustments: boolean
  onOverride: (sourceId: string, override: BoundaryOverride | null) => void
  onRender: (requests: SampleRequest[]) => void
}

export default function CutClips({
  projectId,
  clips,
  samples,
  disabled,
  showAdjustments,
  onOverride,
  onRender,
}: Props) {
  // The backend already returns the newest sample of each slot.
  const bySlot = new Map(samples.map((sample) => [sampleSlot(sample), sample]))

  return (
    <ul className="clips boundary-clips">
      {clips.map((clip, index) => (
        <ClipRow
          key={clip.source_id}
          projectId={projectId}
          clip={clip}
          next={clips[index + 1]}
          samples={bySlot}
          disabled={disabled}
          showAdjustments={showAdjustments}
          onOverride={onOverride}
          onRender={onRender}
        />
      ))}
    </ul>
  )
}
