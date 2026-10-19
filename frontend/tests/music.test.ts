// Background-music timing and level (stage 4). Run with: npm.cmd test
import assert from 'node:assert/strict'
import { test } from 'node:test'
import type { MusicProps } from '../src/music/types.ts'
import { musicEndFrame, musicStartFrame, musicVolumeAt } from '../src/music/volume.ts'

const base: MusicProps = {
  asset: { id: '0123456789ab', duration_seconds: 10 },
  volume: 0.5,
  start_seconds: 0,
  fade_in_seconds: 1,
  fade_out_seconds: 2,
  loop: true,
}

test('a looping track plays to the end of the video', () => {
  assert.equal(musicEndFrame(base, 25, 1000), 1000)
})

test('a track that does not loop stops where it ends, measured from the start point', () => {
  // 10 s track from 4 s: 6 s left = 150 frames at 25 fps, inside a 40 s video.
  assert.equal(musicEndFrame({ ...base, loop: false, start_seconds: 4 }, 25, 1000), 150)
  // A track longer than the video still ends with the video.
  assert.equal(musicEndFrame({ ...base, loop: false }, 25, 100), 100)
})

test('the in-point is rounded to the nearest composition frame', () => {
  assert.equal(musicStartFrame({ ...base, start_seconds: 2.019 }, 25), 50)
  assert.equal(musicStartFrame({ ...base, start_seconds: 2.021 }, 25), 51)
})

test('the music fades in from silence and out to silence at its end', () => {
  const at = (frame: number) => musicVolumeAt(base, frame, 25, 250)
  assert.equal(at(0), 0)
  assert.equal(at(12.5), 0.25) // half-way through the 1 s fade-in
  assert.equal(at(25), 0.5)
  assert.equal(at(100), 0.5)
  assert.equal(at(225), 0.25) // 1 s before the end of a 2 s fade-out
  assert.equal(at(250), 0)
})

test('the fade-out follows the track end when it does not loop', () => {
  const music = { ...base, loop: false, asset: { ...base.asset, duration_seconds: 4 } }
  assert.equal(musicVolumeAt(music, 75, 25, 250), 0.25) // 4 s track: ends at frame 100
  assert.equal(musicVolumeAt(music, 120, 25, 250), 0)
})

test('without fades the level is constant and never above the volume', () => {
  const flat = { ...base, fade_in_seconds: 0, fade_out_seconds: 0, volume: 1 }
  assert.equal(musicVolumeAt(flat, 0, 30, 90), 1)
  assert.equal(musicVolumeAt(flat, 89, 30, 90), 1)
  // Fades longer than the video overlap: the smaller of the two wins.
  const long = { ...base, fade_in_seconds: 30, fade_out_seconds: 30 }
  assert.ok(musicVolumeAt(long, 45, 30, 90) < base.volume)
})
