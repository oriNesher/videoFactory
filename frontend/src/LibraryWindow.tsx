import { useCallback, useEffect, useRef, useState } from 'react'
import { getBrollLibrary } from './api'
import { AssetEditor, AssetImport, AssetStatus, AssetThumb, RemeasureButton } from './BrollLibrary'
import { errorText, useLibraryActions } from './broll/libraryActions'
import { formatTime } from './captionFormat'
import type { BrollLibraryAsset } from './types'

/**
 * One asset library in a window over the B-roll stage: this video's own, or
 * the general one every video shares. Adding assets is the first thing in it,
 * and every asset is listed with its description open for editing.
 */
type Props = {
  /** The video the window was opened from. */
  projectId: string
  videoName: string
  which: 'video' | 'general'
  onClose: () => void
}

export default function LibraryWindow({ projectId, videoName, which, onClose }: Props) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [assets, setAssets] = useState<BrollLibraryAsset[] | null>(null)
  const [storageNote, setStorageNote] = useState('')
  const [loadError, setLoadError] = useState('')

  const load = useCallback(async () => {
    try {
      // The general library on its own, or only this video's own assets.
      const library = await getBrollLibrary(which === 'video' ? projectId : null, true)
      setAssets(library.assets)
      setStorageNote(library.storage_note)
      setLoadError('')
    } catch (caught) {
      setLoadError(errorText(caught, 'Loading the library failed.'))
    }
  }, [projectId, which])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    const element = dialog.current
    if (element && !element.open) element.showModal()
  }, [])

  const { message, setMessage, busy, act } = useLibraryActions(load)
  const undescribed = (assets ?? []).filter((asset) => !asset.described).length
  const title = which === 'video' ? `This video's assets — ${videoName}` : 'General library — shared by all videos'

  return (
    <dialog ref={dialog} className="library-window" onClose={onClose} onCancel={onClose}>
      <div className="library-window-header">
        <h3>{title}</h3>
        {assets && (
          <span className="row small">
            {assets.length} {assets.length === 1 ? 'asset' : 'assets'}
            {undescribed > 0 && <span className="badge warn">{undescribed} need a description</span>}
          </span>
        )}
        <button type="button" onClick={() => dialog.current?.close()} aria-label="Close">
          Close
        </button>
      </div>

      <div className="library-window-add">
        <AssetImport
          projectId={which === 'video' ? projectId : null}
          chooseLibrary={false}
          storageNote={storageNote}
          busy={busy}
          act={act}
          setMessage={setMessage}
          compact
        />
      </div>
      {message && <p className={`message ${message.kind}`}>{message.text}</p>}
      {loadError && <p className="message error">{loadError}</p>}

      {!assets && !loadError && <p className="hint">Loading…</p>}
      {assets && assets.length === 0 && (
        <p className="hint">No assets yet.</p>
      )}

      <ul className="library-window-list">
        {assets?.map((asset) => (
          <li key={asset.id} className={asset.described ? undefined : 'needs-description'}>
            <div className="library-window-info">
              <AssetThumb asset={asset} projectId={projectId} />
              <strong className="broll-name" title={asset.filename}>
                {asset.display_name}
              </strong>
              <span className="small">
                {asset.media_type === 'image' ? 'Image' : 'Video'}
                {asset.measured.duration_seconds !== null && ` · ${formatTime(asset.measured.duration_seconds)}`}
              </span>
              <AssetStatus asset={asset} />
              {asset.status === 'changed' && (
                <RemeasureButton asset={asset} projectId={projectId} busy={busy} act={act} />
              )}
            </div>
            {/* Keyed so the form restarts from the saved words after a save. */}
            <AssetEditor
              key={`${asset.id}-${asset.fingerprint.digest}-${asset.description ?? ''}-${asset.tags.join(',')}`}
              asset={asset}
              projectId={projectId}
              busy={busy}
              act={act}
              compact
            />
          </li>
        ))}
      </ul>
    </dialog>
  )
}
