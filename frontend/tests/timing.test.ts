// Seconds → frames for the caption composition. Run with: npm.cmd test
import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  activeIndex,
  captionFrameRange,
  captionFrameRanges,
  durationInFrames,
  secondsToFrame,
} from '../src/captions/timing.ts'

test('a caption covers exactly the frames whose start time is inside it', () => {
  // 30 fps: frame 30 starts at 1.000 s, frame 60 at 2.000 s.
  assert.deepEqual(captionFrameRange(1.0, 2.0, 30), { from: 30, to: 60 })
  // 1.01 s is after frame 30's start, so the caption starts on frame 31.
  assert.deepEqual(captionFrameRange(1.01, 2.01, 30), { from: 31, to: 61 })
})

test('floating-point noise on an exact frame boundary does not move the boundary', () => {
  assert.equal(secondsToFrame(0.1, 30), 3) // 0.1 * 30 = 3.0000000000000004
  assert.equal(secondsToFrame(0.7, 30), 21) // 0.7 * 30 = 20.999999999999996
  assert.equal(secondsToFrame(1001 / 30000, 30000 / 1001), 1) // one NTSC frame
})

test('no drift over a long video: frames are computed from absolute seconds', () => {
  const fps = 30000 / 1001
  const hour = 3600
  // A caption one hour in starts on the frame whose start time is at or after it.
  const from = secondsToFrame(hour, fps)
  assert.ok(from / fps >= hour - 1e-9)
  assert.ok((from - 1) / fps < hour)
  assert.equal(durationInFrames(hour, fps), Math.round(hour * fps))
})

test('back-to-back captions share a boundary with no gap and no double frame', () => {
  const ranges = captionFrameRanges(
    [
      { start: 0.5, end: 1.25 },
      { start: 1.25, end: 2.0 },
    ],
    25,
  )
  assert.deepEqual(ranges, [
    { from: 13, to: 32 },
    { from: 32, to: 50 },
  ])
  assert.equal(activeIndex(ranges, 31), 0)
  assert.equal(activeIndex(ranges, 32), 1)
  assert.equal(activeIndex(ranges, 50), -1)
  assert.equal(activeIndex(ranges, 12), -1)
})

test('a caption shorter than a frame still gets one frame, and ranges stay disjoint', () => {
  const ranges = captionFrameRanges(
    [
      { start: 1.01, end: 1.02 },
      { start: 1.02, end: 1.5 },
    ],
    30,
  )
  assert.deepEqual(ranges[0], { from: 31, to: 32 })
  assert.ok(ranges[1].from >= ranges[0].to)
  assert.equal(activeIndex(ranges, 31), 0)
})

test('duration in frames rounds to the nearest frame and is never zero', () => {
  assert.equal(durationInFrames(4.0, 25), 100)
  assert.equal(durationInFrames(8.21, 30), 246)
  assert.equal(durationInFrames(0.001, 30), 1)
})
