// B-roll timing and geometry (stage 3A). Run with: npm.cmd test
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { PIP_MAX_HEIGHT_FRACTION, pipBox, placementFrameRange, sourceStartFrame } from '../src/broll/layout.ts'

test('a placement covers the frames whose start time is inside it, like a caption', () => {
  // 25 fps: 1.0 s is frame 25, 2.6 s is frame 65.
  assert.deepEqual(placementFrameRange(1.0, 2.6, 25), { from: 25, to: 65 })
  // A start just after a frame boundary begins on the next frame.
  assert.deepEqual(placementFrameRange(1.01, 2.6, 25), { from: 26, to: 65 })
})

test('back-to-back placements meet on one frame boundary, with no gap and no shared frame', () => {
  const a = placementFrameRange(0.5, 1.7, 30)
  const b = placementFrameRange(1.7, 3.0, 30)
  assert.equal(a.to, b.from)
})

test('the in-point is rounded to the nearest composition frame', () => {
  assert.equal(sourceStartFrame(null, 30), 0)
  assert.equal(sourceStartFrame(2.0, 25), 50)
  assert.equal(sourceStartFrame(2.019, 25), 50) // 50.475 → 50
  assert.equal(sourceStartFrame(2.021, 25), 51) // 50.525 → 51
})

test('a picture-in-picture box keeps the asset aspect ratio and sits in its corner', () => {
  // A 16:9 asset in a 1080×1920 portrait frame, 40 % wide, 4 % margin.
  const box = pipBox(1080, 1920, 1920, 1080, { position: 'top_right', size_percent: 40, margin_percent: 4 })
  assert.equal(box.width, 432)
  assert.equal(box.height, 243)
  assert.equal(box.top, Math.round(1080 * 0.04))
  assert.equal(box.left + box.width, Math.round(1080 - 1080 * 0.04))
  const bottomLeft = pipBox(1080, 1920, 1920, 1080, { position: 'bottom_left', size_percent: 40, margin_percent: 4 })
  assert.equal(bottomLeft.left, Math.round(1080 * 0.04))
  assert.equal(bottomLeft.top + bottomLeft.height, Math.round(1920 - 1080 * 0.04))
})

test('a tall asset is shrunk to the height limit, still without stretching', () => {
  // A 9:16 asset 60 % wide in a 16:9 frame would be taller than the frame.
  const box = pipBox(1920, 1080, 1080, 1920, { position: 'top_left', size_percent: 60, margin_percent: 4 })
  assert.equal(box.height, Math.round(1080 * PIP_MAX_HEIGHT_FRACTION))
  assert.ok(Math.abs(box.width / box.height - 1080 / 1920) < 0.01)
})
