import { useCallback, useEffect, useRef, useState } from 'react'
import {
  getMusicLibrary,
  importMusicTrack,
  musicTrackMediaUrl,
  remeasureMusicTrack,
  removeMusicTrack,
  renameMusicTrack,
} from './api'
import { formatTime } from './captionFormat'
import type { AudioTrack } from './types'

type Message = { kind: 'ok' | 'error' | 'warn'; text: string } | null

function errorText(caught: unknown, fallback: string): string {
  return caught instanceof Error ? caught.message : fallback
}

type Props = {
  /** In the Music stage: the chosen track, and choosing one (null: no music). */
  chosen?: string | null
  onChoose?: (trackId: string | null) => void
  /** After any change to the library (added, renamed, re-measured, removed). */
  onChanged?: (event: { added?: AudioTrack; removed?: string }) => void
  disabled?: boolean
}

/**
 * The music library (stage 4): tracks shared by every video, referenced
 * where they are on disk, never copied. Used on its own page and inside the
 * Music stage, where each track can also be chosen for the video.
 */
export default function MusicLibrary({ chosen, onChoose, onChanged, disabled = false }: Props) {
  const [tracks, setTracks] = useState<AudioTrack[] | null>(null)
  const [notes, setNotes] = useState({ storage: '', rights: '' })
  const [message, setMessage] = useState<Message>(null)
  const [busy, setBusy] = useState(false)
  const [path, setPath] = useState('')
  const [listening, setListening] = useState<string | null>(null)
  const audio = useRef<HTMLAudioElement>(null)
  const choosing = onChoose !== undefined

  const load = useCallback(async () => {
    try {
      const library = await getMusicLibrary()
      setTracks(library.assets)
      setNotes({ storage: library.storage_note, rights: library.rights_note })
    } catch (caught) {
      setMessage({ kind: 'error', text: errorText(caught, 'Loading the music library failed.') })
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  async function act(action: () => Promise<void>, fallback: string) {
    setBusy(true)
    setMessage(null)
    try {
      await action()
    } catch (caught) {
      setMessage({ kind: 'error', text: errorText(caught, fallback) })
    } finally {
      setBusy(false)
      await load()
    }
  }

  async function add(fromPath: string | null) {
    await act(async () => {
      const result = await importMusicTrack(fromPath)
      if (result.cancelled || !result.asset) return
      setPath('')
      setMessage(
        result.duplicate
          ? { kind: 'warn', text: `"${result.asset.display_name}" is already in the library.` }
          : { kind: 'ok', text: `Added "${result.asset.display_name}".` },
      )
      onChanged?.({ added: result.asset })
    }, 'Adding the track failed.')
  }

  function listen(track: AudioTrack) {
    const element = audio.current
    if (!element) return
    if (listening === track.id) {
      element.pause()
      setListening(null)
      return
    }
    element.src = musicTrackMediaUrl(track.id, track.fingerprint.digest)
    void element.play().catch(() => setListening(null))
    setListening(track.id)
  }

  const off = busy || disabled

  return (
    <div className="music-library">
      <audio ref={audio} onEnded={() => setListening(null)} hidden />
      {message && <p className={`message ${message.kind}`}>{message.text}</p>}

      <div className="row">
        <button type="button" className="primary" onClick={() => void add(null)} disabled={off}>
          Add a track…
        </button>
        <input
          type="text"
          className="music-path"
          placeholder="…or paste a full path, e.g. C:\Music\calm.mp3"
          value={path}
          onChange={(event) => setPath(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && path.trim()) void add(path)
          }}
          disabled={off}
          aria-label="Path of a music file"
        />
        <button type="button" onClick={() => void add(path)} disabled={off || !path.trim()}>
          Add
        </button>
      </div>
      <p className="small hint">
        MP3, WAV, M4A, AAC, OGG, Opus or FLAC. {notes.storage} {notes.rights}
      </p>

      {tracks === null ? (
        !message && <p className="hint">Loading…</p>
      ) : tracks.length === 0 ? (
        <p className="hint">The music library is empty. Add a track to get started.</p>
      ) : (
        <ul className="music-tracks">
          {choosing && (
            <li>
              <label className="choice">
                <input type="radio" name="music-track" checked={chosen === null} disabled={off} onChange={() => onChoose(null)} />
                No music
              </label>
            </li>
          )}
          {tracks.map((track) => (
            <li key={track.id} className={track.status === 'available' ? '' : 'invalid'}>
              <div className="row">
                {choosing ? (
                  <label className="choice music-track-name">
                    <input
                      type="radio"
                      name="music-track"
                      checked={chosen === track.id}
                      disabled={off || track.status !== 'available'}
                      onChange={() => onChoose(track.id)}
                    />
                    <bdi>{track.display_name}</bdi>
                  </label>
                ) : (
                  <span className="music-track-name">
                    <bdi>{track.display_name}</bdi>
                    {track.display_name !== track.filename && (
                      <span className="small hint">
                        {' '}
                        · <bdi>{track.filename}</bdi>
                      </span>
                    )}
                  </span>
                )}
                <span className="small">
                  {formatTime(track.measured.duration_seconds)}
                  {track.measured.codec ? ` · ${track.measured.codec}` : ''}
                </span>
                {track.status === 'available' && (
                  <button type="button" onClick={() => listen(track)} title="Listen to the track on its own">
                    {listening === track.id ? '■ Stop' : '▶ Listen'}
                  </button>
                )}
                <button
                  type="button"
                  className="link"
                  disabled={off}
                  onClick={() => {
                    const name = window.prompt('Name for this track (empty: the file name)', track.display_name)
                    if (name === null) return
                    void act(async () => {
                      await renameMusicTrack(track.id, name.trim() || null)
                      onChanged?.({})
                    }, 'Renaming failed.')
                  }}
                >
                  Rename
                </button>
                {track.status === 'changed' && (
                  <button
                    type="button"
                    className="link"
                    disabled={off}
                    onClick={() =>
                      void act(async () => {
                        await remeasureMusicTrack(track.id)
                        onChanged?.({})
                      }, 'Re-measuring failed.')
                    }
                  >
                    Re-measure
                  </button>
                )}
                <button
                  type="button"
                  className="link"
                  disabled={off}
                  onClick={() => {
                    if (!window.confirm(`Remove "${track.display_name}" from the music library? The file itself is not touched.`)) return
                    void act(async () => {
                      await removeMusicTrack(track.id)
                      onChanged?.({ removed: track.id })
                    }, 'Removing failed.')
                  }}
                >
                  Remove
                </button>
              </div>
              {track.status_reason && <p className="small message error">{track.status_reason}</p>}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
