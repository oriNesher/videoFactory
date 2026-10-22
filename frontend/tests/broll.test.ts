// B-roll timing and geometry (stage 3A). Run with: npm.cmd test
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { captionLine, PIP_MAX_HEIGHT_FRACTION, pipBox, placementFrameRange, sourceStartFrame } from '../src/broll/layout.ts'
import { entranceFrames, entranceStyle, withTransform } from '../src/broll/entrance.ts'

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

// --- described moments (stage 3B) ---

test('moments are read one per line, in m:ss or seconds, and written back the same way', async () => {
  const { formatMoment, parseMoments } = await import('../src/broll/moments.ts')
  const parsed = parseMoments('0:05-0:12 לוח הבקרה\n\n75.5 – 1:20 the export dialog ')
  assert.deepEqual(parsed, [
    { start: 5, end: 12, description: 'לוח הבקרה' },
    { start: 75.5, end: 80, description: 'the export dialog' },
  ])
  assert.equal(formatMoment({ start: 75.5, end: 80, description: 'x' }), '1:15.5-1:20 x')
  assert.deepEqual(parseMoments(''), [])
})

test('a malformed moment names its line instead of guessing', async () => {
  const { parseMoments } = await import('../src/broll/moments.ts')
  assert.match(String(parseMoments('0:05-0:12 ok\nfrom five to twelve')), /Line 2/)
  assert.match(String(parseMoments('0:12-0:05 backwards')), /after the start/)
  assert.match(String(parseMoments('0:75-1:00 not a time')), /Line 1/)
  assert.match(String(parseMoments('0:05-0:12')), /Line 1/) // no description
})

test('the library page lists undescribed assets and searches the user’s own words', async () => {
  const { filterAssets, needsDescription, NO_FILTER } = await import('../src/broll/libraryFilter.ts')
  const base = { tags: [], moments: [], description: null, described: false, status: 'available' }
  const assets = [
    { ...base, id: 'a', media_type: 'video', display_name: 'clip.mp4', filename: 'clip.mp4' },
    { ...base, id: 'b', media_type: 'image', display_name: 'Logo', filename: 'logo.png', description: 'Our company logo', described: true },
    { ...base, id: 'c', media_type: 'video', display_name: 'demo.mp4', filename: 'demo.mp4', tags: ['dashboard'], described: true, status: 'missing' },
  ] as never[]
  assert.deepEqual(needsDescription(assets).map((asset: { id: string }) => asset.id), ['a'])
  const ids = (filter: object) => filterAssets(assets, { ...NO_FILTER, ...filter }).map((asset: { id: string }) => asset.id)
  assert.deepEqual(ids({}), ['a', 'b', 'c'])
  assert.deepEqual(ids({ query: 'COMPANY logo' }), ['b'])
  assert.deepEqual(ids({ query: 'dashboard' }), ['c'])
  assert.deepEqual(ids({ mediaType: 'video' }), ['a', 'c'])
  assert.deepEqual(ids({ status: 'attention' }), ['c'])
})

test('a custom position centres the box at x/y and keeps it inside the frame', () => {
  const settings = { size_percent: 40, margin_percent: 4, x_percent: 50, y_percent: 50 }
  const box = pipBox(1080, 1920, 1920, 1080, { position: 'custom', ...settings })
  assert.equal(box.width, 432)
  assert.equal(box.left + box.width / 2, 540)
  assert.ok(Math.abs(box.top + box.height / 2 - 960) <= 1)
  const corner = pipBox(1080, 1920, 1920, 1080, { position: 'custom', ...settings, x_percent: 100, y_percent: 0 })
  assert.equal(corner.left, 1080 - corner.width)
  assert.equal(corner.top, 0)
})

test('the 45 % height limit holds up to size 60 and grows above it', () => {
  const portrait = (size: number) =>
    pipBox(1080, 1920, 924, 2048, { position: 'top_right', size_percent: size, margin_percent: 4 })
  assert.equal(portrait(40).height, Math.round(1920 * 0.45))
  assert.equal(portrait(60).height, Math.round(1920 * 0.45))
  assert.equal(portrait(90).height, Math.round(1920 * 0.45 * 1.5))
})

test('above_captions centres the box with its bottom just above the caption line', () => {
  // 1080×1920, captions at 80 % inside a 6 % safe margin, font 5 % of the shorter side.
  const line = captionLine(1080, 1920, { position_percent: 80, safe_margin_percent: 6, font_size_percent: 5 })
  const box = pipBox(1080, 1920, 1920, 1080, { position: 'above_captions', size_percent: 70, margin_percent: 4 }, line)
  assert.equal(box.width, 756)
  assert.equal(box.left, Math.round((1080 - 756) / 2))
  assert.ok(box.top + box.height < line.top, 'the box ends above the caption line')
  assert.ok(line.top - (box.top + box.height) < 40, 'and close to it')
  // Captions at the top: the box goes just below them.
  const high = captionLine(1080, 1920, { position_percent: 10, safe_margin_percent: 6, font_size_percent: 5 })
  const below = pipBox(1080, 1920, 1920, 1080, { position: 'above_captions', size_percent: 70, margin_percent: 4 }, high)
  assert.ok(below.top > high.bottom)
})

test('an entrance lasts its own length, never past the placement, at least one frame', () => {
  assert.equal(entranceFrames(0.4, 25, 100), 10)
  assert.equal(entranceFrames(2, 25, 30), 30)
  assert.equal(entranceFrames(0.01, 25, 30), 1)
  assert.equal(entranceFrames(undefined, 25, 30), 1)
})

test('every entrance starts hidden or away and ends exactly as without one', () => {
  const all = ['fade', 'zoom', 'slide', 'pop', 'blur', 'spin', 'wipe'] as const
  for (const entrance of all) {
    const first = entranceStyle(entrance, 0, 10, 1920)
    assert.notDeepEqual(first, {}, entrance)
    assert.deepEqual(entranceStyle(entrance, 10, 10, 1920), {}, entrance)
  }
  assert.equal(entranceStyle('fade', 0, 10, 1920).opacity, 0)
  assert.equal(entranceStyle('slide', 0, 10, 1920).transform, 'translateY(1920px)')
  assert.equal(entranceStyle('wipe', 0, 10, 1920).clipPath, 'inset(0 100% 0 0)')
  assert.deepEqual(entranceStyle('none', 0, 10, 1920), {})
  assert.deepEqual(entranceStyle(undefined, 0, 10, 1920), {})
  // Pop overshoots before it settles.
  const scales = Array.from({ length: 10 }, (_, frame) =>
    Number(/scale\(([\d.]+)\)/.exec(entranceStyle('pop', frame, 10, 1920).transform ?? '')![1]),
  )
  assert.ok(Math.max(...scales) > 1.05)
})

test("an entrance's transform comes before the box's own rotation", () => {
  assert.equal(withTransform({ transform: 'scale(0.5)' }, 'rotate(10deg)').transform, 'scale(0.5) rotate(10deg)')
  assert.equal(withTransform({}, undefined).transform, undefined)
})
