import { useState } from 'react'
import type { ExternalPrompt } from './api'

type Message = { kind: 'ok' | 'error' | 'warn'; text: string } | null

function errorText(caught: unknown, fallback: string): string {
  return caught instanceof Error ? caught.message : fallback
}

/**
 * Planning with an AI outside the app: export exactly the instructions
 * Gemini would get, paste them into another chat, and import its answer.
 * The backend validates an imported answer exactly like Gemini's.
 */
export default function ExternalPlanner({
  what,
  disabled,
  getPrompt,
  importAnswer,
}: {
  /** "zoom plan" or "B-roll plan". */
  what: string
  disabled: boolean
  getPrompt: () => Promise<ExternalPrompt>
  /** Resolves to the message to show on success, or null when the user cancelled. */
  importAnswer: (answer: string) => Promise<string | null>
}) {
  const [answer, setAnswer] = useState('')
  const [message, setMessage] = useState<Message>(null)
  const [busy, setBusy] = useState(false)

  async function act(action: () => Promise<void>, fallback: string) {
    setBusy(true)
    setMessage(null)
    try {
      await action()
    } catch (caught) {
      setMessage({ kind: 'error', text: errorText(caught, fallback) })
    } finally {
      setBusy(false)
    }
  }

  function copyPrompt() {
    void act(async () => {
      const { prompt } = await getPrompt()
      await navigator.clipboard.writeText(prompt)
      setMessage({ kind: 'ok', text: `Copied (${prompt.length.toLocaleString()} characters). Paste it into the other AI.` })
    }, 'Copying the prompt failed.')
  }

  function downloadPrompt() {
    void act(async () => {
      const { prompt, filename } = await getPrompt()
      const url = URL.createObjectURL(new Blob([prompt], { type: 'text/plain;charset=utf-8' }))
      const link = document.createElement('a')
      link.href = url
      link.download = filename
      link.click()
      URL.revokeObjectURL(url)
    }, 'Downloading the prompt failed.')
  }

  async function loadFile(file: File | undefined) {
    if (!file) return
    setAnswer(await file.text())
    setMessage(null)
  }

  function importPlan() {
    void act(async () => {
      const text = await importAnswer(answer)
      if (text === null) return
      setMessage({ kind: 'ok', text })
      setAnswer('')
    }, 'Importing the plan failed.')
  }

  const locked = disabled || busy
  return (
    <details>
      <summary>Use another AI (ChatGPT, Claude…)</summary>
      <p className="small hint">
        1. Export the exact instructions Gemini would get for this video and paste them into another AI. 2. Paste its
        JSON answer below. The {what} is checked exactly like Gemini&apos;s before it is used. Nothing is sent to Google.
      </p>
      <div className="row">
        <button type="button" onClick={copyPrompt} disabled={locked}>
          Copy prompt
        </button>
        <button type="button" onClick={downloadPrompt} disabled={locked}>
          Download prompt (.txt)
        </button>
      </div>
      <label className="field">
        <span>The other AI&apos;s answer</span>
        <textarea
          rows={6}
          value={answer}
          onChange={(event) => setAnswer(event.target.value)}
          placeholder='{"summary": "…", …}'
          dir="ltr"
          spellCheck={false}
        />
      </label>
      <div className="row">
        <button type="button" className="primary" onClick={importPlan} disabled={locked || !answer.trim()}>
          Import {what}
        </button>
        <label className="button">
          Load from a file…
          <input
            type="file"
            accept=".json,.txt,application/json,text/plain"
            hidden
            disabled={locked}
            onChange={(event) => {
              void loadFile(event.target.files?.[0])
              event.target.value = ''
            }}
          />
        </label>
      </div>
      {message && <p className={`message ${message.kind}`}>{message.text}</p>}
    </details>
  )
}
