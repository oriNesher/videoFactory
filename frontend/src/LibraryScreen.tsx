import { useCallback, useEffect, useRef, useState } from 'react'
import { brollAssetMediaUrl, getBrollLibrary, listProjects, moveBrollAsset, updateBrollAsset } from './api'
import { AssetEditor, AssetImport, AssetStatus, AssetThumb, RemeasureButton } from './BrollLibrary'
import { type Act, errorText, splitTags, useLibraryActions } from './broll/libraryActions'
import { filterAssets, NO_FILTER, needsDescription, type LibraryFilter } from './broll/libraryFilter'
import { describeAsset } from './brollFormat'
import type { BrollLibraryAsset } from './types'

type Library = { assets: BrollLibraryAsset[]; storage_note: string }
type Video = { id: string; name: string }

/** The general library, or one video's own. */
type Scope = string | null

/**
 * The B-roll asset libraries on their own page, one at a time: the general
 * library every video uses, or one video's own. Every asset at a glance,
 * importing, a queue of assets that still need a description, and a full
 * editor for one asset at a time.
 */
export default function LibraryScreen() {
  const [videos, setVideos] = useState<Video[]>([])
  const [scope, setScope] = useState<Scope>(null)
  const [library, setLibrary] = useState<Library | null>(null)
  const [loadError, setLoadError] = useState('')
  const [filter, setFilter] = useState<LibraryFilter>(NO_FILTER)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const editorRef = useRef<HTMLElement>(null)

  const load = useCallback(async () => {
    try {
      setLibrary(await getBrollLibrary(scope, true))
      setLoadError('')
    } catch (caught) {
      setLoadError(errorText(caught, 'Loading the library failed.'))
    }
  }, [scope])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    listProjects()
      .then((listing) =>
        setVideos(listing.projects.filter((entry) => entry.error === null).map(({ id, name }) => ({ id, name }))),
      )
      .catch(() => setVideos([]))
  }, [])

  useEffect(() => {
    document.title = 'Asset library'
    return () => {
      document.title = 'Video Factory'
    }
  }, [])

  const { message, setMessage, busy, act } = useLibraryActions(load)

  const assets = library?.assets ?? []
  const undescribed = needsDescription(assets)
  const attention = assets.filter((asset) => asset.status !== 'available').length
  const shown = filterAssets(assets, filter)
  const selected = assets.find((asset) => asset.id === selectedId) ?? null
  const videoName = videos.find((video) => video.id === scope)?.name

  function choose(next: Scope) {
    setScope(next)
    setLibrary(null)
    setSelectedId(null)
    setMessage(null)
  }

  function select(assetId: string) {
    setSelectedId(assetId)
    window.setTimeout(() => editorRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }), 50)
  }

  return (
    <div className="library-page">
      <section className="panel">
        <div className="editor-header">
          <h2>Asset library</h2>
          {library && (
            <span className="row small">
              <span>
                {assets.length} {assets.length === 1 ? 'asset' : 'assets'}
              </span>
              {undescribed.length > 0 && <span className="badge warn">{undescribed.length} need a description</span>}
              {attention > 0 && <span className="badge bad">{attention} need attention</span>}
            </span>
          )}
        </div>

        <label className="library-scope">
          <span>Library</span>
          <select value={scope ?? ''} onChange={(event) => choose(event.target.value || null)} disabled={busy}>
            <option value="">General — shared by all videos</option>
            {videos.length > 0 && (
              <optgroup label="One video's own">
                {videos.map((video) => (
                  <option key={video.id} value={video.id}>
                    {video.name}
                  </option>
                ))}
              </optgroup>
            )}
          </select>
        </label>
        <p className="hint">
          {scope === null
            ? 'Every video can use these. Automatic planning only chooses assets you have described.'
            : `Only "${videoName ?? 'this video'}" can use these, alongside the general library. Automatic planning only chooses assets you have described.`}
        </p>

        <AssetImport
          key={scope ?? 'general'}
          projectId={scope}
          chooseLibrary={false}
          storageNote={library?.storage_note ?? ''}
          busy={busy}
          act={act}
          setMessage={setMessage}
        />
        {message && <p className={`message ${message.kind}`}>{message.text}</p>}
        {loadError && <p className="message error">{loadError}</p>}
      </section>

      {undescribed.length > 0 && (
        <section className="panel describe-queue">
          <h3>Description needed ({undescribed.length})</h3>
          <p className="hint">
            Say what each one shows, in the words you would speak in a video. Saved assets leave this list and become
            available to automatic planning.
          </p>
          <ul>
            {undescribed.map((asset) => (
              <DescribeRow
                key={asset.id}
                asset={asset}
                scope={scope}
                busy={busy}
                act={act}
                onOpen={() => select(asset.id)}
              />
            ))}
          </ul>
        </section>
      )}

      <div className={selected ? 'library-body with-editor' : 'library-body'}>
        <section className="panel">
          <div className="editor-header">
            <h3>All assets ({assets.length})</h3>
          </div>
          <div className="row library-filters">
            <input
              type="search"
              value={filter.query}
              onChange={(event) => setFilter({ ...filter, query: event.target.value })}
              placeholder="Search names, descriptions, tags…"
              aria-label="Search assets"
              dir="auto"
            />
            <select
              value={filter.mediaType}
              onChange={(event) => setFilter({ ...filter, mediaType: event.target.value as LibraryFilter['mediaType'] })}
              aria-label="Media type"
            >
              <option value="all">Images and videos</option>
              <option value="image">Images</option>
              <option value="video">Videos</option>
            </select>
            <select
              value={filter.status}
              onChange={(event) => setFilter({ ...filter, status: event.target.value as LibraryFilter['status'] })}
              aria-label="File status"
            >
              <option value="all">Any status</option>
              <option value="attention">Needs attention</option>
            </select>
          </div>

          {!library && !loadError && <p className="hint">Loading…</p>}
          {library && assets.length === 0 && (
            <p className="hint">
              {scope === null
                ? 'The general library is empty. Import screen recordings, clips or images to use as B-roll.'
                : 'This video has no assets of its own yet. Import some here, or from the B-roll section of the video.'}
            </p>
          )}
          {assets.length > 0 && shown.length === 0 && <p className="hint">No asset matches.</p>}

          <ul className="broll-grid library-grid">
            {shown.map((asset) => (
              <li key={asset.id}>
                <button
                  type="button"
                  className={[
                    'broll-card',
                    asset.status === 'available' ? '' : 'unusable',
                    asset.id === selectedId ? 'selected' : '',
                  ].join(' ')}
                  onClick={() => (asset.id === selectedId ? setSelectedId(null) : select(asset.id))}
                  aria-pressed={asset.id === selectedId}
                >
                  <AssetThumb asset={asset} projectId={scope} />
                  <strong className="broll-name" title={asset.filename}>
                    {asset.display_name}
                  </strong>
                  <span className="small">{describeAsset(asset)}</span>
                  {asset.description && (
                    <span className="small asset-description" dir="auto">
                      {asset.description}
                    </span>
                  )}
                  <span className="row">
                    {asset.status !== 'available' && <span className="badge bad">{statusWord(asset)}</span>}
                    {!asset.described && <span className="badge warn">No description</span>}
                    {asset.tags.slice(0, 3).map((tag) => (
                      <span key={tag} className="badge" dir="auto">
                        {tag}
                      </span>
                    ))}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </section>

        {selected && (
          <aside className="panel library-editor" ref={editorRef}>
            <div className="editor-header">
              <h3 className="broll-name" title={selected.filename}>
                {selected.display_name}
              </h3>
              <button type="button" onClick={() => setSelectedId(null)} aria-label="Close">
                ✕
              </button>
            </div>
            <AssetPreview asset={selected} scope={scope} />
            <p className="small">{describeAsset(selected)}</p>
            <AssetStatus asset={selected} />
            {selected.status === 'changed' && <RemeasureButton asset={selected} projectId={scope} busy={busy} act={act} />}
            {/* Keyed so the form starts from the saved words of whichever asset is open. */}
            <AssetEditor
              key={`${selected.id}-${selected.fingerprint.digest}`}
              asset={selected}
              projectId={scope}
              busy={busy}
              act={act}
            />
            {scope === null && <MoveToVideo asset={selected} videos={videos} busy={busy} act={act} />}
          </aside>
        )}
      </div>
    </div>
  )
}

function statusWord(asset: BrollLibraryAsset): string {
  return asset.status === 'missing' ? 'File missing' : asset.status === 'changed' ? 'File changed' : 'Unsupported'
}

/** The file itself where it can be shown, the thumbnail otherwise. */
function AssetPreview({ asset, scope }: { asset: BrollLibraryAsset; scope: Scope }) {
  if (asset.status !== 'available') return <AssetThumb asset={asset} projectId={scope} />
  const url = brollAssetMediaUrl(asset.id, asset.fingerprint.digest, scope)
  return (
    <div className="broll-thumb library-preview">
      {asset.media_type === 'video' ? <video src={url} controls muted preload="metadata" /> : <img src={url} alt="" />}
    </div>
  )
}

/** Take a general asset out of the general library and give it to one video. */
function MoveToVideo({
  asset,
  videos,
  busy,
  act,
}: {
  asset: BrollLibraryAsset
  videos: Video[]
  busy: boolean
  act: Act
}) {
  const [videoId, setVideoId] = useState('')
  if (videos.length === 0) return null
  const name = videos.find((video) => video.id === videoId)?.name
  return (
    <div className="move-to-video">
      <span className="small">Only one video needs it?</span>
      <div className="row">
        <select value={videoId} onChange={(event) => setVideoId(event.target.value)} aria-label="Video to move it to">
          <option value="">Choose a video…</option>
          {videos.map((video) => (
            <option key={video.id} value={video.id}>
              {video.name}
            </option>
          ))}
        </select>
        <button
          type="button"
          disabled={busy || !videoId}
          onClick={() => {
            if (
              !window.confirm(
                `Move "${asset.display_name}" into "${name}"'s own library? Other videos can no longer use it, and their B-roll plans that do will report it as missing.`,
              )
            )
              return
            void act(async () => void (await moveBrollAsset(asset.id, videoId, 'project')), 'Moving failed.')
          }}
        >
          Move to that video
        </button>
      </div>
    </div>
  )
}

/** One asset in the "description needed" queue: describe it in place. */
function DescribeRow({
  asset,
  scope,
  busy,
  act,
  onOpen,
}: {
  asset: BrollLibraryAsset
  scope: Scope
  busy: boolean
  act: Act
  onOpen: () => void
}) {
  const [description, setDescription] = useState('')
  const [tags, setTags] = useState('')
  const empty = !description.trim() && splitTags(tags).length === 0

  function save() {
    if (empty) return
    void act(async () => {
      await updateBrollAsset(asset.id, { description: description.trim() || null, tags: splitTags(tags) }, scope)
    }, 'Saving failed.')
  }

  return (
    <li className="describe-row">
      <AssetThumb asset={asset} projectId={scope} />
      <div className="describe-info">
        <strong className="broll-name" title={asset.filename}>
          {asset.display_name}
        </strong>
        <span className="small">{describeAsset(asset)}</span>
        <AssetStatus asset={asset} />
      </div>
      <form
        className="describe-form"
        onSubmit={(event) => {
          event.preventDefault()
          save()
        }}
      >
        <textarea
          rows={2}
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) save()
          }}
          placeholder="What it shows, e.g. “the analytics dashboard scrolling through monthly sales”"
          aria-label={`Description of ${asset.display_name}`}
          dir="auto"
        />
        <input
          type="text"
          value={tags}
          onChange={(event) => setTags(event.target.value)}
          placeholder="Tags, comma-separated (optional)"
          aria-label={`Tags of ${asset.display_name}`}
          dir="auto"
        />
        <div className="row">
          <button type="submit" className="primary" disabled={busy || empty}>
            Save
          </button>
          <button type="button" className="link" onClick={onOpen}>
            {asset.media_type === 'video' ? 'More: name, moments…' : 'More: name…'}
          </button>
        </div>
      </form>
    </li>
  )
}
