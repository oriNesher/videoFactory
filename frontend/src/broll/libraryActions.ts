/** Shared by the B-roll panel's library and the library page. */
import { useState } from 'react'
import type { BrollLibraryAsset } from '../types'

export type Message = { kind: 'ok' | 'error' | 'warn'; text: string } | null

/** Runs one library change, then reloads. */
export type Act = (action: () => Promise<void>, fallback: string) => Promise<void>

export const STATUS_LABEL: Record<BrollLibraryAsset['status'], string> = {
  available: '',
  missing: 'File missing',
  changed: 'File changed',
  unsupported: 'Unsupported format',
}

export function errorText(caught: unknown, fallback: string): string {
  return caught instanceof Error ? caught.message : fallback
}

export function splitTags(text: string): string[] {
  return text.split(',').map((tag) => tag.trim()).filter(Boolean)
}

/** `busy`, `message` and an `act` that reports failures in `message`. */
export function useLibraryActions(onChanged: () => Promise<void> | void) {
  const [message, setMessage] = useState<Message>(null)
  const [busy, setBusy] = useState(false)

  const act: Act = async (action, fallback) => {
    setBusy(true)
    setMessage(null)
    try {
      await action()
      await onChanged()
    } catch (caught) {
      setMessage({ kind: 'error', text: errorText(caught, fallback) })
    } finally {
      setBusy(false)
    }
  }

  return { message, setMessage, busy, act }
}
