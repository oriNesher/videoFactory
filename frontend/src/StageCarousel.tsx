import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { acceptStage, getPipeline } from './api'
import type { PipelineStage, PipelineState, StageId } from './types'
import { createPreviewBridge, PreviewBridgeContext, type PreviewOverride } from './previewBridge'
import VideoPreview from './VideoPreview'

/**
 * The project editor's shell: one slide per stage, a stage bar on top and the
 * video being made beside every slide.
 *
 * A stage is done only when the user accepted it — here for footage, cutting
 * and captions, in their own section for zooms, B-roll and music. Accepting moves to
 * the next slide by itself; any slide can still be opened from the bar.
 *
 * Every slide stays mounted (hidden when not shown), so a stage's drafts,
 * players and polling survive moving between slides.
 */

const POLL_MS = 4000

const ORDER: StageId[] = ['footage', 'cutting', 'captions', 'zooms', 'broll', 'music']

type Props = {
  projectId: string
  slides: Record<StageId, ReactNode>
  /** Bumped by the parent when something may have changed a stage's result. */
  refreshSignal: number
  /** Why the footage cannot be accepted right now (unsaved edits), if so. */
  footageHold?: string | null
}

/**
 * How far the user may go: up to the first stage that is not done. A later
 * stage cannot be opened before the ones before it have done their work.
 */
function reachableIndex(pipeline: PipelineState): number {
  return pipeline.current_stage === null ? ORDER.length - 1 : ORDER.indexOf(pipeline.current_stage)
}

function stageClass(stage: PipelineStage, active: boolean): string {
  const classes = ['stage-step']
  if (stage.done) classes.push('done')
  else if (stage.outdated_reason) classes.push('outdated')
  if (active) classes.push('active')
  return classes.join(' ')
}

