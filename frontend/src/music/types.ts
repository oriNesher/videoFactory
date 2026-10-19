/**
 * The background-music part of the composition's props (stage 4).
 *
 * Written by the backend (`backend/music.py` → `compose`) from the saved
 * music settings and the audio library, so the in-app <Player> and the
 * export render receive identical values. Only `asset.src` differs: the
 * browser points it at the backend, the render script at its own local
 * server.
 */

export type MusicAsset = {
  id: string
  /** Measured length of the whole track. */
  duration_seconds: number
  /** Filled in by whoever renders: never stored. */
  src?: string
}

export type MusicProps = {
  asset: MusicAsset
  /** Linear gain, 0–1, under the speaker's own track. */
  volume: number
  /** Where in the track the video's first frame starts (and every loop restarts). */
  start_seconds: number
  fade_in_seconds: number
  /** Ends where the music ends: the video's end, or the track's when it does not loop. */
  fade_out_seconds: number
  /** A track shorter than the video starts again from `start_seconds`. */
  loop: boolean
}
