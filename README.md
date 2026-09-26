# Video Factory

A local application for producing talking-head videos through one interface,
instead of driving separate command-line tools by hand.

Everything runs on this machine. There is no cloud storage, no authentication
and no upload of any kind. The one exception is deliberate and opt-in: if you
configure a real AI provider, asking for an editing plan sends text and
metadata — never media — to that provider. See
[AI plans](#ai-plans-mock-mode-and-the-real-provider).

Current state: **milestone 1A** — projects, background jobs, structured AI
editing plans, and the first real editing module: **cutting silences with
Auto-Editor**, configured and run from the interface. See
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

Two kinds of work run in the queue today: the processing-tool check, and
**cutting** (see below). Cancelling a cutting job kills the encoder *and its
child processes*, so nothing keeps rendering in the background after you press
**Cancel**.

## AI plans, mock mode and the real provider

An editing plan is a structured, versioned document: a summary plus actions that
name a backend-registered capability, project resource ids and validated
parameters. A plan can never contain a shell command, a script or any
executable expression — there is no field that could hold one. The model
proposes; the backend validates; **you** approve. Nothing runs without an
explicit approval and an explicit **Run** press.

Two capabilities are registered: `diagnostics.tool_check` (a diagnostic) and
`edit.cut_silence` (real video editing, added in milestone 1A). Captions,
zooms, B-roll and audio are still not registered, so a request for them is
answered with an explanation rather than an invented plan.

**Cutting does not need the AI panel.** The normal flow is the **Silence cutting**
section: pick takes, set the numbers, press **Run cut**. No prompt, no plan,
no approval step. The planning layer is an additional route to the same
capability, not a gate in front of it.

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

## Cutting silences (milestone 1A)

The working replacement for `legacy/AutoEditor/RUN_EDIT.bat`. Open a project,
scroll to **Silence cutting**:

1. **Tick the takes** you want to cut. A source whose file is missing is shown
   but cannot be ticked.
2. **Set their order** with ↑ / ↓. This is the processing *and* joining order,
   and it is independent of the order of the sources in the project.
3. **Adjust the five settings** (below), or press **Restore defaults**.
4. **Choose what to produce**: separate trimmed clips, one combined video, or
   both.
5. **Run cut**. The run is queued; progress, cancellation and retry are in
   **Background jobs**. When it finishes, the run appears in **Previous runs** with a
   player and a download link per output.

**Save settings** persists the selection, the order, the five values and the
output mode with the project, so they are there after a restart. Pressing
**Run cut** snapshots whatever is currently in the form — a run always
processes what was submitted, even if you edit the project while it works.

### The five settings

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
missing, unreadable, has no video stream, or has **no audio stream** is
rejected with a specific message and the job is never queued. The no-audio case
is not a bug: this mode decides where to cut by listening, so it has nothing to
work with.

### Outputs

Every run gets its own directory. Nothing is ever overwritten or deleted —
unlike the legacy BAT, which began by wiping its trimmed-output folder.

```
workspace\projects\<project-id>\intermediates\cuts\<run-id>\
  manifest.json                    what ran, with what, and what came out
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
- **No manual boundary editing** and no full source-to-output cut map — that is
  milestone 1B.
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


## Tests and build

```powershell
cd C:\Users\orinesher\Documents\videoFactory
.\.venv\Scripts\python.exe -m pytest backend\tests -q
cd .\frontend
npm.cmd run build
```

The Python tests use a throwaway workspace and a stubbed provider: they never
touch `workspace\`, your footage, or the network.

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

### Cutting (milestone 1A)

These are the checks that need a person: the automated suite proves the
mechanism, but only you can tell whether a cut *sounds* right.

19. In **Silence cutting**, tick one real talking-head take, leave the defaults,
    choose **Both separate clips and a combined video**, and press **Run cut**.
    The job appears in **Background jobs** with a moving percentage naming the clip it
    is on, and the interface stays usable while it works.
20. When it finishes, play the trimmed clip in place. **Listen for the things
    only you can judge:** words clipped at the start of a phrase (raise
    *Margin before speech*), sentences that end too abruptly (raise
    *Margin after speech*), breaths or filler left in (raise *Audio threshold*),
    and speech wrongly removed (lower *Audio threshold* or
    *Minimum speech to keep*).
21. Drag the player's scrubber to the middle of the clip. It should seek
    instantly. Press **Download** and confirm the file opens in your usual player.
22. Compare the original and output durations shown next to the clip against
    your own sense of how much dead air the take had.
23. Tick a second take, set the order with ↑ / ↓, and run again. The combined
    video must join them in **that** order, not the project's order.
24. Press **Run cut** on a long take and then **Cancel** in **Background jobs** while
    it is running. It must stop within a second or two; check Task Manager to
    confirm no `ffmpeg.exe` or `auto-editor.exe` is still burning CPU. The run
    is listed as Cancelled and offers no combined video.
25. Press **Run again** on the cancelled job. A *new* run appears; the cancelled
    one is still listed, unchanged.
26. Change a setting, press **Save settings**, restart both terminals, and reopen
    the project. The selection, the order, the five values, the output mode and
    every previous run with its players are all still there.
27. Rename a source file in Explorer and try to run. The error names the file
    and nothing is queued. Rename it back.
28. Confirm your original recordings are untouched: same names, same sizes,
    same folder.
