// Render one captioned video with Remotion, locally. Started by the backend's
// export job (backend/caption_render.py) as its own process, with the
// frontend directory as the working directory; never by the browser.
//
//   node scripts/render-captions.mjs --job <job.json>     render
//   node scripts/render-captions.mjs --setup [--bundle-cache <dir>]
//                                                         download the
//                                                         headless browser and
//                                                         build the bundle
//
// job.json (written by the backend from an immutable export snapshot):
//   { "props": CaptionedVideoProps without video.src,
//     "input_path": "C:\\…\\take 1.mp4",
//     "output_path": "C:\\…\\exports\\captions\\<id>\\.partial.mp4",
//     "bundle_cache_dir": "C:\\…\\workspace\\cache\\remotion-bundles",
//     "crf": 18, "x264_preset": "medium", "concurrency": null }
//
// Media access. The composition needs a URL for the video. This script serves
// exactly one file — the snapshot's input — on 127.0.0.1, on a random port,
// under a random token, for as long as the render runs. Nothing else on disk
// is reachable through it, and it is gone when the process exits.
//
// Protocol. Lines on stdout starting with "@@" are JSON events for the backend;
// everything else (Remotion's own logs) only goes to the log file.
//   @@{"event":"stage","stage":"bundling"|"browser"|"composition"|"rendering"}
//   @@{"event":"browser_download","percent":12.5}
//   @@{"event":"progress","progress":0.42,"rendered":120,"encoded":118,"stage":"encoding"}
//   @@{"event":"done","output":"…"}
//   @@{"event":"error","kind":"browser_missing"|"failed","message":"…"}
//
// Cancellation is the backend killing this process tree (taskkill /T), which
// takes the headless browser and the encoder with it. The output is written to
// the path the backend gave, which is a partial name; only the backend turns it
// into a finished export, after checking it with FFprobe.

import { createHash, randomBytes } from 'node:crypto'
import { createReadStream, existsSync, promises as fs, readFileSync, statSync } from 'node:fs'
import { createServer } from 'node:http'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const FRONTEND = path.resolve(HERE, '..')
const ENTRY = path.join(FRONTEND, 'src', 'remotion', 'index.ts')
// Everything that changes what the bundle contains. A change to any of these
// builds a new bundle; otherwise the cached one is reused (bundling costs
// tens of seconds, rendering a short clip a few).
const BUNDLE_INPUTS = [
  path.join(FRONTEND, 'src', 'remotion'),
  path.join(FRONTEND, 'src', 'captions'),
  path.join(FRONTEND, 'package-lock.json'),
]
const COMPOSITION_ID = 'CaptionedVideo'

function emit(event) {
  process.stdout.write('@@' + JSON.stringify(event) + '\n')
}

function parseArguments(argv) {
  const parsed = { job: null, setup: false, bundleCache: null }
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index]
    if (argument === '--job') parsed.job = argv[++index]
    else if (argument === '--setup') parsed.setup = true
    else if (argument === '--bundle-cache') parsed.bundleCache = argv[++index]
    else throw new Error(`Unknown argument: ${argument}`)
  }
  return parsed
}

// --- the bundle ---------------------------------------------------------------

async function listFiles(target) {
  const info = statSync(target)
  if (info.isFile()) return [target]
  const entries = await fs.readdir(target, { withFileTypes: true })
  const nested = await Promise.all(
    entries.map((entry) => listFiles(path.join(target, entry.name))),
  )
  return nested.flat()
}

async function bundleKey() {
  const hash = createHash('sha256')
  const files = (await Promise.all(BUNDLE_INPUTS.map(listFiles))).flat().sort()
  for (const file of files) {
    hash.update(path.relative(FRONTEND, file).replaceAll('\\', '/'))
    hash.update('\0')
    hash.update(readFileSync(file))
    hash.update('\0')
  }
  return hash.digest('hex').slice(0, 16)
}

async function ensureBundle(cacheDirectory) {
  const { bundle } = await import('@remotion/bundler')
  const key = await bundleKey()
  const destination = path.join(cacheDirectory, key)
  if (existsSync(path.join(destination, 'index.html'))) return destination

  emit({ event: 'stage', stage: 'bundling' })
  await fs.mkdir(cacheDirectory, { recursive: true })
  // Built beside its final name and renamed, so a killed build never leaves
  // a half-written bundle that a later render would trust.
  const temporary = path.join(cacheDirectory, `.${key}-${randomBytes(4).toString('hex')}`)
  await bundle({ entryPoint: ENTRY, outDir: temporary, enableCaching: true })
  try {
    await fs.rename(temporary, destination)
  } catch (error) {
    // Another render finished the same bundle first; use that one.
    if (!existsSync(path.join(destination, 'index.html'))) throw error
    await fs.rm(temporary, { recursive: true, force: true })
  }
  return destination
}

// --- the headless browser ------------------------------------------------------

class BrowserMissing extends Error {}

async function ensureHeadlessBrowser(allowDownload) {
  const { ensureBrowser } = await import('@remotion/renderer')
  emit({ event: 'stage', stage: 'browser' })
  await ensureBrowser({
    logLevel: 'warn',
    onBrowserDownload: () => {
      if (!allowDownload) {
        // A render never downloads anything on its own: preparing the
        // renderer is an explicit step in the interface.
        throw new BrowserMissing(
          'The headless browser Remotion renders with is not downloaded yet. ' +
            'Press "Prepare renderer" in the Captions section (needs internet once, about 100 MB).',
        )
      }
      let lastPercent = -1
      return {
        version: null,
        onProgress: ({ percent }) => {
          const whole = Math.floor(percent * 100)
          if (whole === lastPercent) return
          lastPercent = whole
          emit({ event: 'browser_download', percent: whole })
        },
      }
    },
  })
}

