# Video Factory

A local application for producing talking-head videos through one interface,
instead of driving separate command-line tools by hand.

Everything runs on this machine. There is no cloud storage, no authentication
and no upload of any kind. The one exception is deliberate and opt-in: if you
configure a real AI provider, asking for an editing plan sends text and
metadata — never media — to that provider. See
[AI plans](#ai-plans-mock-mode-and-the-real-provider).

Current state: **milestone 1B** — projects, background jobs, structured AI
editing plans, subtitles, and cutting in two modes: **boundary-only trimming**
(the default: removes the dead time around each take and keeps the pauses
inside it), and the original **full-clip silence removal** with Auto-Editor.
Boundary-only mode has focused previews, manual start/end adjustment,
AI-assisted settings and a source-to-output cut map. See
[docs/PROGRESS.md](docs/PROGRESS.md).

## Requirements

Already verified on this machine:

| Component | Version |
| --- | --- |
| Python | 3.12.6 (root `.venv`) |
| Node.js | 22.14.0 |
| npm | 11.4.2 |
| FFmpeg / FFprobe | 8.1.2 (`C:\tools`) |
| Auto-Editor | 31.3.2 (`C:\tools`) |
| faster-whisper | in `.venv`, from `requirements.txt` |

FFmpeg, FFprobe and Auto-Editor must be reachable through `PATH`.

## Configuration (Windows PowerShell)

Everything has a working default, so this section is optional until you want a
real AI provider or a different workspace location.

Settings are read from the environment. The backend also reads a `.env` file in
the repository root at startup, which is the convenient way to keep them:

```powershell
cd C:\Users\orinesher\Documents\videoFactory
Copy-Item .env.example .env
notepad .env
```

`.env` is git-ignored. A variable already set in the PowerShell session always
wins over the file, so you can override one setting for a single run:

```powershell
$env:VIDEO_FACTORY_LLM_PROVIDER = "mock"
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `VIDEO_FACTORY_WORKSPACE` | `<repo>\workspace` | Where projects, jobs, plans and exports are stored |
| `VIDEO_FACTORY_LLM_PROVIDER` | `mock` | `mock` or `anthropic` |
| `ANTHROPIC_API_KEY` | *(empty)* | Only used when the provider is `anthropic` |
| `VIDEO_FACTORY_LLM_MODEL` | `claude-opus-5` | The model used for plan generation |
| `VIDEO_FACTORY_LLM_TIMEOUT` | `120` | Seconds one provider request may take |

Keys live only in the backend process. They are never written to a project
file, a job record or a plan, never logged, and never sent to the browser — the
interface only ever learns *which* provider is configured and whether it is
ready.

## Running it (Windows PowerShell)

Two terminals, both opened at the repository root. No execution-policy change is
needed: the virtual environment's `python.exe` and `npm.cmd` are called
directly rather than through activation scripts.

**Terminal 1 — backend (FastAPI on port 8000):**

```powershell
cd C:\Users\orinesher\Documents\videoFactory
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --reload --port 8000
```

**Terminal 2 — frontend (Vite dev server on port 5173):**

```powershell
cd C:\Users\orinesher\Documents\videoFactory\frontend
npm.cmd run dev
```

Then open <http://localhost:5173>.

Run **one** backend process at a time. The background job queue lives inside
that process; two backends over one workspace would each run their own worker.

First-time install, if `.venv` or `node_modules` is missing:

```powershell
cd C:\Users\orinesher\Documents\videoFactory
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
cd .\frontend
npm.cmd install
```

The frontend calls `/api/...`; the Vite dev server strips the `/api` prefix and
forwards to `http://127.0.0.1:8000`, so backend routes stay unprefixed
(`/health`, `/tools`, `/projects`, `/capabilities`, `/llm`).

## Background jobs

Anything that can take a while runs in a queue inside the backend rather than
inside an HTTP request, so the interface stays usable. One job runs at a time.

Every job is a JSON file under its project, which means job state and results
survive a backend restart. A job that was still queued or running when the
backend stopped is marked **Interrupted** on the next start — never
silently re-run — and you decide whether to retry it. **Run again** always starts
a *new* job from the original input snapshot; the old record is kept.

Cancellation is reported honestly: a queued job is cancelled immediately, while
a running job is *asked* to stop and stays "running, cancellation requested"
until it actually stops.

Everything slow runs in the queue: the processing-tool check, **cutting**,
**boundary detection**, **cut previews**, **cutting recommendations**,
subtitles and plans. Cancelling a job that is decoding or rendering kills the
tool *and its child processes*, so nothing keeps working in the background
after you press **Cancel**.

## AI plans, mock mode and the real provider

An editing plan is a structured, versioned document: a summary plus actions that
name a backend-registered capability, project resource ids and validated
parameters. A plan can never contain a shell command, a script or any
executable expression — there is no field that could hold one. The model
proposes; the backend validates; **you** approve. Nothing runs without an
explicit approval and an explicit **Run** press.

Three capabilities are registered: `diagnostics.tool_check` (a diagnostic),
`edit.trim_boundaries` (boundary-only trimming, milestone 1B) and
`edit.cut_silence` (full-clip silence removal, milestone 1A). Two capabilities
rather than one with a mode parameter: a plan's capability id says by itself
whether pauses inside a clip will be removed. Captions, zooms, B-roll and audio
are still not registered, so a request for them is answered with an explanation
rather than an invented plan.

**Cutting does not need AI.** The normal flow is the **Cutting** section: set
the numbers, look at the previews, press **Render clips and merged video**. No
prompt, no plan, no approval step, no API key. The AI recommendation inside
that section and the plan panel are additional routes to the same capabilities,
not a gate in front of them.

### Mock mode (the default)

With `VIDEO_FACTORY_LLM_PROVIDER=mock` nothing needs a key and nothing touches
the network: plans come from fixed local rules. The interface labels this
clearly (**Demo mode (no AI)**) on the panel and on every plan it produced. It
is never presented as a model's answer.

### The real provider

```powershell
# in .env, or in the PowerShell session before starting the backend
$env:VIDEO_FACTORY_LLM_PROVIDER = "anthropic"
$env:ANTHROPIC_API_KEY = "sk-ant-..."
$env:VIDEO_FACTORY_LLM_MODEL = "claude-opus-5"   # optional
```

Restart the backend. The plan panel then shows the provider and model instead of
the demo badge.

Anthropic is the only real integration in this milestone, through the official
`anthropic` Python SDK (installed by `requirements.txt`).

**What one plan request sends to Anthropic:**

- the instruction you typed;
- the capability catalog (ids, purposes, parameter rules);
- the project resource catalog: resource ids, **filenames**, media type,
  availability and file sizes;
- the required response schema.

**What it never sends:** video, audio or image data, absolute paths, the
workspace location, the project name, or anything else from this machine. If a
filename itself is sensitive, rename the file before adding it.

A request has a timeout (`VIDEO_FACTORY_LLM_TIMEOUT`) and at most one retry, so
a provider outage fails quickly with a readable message instead of blocking the
queue.

**No live call to the paid API has been made from this repository.** To verify
your own setup — at your own cost — configure the key as above, restart the
backend, open a project and ask for a plan, for example
`check which processing tools are installed`. The job in **Background jobs**
will show either a
plan or a specific provider error.

## Cutting

Open a project and scroll to **Cutting**. Every run cuts all of the project's
footage that is on disk, in the project's order, one clip per source, and then
merges the clips into one video.

### The recording workflow this is built for

A script is recorded as short sections, one successful take per clip, and the
clips are joined. A mistake means recording the take again, not cutting it out.
So the thing to remove is the dead time *around* each take — between pressing
record and the first word, and between the last word and pressing stop. The
pauses *inside* a take are your delivery and should stay.

### Two modes

| Mode | What it removes | Tool | Previews | Manual start/end |
| --- | --- | --- | --- | --- |
| **Boundary-only trimming** (default) | The time before the first sound and after the last sound of each clip. Everything between is kept as **one continuous piece**, internal pauses included. | FFmpeg, for detection and for rendering | Opening, ending, join | Yes |
| **Full-clip silence removal** | Every silence it finds, including pauses inside the clip; the remaining pieces are joined. | Auto-Editor 31.3.2 | None | No |

A project that has never saved cutting settings starts in boundary-only mode.
A project whose cutting settings were saved before modes existed stays in
full-clip mode with its saved values, and earlier runs are shown as the
full-clip runs they were. Nothing old is reinterpreted.

### How boundary-only mode works

Each source clip is handled separately, before anything is joined.

1. **Decode.** FFmpeg decodes the audio (no video) to 16 kHz PCM, which is
   reduced to the loudest sample in every 10 ms, as a share of full scale
   (0–1, the same scale as Auto-Editor's threshold). The channels are measured
   as recorded; nothing is remixed first.
2. **Find activity.** A 10 ms frame at or above the *detection threshold* is
   active. Active frames less than 0.15 s apart form one run (so the closure of
   a "t" does not end a word). A run at least *ignore sounds shorter than* long
   qualifies.
3. **Pick the boundary.** The start of the first qualifying run and the end of
   the last one. Padding is added on each side, clamped to the clip, and the
   result is moved outward to whole video frames.
4. **Render.** That single interval is rendered with
   `ffmpeg -ss START -t LENGTH -i source …` and re-encoded to H.264 + AAC.

**This is loudness, not speech recognition.** A cough, a door or a keyboard is
"sound" exactly as a word is. The minimum-duration rule only rejects sounds
that are *short* — the click of the record button, a bump. The interface says
"sound", never "speech", for that reason.

**Why not Auto-Editor for this.** Checked on 31.3.2: a very large `--smooth`
MINCUT does stop it cutting internal pauses, but it equally stops it cutting a
leading or trailing silence shorter than MINCUT, and `--margin` is one pair of
numbers applied around every section. The two ends cannot be treated
separately from the inside, so detection is done here instead.

**Why it re-encodes.** A stream copy can only begin on a keyframe, which in a
normal recording is up to several seconds from the boundary that was chosen.
Re-encoding is frame-accurate, keeps audio and video in step and produces a
file the browser can play. The cost is encoding time in proportion to what is
kept.

#### Settings

| Setting | Unit | Default | What it does |
| --- | --- | --- | --- |
| Detection threshold | ratio, 0–1 | `0.04` | How loud a sound must be to count. **Lower** catches quieter sound and more background noise. |
| Padding before the start | seconds | `0.15` | Kept before the first detected sound, so a soft first consonant or a breath is not clipped. |
| Padding after the end | seconds | `0.35` | Kept after the last detected sound, so the last word can decay and the join has a moment to breathe. |
| Ignore sounds shorter than | seconds | `0.20` | A sound shorter than this cannot set the start or the end. `0` turns the rule off. |
| Preview length | seconds | `4` | How much of the edited clip each preview shows (1–15). Does not affect the cut. |

These are a separate set from the five full-clip settings. The threshold means
the same thing in both modes; the padding does not (Auto-Editor's margin is
applied around every section it keeps), so no number is shared between modes
and changing one mode's settings never changes the other's results.

#### When no confident boundary is found

A clip is **never dropped and never rendered empty**. Each case is shown on the
clip with a **Needs a look** badge and a sentence saying what happened:

| Situation | What happens |
| --- | --- |
| Sound at the very first or last frame | Nothing is removed on that side; noted as information. |
| No sound crosses the threshold (a silent take) | The whole clip is kept, flagged, with the loudest level measured. |
| No audio track | The whole clip is kept, flagged, and rendered with a silent audio track so it can still be merged. |
| Background level close to the threshold | The boundary is used and flagged: it may be following noise. |
| Sound only just above the threshold | The boundary is used and flagged: quiet words at the ends may have been missed. |
| Padding reaches the start or end of the file | Clamped; noted as information. |
| Clip under a second long | Analysed in one pass; noted. |

In every case you can press **Adjust clips individually** (above the clip list;
the controls are hidden until you do) and set a clip's **Start** and **End** by
hand (in seconds of the source, or with the − / + buttons, 0.1 s a press), one
side or both, or tick **Keep the whole clip**. The button shows how many clips
are adjusted, and an adjusted clip keeps its **Adjusted by hand** badge while
the controls are hidden. **Back to detected** removes the manual values. A
manual value outside the clip, or an end before the start, is refused with the
reason before anything is queued.

#### Analysing only the ends

Decoding starts with a 12-second window at each end of the file. If no
qualifying sound is inside a window it grows (×2, decoding only the new
stretch) until one is found or the two windows meet, at which point the clip
has been decoded exactly once. A window edge is never used as a boundary: a
sound cut off by the edge that is too short *as seen through the window*
triggers the next expansion. The result is identical to decoding the whole
file; the test suite checks that on 150 random clips and against a real
decoder. Clips of 24 seconds or less are decoded in one pass.

Detection results are cached per project, keyed by the file's fingerprint and
the two detection settings, so changing padding or a manual boundary never
decodes again, and a clip that was previewed is cut from the same analysis.

**Measured on this machine** (FFmpeg 8.1.2, libx264 `medium`, CRF 18):

| Clip | Analysis, ends only | Analysis, whole file | Rendering the kept part |
| --- | --- | --- | --- |
| 180 s, 1920×1080, 30 fps, synthetic (352 MB) | 0.22 s (24 s of audio decoded) | 0.45 s (180 s decoded) | 63.1 s for 172.6 s kept |
| 8.2 s, 474×850, real phone take | 0.10 s (one pass: under 24 s) | 0.10 s | 1.8 s for 7.7 s kept |
| 34.7 s, 1080×1920, 60 fps, real, silent | 0.35 s (three decodes, 34.7 s in total) | 0.17 s | 32.6 s for 34.7 s kept |

What that shows, and no more: analysing only the ends halves the analysis of a
long take, and analysis is under one percent of the total either way.
**Encoding the kept video is where the time goes, and analysing less audio
does not reduce it.** For takes this short the windows cover the whole file
and there is no saving at all; for a clip with no sound the expansion costs a
few tenths of a second more than a single pass. No faster renderer was
adopted, because the faster option (stream copy) would give up frame accuracy.

### Previews

Previews show only the places a boundary cut can be wrong — never the middle
of a clip and never a random excerpt. Each is a short render, made by a queued,
cancellable job, without rendering the project:

- **Opening** (the main one): the first seconds of the edited clip from its
  retained start, next to the same region of the **source** with up to three
  seconds before it, so you can hear exactly what is removed.
- **Ending**: the last seconds of the edited clip, next to the source with up
  to three seconds after it.
- **Join**: the end of one edited clip followed by the start of the next, to
  judge the rhythm of the transition.

A clip shorter than the preview length is shown whole, and the preview says so.

Previews use the same boundary resolution, the same seek and the same encoder
settings as the final render, from the same analysis cache; the test suite
asserts that a preview and the final run cut at identical source times. Each
preview records the settings revision, the values and (where there is one) the
approved AI proposal that produced it. When a setting, a manual boundary, the
clip order or the source file changes, the affected previews are marked **Out
of date** with the reason — a manual change to one clip leaves the other
clips' previews current. Staleness is decided by the backend, by comparing
what each preview was cut from with the form as it stands. Old previews are
kept on disk, never overwritten.

### AI-assisted settings (optional)

Under the clips is **AI recommendation**. Describe the pacing you want —
"make the joins slightly tighter without clipping the first or last word" — and
the configured provider proposes a mode, settings, an explanation and its
limitations.

- **The model has not heard your audio.** It is sent your request, the
  selected mode and its current settings, the two cutting capabilities with
  their real parameter ranges, and per-clip *measurements*: duration, where
  sound was first and last detected, loudness statistics, warnings, and which
  previews exist. No media, no file names, no paths. The request itself says
  so, and the prompt forbids claiming otherwise.
- **The backend validates the answer** with the same validators the form uses.
  An unknown or out-of-range parameter fails the job; nothing is saved.
- **It is an ordinary plan.** The proposal is stored as a plan revision (it
  also appears in **Editing plan (AI)**). You can edit its numbers — that
  saves a new revision — and nothing changes until you press **Approve and
  apply to the form**, which records the approval through the existing plan
  approval and copies the settings into the form. It renders nothing.
- **It cannot switch modes quietly.** A proposal for a different mode than the
  one selected is labelled as such and cannot be applied without ticking a
  separate confirmation.
- **It goes stale.** If the settings, a manual boundary or the selected clips
  change after the proposal was made, applying it is refused.
- **One request, one proposal.** A revision happens only when you type
  feedback and ask for one. There is no loop that renders and retries.

With the default `mock` provider the proposal comes from fixed keyword rules
and is labelled **Demo, not AI**. Manual cutting works with no provider
configured at all.

### The cut map

Every run writes `cut-map.json` beside its manifest: for each clip, which
source intervals were kept and where they landed. It is what later captions,
zooms and B-roll will use to move between source time and edited time.

```json
{
  "schema_version": 1,
  "kind": "video_factory.cut_map",
  "run_id": "f8ce09220a0d",
  "mode": "boundary",
  "available": true,
  "settings": { "detection_threshold": 0.04, "leading_padding_seconds": 0.15,
                "trailing_padding_seconds": 0.35, "min_activity_seconds": 0.2 },
  "settings_revision": 3,
  "applied_plan": null,
  "plan": null,
  "timebase": { "unit": "seconds", "intervals": "half-open [start, end)",
                "origin": "the first frame of the file each time refers to" },
  "sequence": { "rendered": true, "output_id": "combined",
                "nominal_duration_seconds": 11.08, "measured_duration_seconds": 11.101,
                "tolerance_seconds": 0.1654, "within_tolerance": true },
  "clips": [
    {
      "output_id": "clip-0001", "order": 1,
      "source_id": "…", "source_filename": "take 1.mp4",
      "source_fingerprint": { "method": "sha256:size+head1m+tail1m", "digest": "sha256:…" },
      "source_duration_seconds": 10.0,
      "available": true, "origin": "boundary_plan",
      "retained": [ { "source_start": 1.84, "source_end": 7.88,
                      "output_start": 0.0, "output_end": 6.04 } ],
      "removed":  [ { "source_start": 0.0,  "source_end": 1.84, "position": "leading" },
                    { "source_start": 7.88, "source_end": 10.0, "position": "trailing" } ],
      "nominal_output_seconds": 6.04, "measured_output_seconds": 6.04,
      "difference_seconds": 0.0, "tolerance_seconds": 0.0827,
      "sequence_index": 0, "sequence_start_seconds": 0.0, "sequence_end_seconds": 6.04
    }
  ]
}
```

- **Times** are seconds from the first frame of the file they refer to;
  **intervals** are half-open `[start, end)`.
- To map a source time `t` inside a retained interval:
  `output = output_start + (t − source_start)`, and
  `sequence = sequence_start_seconds + output`.
- **Boundary-only runs**: the one retained interval is the interval the
  renderer was given.
- **Full-clip runs**: the intervals are read from Auto-Editor's own timeline
  (`--export v3`, run with the same editing flags as the render), so internal
  cuts are real, not reconstructed. If that export fails or has a shape this
  map cannot express (speed changes, several tracks), the clip's map is
  `"available": false` with the reason. Intervals are never invented from
  durations.
- **Tolerance**: a clip's intervals must add up to the rendered file's
  measured duration within one video frame plus two AAC frames (0.083 s at
  25 fps / 48 kHz). Outside it, that clip's map is marked unavailable.
- **Sequence positions** come from the merge itself. A run with no merged
  video reports nominal positions and `"rendered": false`; merging later
  (the subtitles step does it) rewrites the map.
- Runs made before 1B have no map; the interface says so.

### Full-clip silence removal: the five settings

The 1A Auto-Editor cut, unchanged, now selected explicitly.

Defaults are the legacy script's. They are a sensible starting point for these
takes, not universal recommendations — expect to tune them per recording.

| Setting | Unit | Default | Auto-Editor flag | What it does |
| --- | --- | --- | --- | --- |
| Audio threshold | ratio, 0–1 | `0.04` | `--edit audio:threshold=` | How loud counts as speech. `0.04` = 4% of full scale. **Lower keeps more audio; higher cuts more.** |
| Margin before speech | seconds | `0.00` | `--margin` (first value) | Kept before each detected phrase, so the first syllable is not clipped. |
| Margin after speech | seconds | `0.50` | `--margin` (second value) | Kept after each detected phrase. Generous here avoids a clipped feeling at the end of a sentence. |
| Minimum silence to cut | seconds | `0.10` | `--smooth` (MINCUT) | Silence shorter than this is **not** cut. **Lower is more aggressive.** |
| Minimum speech to keep | seconds | `0.60` | `--smooth` (MINCLIP) | A speech segment shorter than this is treated as noise and removed. **Higher is more aggressive.** |

Every value is validated by the backend against the range shown in the
interface. The frontend never supplies an executable name, a command fragment
or an output path — it names project source ids, and the backend builds the
command as an argument list with no shell involved.

### Inputs

MKV, MP4 and MOV are the formats this was built and tested against; anything
else the installed FFmpeg can decode will also work, since the check is a real
FFprobe read rather than an extension test.

Before anything is rendered, every selected file is probed. A file that is
missing, unreadable or has no video stream is rejected with a specific message
and the job is never queued. In **full-clip** mode a file with no audio stream,
or with a silent one, is rejected too: that mode decides where to cut by
listening and has nothing to work with. In **boundary-only** mode such a clip is
kept whole and flagged instead (see above).

### Outputs

Every run gets its own directory. Nothing is ever overwritten or deleted —
unlike the legacy BAT, which began by wiping its trimmed-output folder.

```
workspace\projects\<project-id>\intermediates\cuts\<run-id>\
  manifest.json                    what ran, with what, and what came out
  cut-map.json                     source time to output time, per clip
  clips\0001_<take>_trimmed.mp4    one per source, numbered in run order
  combined\combined.mp4            the joined video, when requested
  concat-list.txt                  the FFmpeg concat list, when used
  logs\                            each tool invocation and its full output
```

Clips are **MP4 (H.264 + AAC)** at the source's own dimensions — nothing is
rescaled. That is what Auto-Editor produces and what a browser can play, so the
same files serve preview, download and the next module.

Joining picks a strategy and records which one it used:

- **Stream copy** when every clip agrees on codec, dimensions, pixel format,
  frame rate, sample rate and channel count. Fast and lossless.
- **Re-encode** otherwise, through FFmpeg's concat *filter* (not the concat
  demuxer, which does not resample and produces a file that drifts):
  `libx264 -preset medium -crf 18 -pix_fmt yuv420p`, `aac -b:a 192k -ar 48000`,
  `+faststart`.

The joined file is probed afterwards and its duration compared with the sum of
its parts. A join that exits zero and produces the wrong length is treated as a
failure, not a result.

### Known limitations

- **Mixed dimensions cannot be joined.** If the selected takes are not all the
  same resolution and orientation, the combined output is refused with a
  message naming each clip's size. FFmpeg would copy them happily and hand back
  a file with a broken picture. Run those takes as **separate clips**, or
  re-record/normalise them to one size first. Per-clip cutting is unaffected.
- **Cutting is audio-driven only.** No transcript, no scene detection.
- **Detection is loudness-based.** Boundary-only mode finds the first and last
  *sound*, not the first and last *word*. Noise at either end of a take that is
  longer than the minimum duration will be kept.
- **Nothing inside a clip is removed in boundary-only mode**, by design: a
  mistake or a long pause in the middle of a take stays.
- **Full-clip mode has no previews and no manual boundaries.** Its timeline is
  only known after Auto-Editor has analysed the whole clip.
- **Manual boundaries are one start and one end per clip.** There is no
  timeline editor.
- **Variable-frame-rate sources** are snapped to the nominal frame rate
  FFprobe reports. Cuts are still rendered accurately; a clip whose rendered
  length drifts beyond the tolerance has its cut map marked unavailable.
- **Previews and analyses are never pruned.** Delete
  `intermediates\cut-samples\` by hand to reclaim space.
- **The live AI provider is still unverified.** No request was made to the paid
  API; the recommendation flow is exercised with the demo rules and a scripted
  provider in the tests.
- **Generated clips are not cutting inputs.** A run always reads the project's
  original sources, so cutting never feeds on its own output by accident.
- **A cancelled run registers nothing**, even for clips that had already
  finished. A failed run does keep its finished clips, labelled as belonging to
  a failed run.
- **Windows path length.** The full path of an output file must stay under 255
  characters. A run whose paths would be too long is refused up front with the
  offending path, because past that limit Auto-Editor exits *zero* and writes
  nothing. Keep `VIDEO_FACTORY_WORKSPACE` short.
- **Progress is per clip.** The percentage is real — it comes from the tool's
  own frame counter — but it is a run-level average, so a long take and a short
  one advance the bar at different speeds.
- **Runs are never pruned.** Delete a run directory by hand to reclaim space.

## Subtitles with Whisper

The in-app replacement for `legacy/Whisper/transcribe_video.bat`, and the step
after cutting. Every cut run now trims the clips **and merges them into one
video**; the subtitles are made for that merged video:

1. **Silence cutting** → **Run cut**. The run appears under **Cut runs** in the
   **Subtitles (Whisper)** section.
2. Adjust the four line rules below if you like, and **Save settings**.
3. Press **Create subtitles** on the run. A run cut before merging was
   automatic has no merged video yet; the same job merges its clips first, in
   the background. Single clips can still be transcribed from **Show details**.
4. The job runs in **Background jobs** with a percentage and can be cancelled.
   When it finishes, the merged video's player shows the subtitles (the CC
   button) and a **Download SRT** link appears next to **Download**.

The language is always Hebrew, and the model is fixed:
[ivrit.ai's large-v3-turbo fine-tuned on Hebrew](https://huggingface.co/ivrit-ai/whisper-large-v3-turbo-ct2)
(`subtitles.MODEL` in `backend/subtitles.py`), which is far more accurate on
Hebrew than the generic Whisper models at about Medium's speed. Everything
runs on this computer, on the CPU, with `faster-whisper` (installed by
`requirements.txt`). The model is downloaded once from Hugging Face (≈ 1.6 GB)
into `%USERPROFILE%\.cache\huggingface`; after that no network is needed.

| Setting | Default | What it does |
| --- | --- | --- |
| Words per line | `5` | The most words on screen at once. |
| Characters per line | `30` | A longer line is split before it. |
| Words before a punctuation split | `2` | A line ends at a comma or full stop only once it has this many words. |
| Split on pause | `0.45` s | A pause at least this long starts a new line. |

These and the transcription recipe (beam size 5, voice-activity filter, word
timestamps) are the legacy script's. Whisper runs in its own process, so
**Cancel** stops it immediately and its memory is released afterwards.

Files go into the run's own folder, next to the clips:

```
…\cuts\<run-id>\combined\combined.mp4   the merged video
…\cuts\<run-id>\subtitles\
  combined.srt               UTF-8 with BOM, which Premiere needs for Hebrew
  combined.transcript.json   every word with its timestamps
  combined.json              which model and settings made the SRT
…\cuts\<run-id>\logs\subtitles-combined.log
```

Merging needs every clip of the run at the same resolution and orientation. If
they differ, the merge is refused with a message naming each clip's size, and
the run's clips can still be transcribed one by one.

Transcribing a clip again replaces its SRT only once the new one is complete;
a failed or cancelled attempt leaves the earlier subtitles in place. Subtitles
are not available through an AI editing plan yet.


## Tests and build

```powershell
cd C:\Users\orinesher\Documents\videoFactory
.\.venv\Scripts\python.exe -m pytest backend\tests -q
cd .\frontend
npm.cmd run build
```

The Python tests use a throwaway workspace and a stubbed provider: they never
touch `workspace\`, your footage, or the network.

`backend\tests\test_boundaries.py` covers milestone 1B: detection on synthetic
envelopes, padding and clamping, window expansion against a full scan, manual
overrides, stale previews and proposals, cut maps, cancellation of analysis,
preview and final rendering, and end-to-end runs on a generated fixture with
leading silence, two audible sections separated by a pause, and trailing
silence. Synthetic fixtures prove the mechanism; they say nothing about how a
voice sounds at the cut — that is the listening checklist below.

The cutting tests do run the real FFmpeg and Auto-Editor, but only on a few
seconds of 320×180 test pattern that they generate themselves and delete
afterwards. **None of your footage is ever rendered by the test suite.** If the
tools are not on `PATH`, those tests skip rather than fail.

## Where files live

| What | Where |
| --- | --- |
| Project metadata | `workspace\projects\<project-id>\project.json` |
| Background jobs | `workspace\projects\<project-id>\jobs\<job-id>.json` |
| Editing plans | `workspace\projects\<project-id>\plans\<plan-id>\rev-000N.json` |
| Plan approvals | `workspace\projects\<project-id>\plans\<plan-id>\rev-000N.approval.json` |
| Generated intermediates | `workspace\projects\<project-id>\intermediates\` |
| Cutting runs | `workspace\projects\<project-id>\intermediates\cuts\<run-id>\` |
| Trimmed clips | `…\cuts\<run-id>\clips\NNNN_<take>_trimmed.mp4` |
| Combined video | `…\cuts\<run-id>\combined\combined.mp4` |
| Run manifest and tool logs | `…\cuts\<run-id>\manifest.json`, `…\logs\` |
| Cut map | `…\cuts\<run-id>\cut-map.json` |
| Cut previews | `…\intermediates\cut-samples\<sample-id>\` (`sample.json`, `edited.mp4`, `source.mp4`) |
| Boundary analysis cache | `…\intermediates\cut-analysis\<key>.json` |
| Subtitles | `…\cuts\<run-id>\subtitles\<clip-id>.srt` |
| Exports | `workspace\projects\<project-id>\exports\` |
| Source footage | **stays where you recorded it** |

`workspace\` is application-managed and ignored by Git. Move it elsewhere (for
example to a fast drive) by setting an environment variable before starting the
backend:

```powershell
$env:VIDEO_FACTORY_WORKSPACE = "D:\VideoFactoryWorkspace"
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --reload --port 8000
```

Keep that path short. Windows still limits a full path to 260 characters, and
the workspace holds several nested directories.

### Source footage is referenced, never copied

A project stores the **absolute path** of every source file. Video Factory never
moves, copies, renames or overwrites your footage — recordings are large and
belong wherever you put them. The consequences are worth knowing:

- Moving or renaming a file after adding it breaks the reference. The project
  still opens; the file is marked `File missing` and everything else keeps working.
- Removing a source in the interface removes only the reference. The file on
  disk is untouched.
- The project file is not portable to another machine unless the same paths
  exist there.

Footage is added a folder at a time. **Upload folder…** asks the *backend* to
open the machine's own folder dialog, because a browser file input never hands a
web page a real absolute path — it hands over file contents, which would mean
copying gigabytes of footage into the workspace for no gain. The dialog returns
a true path, every video file directly inside that folder is added in the order
Explorer shows them (`take2` before `take10`), and anything that is not a video
is skipped and reported. A folder added twice adds only what is new.

This works because the backend and the browser are the same machine. Where the
dialog cannot open at all, a path field appears as a fallback; paths copied with
Explorer's "Copy as path" (wrapped in double quotes) are accepted as-is.

## Manual verification

With both services running, at <http://localhost:5173>:

### Projects (milestone 0A)

1. **Tools** tab → **Check tools**. FFmpeg, FFprobe and Auto-Editor should each
   show Available = Yes with a version and a `C:\tools\...` path.
2. **Projects** tab → type a project name → **Create**. The editor opens on
   the right and the project appears in the list.
3. Press **Upload folder…** and pick a folder holding a few real clips (a path
   with spaces is a good test). The machine's folder dialog opens; choose the
   folder and every video in it is added, ordered by name, with filename, size
   and full path. Non-video files in the folder are reported as ignored.
4. Reorder the clips with ↑ / ↓ and change the project name. An
   `Unsaved changes` badge appears.
5. Press **Save changes**. A green confirmation with the save time appears and
   the badge disappears.
6. Stop both terminals (Ctrl+C) and start them again. Reopen the project: the
   name and the source order are exactly as saved.
7. Rename one of the source files in Explorer, then reload the project. It is
   marked `File missing`, the other sources are unaffected, and the project still
   opens and saves. Rename the file back and reload to clear it.
8. Press **Upload folder…** and dismiss the dialog → nothing changes. Choose the
   same folder again → every clip is reported as already in the project and
   nothing is duplicated. Clear the project name and press save → an error
   explains that the name cannot be empty, and nothing is written.
9. Remove a source with **Remove** and save. The file still exists in Explorer.

### Jobs and plans (milestone 0B)

With a project open, scroll to **Background jobs** and **Editing plan (AI)**:

10. **Run tool check**. The job appears as Queued/Running and finishes as Finished
    with a tool table. While it runs, switching projects and editing the name
    still work.
11. Press it again and immediately press **Cancel**. Either the job is cancelled
    outright (it was still queued) or it shows `Cancellation requested` and then Cancelled.
    The status shown is always what actually happened.
12. Press **Run again** on a finished job: a new job appears with the
    `Re-run` badge, and the original record stays as it was.
13. Restart the backend (Ctrl+C, then the uvicorn command again) and reload the
    page. Finished jobs and their results are still listed. Anything that was
    mid-run is now Interrupted with an explanation.
14. In the AI panel, confirm the badge says **Demo mode (no AI)** when no key
    is configured. Type `check which processing tools are installed` →
    **Ask for a plan**. A
    generation job runs, and the plan appears below with a summary and one
    action.
15. Type instead `trim all the silences and add subtitles`. The job succeeds with a
    plain explanation that no implemented capability covers it — and no plan is
    created.
16. Open the plan, untick a tool, edit the summary, and press
    **Save as a new revision**. Revision 2 appears, needs approval again, and
    **Revision 1** is still openable and unchanged.
17. Press **Approve plan**, then **Run approved plan**. An execution job runs and
    reports the tool table for exactly the tools you left ticked.
18. Add another source file to the project. The plan is immediately marked
    `Out of date`, and both approval and execution are refused with a reason.
    Asking for a new plan produces a current one.

### Cutting (milestone 1B)

These are the checks that need a person. The automated suite proves the
mechanism on test tones; only you can tell whether a cut *sounds* right.

**Walkthrough with two real clips** (two consecutive takes of one script):

19. Create a video, **Upload folder…** with the two takes, and scroll to
    **Cutting**. **Boundary-only trimming** is selected and marked Default, and
    the note under it says pauses inside each clip are kept.
20. Press **Find boundaries**. Within a second or two each clip shows
    `Keeps X s to Y s of the source`, what is removed on each side, and its
    original and kept durations. The job is in **Background jobs** and can be
    cancelled.
21. On clip 1 press **Render preview** next to **Opening**. Play **Source**
    first, then **Edited**.
22. Render **Ending** on clip 1 and **Join with the next clip**.
23. Adjust: change **Padding after the end** (every preview turns **Out of
    date**, with the reason), or press **Adjust clips individually** and then
    **+** / **−** on one clip's **Start**
    (only that clip's previews and its join go out of date). **Render again**
    on what you changed. **Back to detected** undoes a manual value.
24. Optional: type a request under **AI recommendation** and press **Ask for a
    recommendation**. In demo mode the proposal is labelled **Demo, not AI**.
    Edit a number, **Save my edits as a new revision**, then **Approve and
    apply to the form** — the settings change above, nothing is rendered, and
    the previews go out of date because the settings changed.
25. Press **Render clips and merged video**. When the run is **Finished**,
    open **Show details**: each clip shows the source interval it kept, and
    **Cut map** links to `cut-map.json`.
26. Press **Render** again on a long take and **Cancel** it in **Background
    jobs**. It stops within a second or two, the run is Cancelled, and no
    `ffmpeg.exe` is left in Task Manager.
27. Switch to **Full-clip silence removal**. The warning says this mode also
    removes pauses inside each clip, the five Auto-Editor settings appear, and
    the clip list and previews are gone. Switch back: your boundary settings
    are as you left them.
28. Restart both terminals and reopen the video. The mode, the settings, the
    manual boundaries, the previews and the runs are all still there, and your
    original recordings are untouched.

**Listening checklist** (headphones, one real talking-head take at a time):

- **First word.** In the opening preview, is the first consonant whole? Soft
  starts ("h", "s", "f", a vowel) are the ones that get clipped. If it is
  clipped, raise *Padding before the start* or lower the *Detection threshold*.
- **What is left before it.** Is there an audible breath, click or lip noise
  before the first word? If it sounds wrong, lower the padding or nudge
  **Start** later for that clip.
- **What was removed.** In the **Source** player, is anything you wanted —
  a deliberate breath, a quiet first word — in the part marked as removed?
- **Last word.** In the ending preview, does the last syllable decay fully, or
  is it chopped? Raise *Padding after the end* if it is chopped.
- **The tail.** After the last word, is the remaining pause about what you
  would leave when speaking? Does a mouse click or a chair noise survive
  there? If it does, nudge **End** earlier on that clip.
- **The join.** In the join preview, does the second take start on the beat,
  too early (the two sentences collide) or too late (dead air)? The pause at a
  join is this clip's trailing padding plus the next clip's leading padding.
- **Room tone.** Does the background sound jump at the join? That is a
  recording difference between takes; trimming cannot fix it.
- **Internal pauses.** Play the whole rendered clip once: every pause inside
  the take must be exactly as you recorded it.
- **Flagged clips.** Open every clip marked **Needs a look** and read why
  before trusting it.
- **Sync.** Watch the lips on the first and last word of the rendered clip and
  across the join in the merged video.
