import { useState } from 'react'
import {
  brollAssetMediaUrl,
  brollAssetThumbnailUrl,
  importBrollAsset,
  moveBrollAsset,
  remeasureBrollAsset,
  removeBrollAsset,
  updateBrollAsset,
} from './api'
import { type Act, type Message, STATUS_LABEL, splitTags, useLibraryActions } from './broll/libraryActions'
import { formatMoment, parseMoments } from './broll/moments'
import { describeAsset } from './brollFormat'
import type { BrollLibraryAsset } from './types'

/**
 * The assets one video can use — its own and the general library's —
 * compactly: thumbnail, measured facts, status and which library each is in.
 * Name, description and tags are the user's own words and sit behind
 * "Details"; nothing is guessed from a filename.
 */
export default function BrollLibrary({
  projectId,
  assets,
  storageNote,
  onChanged,
}: {
  projectId: string
  assets: BrollLibraryAsset[]
  storageNote: string
  onChanged: () => Promise<void> | void
}) {
  const { message, setMessage, busy, act } = useLibraryActions(onChanged)
  const [playing, setPlaying] = useState<string | null>(null)
  // This video's own first, then the general library.
  const ordered = [...assets].sort((a, b) => Number(a.library === 'general') - Number(b.library === 'general'))

  return (
    <div className="broll-library">
      <AssetImport projectId={projectId} storageNote={storageNote} busy={busy} act={act} setMessage={setMessage} />
      {message && <p className={`message ${message.kind}`}>{message.text}</p>}

      {assets.length === 0 ? (
        <p className="hint">No assets yet. Import screen recordings, clips or images to use as B-roll.</p>
      ) : (
        <ul className="broll-grid">
          {ordered.map((asset) => (
            <li key={asset.id} className={`broll-card ${asset.status === 'available' ? '' : 'unusable'}`}>
              <AssetThumb asset={asset} projectId={projectId} playing={playing === asset.id} />
              <strong className="broll-name" title={asset.filename}>
                {asset.display_name}
              </strong>
              <span className="small">{describeAsset(asset)}</span>
              <span className="row">
                <LibraryBadge asset={asset} />
              </span>
              <AssetStatus asset={asset} />
              {asset.status === 'available' && !asset.described && (
                <span className="badge warn" title="Automatic planning only chooses assets you have described.">
                  Not described — add a description to use it automatically
                </span>
              )}
              <AssetButtons
                asset={asset}
                projectId={projectId}
                busy={busy}
                act={act}
                playing={playing === asset.id}
                onPlay={setPlaying}
              />
              <details className="small">
                <summary>{asset.described ? 'Details' : 'Describe…'}</summary>
                <AssetEditor asset={asset} projectId={projectId} busy={busy} act={act} />
              </details>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/** Which library an asset is in, as seen from a video. */
export function LibraryBadge({ asset }: { asset: BrollLibraryAsset }) {
  return asset.library === 'project' ? (
    <span className="badge library-own" title="Only this video can use it.">
      This video
    </span>
  ) : (
    <span className="badge" title="Every video can use it.">
      General
    </span>
  )
}

/**
 * Import by file dialog, or by a pasted path where the dialog cannot open.
 * From a video, the user picks its own library or the general one.
 */
export function AssetImport({
  projectId,
  chooseLibrary = true,
  storageNote,
  busy,
  act,
  setMessage,
}: {
  /** Null: the general library, with no choice offered. */
  projectId: string | null
  /** False: always into `projectId`'s own library. */
  chooseLibrary?: boolean
  storageNote: string
  busy: boolean
  act: Act
  setMessage: (message: Message) => void
}) {
  const [path, setPath] = useState('')
  const [showPath, setShowPath] = useState(false)
  const [target, setTarget] = useState<BrollLibraryAsset['library']>('project')
  const into = projectId && (!chooseLibrary || target === 'project') ? projectId : null

  async function importFile(fromPath: string | null) {
    await act(async () => {
      const result = await importBrollAsset(fromPath, into)
      if (result.cancelled) return
      setPath('')
      const where = result.asset?.library === 'project' ? "this video's library" : 'the general library'
      setMessage(
        result.duplicate
          ? { kind: 'warn', text: `Already in ${where} as "${result.asset?.display_name}".` }
          : { kind: 'ok', text: `Added "${result.asset?.display_name}" to ${where}. The file stays where it is.` },
      )
    }, 'Importing failed.')
  }

  return (
    <>
      <div className="row">
        <button type="button" className="primary" onClick={() => void importFile(null)} disabled={busy}>
          {busy ? 'Working…' : 'Import image or video…'}
        </button>
        {projectId && chooseLibrary && (
          <label className="choice">
            into
            <select
              value={target}
              onChange={(event) => setTarget(event.target.value as BrollLibraryAsset['library'])}
              disabled={busy}
              aria-label="Library to import into"
            >
              <option value="project">this video only</option>
              <option value="general">the general library (all videos)</option>
            </select>
          </label>
        )}
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

export function AssetThumb({
  asset,
  projectId = null,
  playing = false,
}: {
  asset: BrollLibraryAsset
  projectId?: string | null
  playing?: boolean
}) {
  return (
    <div className="broll-thumb">
      {playing && asset.status === 'available' ? (
        <video src={brollAssetMediaUrl(asset.id, asset.fingerprint.digest, projectId)} controls autoPlay muted />
      ) : asset.has_thumbnail ? (
        <img src={brollAssetThumbnailUrl(asset, projectId)} alt="" loading="lazy" />
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
  projectId = null,
  busy,
  act,
  playing,
  onPlay,
}: {
  asset: BrollLibraryAsset
  projectId?: string | null
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
      {asset.status === 'changed' && <RemeasureButton asset={asset} projectId={projectId} busy={busy} act={act} />}
    </div>
  )
}

export function RemeasureButton({
  asset,
  projectId = null,
  busy,
  act,
}: {
  asset: BrollLibraryAsset
  projectId?: string | null
  busy: boolean
  act: Act
}) {
  return (
    <button
      type="button"
      onClick={() => void act(async () => void (await remeasureBrollAsset(asset.id, projectId)), 'Re-measuring failed.')}
      disabled={busy}
    >
      Re-measure
    </button>
  )
}

/**
 * Name, description, tags and moments — the user's own words — plus removal.
 * Seen from a video, also moving the asset between that video's library and
 * the general one.
 */
export function AssetEditor({
  asset,
  projectId = null,
  busy,
  act,
}: {
  asset: BrollLibraryAsset
  projectId?: string | null
  busy: boolean
  act: Act
}) {
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
              await updateBrollAsset(
                asset.id,
                {
                  display_name: name.trim() || null,
                  description: description.trim() || null,
                  tags: splitTags(tags),
                  ...(asset.media_type === 'video' ? { moments: parsed } : {}),
                },
                projectId,
              )
            }, 'Saving failed.')
          }}
        >
          Save details
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            const scope = asset.library === 'general' ? ' Every video loses it.' : ''
            if (window.confirm(`Remove "${asset.display_name}" from the library?${scope} The file itself is not touched.`))
              void act(async () => void (await removeBrollAsset(asset.id, projectId)), 'Removing failed.')
          }}
        >
          Remove from library
        </button>
      </div>
      {projectId && <MoveButton asset={asset} projectId={projectId} busy={busy} act={act} />}
      <p className="small">
        Measured from the file: {asset.measured.codec || 'unknown codec'}, aspect {asset.measured.aspect_ratio.toFixed(3)} (
        {asset.measured.orientation}). Stored by reference: {asset.filename}.
      </p>
    </div>
  )
}

/** Share this video's asset with every video, or keep a general asset for this video alone. */
function MoveButton({ asset, projectId, busy, act }: { asset: BrollLibraryAsset; projectId: string; busy: boolean; act: Act }) {
  const toGeneral = asset.library === 'project'
  return (
    <div className="row">
      <button
        type="button"
        disabled={busy}
        onClick={() => {
          if (
            !toGeneral &&
            !window.confirm(
              `Make "${asset.display_name}" this video's alone? Other videos can no longer use it, and their B-roll plans that do will report it as missing.`,
            )
          )
            return
          void act(
            async () => void (await moveBrollAsset(asset.id, projectId, toGeneral ? 'general' : 'project')),
            'Moving failed.',
          )
        }}
      >
        {toGeneral ? 'Share with all videos' : 'Make it this video’s only'}
      </button>
      <span className="small hint">
        {toGeneral ? 'Moves it to the general library.' : 'Moves it out of the general library.'}
      </span>
    </div>
  )
}