// --- serving the one input file -------------------------------------------------

function serveSingleFile(filePath) {
  const token = randomBytes(16).toString('hex')
  const route = `/${token}/input${path.extname(filePath).toLowerCase() || '.mp4'}`
  const size = statSync(filePath).size

  const server = createServer((request, response) => {
    // The render page is served from Remotion's own local server on another
    // port, so the media request is cross-origin.
    response.setHeader('Access-Control-Allow-Origin', '*')
    response.setHeader('Access-Control-Allow-Headers', 'Range')
    response.setHeader('Access-Control-Expose-Headers', 'Content-Range, Content-Length, Accept-Ranges')
    if (request.method === 'OPTIONS') {
      response.writeHead(204)
      response.end()
      return
    }
    const url = new URL(request.url ?? '/', 'http://127.0.0.1')
    if (url.pathname !== route || (request.method !== 'GET' && request.method !== 'HEAD')) {
      response.writeHead(404)
      response.end()
      return
    }

    response.setHeader('Accept-Ranges', 'bytes')
    response.setHeader('Content-Type', 'video/mp4')
    const range = /^bytes=(\d*)-(\d*)$/.exec(request.headers.range ?? '')
    let start = 0
    let end = size - 1
    if (range) {
      if (range[1] === '' && range[2] !== '') {
        start = Math.max(0, size - Number(range[2]))
      } else {
        start = Number(range[1] || 0)
        if (range[2] !== '') end = Math.min(end, Number(range[2]))
      }
      if (start > end || start >= size) {
        response.writeHead(416, { 'Content-Range': `bytes */${size}` })
        response.end()
        return
      }
      response.writeHead(206, {
        'Content-Range': `bytes ${start}-${end}/${size}`,
        'Content-Length': end - start + 1,
      })
    } else {
      response.writeHead(200, { 'Content-Length': size })
    }
    if (request.method === 'HEAD') {
      response.end()
      return
    }
    const stream = createReadStream(filePath, { start, end })
    stream.on('error', () => response.destroy())
    stream.pipe(response)
  })

  return new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address()
      resolve({ server, url: `http://127.0.0.1:${port}${route}` })
    })
  })
}

// --- render -------------------------------------------------------------------

async function render(jobPath) {
  // A UTF-8 byte-order mark is tolerated: Windows tools like to add one.
  const job = JSON.parse(readFileSync(jobPath, 'utf8').replace(/^﻿/, ''))
  if (!existsSync(job.input_path)) throw new Error(`The input video is missing: ${job.input_path}`)

  const serveUrl = await ensureBundle(job.bundle_cache_dir)
  await ensureHeadlessBrowser(false)

  const { server, url } = await serveSingleFile(job.input_path)
  try {
    const { renderMedia, selectComposition } = await import('@remotion/renderer')
    const inputProps = { ...job.props, video: { ...job.props.video, src: url } }

    emit({ event: 'stage', stage: 'composition' })
    const composition = await selectComposition({
      serveUrl,
      id: COMPOSITION_ID,
      inputProps,
      logLevel: 'warn',
    })

    // The snapshot is explicit about all three; refuse to render anything else.
    const expected = job.props.video
    if (
      composition.width !== expected.width ||
      composition.height !== expected.height ||
      composition.fps !== expected.fps ||
      composition.durationInFrames !== expected.duration_in_frames
    ) {
      throw new Error(
        `The composition resolved to ${composition.width}x${composition.height} at ${composition.fps} fps ` +
          `for ${composition.durationInFrames} frames, not the snapshot's ` +
          `${expected.width}x${expected.height} at ${expected.fps} fps for ${expected.duration_in_frames}.`,
      )
    }

    emit({ event: 'stage', stage: 'rendering' })
    let lastReported = -1
    await renderMedia({
      serveUrl,
      composition,
      inputProps,
      codec: 'h264',
      audioCodec: 'aac',
      audioBitrate: '192k',
      pixelFormat: 'yuv420p',
      crf: job.crf,
      x264Preset: job.x264_preset,
      concurrency: job.concurrency ?? null,
      outputLocation: job.output_path,
      overwrite: true,
      // The source has audio; if it somehow did not, keep a silent track so
      // every export has the same streams.
      enforceAudioTrack: true,
      logLevel: 'warn',
      timeoutInMilliseconds: 120000,
      onProgress: ({ progress, renderedFrames, encodedFrames, stitchStage }) => {
        const rounded = Math.floor(progress * 1000)
        if (rounded === lastReported) return
        lastReported = rounded
        emit({
          event: 'progress',
          progress,
          rendered: renderedFrames,
          encoded: encodedFrames,
          stage: stitchStage,
        })
      },
    })
    emit({ event: 'done', output: job.output_path })
  } finally {
    server.close()
  }
}

async function setup(cacheDirectory) {
  await ensureHeadlessBrowser(true)
  if (cacheDirectory) await ensureBundle(cacheDirectory)
  emit({ event: 'done', output: null })
}

async function main() {
  const options = parseArguments(process.argv.slice(2))
  if (options.setup) return setup(options.bundleCache)
  if (!options.job) throw new Error('Pass --job <job.json> or --setup.')
  return render(options.job)
}

main().then(
  () => process.exit(0),
  (error) => {
    const kind = error instanceof BrowserMissing ? 'browser_missing' : 'failed'
    emit({ event: 'error', kind, message: String(error?.message ?? error) })
    process.stderr.write(String(error?.stack ?? error) + '\n')
    process.exit(kind === 'browser_missing' ? 3 : 1)
  },
)
