import { useState } from 'react'
import {
  brollAssetMediaUrl,
  brollAssetThumbnailUrl,
  importBrollAsset,
  remeasureBrollAsset,
  removeBrollAsset,
  updateBrollAsset,
} from './api'
import { describeAsset } from './brollFormat'
import type { BrollLibraryAsset } from './types'

type Message = { kind: 'ok' | 'error' | 'warn'; text: string } | null

const STATUS_LABEL: Record<BrollLibraryAsset['status'], string> = {
  available: '',
  missing: 'File missing',
  changed: 'File changed',
  unsupported: 'Unsupported format',
}

function errorText(caught: unknown, fallback: string): string {
  return caught instanceof Error ? caught.message : fallback
}

/**
 * The workspace-wide B-roll library, compactly: thumbnail, measured facts and
 * status per asset. Name, description and tags are the user's own words and
 * sit behind "Details"; nothing is guessed from a filename.
 */
export default function BrollLibrary({
  assets,
  storageNote,
  onChanged,
}: {
  assets: BrollLibraryAsset[]
  storageNote: string
  onChanged: () => Promise<void> | void
}) {
  const [message, setMessage] = useState<Message>(null)
  const [busy, setBusy] = useState(false)
  const [path, setPath] = useState('')
  const [showPath, setShowPath] = useState(false)
  const [playing, setPlaying] = useState<string | null>(null)

  async function act(action: () => Promise<void>, fallback: string) {
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

  async function importFile(fromPath: string | null) {
    await act(async () => {
      const result = await importBrollAsset(fromPath)
      if (result.cancelled) return
      setPath('')
      setMessage(
        result.duplicate
          ? { kind: 'warn', text: `Already in the library as "${result.asset?.display_name}".` }
          : { kind: 'ok', text: `Added "${result.asset?.display_name}". The file stays where it is.` },
      )
    }, 'Importing failed.')
  }

  return (
    <div className="broll-library">
      <div className="row">
        <button type="button" onClick={() => void importFile(null)} disabled={busy}>
          Import image or video…
        </button>
        <button type="button" className="link" onClick={() => setShowPath((value) => !value)} disabled={busy}>
          {showPath ? 'Hide path field' : 'Paste a path instead'}
        </button>
      </div>
      {showPath && (
        <div className="row">
          <input
            type="text"
            value={path}
            onChange={(event) => setPath(event.target.value)}
            placeholder="C:\\B-roll\\screen recording.mp4"
            aria-label="Path of an image or video"
            style={{ flex: 1 }}
          />
          <button type="button" onClick={() => void importFile(path)} disabled={busy || !path.trim()}>
            Add
          </button>
        </div>
      )}
      <p className="small hint">{storageNote} PNG, JPEG, WebP; H.264, VP8/9 or AV1 video (MP4, MOV, WebM, MKV).</p>
      {message && <p className={`message ${message.kind}`}>{message.text}</p>}

      {assets.length === 0 ? (
        <p className="hint">The library is empty. Import screen recordings, clips or images to use as B-roll.</p>
      ) : (
        <ul className="broll-grid">
          {assets.map((asset) => (
            <li key={asset.id} className={`broll-card ${asset.status === 'available' ? '' : 'unusable'}`}>
              <div className="broll-thumb">
                {playing === asset.id && asset.status === 'available' ? (
                  <video src={brollAssetMediaUrl(asset.id, asset.fingerprint.digest)} controls autoPlay muted />
                ) : asset.has_thumbnail ? (
                  <img src={brollAssetThumbnailUrl(asset)} alt="" loading="lazy" />
                ) : (
                  <span className="small">{asset.media_type === 'image' ? 'Image' : 'Video'}</span>
                )}
              </div>
              <strong className="broll-name" title={asset.filename}>
                {asset.display_name}
              </strong>
              <span className="small">{describeAsset(asset)}</span>
              {asset.status !== 'available' && (
                <span className="badge bad" title={asset.status_reason ?? ''}>
                  {STATUS_LABEL[asset.status]}
                </span>
              )}
              {asset.status !== 'available' && asset.status_reason && <span className="small">{asset.status_reason}</span>}
              <div className="row">
                {asset.media_type === 'video' && asset.status === 'available' && (
                  <button type="button" onClick={() => setPlaying(playing === asset.id ? null : asset.id)}>
                    {playing === asset.id ? 'Stop' : 'Play'}
                  </button>
                )}
                {asset.status === 'changed' && (
                  <button type="button" onClick={() => void act(async () => void (await remeasureBrollAsset(asset.id)), 'Re-measuring failed.')} disabled={busy}>
                    Re-measure
                  </button>
                )}
              </div>
              <AssetDetails asset={asset} busy={busy} act={act} />
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function AssetDetails({
  asset,
  busy,
  act,
}: {
  asset: BrollLibraryAsset
  busy: boolean
  act: (action: () => Promise<void>, fallback: string) => Promise<void>
}) {
  const [name, setName] = useState(asset.display_name_source === 'user' ? asset.display_name : '')
  const [description, setDescription] = useState(asset.description ?? '')
  const [tags, setTags] = useState(asset.tags.join(', '))

  return (
    <details className="small">
      <summary>Details</summary>
      <label className="field">
        <span>Name (optional; the file name is shown otherwise)</span>
        <input type="text" value={name} onChange={(event) => setName(event.target.value)} placeholder={asset.filename} />
      </label>
      <label className="field">
        <span>Description (optional — what it shows, in your words)</span>
        <textarea rows={2} value={description} onChange={(event) => setDescription(event.target.value)} />
      </label>
      <label className="field">
        <span>Tags (optional, comma-separated)</span>
        <input type="text" value={tags} onChange={(event) => setTags(event.target.value)} />
      </label>
      <div className="row">
        <button
          type="button"
          disabled={busy}
          onClick={() =>
            void act(async () => {
              await updateBrollAsset(asset.id, {
                display_name: name.trim() || null,
                description: description.trim() || null,
                tags: tags.split(',').map((tag) => tag.trim()).filter(Boolean),
              })
            }, 'Saving failed.')
          }
        >
          Save details
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            if (window.confirm(`Remove "${asset.display_name}" from the library? The file itself is not touched.`))
              void act(async () => void (await removeBrollAsset(asset.id)), 'Removing failed.')
          }}
        >
          Remove from library
        </button>
      </div>
      <p className="small">
        Measured from the file: {asset.measured.codec || 'unknown codec'}, aspect {asset.measured.aspect_ratio.toFixed(3)} (
        {asset.measured.orientation}). Stored by reference: {asset.filename}.
      </p>
    </details>
  )
}
