import { useEffect } from 'react'
import MusicLibrary from './MusicLibrary'

/** The music library on its own page: every track, shared by all videos. */
export default function MusicLibraryScreen() {
  useEffect(() => {
    document.title = 'Music library'
    return () => {
      document.title = 'Video Factory'
    }
  }, [])

  return (
    <div className="library-page">
      <section className="panel music">
        <div className="editor-header">
          <h2>Music library</h2>
        </div>
        <p className="hint">
          Background music for your videos. Choose a track for a video in its Music stage; sound effects will be kept here
          too in a later version.
        </p>
        <MusicLibrary />
      </section>
    </div>
  )
}