export default function StageCarousel({ projectId, slides, refreshSignal, footageHold }: Props) {
  const [pipeline, setPipeline] = useState<PipelineState | null>(null)
  const [active, setActive] = useState<StageId | null>(null)
  const [error, setError] = useState('')
  const [accepting, setAccepting] = useState(false)
  const [override, setOverride] = useState<PreviewOverride | null>(null)
  const [bridge] = useState(() => createPreviewBridge(setOverride))

  // What was done at the last answer, to notice a stage becoming done.
  const doneBefore = useRef<Record<string, boolean> | null>(null)
  const activeRef = useRef(active)
  useEffect(() => {
    activeRef.current = active
  }, [active])

  const adopt = useCallback((next: PipelineState) => {
    setPipeline(next)
    setError('')
    const previous = doneBefore.current
    doneBefore.current = Object.fromEntries(next.stages.map((stage) => [stage.id, stage.done]))
    if (previous === null) {
      // First answer: open where the work is.
      setActive(next.current_stage ?? ORDER[ORDER.length - 1])
      return
    }
    // An upstream change reopened an earlier stage: go back to it.
    const reachable = reachableIndex(next)
    if (activeRef.current && ORDER.indexOf(activeRef.current) > reachable) {
      setActive(ORDER[reachable])
      return
    }
    // Automatic continuation: the slide on screen was just accepted.
    const shown = activeRef.current
    const justDone = next.stages.find((stage) => stage.done && !previous[stage.id])
    if (shown && justDone?.id === shown) {
      const following = ORDER[ORDER.indexOf(shown) + 1]
      if (following) setActive(following)
    }
  }, [])

  const refresh = useCallback(async () => {
    try {
      adopt(await getPipeline(projectId))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Loading the stages failed.')
    }
  }, [projectId, adopt])

  useEffect(() => {
    void refresh()
  }, [refresh, refreshSignal])

  useEffect(() => {
    const interval = window.setInterval(() => void refresh(), POLL_MS)
    return () => window.clearInterval(interval)
  }, [refresh])

  async function accept(stage: PipelineStage) {
    if (!stage.candidate) return
    setAccepting(true)
    try {
      adopt(await acceptStage(projectId, stage.id, stage.candidate.token))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Accepting failed.')
      await refresh()
    } finally {
      setAccepting(false)
    }
  }

  const stages = pipeline?.stages ?? []
  const shown = active ?? 'footage'
  const index = ORDER.indexOf(shown)
  const current = stages.find((stage) => stage.id === shown)
  const reachable = pipeline ? reachableIndex(pipeline) : 0

  return (
    <div className="stage-shell">
      <nav className="stage-bar" aria-label="Stages">
        <ol>
          {ORDER.map((id, position) => {
            const stage = stages.find((entry) => entry.id === id)
            return (
              <li key={id}>
                <button
                  type="button"
                  className={stage ? stageClass(stage, id === shown) : 'stage-step'}
                  aria-current={id === shown ? 'step' : undefined}
                  title={
                    position > reachable
                      ? 'Finish the stages before this one first'
                      : (stage?.outdated_reason ?? stage?.summary)
                  }
                  disabled={position > reachable}
                  onClick={() => setActive(id)}
                >
                  <span className="stage-dot" aria-hidden="true">
                    {stage?.done ? '✓' : position + 1}
                  </span>
                  <span className="stage-name">{stage?.label ?? id}</span>
                </button>
              </li>
            )
          })}
        </ol>
        <span className="small stage-left">
          {pipeline === null
            ? ''
            : pipeline.stages_left === 0
              ? 'All stages done'
              : `${pipeline.stages_left} of ${ORDER.length} stages left`}
        </span>
      </nav>

      {error && <p className="message error">{error}</p>}

      <PreviewBridgeContext.Provider value={bridge}>
      <div className="stage-body">
        <div className="stage-carousel">
          <div className="stage-nav">
            <button type="button" disabled={index <= 0} onClick={() => setActive(ORDER[index - 1])}>
              ‹ {index > 0 ? stages.find((stage) => stage.id === ORDER[index - 1])?.label : ''}
            </button>
            <h3>
              {index + 1}. {current?.label ?? shown}
            </h3>
            <button
              type="button"
              disabled={index >= ORDER.length - 1 || index + 1 > reachable}
              title={index + 1 > reachable ? 'Accept this stage first' : undefined}
              onClick={() => setActive(ORDER[index + 1])}
            >
              {index < ORDER.length - 1 ? stages.find((stage) => stage.id === ORDER[index + 1])?.label : ''} ›
            </button>
          </div>

          {current && (
            <StageFooter
              stage={current}
              hold={current.id === 'footage' ? footageHold ?? null : null}
              accepting={accepting}
              onAccept={() => void accept(current)}
            />
          )}

          {ORDER.map((id) => (
            <section key={id} className="stage-slide" hidden={id !== shown}>
              {slides[id]}
            </section>
          ))}

        </div>

        <VideoPreview
          projectId={projectId}
          preview={pipeline?.preview ?? null}
          stages={stages}
          override={override}
          bridge={bridge}
        />
      </div>
      </PreviewBridgeContext.Provider>
    </div>
  )
}

function StageFooter({
  stage,
  hold,
  accepting,
  onAccept,
}: {
  stage: PipelineStage
  hold: string | null
  accepting: boolean
  onAccept: () => void
}) {
  return (
    <div className={stage.done ? 'stage-footer stage-actions done' : 'stage-footer stage-actions'}>
      {stage.done && (
        <p className="message ok">
          <strong>Accepted.</strong> {stage.summary}
        </p>
      )}
      {!stage.done && stage.outdated_reason && <p className="message warn">{stage.outdated_reason}</p>}

      {stage.accepted_here ? (
        !stage.done && (
          <div className="row">
            {stage.candidate && !hold ? (
              <>
                <span className="small">{stage.candidate.summary}</span>
                <button type="button" className="primary" disabled={accepting} onClick={onAccept}>
                  Accept {stage.label.toLowerCase()} and continue
                </button>
              </>
            ) : (
              <span className="hint small">{hold ?? stage.blocked_reason}</span>
            )}
          </div>
        )
      ) : (
        !stage.done && (
          <p className="hint small">
            {stage.summary}. Approve the result in this section to continue.
          </p>
        )
      )}
    </div>
  )
}
