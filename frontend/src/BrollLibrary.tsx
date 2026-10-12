import { useState } from 'react'
import {
  brollAssetMediaUrl,
  brollAssetThumbnailUrl,
  importBrollAsset,
  remeasureBrollAsset,
  removeBrollAsset,
  updateBrollAsset,
} from './api'
import { type Act, type Message, STATUS_LABEL, splitTags, useLibraryActions } from './broll/libraryActions'
import { formatMoment, parseMoments } from './broll/moments'
import { describeAsset } from './brollFormat'
import type { BrollLibraryAsset } from './types'

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
  const { message, setMessage, busy, act } = useLibraryActions(onChanged)
  const [playing, setPlaying] = useState<string | null>(null)

  return (
    <div className="broll-library">
      <AssetImport storageNote={storageNote} busy={busy} act={act} setMessage={setMessage} />
      {message && <p className={`message ${message.kind}`}>{message.text}</p>}

      {assets.length === 0 ? (
        <p className="hint">The library is empty. Import screen recordings, clips or images to use as B-roll.</p>
      ) : (
        <ul className="broll-grid">
          {assets.map((asset) => (
            <li key={asset.id} className={`broll-card ${asset.status === 'available' ? '' : 'unusable'}`}>
              <AssetThumb asset={asset} playing={playing === asset.id} />
              <strong className="broll-name" title={asset.filename}>
                {asset.display_name}
              </strong>
              <span className="small">{describeAsset(asset)}</span>
              <AssetStatus asset={asset} />
              {asset.status === 'available' && !asset.described && (
                <span className="badge warn" title="Automatic planning only chooses assets you have described.">
                  Not described — add a description to use it automatically
                </span>
              )}
              <AssetButtons asset={asset} busy={busy} act={act} playing={playing === asset.id} onPlay={setPlaying} />
              <details className="small">
                <summary>{asset.described ? 'Details' : 'Describe…'}</summary>
                <AssetEditor asset={asset} busy={busy} act={act} />
              </details>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/** Import by file dialog, or by a pasted path where the dialog cannot open. */
export function AssetImport({
  storageNote,
  busy,
  act,
  setMessage,
}: {
  storageNote: string
  busy: boolean
  act: Act
  setMessage: (message: Message) => void
}) {
  const [path, setPath] = useState('')
  const [showPath, setShowPath] = useState(false)

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
    <>
      <div className="row">
        <button type="button" className="primary" onClick={() => void importFile(null)} disabled={busy}>
          {busy ? 'Working…' : 'Import image or video…'}
        </button>
        <button type="button" className="link" onClick={() => setShowPath((value) => !value)} disabled={busy}>
          {showPath ? 'Hide path field' : 'Paste a path instead'}
        </button>
      </div>
      {showPath && (
        <form
          className="row"
          onSubmit={(event) => {
            event.preventDefault()
            void importFile(path)
          }}
        >
          <input
            type="text"
            value={path}
            onChange={(event) => setPath(event.target.value)}
            placeholder="C:\\B-roll\\screen recording.mp4"
            aria-label="Path of an image or video"
            style={{ flex: 1 }}
          />
          <button type="submit" disabled={busy || !path.trim()}>
            Add
          </button>
        </form>
      )}
      <p className="small hint">{storageNote} PNG, JPEG, WebP; H.264, VP8/9 or AV1 video (MP4, MOV, WebM, MKV).</p>
    </>
  )
}

export function AssetThumb({ asset, playing = false }: { asset: BrollLibraryAsset; playing?: boolean }) {
  return (
    <div className="broll-thumb">
      {playing && asset.status === 'available' ? (
        <video src={brollAssetMediaUrl(asset.id, asset.fingerprint.digest)} controls autoPlay muted />
      ) : asset.has_thumbnail ? (
        <img src={brollAssetThumbnailUrl(asset)} alt="" loading="lazy" />
      ) : (
        <span className="small">{asset.media_type === 'image' ? 'Image' : 'Video'}</span>
      )}
    </div>
  )
}

export function AssetStatus({ asset }: { asset: BrollLibraryAsset }) {
  if (asset.status === 'available') return null
  return (
    <>
      <span className="badge bad" title={asset.status_reason ?? ''}>
        {STATUS_LABEL[asset.status]}
      </span>
      {asset.status_reason && <span className="small">{asset.status_reason}</span>}
    </>
  )
}

/** Play (videos) and Re-measure (changed files). */
export function AssetButtons({
  asset,
  busy,
  act,
  playing,
  onPlay,
}: {
  asset: BrollLibraryAsset
  busy: boolean
  act: Act
  playing: boolean
  onPlay: (assetId: string | null) => void
}) {
  const canPlay = asset.media_type === 'video' && asset.status === 'available'
  if (!canPlay && asset.status !== 'changed') return null
  return (
    <div className="row">
      {canPlay && (
        <button type="button" onClick={() => onPlay(playing ? null : asset.id)}>
          {playing ? 'Stop' : 'Play'}
        </button>
      )}
      {asset.status === 'changed' && (
        <button
          type="button"
          onClick={() => void act(async () => void (await remeasureBrollAsset(asset.id)), 'Re-measuring failed.')}
          disabled={busy}
        >
          Re-measure
        </button>
      )}
    </div>
  )
}

/** Name, description, tags and moments — the user's own words — plus removal. */
export function AssetEditor({ asset, busy, act }: { asset: BrollLibraryAsset; busy: boolean; act: Act }) {
  const [name, setName] = useState(asset.display_name_source === 'user' ? asset.display_name : '')
  const [description, setDescription] = useState(asset.description ?? '')
  const [tags, setTags] = useState(asset.tags.join(', '))
  const [moments, setMoments] = useState(asset.moments.map(formatMoment).join('\n'))
  const [momentError, setMomentError] = useState<string | null>(null)

  return (
    <div className="asset-editor">
      <label className="field">
        <span>Name (optional; the file name is shown otherwise)</span>
        <input type="text" value={name} onChange={(event) => setName(event.target.value)} placeholder={asset.filename} />
      </label>
      <label className="field">
        <span>Description (optional — what it shows, in your words)</span>
        <textarea rows={2} value={description} onChange={(event) => setDescription(event.target.value)} dir="auto" />
      </label>
      <label className="field">
        <span>Tags (optional, comma-separated)</span>
        <input type="text" value={tags} onChange={(event) => setTags(event.target.value)} dir="auto" />
      </label>
      {asset.media_type === 'video' && (
        <label className="field">
          <span>
            Moments (optional) — one per line, e.g. <code>0:05-0:12 the dashboard</code>. Without moments, automatic
            planning plays the video from its start.
          </span>
          <textarea rows={2} value={moments} onChange={(event) => setMoments(event.target.value)} dir="auto" />
        </label>
      )}
      {momentError && <p className="message error">{momentError}</p>}
      <p className="small hint">
        Automatic planning uses only these words to know what the asset shows — describe it in the words you speak.
      </p>
      <div className="row">
        <button
          type="button"
          className="primary"
          disabled={busy}
          onClick={() => {
            const parsed = parseMoments(moments)
            if (typeof parsed === 'string') {
              setMomentError(parsed)
              return
            }
            setMomentError(null)
            void act(async () => {
              await updateBrollAsset(asset.id, {
                display_name: name.trim() || null,
                description: description.trim() || null,
                tags: splitTags(tags),
                ...(asset.media_type === 'video' ? { moments: parsed } : {}),
              })
            }, 'Saving failed.')
          }}
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
    </div>
  )
}
