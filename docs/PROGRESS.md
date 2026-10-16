# Progress

## Milestone 0A — projects: create, save, reopen

Status: complete.

### What was built

**Backend** (`backend/`, now a Python package):

| File | Role |
| --- | --- |
| `config.py` | Workspace location; `VIDEO_FACTORY_WORKSPACE` overrides the default `<repo>/workspace` |
| `storage.py` | Validation, atomic JSON persistence, derived source state |
| `models.py` | Request bodies |
| `projects.py` | `/projects` router |
| `main.py` | Existing `/health`, `/tools`, `/tools/versions` plus `/workspace`; mounts the router |
| `tests/test_projects.py` | 31 tests over a throwaway workspace |

Endpoints added:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/projects` | List projects in the workspace |
| `POST` | `/projects` | Create a named project |
| `GET` | `/projects/{id}` | Load a project |
| `PUT` | `/projects/{id}` | Save the name and the ordered source list |
| `POST` | `/projects/{id}/sources` | Add a local video by absolute path |
| `GET` | `/workspace` | Report the configured workspace |

**Frontend** (`frontend/src/`): `api.ts`, `types.ts`, `ProjectsScreen.tsx`,
`ToolsPanel.tsx`, and an `App.tsx` with two tabs (פרויקטים / כלים). The page is
`lang="he" dir="rtl"`. Vite now proxies `/api` to `http://127.0.0.1:8000` with
the prefix stripped.

Note on the starting point: the repository description said a Vite `/api` proxy
and a tool-status button already existed. They did not — `App.tsx` was still the
untouched Vite scaffold and `vite.config.ts` had no proxy. Both were built here.
The scaffold demo (counter, Vite/React links) was replaced; the backend
endpoints it would have called were already present and are unchanged.

### Storage and architecture decisions

**Versioned JSON, one file per project.** No database: a project is a small
document, and a readable file is easy to inspect, diff and back up by hand.

```
workspace/
  projects/
    <32-hex-project-id>/
      project.json
      intermediates/
      exports/
```

```json
{
  "schema_version": 1,
  "id": "3cd909a5c1244a44bacbd8bd7fc639c6",
  "name": "סרטון בדיקה",
  "created_at": "2026-09-22T19:06:51+00:00",
  "updated_at": "2026-09-22T19:06:51+00:00",
  "sources": [
    { "id": "...", "path": "C:\\Videos\\take 1.mp4", "added_at": "..." }
  ],
  "settings": {}
}
```

- **`schema_version`** is checked on read. A file from a newer version is
  refused with a clear message rather than silently misread.
- **`settings`** is an empty object on purpose. Per-module schemas are invented
  when the modules exist, not before.
- **Derived fields are never stored.** `filename`, `exists` and `size_bytes` are
  computed per request, so a file that comes back is instantly healthy again.
- **Storage paths come from a generated UUID**, never from the project name, so
  Hebrew names, spaces and `\ / : *` need no sanitising and renaming a project
  never moves a directory.
- **Source footage is referenced by absolute path and never touched.** Adding,
  reordering and removing operate on references only.
- **Atomic saves.** Each write goes to a temp file in the same directory, is
  flushed and `fsync`-ed, then `os.replace`-d over `project.json` — atomic on
  one volume, so a failure leaves the previous save intact. Validation runs
  before anything is written, so a rejected save changes nothing.
- **Explicit save with a draft in the UI.** Name, order and removals live in
  frontend state until **שמור שינויים**. Switching projects while dirty asks for
  confirmation, and `beforeunload` guards a tab close. Adding a file is the one
  exception: it is validated and persisted immediately by the server (the UI
  says so), because validating a path means touching the filesystem anyway.
- **Errors are Hebrew strings in `detail`**, because they are shown to the user
  verbatim. Validation is done in `storage.py` rather than by Pydantic types so
  every rejection is one readable sentence instead of a schema dump.
- **Failure handling.** A missing source never blocks loading — it is flagged
  per source. A malformed `project.json` returns 422 with a specific message,
  and in the listing it is reported per entry so one bad file cannot hide the
  other projects.

### Checks performed

Automated, on this machine:

- `.venv\Scripts\python.exe -m pytest backend\tests -q` → **31 passed**.
  Covers: file layout and schema on disk; id-derived paths; reopen after a
  restart; rename and reorder; removal keeping the file; other projects
  untouched; no temp files left behind; listing counts; name validation
  (empty, blank, non-string, too long, control characters); relative paths,
  non-video extensions, nonexistent files, directories, quoted Windows paths,
  duplicates; unknown project and unknown/duplicate source ids; a rejected save
  leaving the previous file byte-identical; missing sources flagged and still
  saveable; malformed and newer-schema files; `/health` and `/tools` still
  responding.
- `npm.cmd run build` (`tsc -b && vite build`) → **succeeded**, 20 modules.
- `npm.cmd run lint` (oxlint) → one warning: `set-state-in-effect` on the
  mount-time project fetch. The fetch is an async call to the backend, which is
  what effects are for; the rule cannot see through the `await`.

Integrated, against the running services (uvicorn on 8000, Vite on 5173):

- `/health`, `/tools`, `/tools/versions` → FFmpeg, FFprobe and Auto-Editor all
  found at `C:\tools\...`.
- Proxy: `http://localhost:5173/api/health`, `/api/tools` and `/api/projects`
  all reached the backend.
- Full flow with real files created by FFmpeg, including a Hebrew filename and a
  path with a space: create → add three sources → rejected duplicate (409),
  relative path (400), missing file (400), empty name (400) → rename and reorder
  → deleted one source file on disk → reopened with `exists=false` on exactly
  that source → restarted uvicorn → reloaded through the proxy with the name,
  order and missing flag intact. The remaining source files were untouched.
- The smoke-test project was deleted afterwards; `workspace/` is empty.

Still requiring manual checks (no browser was driven here):

- The interface itself: RTL layout, the tab switch, the unsaved-changes badge
  and confirmation dialog, the ↑/↓ and הסר buttons, and the success and failure
  messages. The procedure is at the end of the README.

### Known limitations

- Source paths are typed or pasted. A browser file input cannot supply a real
  absolute path, and no native file picker is wired up yet.
- No `.mp4` content validation: extension, existence and file-ness only. Nothing
  probes the file with FFprobe, so a renamed non-video passes.
- A project is not portable between machines: it stores absolute paths.
- No way to delete a project from the interface — remove its directory under
  `workspace\projects\` by hand.
- Last write wins. Two browser tabs editing one project will overwrite each
  other; there is no concurrency check.
- Reordering is ↑/↓ buttons, not drag and drop.
- Project names are not required to be unique.
- `intermediates\` and `exports\` are created but nothing writes to them yet.

## Milestone 0B — background jobs and structured AI editing plans

Status: complete.

### What was built

**Backend** (`backend/`):

| File | Role |
| --- | --- |
| `tools.py` | The 0A FFmpeg / FFprobe / Auto-Editor checks, extracted so the endpoints and the job share one implementation |
| `jobs.py` | The queue: job records, persistence, one worker thread, cancellation, restart recovery |
| `job_tasks.py` | The three job types and their input validators |
| `capabilities.py` | The capability catalog and the parameter-spec validator |
| `resources.py` | The per-project resource catalog and the input fingerprint |
| `plans.py` | Plan schema, validation, immutable revisions, approval, outdated detection |
| `llm.py` | Provider interface, deterministic mock, Anthropic integration |
| `api_jobs.py`, `api_ai.py` | The new routers |
| `tests/test_jobs.py` | 18 tests: lifecycle, persistence, restart, cancel, retry |
| `tests/test_plans.py` | 32 tests: catalogs, validation, revisions, approval, staleness, provider failures |

Changed, not rewritten: `config.py` (a tiny `.env` reader and the provider
settings), `storage.py` (shared atomic-write and resilient-read helpers, now
used by jobs and plans too), `models.py` (three request bodies), `main.py`
(a lifespan that starts and stops the worker; `/tools` now calls `tools.py`).

**Frontend** (`frontend/src/`): `JobsPanel.tsx` and `PlanPanel.tsx`, both
rendered inside the existing project editor, plus the new types and API calls.
No new dependency.

Endpoints added:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/projects/{id}/jobs` | List this project's jobs, newest first |
| `POST` | `/projects/{id}/jobs` | Submit a job (`tool_check`, `plan_generation`, `plan_execution`) |
| `GET` | `/projects/{id}/jobs/{job}` | Inspect one job |
| `POST` | `/projects/{id}/jobs/{job}/cancel` | Request cancellation |
| `POST` | `/projects/{id}/jobs/{job}/retry` | New job from the same input snapshot |
| `GET` | `/capabilities` | The capability catalog |
| `GET` | `/llm` | Which provider is configured, and whether it is ready |
| `GET` | `/projects/{id}/resources` | The project resource catalog + input fingerprint |
| `GET` | `/projects/{id}/plans` | Plans, each at its latest revision |
| `POST` | `/projects/{id}/plans/generate` | Queue a plan-generation job |
| `GET` | `/projects/{id}/plans/{plan}` | Latest revision, with derived state |
| `GET` | `/projects/{id}/plans/{plan}/revisions/{n}` | One revision |
| `POST` | `/projects/{id}/plans/{plan}/revisions` | Save an edit as a new revision |
| `POST` | `/projects/{id}/plans/{plan}/revisions/{n}/approve` | The explicit human approval |
| `POST` | `/projects/{id}/plans/{plan}/revisions/{n}/execute` | Queue execution of an approved plan |

### Job execution and persistence decisions

**One worker thread in the backend process, no broker.** Redis, Celery or a
separate worker process would be infrastructure the user has to install, start
and keep alive for a single-user local app. A `queue.Queue` plus one daemon
thread costs nothing and is honest about what it is. The trade-off is explicit:
the queue assumes a single backend process, and two `uvicorn` processes over one
workspace would each run their own worker.

**Disk is the source of truth; memory holds only the queue and cancel flags.**
Each job is `workspace/projects/<project>/jobs/<job-id>.json`, written with the
same atomic temp-file + `os.replace` discipline as `project.json`. Listing and
inspecting always read from disk, which is why results survive a restart without
any extra bookkeeping.

```json
{
  "schema_version": 1,
  "id": "...", "project_id": "...", "type": "tool_check",
  "status": "succeeded",
  "input": { "tools": ["ffmpeg", "ffprobe", "auto_editor"] },
  "created_at": "...", "started_at": "...", "finished_at": "...",
  "progress_message": "הסתיימה בהצלחה.", "progress_percent": 100.0,
  "result": { "...": "..." }, "error": null,
  "cancel_requested": false, "retry_of": null
}
```

- **`input` is a snapshot**, not a reference. A retry re-runs exactly what the
  original job was given, whatever the project looks like now — and a retry is
  always a *new* record, so the failed one stays readable.
- **`progress_percent` is null unless it is measurable.** The tool check divides
  by a known number of tools; plan generation has no denominator, so it reports
  a message only. A fake percentage is worse than none.
- **Cancellation reports what happened.** A queued job is cancelled
  immediately. A running job gets `cancel_requested: true` and a
  "waiting to stop" message while its status stays `running`; it becomes
  `cancelled` only when the handler actually returns at its next
  `raise_if_cancelled()`. The state transitions are taken under one lock, so a
  cancel cannot be overwritten by the worker starting the same job.
- **Restart means interrupted, never replay.** On startup every record still
  marked queued or running is rewritten as `interrupted` with an explanation.
  Re-running it automatically would be a silent side effect the user never
  asked for; retrying is one button away.
- **Errors distinguish expected from unexpected.** `JobFailed` carries a
  message written for the user; anything else is caught, recorded, and the
  worker keeps running rather than dying with the job.

**Windows-specific robustness, found by running it rather than by reasoning.**
The job worker writes while the interface polls, and on Windows that collides:
`os.replace` fails with a sharing violation if anything has the target open for
a moment, and a reader can briefly see the file as missing. Reads and the
replace both retry for a few tens of milliseconds (`storage.py`). Timestamps are
microsecond-precision and strictly increasing per process, because the Windows
clock is coarse enough that two jobs submitted together otherwise tie and list
in an arbitrary order. Temp-file names were shortened after a live run hit the
260-character path limit with a deep workspace.

### Plan schema and catalog locations

Plans live beside the project, one directory per plan, one file per revision:

```
workspace/projects/<project-id>/plans/<plan-id>/
  rev-0001.json
  rev-0001.approval.json      # only if that revision was approved
  rev-0002.json
```

```json
{
  "schema_version": 1,
  "plan_id": "...", "project_id": "...", "revision": 2,
  "created_at": "...", "revised_at": "...",
  "origin": "ai",
  "instruction": "בדוק אילו כלי עיבוד מותקנים",
  "provider": { "id": "mock", "label": "...", "model": "...", "is_mock": true },
  "capability_catalog_version": 1,
  "resource_catalog_version": 1,
  "input_fingerprint": "sha256:...",
  "input_snapshot": { "resources": [ ... ], "settings": {} },
  "summary": "...",
  "actions": [
    {
      "id": "a1",
      "capability_id": "diagnostics.tool_check",
      "capability_version": 1,
      "resource_ids": [],
      "parameters": { "tools": ["ffmpeg", "ffprobe"] },
      "note": "..."
    }
  ]
}
```

- **Revisions are immutable.** A revision file is written with an exclusive
  create (`os.link` from a fully written temp file, so it is atomic *and*
  fails if the revision already exists). Editing never rewrites history; it
  appends `rev-000N+1`.
- **Approval is a separate file**, precisely so that approving does not modify
  the revision it approves. A new revision has no approval file, which is what
  makes "editing an approved plan requires approval again" a property of the
  storage layout rather than a rule someone has to remember.
- **`approved`, `outdated`, `is_latest` and `executable` are derived on read,
  never stored.** `outdated` compares the revision's `input_fingerprint` with
  the project's current one, so a plan cannot claim to be current after the
  project changed underneath it.
- **The fingerprint covers what a plan actually depends on**: the ordered
  resource ids, filenames, availability, and `settings`. The project *name* is
  deliberately excluded — renaming a project does not change what a plan would
  do, so it must not invalidate one.
- **A plan cannot carry executable content.** Parameters are validated against
  a small spec language (type, range, choices); an undeclared key is rejected,
  and keys like `command`, `script`, `code` or `path` are rejected with a
  specific message. There is no free-form field for a model to smuggle
  anything into.
- **Validation is structural *and* semantic**, and runs at three points: when a
  proposal is saved, when it is approved, and again when execution starts —
  because the project or the catalog may have changed while the job waited.
- **A refusal is not a failure.** If nothing in the catalog matches the request,
  the generation job *succeeds* with an explanation and creates no plan. The
  alternative — inventing plausible actions — is the specific failure mode this
  design exists to prevent.

Catalogs: `backend/capabilities.py` (capability catalog, `CATALOG_VERSION`) and
`backend/resources.py` (per-project resource catalog, fingerprint). Both are
backend-owned; the model only ever receives them as input.

### Supported capabilities

One, and it is not video editing:

| Capability | Kind | Parameters | Executable |
| --- | --- | --- | --- |
| `diagnostics.tool_check` | `diagnostic` | `tools`: any of `ffmpeg`, `ffprobe`, `auto_editor` | yes |

FFmpeg and Auto-Editor being installed, and the scripts under `legacy/`, do not
make cutting, transcription or zoom rendering capabilities: nothing in this
application drives them yet, so nothing is registered. The catalog carries a
plain-text `not_yet_supported` list (cuts, captions, zooms, B-roll, audio,
export/OBS) purely so the model can explain what is missing — those entries
cannot be referenced by a plan.

### LLM integration

`VIDEO_FACTORY_LLM_PROVIDER` selects `mock` (default) or `anthropic`.

- **Mock** is deterministic rules, no key, no network. Same instruction in, same
  proposal out. Everything it produces is labelled `מצב הדגמה (ללא AI)` — on the
  panel, on the plan list entry and on the open plan — so it can never be read
  as a model's answer.
- **Anthropic** is the one real integration, through the official `anthropic`
  SDK (now in `requirements.txt`), default model `claude-opus-5`, configurable
  with `VIDEO_FACTORY_LLM_MODEL`. Credentials come from `ANTHROPIC_API_KEY` in
  the backend environment (or `.env`, which is git-ignored; `.env.example` ships
  placeholders). The key is never written to a plan, a job record or a log, and
  `/llm` reports only which provider is configured and whether it is ready.
- **Requests are bounded**: a configurable timeout (120s default) and at most
  one SDK retry, so a provider outage fails quickly instead of holding the
  single worker.
- **What is sent** (documented in the README and shown in the interface): the
  instruction, the capability catalog, the resource catalog (ids, filenames,
  media type, availability, sizes) and the response schema. **Not sent:** any
  video, audio or image data, absolute paths, the workspace location or the
  project name.
- Generation always runs through the job queue, so it never blocks a request and
  is cancellable and retryable like any other job.

### Interface

Both panels live inside the existing project editor, in the existing style:

- **משימות רקע** — status badge, progress message, a progress bar where the
  percentage is real, the tool table for tool-check results, errors, and
  cancel / retry buttons where they apply. Polling every 1.5s while something is
  active, every 8s when idle; the two panels nudge each other to refetch
  immediately after a submit or a finish.
- **תוכנית עריכה (AI)** — the provider badge (demo or provider + model) and what
  a real request would send, a plain-language instruction field, the plan list,
  and for the open plan: summary and per-action editing driven by the capability's
  own parameter spec, revision tabs, and the explicit
  save-revision → approve → execute sequence. Unsupported capabilities, outdated
  plans, superseded revisions and unsaved edits each block the relevant button
  and say why.

### Checks performed

Automated, on this machine:

- `.venv\Scripts\python.exe -m pytest backend\tests -q` → **81 passed**
  (31 from 0A, unchanged, plus 18 job tests and 32 plan tests). The suite was
  run repeatedly (>15 full runs) to shake out concurrency flakiness; the three
  races it exposed are fixed and described above, and the suite has been stable
  since.
  - Jobs: submit-and-continue, persistence shape on disk, results surviving a
    restart, unfinished jobs becoming interrupted and *not* replaying, running
    cancellation being cooperative, queued cancellation being immediate and not
    resurrected, cancelling a finished job rejected, handler failure reported
    with its message, retry from the input snapshot, retry of an active job
    rejected, unknown type and invalid input rejected before queueing.
  - Plans: catalog contents, resources exposing no paths, mock generation,
    refusal without a fabricated plan, unknown capability / bad parameter /
    unknown parameter / wrong type / unknown resource / duplicate action id /
    empty plan / forbidden `command`-style keys all rejected with a clear
    message, cross-project access refused, revision 1 unchanged after an edit,
    approval per revision, approval of a superseded revision refused, adding a
    source making a plan outdated (and renaming not), regeneration current
    again, approved execution succeeding, unapproved execution refused,
    provider timeout / missing key / invalid output / non-JSON output all
    failing the job with a readable message and creating no plan.
- `npm.cmd run build` (`tsc -b && vite build`) → **succeeded**, 22 modules.
- `npm.cmd run lint` (oxlint) → three `set-state-in-effect` warnings, all the
  same mount-time fetch pattern as the 0A warning. The rule cannot see through
  the `await`.
- The Anthropic request shape was checked against the installed SDK 1.8.0 by
  introspection (parameter names, `output_config` fields, exception classes) —
  without issuing a request.

Integrated, against a real `uvicorn` process on a throwaway workspace:

- Full flow: create project → queue a tool check → mock plan generation →
  read plan → edit and save revision 2 → approve → execute → tool table
  returned for exactly the two tools left selected.
- Rejections: a `command` parameter (400) and an unknown capability (400).
- Restart: the process was killed and started again. Finished jobs and their
  results were intact; a record doctored to `running` came back as
  `interrupted` with an explanation and no result.
- Outdated: adding a source to the project flipped the approved plan to
  outdated and made execution return 400.
- Retry: retrying the interrupted execution produced a new job that failed with
  the honest reason (the plan is outdated) instead of running anyway; retrying
  the tool check succeeded.
- The smoke workspace was deleted afterwards. The real `workspace\` and its
  existing project were never touched.

Still requiring manual checks (no browser was driven here): the two new panels
themselves — polling, the progress bar, the cancel/retry buttons, the parameter
checkboxes, the revision tabs and the approval/execution flow in the RTL layout.
The procedure is at the end of the README.

### Known limitations

- **Live provider behaviour is unverified.** No request has been made to the
  paid API from this repository — deliberately, since that was not authorised.
  The SDK call shape was verified statically against the installed SDK; what
  remains unproven is the round trip: that the model returns JSON matching the
  schema often enough to be pleasant, and that the error mapping matches real
  failures. The README documents exactly how to test it. The mock path is fully
  exercised.
- **One process only.** The queue lives in the backend process; running two
  backends over one workspace would run two workers.
- **Cancelling a running tool check is theoretical.** It checks three tools in
  well under a second, so it will almost always finish before the request
  arrives. The cooperative mechanism is real and tested (with a deliberately
  slow test job); this particular job is just too fast to interrupt.
- **No structured-output enforcement.** The real provider is asked for JSON in
  the system prompt and the reply is parsed tolerantly, rather than using the
  API's `output_config.format` schema constraint. Backend validation is
  authoritative either way, but a malformed reply fails the job rather than
  being corrected.
- **A plan is not re-runnable as a document.** Execution re-validates and runs;
  there is no record of *which* execution job produced which result beyond the
  job record itself.
- **Jobs are never pruned.** Every job record stays until the project directory
  is deleted by hand.
- **An unreadable job or plan file is skipped in listings** rather than shown as
  broken, unlike the project listing which reports a per-entry error.
- **Progress is coarse.** Only the tool check reports a percentage, and only at
  tool granularity.
- **`.env` parsing is minimal**: `KEY=VALUE`, `#` comments, optional quotes. No
  multi-line values, no interpolation.
- Windows path length still applies: keep `VIDEO_FACTORY_WORKSPACE` short.

## Milestone 1A — manual Auto-Editor cutting through the interface

Status: complete.

### What was built

The first module that actually edits video. It is the working replacement for
`legacy/AutoEditor/RUN_EDIT.bat`, driven from the project screen rather than
from a console window, and it runs from its own button — no prompt, no AI plan
and no approval step.

**Backend** (`backend/`):

| File | Role |
| --- | --- |
| `media.py` | FFprobe inspection: input validation, output verification, file fingerprints, stream-compatibility comparison |
| `processes.py` | Running external tools: argument lists, merged live output, process-**tree** cancellation |
| `cutting.py` | Settings spec and validation, command construction, run directories and manifests, output resolution, generated-resource catalog |
| `cut_runner.py` | The pipeline: trim each source in order, then join |
| `api_cutting.py` | Settings, runs, and Range-capable media serving by id |
| `tests/test_cutting.py` | 73 tests: pure validation and command construction, the job path with the tool layer stubbed, and real end-to-end runs on generated footage |

Changed, not rewritten: `capabilities.py` (registers `edit.cut_silence`,
`CATALOG_VERSION` → 2), `job_tasks.py` (the `cut_media` job type and the plan
executor for the new capability), `resources.py` (a `generated` section,
`RESOURCE_CATALOG_VERSION` → 2, and cutting settings excluded from the plan
fingerprint), `plans.py` (`min_resources` enforcement), `models.py`, `main.py`.

**Frontend** (`frontend/src/`): `CuttingPanel.tsx`, rendered inside the existing
project editor, plus the new types and API calls. Playback is a plain `<video>`
element — no Remotion, as scoped. No new dependency.

Endpoints added:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/projects/{id}/cutting/settings` | Saved form plus the spec needed to render it |
| `PUT` | `/projects/{id}/cutting/settings` | Persist selection, order, the five values, output mode |
| `POST` | `/projects/{id}/cutting/runs` | Queue a cutting job (returns the job, not a result) |
| `GET` | `/projects/{id}/cutting/runs` | Every run of this project, newest first |
| `GET` | `/projects/{id}/cutting/runs/{run}` | One run manifest with derived fields |
| `GET` | `/projects/{id}/cutting/runs/{run}/outputs/{output}/stream` | Playback, with `Range` support |
| `GET` | `/projects/{id}/cutting/runs/{run}/outputs/{output}/download` | The same file as a download |

### Verified Auto-Editor version and CLI behaviour

Checked by running the installed binary, not from memory. **Auto-Editor 31.3.2**
at `C:\tools\auto-editor.exe`; FFmpeg and FFprobe **8.1.2** at `C:\tools`.

The legacy BAT's three editing arguments map like this:

| BAT | Verified meaning on 31.3.2 | Used here as |
| --- | --- | --- |
| `--edit audio:0.04` | Loudness threshold as a 0–1 ratio. `--edit audio:threshold=0.04` produces an identical timeline (7.56s output from the same 12s input, both spellings). | `--edit audio:threshold=X` — the explicit form, because it says what the number means |
| `--margin 0.00s,0.50s` | Two values: kept **before**, then **after**, each detected loud section. Confirmed by measurement: `0s,0s` → 2.04s clips, `0s,0.5s` → 2.52s clips on the same source. | `--margin BEFORE,AFTER` |
| `--smooth 0.10s,0.60s` | `MINCUT,MINCLIP`. MINCUT is the shortest silence that will actually be removed — `--smooth 5s,0.6s` on material with 1.48s gaps produced **no cuts at all**. MINCLIP is the shortest speech segment kept — `--smooth 0.1s,5s` on 2.52s segments discarded everything. | `--smooth MINCUT,MINCLIP` |

Two further behaviours found by running it, both of which shaped the design:

- **`--progress machine`** emits `~done~total~eta` on **stdout**; errors go to
  **stderr**. This is the source of the percentage, which is therefore a real
  ratio of rendered frames rather than an invention.
- **An all-cut timeline is not a crash.** Auto-Editor prints
  `Error! Timeline is empty, nothing to do.` and exits **2**, writing no file.
  A source with no audio stream exits **1** with
  `audio: channel 'all' does not exist in any audio stream`.

### Output and cancellation decisions

**Every run owns a new directory.** The BAT began with
`rmdir /s /q output\trimmed`. Here a run id names a fresh directory under
`intermediates\cuts\`, so a rerun cannot destroy an earlier result and a failed
run leaves its partial output in place to be examined. Nothing is ever deleted
except a file the same run just created and then rejected.

**Run ids are 12 hex characters, not 32.** Found by running it: with a deep
workspace the output path passed Windows' 260-character limit, at which point
**Auto-Editor exits zero and silently writes nothing**. Two changes came out of
that — shorter ids, and a pre-flight path-length check that refuses the run up
front and names the offending path and the environment variable to change. The
failure is now one sentence instead of a tool that appears to succeed.

**A zero exit code is never proof.** Every produced file is read back with
FFprobe: it must exist, be non-empty, carry video and audio, and have a
readable duration. The joined file is additionally checked against the sum of
its parts and rejected if it disagrees.

**Joining picks a strategy and records it.** Stream copy when every clip agrees
on codec, dimensions, pixel format, frame rate, sample rate and channel count;
otherwise a re-encode. The re-encode uses FFmpeg's concat **filter**, not the
concat demuxer — measured: the demuxer on inputs differing in frame and sample
rate produced **17.56s** of material that should have been **14.63s**, because
it does not resample. The filter normalises each input first and produced
14.633s.

**Mixed dimensions are refused, not stretched.** `-f concat -c copy` across a
640×360 and a 360×640 clip **exits 0** and yields a file with a plausible
duration and a broken picture. Rather than attempt it, the clips are compared
first and the combined output is refused with each clip's resolution named, and
the interface says what would have to be normalised. Per-clip cutting is
unaffected, which is the documented way forward.

**A clip with nothing left is named, never dropped.** An empty timeline is
recorded per clip with an explanation and a suggested setting change. The run
continues, and the combined video is marked `complete: false` with the excluded
takes listed, so it can never be presented as "all of your footage, joined". If
*every* clip comes back empty the run fails with that reason.

**Cancellation kills the tree.** Auto-Editor spawns its own encoder, so
terminating only the launched process would leave it rendering in the
background. `taskkill /F /T` takes the whole tree; the partial file is removed,
the remaining clips are recorded as `skipped`, the join never starts, and the
run is `cancelled`. A cancelled or failed run is never reported as a completed
result — `is_complete_result` is derived from the run status, and a cancelled
run contributes no project resources at all. A failed run *does* keep the clips
it had already rendered and verified, each carrying `run_status`, because
discarding finished work over a later failure is its own kind of data loss.

**The job runs its snapshot.** Submitting resolves ids to paths, probes every
file and takes a fingerprint (`sha256` of size plus the first and last mebibyte
— a full hash of a multi-gigabyte take would cost minutes per run for no
practical gain, and the method name is stored so it is never mistaken for a
checksum). Editing the project afterwards cannot change what the run processes,
and the manifest keeps the run identifiable as belonging to that snapshot.

**Ids in, ids out.** The frontend names project source ids and, for playback,
a run id and an output id. It never supplies an executable name, a flag, a
command fragment or a path. Output ids are resolved through the run's own
manifest and the resulting path is then checked to be inside that run's
directory, so even a hand-edited manifest cannot make the server read elsewhere.
There is deliberately no endpoint that accepts a filesystem path.

**Cutting settings do not invalidate plans.** They live in
`settings["cutting"]` and are excluded from the plan input fingerprint: a plan
carries its own parameters, so nudging a threshold in the manual form must not
mark every existing plan outdated.

**Generated clips are not cutting inputs.** Outputs are catalogued separately
from sources and the cutting module selects only from the project's source
manifest, so a run can never silently feed on the previous run's output.

### Checks performed

Automated, on this machine:

- `.venv\Scripts\python.exe -m pytest backend\tests -q` → **154 passed**
  (81 from 0A/0B, plus 73 new). One 0B test was updated rather than deleted:
  it asserted the catalog held only `diagnostics.tool_check`; it now asserts
  that every catalogued capability has a registered executor — the invariant it
  was actually protecting — and that cutting has left the "not yet supported"
  text.
  - Validation: defaults matching the BAT, ranges, non-numeric and unknown
    keys, command-shaped keys, output modes, and the catalog exposing a unit,
    a description and a default for every parameter.
  - Command construction: the exact argument list against the verified flags,
    number formatting, paths with spaces, quotes, ampersands and Hebrew kept as
    single arguments, concat-list escaping, and the filter graph normalising
    every input without scaling.
  - Ordering: submitted order honoured over project order; duplicate, unknown
    and empty selections refused.
  - Pre-flight: no-audio, missing and unreadable files refused **before** the
    job is queued; an over-long output path refused before anything renders.
  - Failure: a non-zero tool exit, a missing executable, an empty timeline per
    clip, every clip empty, mixed dimensions, and a join whose duration
    disagrees with its parts (which falls back to re-encoding and then fails
    honestly).
  - Cancellation: a real FFmpeg render started, cancelled and confirmed stopped
    with the output no longer growing; a real cutting job cancelled mid-render
    and recorded as cancelled with no combined output, no registered resource
    and the originals intact; the stubbed path proving the remaining clips and
    the join never start; and `terminate_tree` proven to be the mechanism.
  - Isolation and traceability: a second run leaving the first byte-identical,
    the manifest carrying run/job ids, ordered sources, fingerprints, settings,
    tool versions, per-clip status and measured durations, a retry producing a
    separate run, and a run continuing on its snapshot after the project's
    sources were removed mid-flight.
  - Serving: playback by id, `Range` returning 206 with the right slice, the
    Hebrew download header, and refusal of `../`-style, unknown and
    failed-clip output ids and of another project's run.
  - Real end-to-end on generated footage (12s and 8s of alternating tone and
    silence, 320×180): silence genuinely removed, clips playable H.264/AAC at
    source dimensions, the join equal to the sum of its parts, a bigger
    trailing margin measurably keeping more material, and the mixed-dimension
    limitation reported — then succeeding in clips-only mode.
- `npm.cmd run build` (`tsc -b && vite build`) → **succeeded**, 23 modules.
- `npm.cmd run lint` (oxlint) → five `set-state-in-effect` warnings, two of them
  new and both the same mount-time fetch pattern already present in the 0A and
  0B panels. The rule cannot see through the `await`.

Integrated, against a real `uvicorn` process on a throwaway workspace:

- Full flow over HTTP: create project → add an MP4 and an MKV with a Hebrew
  name → a no-audio source refused with a readable message → settings saved →
  run submitted in **reverse** of the project order → job succeeded with a real
  percentage → clips shorter than their sources → combined produced by
  `stream_copy` at 14.181s, the sum of 5.64s and 8.52s.
- Serving: `Range: bytes=100-199` → 206 with exactly 100 bytes and a
  `Content-Range`; full stream 200 with `Accept-Ranges`; the Hebrew clip's
  download carrying `filename*=UTF-8''`.
- Catalogs: three generated resources registered, the three sources unchanged,
  no absolute path anywhere in the resource payload, and `edit.cut_silence`
  present in `/capabilities`.
- Restart: the backend was stopped and started again on the same workspace.
  Settings, the run, per-clip statuses and durations, the combined output and
  its strategy were all intact, and a `Range` request still returned 206.
- FFprobe read the combined file back independently: `h264` 320×180 + `aac`,
  14.181s.
- The originals were byte-identical afterwards. The smoke workspace was
  deleted; the real `workspace\` and its existing project were never touched.

**Not verified here, and yours to judge:** whether a cut *sounds* right on a
real talking-head take — clipped syllables, abrupt sentence endings, breaths
left in, speech wrongly removed — and the RTL layout, the players and the
buttons in a browser. No real footage was rendered. The procedure is at the end
of the README, steps 19–28.

### Known limitations

- **Mixed dimensions cannot be joined.** Reported clearly with each clip's
  resolution; no scaling, padding or rotation normalisation exists yet. Cutting
  those takes separately works.
- **Audio-driven only.** No transcript, no scene detection, no per-speaker
  logic.
- **No manual boundary editing and no full cut map.** The manifest records what
  went in, what came out and how long each result is — not the source
  timestamps of each individual cut. That is 1B.
- **Progress is a run-level average.** Real, but a long take and a short one
  move the bar at different speeds.
- **The join reads clip properties from the manifest**, which FFprobe wrote
  moments earlier. A file replaced on disk between cutting and joining would
  not be noticed within a run.
- **Runs are never pruned**, like jobs and plans before them.
- **One backend process only**, unchanged from 0B: the queue lives in it.
- **Windows path length still applies**, now with a clear error instead of a
  silent no-op. Keep `VIDEO_FACTORY_WORKSPACE` short.
- **A run directory is not portable**: the manifest records absolute source
  paths, like the project file.
- **Cutting cannot yet consume generated clips**, by design. Re-cutting an
  output means adding it as a project source by hand.

## Milestone 1B — boundary-only trimming, focused previews, AI-assisted settings

Status: complete.

### The clarification that shaped it

The 1A plan for this milestone was "sample a bit of the take and compare
settings". The actual recording workflow made that the wrong feature. A script
is recorded as short sections, one successful take per clip, and the clips are
joined; a mistake is re-recorded, not cut out. What needs removing is the dead
time at the two ends of each clip. Pauses inside a take are delivery. So the
default became a mode that cuts each clip exactly twice, and the previews
became the three places such a cut can be wrong.

Also found on reading the repository: a subtitles module (Whisper) and folder
upload had been added after 1A and were not described in this file. They are
untouched by this milestone beyond the merge step now refreshing the cut map.

### What was built

**Backend** (`backend/`):

| File | Role |
| --- | --- |
| `boundaries.py` | New. Boundary settings and overrides, the peak envelope, activity detection, end-window analysis with expansion, resolving the retained interval, the analysis cache |
| `cut_map.py` | New. The versioned source-to-output map, for both modes |
| `cut_samples.py` | New. Opening / ending / join preview job, sample storage, staleness |
| `cut_state.py` | New. The cutting screen's live state and the analysis job |
| `cut_advice.py` | New. The recommendation job, proposal validation, approve-and-apply |
| `tests/test_boundaries.py` | New. 94 tests |
| `cutting.py` | Modes, the extended saved configuration and its revision, basis keys, the trim / join-preview / timeline-export commands, run schema 2 |
| `cut_runner.py` | The per-clip step branches by mode; cut map per clip; merge records per-clip segments |
| `capabilities.py` | `edit.trim_boundaries` registered, `CATALOG_VERSION` → 3 |
| `job_tasks.py` | Three job types, and the plan executor for the new capability |
| `llm.py` | `recommend_cut` on both providers; the Anthropic call shape is shared and unchanged |
| `plans.py`, `api_ai.py` | An optional `context` on a revision, carried into edits |
| `api_cutting.py`, `models.py` | The endpoints below |

**Frontend** (`frontend/src/`): `CuttingPanel.tsx` reworked (mode selector,
per-mode settings, clip list, run details), plus `CutClips.tsx` (boundaries,
manual start/end, previews), `CutAdvice.tsx` (the recommendation and its
editable proposal) and `cutFormat.ts`. No new dependency.

Endpoints added, all under `/projects/{id}/cutting`:

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/state` | For the form as it stands: each clip's boundary, which previews are stale, the recommendation, active and recent cutting jobs. Reads caches; renders nothing |
| `POST` | `/analysis` | Queue boundary detection |
| `POST` | `/samples` | Queue preview samples (all, or a named list) |
| `GET` | `/samples/{sample}/{edited\|source}/stream` | Play a preview file by id |
| `POST` | `/recommendations` | Queue one AI recommendation, or a revision with feedback |
| `POST` | `/recommendations/{plan}/revisions/{n}/apply` | Approve and copy into the saved form |
| `GET` | `/runs/{run}/cut-map` | The run's cut map |

`GET`/`PUT /settings` and `POST /runs` now carry the whole form: `mode`,
`settings` (full-clip), `boundary_settings`, `overrides`, `sample_seconds`.

### Decisions

**Detection is done here, not by Auto-Editor.** Checked on 31.3.2. Its model is
"cut every inactive section"; a large `--smooth` MINCUT stops it cutting
internal pauses but equally stops it cutting an end silence shorter than
MINCUT, and `--margin` is one pair applied around every section. There is no
invocation that treats the two ends differently from the inside. Detection is
a small module over FFmpeg-decoded PCM, in pure Python — `array` slices,
no NumPy, no new dependency — and is a pure function of an envelope, which is
what makes it testable without media.

**Rendering is FFmpeg, re-encoded, with `-ss` before `-i`.** Frame-accurate
and in sync. A stream copy was rejected on purpose: it can only start on a
keyframe, which would move the cut by up to seconds.

**Levels are measured on the channels as recorded.** Found by running it: the
first version forced stereo, and FFmpeg attenuates a mono track by 3 dB when
it spreads it over two channels, so a tone at 0.125 measured 0.088 — the
threshold would have meant something different for mono and stereo takes.
There is a test pinning the measured level of FFmpeg's sine source.

**End windows, grown incrementally, never trusted at their edge.** See the
README for the mechanism. Two things came out of measuring rather than
assuming: the first version re-decoded from the start on every expansion
(70.7 s of audio decoded for a 34.7 s silent clip), and let the two windows
overlap. Both are fixed — each stretch is decoded at most once, and a test
asserts it. The equivalence with a full scan is a property test over 150
random clips plus a real-decoder test.

**Two settings sets, never shared.** The threshold has the same meaning in
both modes; padding does not. Rather than reuse `margin_*` with different
semantics, boundary-only mode has its own four keys, its validator refuses the
full-clip names, and the full-clip validator refuses its names. A run records
only the settings of the mode it ran in; the other is `null`.

**Old things stay what they were.** A saved cutting block with no `mode` is
full-clip (that is all 1A had). A manifest or a job snapshot with no `mode` is
full-clip, so retrying an old job runs what it ran. Only a project that never
saved cutting settings gets the new default. `RUN_SCHEMA_VERSION` is 2; v1
manifests read unchanged.

**One path to a boundary.** `cut_runner.resolve_boundary` is called by the
analysis job, every sample and the final render, over one analysis cache keyed
by file fingerprint and the two detection settings. Padding and overrides are
applied afterwards by a pure function, so moving them never decodes again.

**Staleness is a key, computed on the server.** `cutting.basis_key` hashes
exactly what decides a cut: the mode, that mode's settings, and per source its
id, its file digest and its override, in order. A sample stores the key of the
sources it shows; the state endpoint recomputes it for the form in the browser
and compares. Consequences that are tested: a manual change to clip B leaves
clip A's previews current; tuning the unselected mode invalidates nothing;
re-recording a take under the same name invalidates its previews.

**The settings revision.** Saved cutting settings carry a revision that goes
up only when something that moves a cut changes. Previews, recommendations,
runs and cut maps record it (or `null` for an unsaved form), along with the
approved AI proposal the settings came from when there is one. The interface
saves before it queues a preview, a recommendation or a render so that the
number is always there.

**The recommendation is a plan.** Rather than a parallel approval mechanism, a
proposal is stored with `plans.create_revision` as one action on
`edit.trim_boundaries` or `edit.cut_silence`. Editing it is the existing
"save as new revision"; approving it is the existing approval file;
`/apply` adds two checks the generic approval cannot know about — the proposal
must still describe the current configuration, and a mode change must be
confirmed — and then copies the parameters into the saved form. It renders
nothing. The plan is also runnable from the plan panel through the existing
execution job.

**Two capabilities, not a mode parameter.** A plan's capability id then says
whether internal pauses will be removed, and no parameter edit can turn one
into the other.

**Manual overrides are not plan parameters.** The plan spec language has no
per-resource values. A boundary-only plan applies the overrides saved in the
cutting screen, and the run records them; this is stated in the capability's
limitations.

**A clip with no audio is kept, with a silent track.** So every clip of a run
has the same streams and the merge still works. The clip records
`audio_synthesised: true`.

**Cut maps are never reconstructed.** Boundary-only: the interval given to the
renderer. Full-clip: Auto-Editor's `--export v3` timeline, verified on 31.3.2
to carry `start`/`dur`/`offset` in timeline frames and to add up exactly to
the rendered clip (126 frames, 5.04 s, on the fixture). Either is then checked
against the measured duration of the rendered file; outside one video frame
plus two AAC frames the clip's map is marked unavailable rather than offered.

### Measured performance

On this machine, FFmpeg 8.1.2, libx264 `medium` CRF 18. Best of three for the
analysis figures.

| Clip | Analysis, ends only | Analysis, whole file | Render |
| --- | --- | --- | --- |
| 180 s, 1920×1080, 30 fps, synthetic, 352 MB | 0.22 s, 24 s decoded | 0.45 s, 180 s decoded | 63.1 s for 172.6 s kept |
| 8.2 s, 474×850, real | 0.10 s (one pass) | 0.10 s | 1.8 s for 7.7 s kept |
| 8.2 s, 474×850, real | 0.11 s (one pass) | 0.11 s | 1.6 s for 7.8 s kept |
| 34.7 s, 1080×1920, 60 fps, real, no sound | 0.35 s, 34.7 s decoded in three pieces | 0.17 s | 32.6 s for 34.7 s kept |

Rendered durations matched the requested interval within 1–17 ms.

The honest reading: analysing only the ends halves the analysis of a long
take, and the analysis is under one percent of the work either way. Encoding
the kept video is the cost, and it is unchanged. For this user's current takes
(about eight seconds) the windows cover the whole file and save nothing; for a
soundless clip the expansion is slower than one pass by about two tenths of a
second. Three of the user's own clips were read for these measurements and
rendered into a temporary directory that was then deleted; nothing was written
to the workspace or to the footage folders.

### Checks performed

Automated:

- `.venv\Scripts\python.exe -m pytest backend\tests -q` → **289 passed**
  (195 existing, 94 new). Two existing tests were adjusted, not weakened: the
  1A tests' `start_run` helper now names `full_clip` explicitly, since that
  pipeline is no longer the default, and the capability-catalog invariant
  test lists the third capability.
- The new tests, by risk:
  - leading and trailing silence removed while the internal pause remains —
    on a synthetic envelope and on a rendered file, where the pause is
    measured in the output (1.5 s, present);
  - padding applied, clamped to the source and snapped outward to frames;
  - sound at the first and last frame preserved;
  - short, silent, noisy, quiet and no-audio inputs: each kept and flagged;
  - window expansion equal to a full scan (targeted cases, 150 random clips,
    and a real decoder), and no stretch decoded twice;
  - manual overrides validated (eight invalid shapes), bounded by the clip,
    respected in the render, and `keep whole` honoured;
  - previews and the final run cutting at identical source times;
  - cut maps: the rendered duration within tolerance, the concatenated offsets
    adding up to the merged file, and a source time landing where the sound
    actually is in the merged video; Auto-Editor's timeline giving two
    retained sections and an internal removal on the same fixture; seven
    unusable timelines yielding "unavailable", never a guess;
  - stale previews and stale proposals detected, and a stale proposal refused;
  - a mode-changing proposal refused without confirmation;
  - invalid provider answers failing the job and creating no plan;
  - cancellation of analysis, of a preview render and of the final render,
    each leaving nothing half-made; a real decode cancelled;
  - manual cutting succeeding with a misconfigured provider.
- `npm.cmd run build` → succeeded, 26 modules.
- `npm.cmd run lint` → five `set-state-in-effect` warnings, the same count and
  the same mount-time fetch pattern as before this milestone.

In a real browser: a second backend and dev server were started against a
throwaway workspace with three generated takes (one with Hebrew in its name,
one silent), and headless Chrome was driven through the panel — find
boundaries, render an opening preview (both players loaded, the edited frame
showing the source timestamp of the detected start), nudge a start and see the
preview go out of date, render all previews, ask the demo engine for a
recommendation, approve and apply it, render the run, open its details,
switch to full-clip mode. No console errors. Screenshots were inspected. The
user's own running backend, dev server and workspace were not used.

**Not verified, and yours to judge:** how any of this *sounds* on a real voice.
The fixtures are test tones. Whether 0.15 s and 0.35 s are the right paddings
for this user's delivery, whether the first consonant survives, whether the
join has the right rhythm — those need the listening checklist in the README.
The live Anthropic provider has still never been called from this repository.

### Known limitations

- **Loudness, not speech.** A noise longer than the minimum duration at either
  end of a take sets the boundary.
- **One start and one end per clip.** No timeline editor, no internal cuts in
  boundary-only mode.
- **Full-clip mode has no previews and no manual boundaries.**
- **Encoding cost is unchanged** by the analysis optimisation.
- **Variable-frame-rate sources** are snapped to the nominal frame rate; a
  clip that drifts past the tolerance loses its cut map, not its render.
- **Previews and the analysis cache are never pruned.**
- **The join preview is rendered from the two sources in one pass**, with the
  same boundaries and encoder settings as the run, whereas the merged video is
  joined from the rendered clips. The cut points are identical; the encodes
  are not the same bytes.
- **A plan's boundary-only run uses the saved manual overrides**, which can
  change after the plan was approved.
- **The demo recommendation engine matches a handful of English keywords.**
- **The mock plan generator's refusal text still says no editing capability
  exists**, as it has since 1A. Left alone: plan generation is outside this
  milestone.
- **Live provider behaviour is unverified**, as before.

### Running it

```powershell
cd C:\Users\orinesher\Documents\videoFactory
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --reload --port 8000
```

```powershell
cd C:\Users\orinesher\Documents\videoFactory\frontend
npm.cmd run dev
```

Then <http://localhost:5173>. Tests and build:

```powershell
cd C:\Users\orinesher\Documents\videoFactory
.\.venv\Scripts\python.exe -m pytest backend\tests -q
cd .\frontend
npm.cmd run build
```

## Stage 2A — transcription, editable captions, Remotion preview and export

Status: complete. Followed by Stage 2B (below).

### What I found first

- A subtitles module already existed (`subtitles.py`, `subtitle_runner.py`,
  `whisper_worker.py`): one SRT per cut-run output straight from Whisper, no
  editing, no revisions, no staleness, a model fixed in code and a fresh
  Whisper process per job. It is left working and documented as the older
  path; its one-shot worker mode is unchanged.
- The legacy `transcribe.py`'s recipe (beam 5, VAD filter, conditioning on
  previous text, word timestamps) and its grouping rules were the useful
  parts and are reused, with changes listed below.
- On this machine: faster-whisper 1.2.1 / CTranslate2 4.8.2, the ivrit.ai
  turbo and Systran medium models already cached, no NVIDIA GPU
  (i5-1335U, 16 GB), Node 22.14. Remotion was not installed; 4.0.532 was the
  current release and supports React 19.

### What was built

**Backend:**

| File | Role |
| --- | --- |
| `caption_inputs.py` | New. Input keys (`cut.<run>.<output>`, `source.<id>`, `import.<id>`), captions-only imports, resolving to files, FFprobe metadata (display size after rotation, frame-rate decision), fingerprints, composition size |
| `captions.py` | New. Transcription settings and model catalog, grouping, validation/normalisation, split/merge/fix-overlaps, transcript and revision storage, regrouping, staleness, style spec, SRT, composition props |
| `whisper_service.py` | New. One long-lived Whisper process keyed by model/device/precision/threads; reuse, idle shutdown, cancellation by tree kill |
| `caption_jobs.py` | New. Jobs: `caption_transcription`, `caption_model_download`, `caption_renderer_setup`, `caption_export`; renderer readiness; export records, verification, listing |
| `api_captions.py` | New. The router below |
| `whisper_worker.py` | Adds `--serve` (persistent, `local_files_only`) and `--download`; the one-shot mode is unchanged |
| `folders.py` | The native dialog runner is shared; adds a video-file picker |
| `storage.py` | `write_json_exclusive` (moved from `plans.py`, which now calls it) |
| `capabilities.py` | `captions.transcribe` registered, `CATALOG_VERSION` → 4 |
| `job_tasks.py` | Executor for `captions.transcribe` (same path as the button, with reuse) |
| `resources.py` | `settings.captions` excluded from the plan fingerprint |
| `main.py`, `models.py` | Router, request bodies, Whisper service stopped at shutdown |
| `tests/test_captions.py` | New. 60 tests |

**Frontend:** `src/captions/` (the composition: `CaptionedVideo.tsx`,
`timing.ts`, `fonts.ts`, `types.ts`), `src/remotion/` (render entry),
`scripts/render-captions.mjs` (Node render/setup entry), `CaptionsPanel.tsx`,
`CaptionEditor.tsx`, `CaptionStyleForm.tsx`, `CaptionPreview.tsx`,
`captionFormat.ts`, `tests/timing.test.ts` (`npm.cmd test`). New
dependencies: `remotion`, `@remotion/player`, `@remotion/media`,
`@remotion/bundler`, `@remotion/renderer` (4.0.532, exact) and three
`@fontsource-variable` fonts (OFL). `vite.config.ts` reads an optional
`VIDEO_FACTORY_API_PORT` (default 8000).

Endpoints, all under `/projects/{id}/captions`:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/catalog` | Models (and whether downloaded), languages, devices, grouping and style specs, renderer readiness |
| `GET` | `/inputs` | Selectable videos, the selected one, saved style and grouping |
| `POST` | `/imports` | Import a video for captioning (file dialog when no path) |
| `PUT` | `/selection` | Remember the selected video |
| `GET` | `/inputs/{key}` | Metadata, fingerprint, composition, transcripts (with stale flags), exports, caption jobs |
| `GET` | `/inputs/{key}/stream` | The video for the preview, with Range |
| `POST` | `/transcriptions` | Reuse a matching transcript, or queue Whisper (`force` for a new one) |
| `POST` | `/model-downloads` | Queue a model download |
| `POST` | `/renderer-setup` | Queue the headless browser download and bundle build |
| `GET` | `/transcripts/{id}` | Transcript summary and text |
| `GET` | `/transcripts/{id}/revisions/{n}` | One caption revision |
| `POST` | `/transcripts/{id}/validate` | Check a draft; writes nothing |
| `POST` | `/transcripts/{id}/operations` | Split / merge / fix overlaps on a draft; writes nothing |
| `POST` | `/transcripts/{id}/revisions` | Save a draft as a new revision |
| `POST` | `/transcripts/{id}/regroup` | New revision from the transcript with new grouping |
| `GET`/`PUT` | `/style` | The caption style |
| `POST` | `/exports` | Queue an MP4 or SRT export of a revision |
| `GET` | `/exports` | Export history |
| `GET` | `/exports/{id}/file` | Play or download (`?download=true`) |

### Decisions

**Transcripts are tied to bytes, not names.** Every transcript records the
input key and the `media.fingerprint` digest; reuse needs the same digest and
every setting that changes Whisper's output (model, language, device,
precision, beam, VAD, recipe version). A different digest under the same key
makes the transcript stale: listed, read-only, not drawn on the new video,
not exportable. Nothing is ever deleted.

**Three layers, never mixed.** Raw transcript (immutable) → caption revisions
(immutable, numbered, each with its parent) → style (project-level). The tests
assert that a style save writes no transcript and no revision and calls no
Whisper.

**Grouping** keeps the legacy rules (word limit, character limit before
overflow, pause, punctuation after N words, segment boundaries) and changes:
a lone word left by a *length* split is rebalanced (5+1 → 3+3; pause and
punctuation splits are never moved); a word longer than the line gets its own
caption and wraps on screen; a caption shorter than the minimum is held into
the following gap but never into the next caption or past the end, without
changing word times; the Hebrew sof pasuq counts as punctuation.

**Word timing is kept or dropped, never invented.** Same word count → timing
kept with corrected spellings. Different count, unreadable timing, or times
that no longer contain the words → group-level timing with the reason on the
caption. Splitting uses real word boundaries when there are any; otherwise the
player position or a marked estimate.

**Overlaps are refused, not resolved silently.** Saving rejects an overlap with
the caption numbers and times; "Fix overlaps" is an explicit button that trims
ends and never moves starts.

**Edits go through one implementation.** The editor's split, merge and
fix-overlaps call the backend on the unsaved draft, and live validation uses
the save's own validator, so the preview, the save and the export all agree
with the Python tests.

**Model handling.** Serve mode loads with `local_files_only=True`; a missing
model is a specific error naming **Download model**, never a silent download.
One worker process stays loaded (10 min idle, configurable) — measured on an
8.2 s take: 37 s first run (10.2 s of it loading), 17 s reusing the model.
Cancellation kills the worker, so the next job reloads. GPU is offered only if
`ctranslate2.get_cuda_device_count()` > 0, with a message naming the driver,
CUDA 12 and cuDNN 9 otherwise.

**Progress is honest.** Loading has no percentage. Transcription's percentage
is the end of the last recognised segment over the duration, labelled as
stepping; a short take moves in one step. Rendering's percentage is
Remotion's own rendered/encoded frame ratio.

**Remotion integration.**
- One composition (`CaptionedVideo`) for the Player and the render; the
  backend writes the same props shape from the saved data.
- Video via `@remotion/media`'s `<Video>` (Remotion's recommended component;
  it falls back to `<OffthreadVideo>` by itself). Its own audio is the only
  audio.
- Dimensions, fps and frame count come from the snapshot through
  `calculateMetadata`, and the render script refuses a composition that
  resolves to anything else.
- The render script serves exactly one file on 127.0.0.1 with a random port
  and token (with Range and CORS for the headless page); no workspace or
  filesystem path is exposed by the API.
- The bundle is cached by a hash of the composition sources and the
  lockfile.
- A render never downloads the browser (`onBrowserDownload` throws);
  **Prepare renderer** is the explicit step.
- Output goes to `.partial.mp4`, is verified with FFprobe, then renamed.

**Frame rate** — see the README: exact for constant-rate sources, the
nominal standard rate for variable-rate phone clips, otherwise the average
snapped to a standard rate. Frames come from absolute seconds
(`ceil(t·fps − 1e-6)`), never accumulated.

**RTL** — direction from the transcript language, not the first character;
bidi left to the browser; no string reversal; RLM-wrapped SRT lines.

**Plans.** `captions.transcribe` is registered because it runs end to end
(footage only — plan resources are footage ids). Caption export is not a plan
capability: it needs a reviewed revision. No LLM is involved anywhere in
captioning.

### Checks performed

Automated:

- `.venv\Scripts\python.exe -m pytest backend\tests -q` → **349 passed**;
  `test_captions.py` has 60 tests, including two real Remotion
  renders. One existing test was updated, not weakened: the capability-catalog
  invariant test lists the fourth capability.
- `npm.cmd test` → 6 passed (seconds-to-frames, boundaries, no drift over an
  hour at 29.97, sub-frame captions, duration rounding).
- `npm.cmd run build` → succeeded. Vite warns that the bundle is over 500 kB
  (Remotion and its decoder); harmless for a local app.
- `npm.cmd run lint` → the same five pre-existing `set-state-in-effect`
  warnings; none from the new files.

Real media, on this machine:

- Remotion render of a generated 360×640 clip with Hebrew/English/number/₪/
  parenthesis captions and a long caption: frames 15, 50 and 87 show the
  right captions in the right bidi order, the long one wraps inside the safe
  area. Tone onset 1.0217 s in source and export, mean level −30.2 dB in
  both: audio once, in sync. Duration 4.011 s for 100 frames at 25 fps.
- Whisper smoke test on one 8.2 s Hebrew phone take (read only): correct
  Hebrew text, 18 words, 4 captions; model reuse measured as above.
- End to end in headless Chrome against a throwaway backend and workspace
  (the user's servers and `workspace\` untouched): select the take →
  transcribe (real Whisper, 37 s) → repeat request reused → seek, active-row
  highlight → correct a word, add a word (group timing shown), type a long
  mixed-language caption, split, merge, save (revision 2) → box style, Rubik →
  export MP4 and SRT. The export (474×850, 8.213 s vs 8.213 s source, H.264 +
  AAC, one stream each) was compared frame by frame with the preview
  screenshots: same text, wrapping, style and timing. The SRT carried every
  edit. No console errors. A first export attempt was refused up front by the
  Windows path-length guard because the throwaway workspace was nested deep in
  a temp folder — the guard working, not a bug — and succeeded from a short
  path.

**Not verified here:** GPU transcription (no NVIDIA GPU); a model download
from Hugging Face (both offered-by-default models were already cached, and
downloading several gigabytes for a test was out of scope); transcription
accuracy beyond one short take; long (minutes) or 4K inputs; a rotated-flag
phone source (the code reads both rotation metadata forms, untested on a real
file); playback in browsers other than Chrome.

### Known limitations

See the README's captions section. In short: no remapping of captions across
re-cuts; stepwise transcription progress; no word-level timing editor or
waveform; one template; GPU untested; slow 4K renders on a laptop CPU.

## Stage 2B — automatic zoom planning (Gemini) and rendering

Status: complete for the mechanics; editorial quality on real footage still
needs your review (below).

### What I found first

- **No CLAUDE.md, no PRODUCT_PRINCIPLES.md, and no "UX refinement session"**
  anywhere in the repository — not on any branch, stash or worktree. Modules
  are stacked panels in the project editor; there was no automatic module
  pipeline and no "Approve and continue" flow to preserve. I built the
  minimum Stage 2B needs (one-click automatic chain, review, approve, then
  the existing export) and did not invent a cross-module pipeline.
- The legacy presets are named `NORMAL`, `PUNCH_LIGHT`, `PUNCH_MEDIUM`,
  `PUNCH_STRONG` (1.00 / 1.08 / 1.12 / 1.20) — kept exactly. The legacy
  renderer cut each clip into `trim`/`atrim` pieces and concatenated them,
  splitting and resampling the audio at every change; `FACE_CENTER` was a
  fixed centre crop. Nothing in the app had used it.
- The Whisper transcripts and caption revisions from 2A are keyed by input
  key + content fingerprint, which made exact reuse straightforward.

### What was built

**Backend:**

| File | Role |
| --- | --- |
| `gemini.py` | New. Adapter over `google-genai`: one JSON request with a response schema; classified, redacted errors; `call_with_retries` (bounded, visible, cancellable); SDK retries off |
| `zoom_planner.py` | New. Preset catalog, prompt, response schema, the request sent, plan validation and technical normalisation, Gemini and demo planners, one-repair flow, provider status |
| `zooms.py` | New. Settings, inputs, transcript lookup, plan and run storage, crop geometry, filter graph and FFmpeg command, FFprobe verification, lineage, review state and approval |
| `zoom_jobs.py` | New. The `zoom_auto` job: transcript → plan → render, each stage reused when possible |
| `api_zooms.py` | New. The router below |
| `scripts/gemini_smoke_test.py` | New. The opt-in live check (`--yes`) |
| `tests/test_zooms.py` | New. 75 tests |
| `caption_inputs.py` | `zoom.<run-id>` input kind; `timing_identity` (lineage) |
| `api_captions.py`, `caption_jobs.py` | Zoomed videos show/export their parent's captions; transcribing one reuses the parent's transcript; export takes an optional `input_key`, lineage-checked |
| `capabilities.py`, `job_tasks.py` | `zooms.auto` registered (`CATALOG_VERSION` → 5) with an executor using the same job code |
| `config.py` | Zoom provider settings (`GEMINI_API_KEY`, model, timeout) |
| `resources.py` | `settings.zooms` excluded from the plan fingerprint |
| `models.py`, `main.py` | Request bodies; router and job registration |

**Frontend:** `ZoomsPanel.tsx` (new), zoom types and API calls, Captions panel
lists zoomed videos and exports them with the parent's captions, a few CSS
rules. No new dependency.

Endpoints, under `/projects/{id}/zooms`:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/status` | Provider status (key presence only), presets, renderer, inputs, settings |
| `PUT` | `/settings` | Preference and selected video |
| `GET` | `/inputs/{key}` | Transcript availability, active/last job, current run with review state, run history |
| `POST` | `/runs` | Start (idempotent: returns the active job or a reusable result) |
| `GET` | `/runs/{run}` | Run, plan, review state, continuation |
| `GET` | `/runs/{run}/video` | The FFmpeg output (Range; `?download=true`) |
| `GET` | `/plans/{plan}` | The stored plan, including the exact request sent |
| `POST` | `/runs/{run}/approve` | Approve exactly what was reviewed, then start the export |

### Gemini

- **Model:** `gemini-3.5-flash` (configurable: `VIDEO_FACTORY_GEMINI_MODEL`).
  Checked on 2026-10-06 against ai.google.dev: listed stable on the models
  page with structured outputs; free tier offered for text on the pricing
  page. A stable, established Flash was preferred over the newest
  (`gemini-3.8-flash`).
- **SDK:** `google-genai` 2.28.0, verified by introspection:
  `GenerateContentConfig(response_mime_type, response_json_schema,
  system_instruction, max_output_tokens, automatic_function_calling)`,
  `HttpOptions(timeout, retry_options=HttpRetryOptions(attempts=1))`,
  `errors.APIError(code, status, message, details)`.
- **Docs consulted:** models, pricing, structured-output, rate-limits,
  api-errors, troubleshooting and the Gemini API terms (free-tier data use:
  may improve Google's products, human review possible, no sensitive data).
  The structured-output page now shows a newer `interactions` API; the
  `generate_content` path used here is the one the installed SDK documents
  and the live test exercised.
- **Retries:** see README. Three attempts at most, the API's `RetryInfo`
  delay honoured up to 65 s, daily quotas never retried, no fallback.
- **Free tier:** the app cannot verify billing status and does not claim to.
  The README tells you where to check. You confirmed your dedicated project
  is on the free tier and keep the key in a Windows user variable.
- **Live test:** one request, run once with your key passed from the user
  environment into that process only: **SUCCESS** — a structured plan that
  passed validation, 1,681 tokens. Nothing was printed but the outcome.
  After it I disabled the SDK's automatic function calling (we offer no
  tools; the SDK logged a notice about it); that config change is covered by
  the fake-client test and was not re-tested live.

### Decisions

**Durations belong to the model.** Validation rejects what is broken and
records what it snaps; it never reshapes timings. Tests prove a 95 s single
NORMAL interval, a 0.28 s strong punch and uneven intervals all pass
unchanged.

**Renderer: overlay by timestamp, not trim + concat.** Each zoomed preset in
use is a crop+scale branch overlaid on the untouched frame while its
intervals are active (`enable='gte(t,a)*lt(t,b)+…'`, boundaries half a frame
early so float noise cannot move a frame across one). Every frame keeps its
own timestamp, so duration, frame count and VFR timing are the input's; the
audio is copied, not cut into pieces. Measured on fixtures: identical frame
counts, identical decoded-audio MD5.

**One job, three stages, reuse at each.** Plans are keyed by input bytes,
transcript id + revision, provider, model, prompt version, preset catalog and
preference; renders by input bytes, plan id, renderer version and encoder.
A stored plan is re-validated before reuse.

**Lineage instead of re-transcription.** A zoom output records its timing
parent (key + digest). The captions module resolves a zoomed video's
transcripts through it, refuses to transcribe the zoomed file, and refuses an
export whose captions are not the parent's. Broken lineage (either file's
bytes changed) shares nothing.

**Approval is a separate file naming the output digest and the caption
revision + style revision that were shown.** Derived on read: outdated when
any of them, or the input, changes. Never re-renders.

**Continue = existing export.** No later module exists, so approval starts
the Captions MP4 export with `input_key=zoom.<run>` (or, with no captions,
the zoomed file is final). An existing matching export is reused.

**Inputs.** Cut outputs, footage and imports — never a zoomed video, never a
captioned export. Preselected: newest merged cut video, else a cut clip, else
the first available video.

### Checks performed

Automated, on this machine:

- `.venv\Scripts\python.exe -m pytest backend\tests -q` → **424 passed (349 existing + 75 new)**.
  `test_zooms.py` has 75 tests (run twice in a row, stable). One existing
  test updated, not weakened: the catalog-invariant test lists `zooms.auto`.
- `npm.cmd run build` → succeeded (the existing >500 kB chunk warning).
- `npm.cmd test` → 6 passed.
- `npm.cmd run lint` → seven `set-state-in-effect` warnings: the five
  pre-existing ones plus two in `ZoomsPanel.tsx` of the same mount-time
  fetch/reset pattern used by the other panels.
- Secrets: the real key's value was searched for (by the shell, without
  printing it) across `frontend\dist`, `frontend\src`, `backend`, `workspace`,
  the README and docs → not found. Git history: only `.env.example` was ever
  committed; no key-like string in any commit. `.env`/`.env.*` ignored,
  `.env.example` not.

Real media:

- Grid fixture (320×240, 25 fps, 4 s): frames 0, 24, 25, 40, 64, 65, 99
  compared with the source and with the source cropped once / twice — the
  zoom starts exactly at frame 25 (1.00 s), ends after frame 64, is applied
  once. 100/100 frames, 4.0 s, audio MD5 identical.
- Caption composition: a flat-colour fixture zoomed to 120% and exported with
  captions through Remotion, compared with the unzoomed export of the same
  caption revision: PSNR > 40 dB on frames with captions (captions identical
  in size and place), audio identical, one video + one audio stream.
- In headless Chrome against a throwaway backend (demo planner, stand-in
  Whisper, separate workspace; your servers and `workspace\` untouched):
  select the project → **Create zooms** → review (plan table, FFprobe line
  "120 of 120 frames, audio aac (copied once)") → **Approve and continue** →
  captioned export → **Download the captioned, zoomed video**. No console
  errors. A frame from that export at 1.3 s shows the punch-in with the Hebrew
  caption at the same size and position as at 0.5 s (unzoomed).
- Found by running it: with only raw footage (no cut yet) nothing was
  preselected; the default now falls back to the first available video.

**Not verified here, and yours to judge:** how Gemini's plans *feel* on real
Hebrew talking-head footage (rhythm, emphasis, whether 120% is too tight for
your framing), behaviour under real quota exhaustion (classification is
tested with recorded response shapes, not a live 429), and long or 4K inputs.

### Known limitations

See the README's zooms section. In short: centre crop only, instant cuts
only, no face tracking, no manual zoom editor, free-tier data use unsuitable
for confidential scripts, no module pipeline for a "skip" to advance.

### Walkthrough

Cut takes → **Zooms**: the merged video is preselected → **Create zooms**
(transcribes if needed, plans with Gemini, renders, verifies) → watch the
real result with your captions on top, read *Style applied* → **Approve and
continue** → the captioned, zoomed MP4 is exported → **Download**.

## Stage 3A — B-roll asset library and composition foundation

Status: complete for the foundation; automatic B-roll is stage 3B.

### What I found first

- Still **no CLAUDE.md, no PRODUCT_PRINCIPLES.md and no roadmap document**
  in the repository. The scope below follows the stage brief; the roadmap
  is now kept at the end of this file.
- "Factory orchestration" is the chain built in 2B: Zooms → **Approve and
  continue** → the captions export. There is no general module pipeline;
  B-roll was slotted into that chain (see Decisions) rather than inventing
  one.
- The 2A Remotion composition (`CaptionedVideo`) already rendered both the
  preview and the export, the render script served exactly one file, and
  every input had a key + content fingerprint (`caption_inputs`). Zoomed
  videos are FFmpeg outputs with timing lineage to their clean parent.
- The renderer was prepared on this machine and the existing suite passed:
  **424 passed** before any change.

### What was built

**Backend:**

| File | Role |
| --- | --- |
| `broll_library.py` | New. Workspace-wide library: import (path or dialog), measured metadata, fingerprints, thumbnails, derived status (available / missing / changed / unsupported), user details, re-measure, remove, the model-facing catalog entry |
| `broll_templates.py` | New. The two-template registry: settings schemas, defaults, validation, `auto` resolution |
| `broll.py` | New. Plans (immutable revisions per base video + fingerprint), placement validation, add / update / remove, composition props and digest, approve / skip decisions and their staleness, constraints and the stage 3B catalog |
| `broll_jobs.py` | New. The `broll_export` job: immutable snapshot, fingerprints checked before and after, shared Remotion render, FFprobe verification |
| `api_broll.py` | New. `/broll/...` (library, catalog) and `/projects/{id}/broll/...` |
| `caption_jobs.py` | The Remotion process loop extracted into `render_composition`, used by both exports (behaviour unchanged) |
| `api_zooms.py` | Approval hands over to B-roll when the step is enabled; `broll_enabled` in the status |
| `folders.py` | An image-or-video file dialog |
| `resources.py` | `settings.broll` excluded from the plan fingerprint |
| `capabilities.py` | The "not yet supported" text now says B-roll is manual, not a plan capability (catalog version unchanged: text only) |
| `models.py`, `main.py` | Request bodies, routers, job registration |
| `tests/test_broll.py` | New. 33 tests |
| `tests/test_zooms.py` | Its project fixture switches the B-roll step off, because those tests cover approval → export directly (see Decisions). No assertion changed |

**Frontend:** `src/broll/` (`types.ts`, `layout.ts`, `BrollLayer.tsx`) joins
the shared composition; `CaptionedVideo.tsx` draws video → B-roll →
captions; `BrollPanel.tsx`, `BrollLibrary.tsx`, `brollFormat.ts`, API calls,
types and CSS; `ZoomsPanel.tsx` reacts to the hand-over;
`scripts/render-captions.mjs` serves B-roll assets beside the input and
bundles `src/broll`; `tests/broll.test.ts`. No new dependency.

Endpoints:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/broll/library` | Library entries with derived status (no paths) |
| `POST` | `/broll/library/imports` | Import a file (dialog when no path) |
| `PATCH` | `/broll/library/{asset}` | Name, description, tags — nothing measured |
| `POST` | `/broll/library/{asset}/remeasure` | Measure a changed file again |
| `DELETE` | `/broll/library/{asset}` | Remove the entry; the file is untouched |
| `GET` | `/broll/library/{asset}/media`, `/thumbnail` | The file (Range) and its thumbnail, by id |
| `GET` | `/broll/catalog` | Templates, constraints, assets for a planner — no paths, no file names |
| `GET` | `/projects/{id}/broll/status` | Settings, base videos, default, templates, renderer |
| `PUT` | `/projects/{id}/broll/settings` | Enabled flag, selected base video |
| `GET` | `/projects/{id}/broll/catalog?input_key=` | The catalog plus the base video's length and rate |
| `GET` | `/projects/{id}/broll/inputs/{key}` | Plan, issues, composed props + digest, review, continuation, exports |
| `POST` | `/projects/{id}/broll/inputs/{key}/placements` | Add at the playhead |
| `PATCH`/`DELETE` | `/projects/{id}/broll/inputs/{key}/placements/{p}` | Change / remove one placement |
| `PUT` | `/projects/{id}/broll/inputs/{key}/plan` | Save a whole plan (the stage 3B path) |
| `POST` | `/projects/{id}/broll/inputs/{key}/validate` | Check placements; writes nothing |
| `POST` | `/projects/{id}/broll/inputs/{key}/approve`, `/skip` | Decide on exactly the reviewed composition, then continue |
| `GET` | `/projects/{id}/broll/inputs/{key}/video` | The base video (the result of a skip without captions) |
| `GET` | `/projects/{id}/broll/exports`, `/exports/{e}/file` | Export history, play / download |

### Decisions

**The library is workspace-wide and references files.** Assets are meant to
be reused across videos, so the library is one versioned JSON file under
`workspace\library\broll\`, outside any project, written with the same
atomic discipline. Files are referenced by absolute path, never copied —
the footage rule — so a 2 GB screen recording costs nothing to import.
Fingerprints (`caption_inputs.fingerprint`, cached by size + mtime) make
"moved" and "edited" explicit states instead of silent breakage; a changed
file is blocked until **Re-measure** so its new length cannot silently
violate a placement. Duplicate imports (same path or same bytes) return the
existing entry.

**Measured vs. said.** `measured` comes only from FFprobe. `description` and
`tags` only from the user. The model-facing catalog omits paths *and* file
names (a file name is not a description and may be private) and marks
`semantic_description: "none"` with a note that technical metadata says
nothing about content.

**Unsupported means the browser, not FFprobe.** The preview is Chrome and
so is Remotion's renderer; HEVC, ProRes and MPEG-4 Part 2 are listed with a
conversion hint rather than failing mid-render.

**A plan belongs to exact bytes.** Plans are keyed by base input key *and*
fingerprint, so a re-cut or re-recorded video never inherits placements
timed for different content; the old plan is listed as not applied. The
timeline is the base video's own seconds — for a zoomed video identical to
its clean parent's, frame for frame.

**Overlaps are rejected, not layered.** "One at a time" is enforced by the
validator for every source of placements; adding at the playhead clamps the
default length to the next placement so the button itself never creates a
conflict.

**No looping, no freezing.** A video placement needs
`source_start + duration ≤ asset length`; the add action clamps instead of
padding.

**The backend composes; the browser only adds URLs.** `broll.compose`
produces the full props (caption revision and style, placements with
`auto` resolved to a corner, the caption band) and a digest over the props,
base and asset fingerprints, plan revision and caption reference. The
review player and the export render those identical props — the real-render
test asserts the export's snapshot equals what the review screen received.
Approval and skip store that digest, so any change anywhere outdates them,
with a reason.

**Composition order and zooms.** Zooms stay an FFmpeg pre-render of the
talking-head file (2B). B-roll is a layer above it and captions above that,
so neither can be zoomed, and the base video is never paused or restarted:
the end of a full-frame cutaway simply uncovers the speaker at that frame's
framing. B-roll audio is muted by default; the base video's track is the
only sound.

**Where B-roll sits in the factory.** Approving zooms now continues to
B-roll (the zoomed file becomes the base), and B-roll's approve or skip
continues to the export that zooms used to start: the B-roll export, or
the captions export / the file itself on skip. The step can be switched off
per project (`settings.broll.enabled`); then approving zooms exports exactly
as in 2B. The 2B tests run with it off, because they test that direct path;
the hand-over is tested in `test_broll.py`.

**One render path.** The B-roll export uses the captions export's render
script, composition, encoder settings, progress, cancellation and FFprobe
checks (`caption_jobs.render_composition`, extracted from the captions
export without changing it). The script now serves the snapshot's asset
files beside the input — still one random port and token, nothing else
reachable.

**Templates are a small registry, not a framework.** Two ids, a schema per
setting, defaults, and fixed renderers. PiP boxes take the asset's aspect
ratio (capped at 45 % of the frame height), so `contain` fills them
exactly; full-frame defaults to `contain` for readability, `cover` is an
explicit, documented crop.

### Checks performed

Automated, on this machine:

- `.venv\Scripts\python.exe -m pytest backend\tests -q` → **457 passed** (424 existing + 33 new).
  `test_broll.py` adds 33 tests, two of them real Remotion renders.
- `npm.cmd test` → 11 passed (6 existing + 5 B-roll: placement frames,
  back-to-back boundaries, in-point rounding, PiP geometry and the height
  cap).
- `npm.cmd run build` → succeeded (the existing >500 kB chunk warning).
- `npm.cmd run lint` → nine warnings: the seven pre-existing
  `set-state-in-effect` ones and two of the same mount-time fetch pattern
  in `BrollPanel.tsx`.

Real media (generated fixtures; the user's library and `workspace\` were
not touched):

- **Composition render** (`test_a_real_export_composes_…`): a 360×640,
  25 fps, 4 s grid with a tone at 1.0–1.5 s; transcribed by the stand-in
  Whisper into Hebrew captions; zoomed to 120 % from 1.0 s; a red 16:9 PNG
  full-frame at 0.6–1.4 s and a 4:3 H.264 clip (blue 0–0.5 s, then lime,
  with a loud 300 Hz tone) picture-in-picture at 2.0–3.2 s from a 0.5 s
  in-point. FFprobe: one H.264 and one AAC stream, 360×640, 100 frames,
  4.0 s. Frame 14 equals the captions-only export of the zoomed video;
  frames 15 and 34 are the first and last with the image, which is 202–203
  rows tall (letterboxed, *not* zoomed — 243 would mean zoomed); frame 35
  is the zoomed speaker again (45.5 dB from the reference, 9.5 dB from the
  un-zoomed source). Frames 50–79 carry a 144×108 lime box top-right
  (the asset's 4:3, 40 % of the width, lime = the in-point took effect);
  frames 49 and 80 equal the reference there. Captions: identical to the
  reference under the PiP (PSNR > 35), and the same text pixels over the
  full-frame image. Audio: tone RMS within ±1 dB of the source, onset
  within 30 ms, no 300 Hz tone during the PiP (RMS < 0.003). No new
  transcription, transcript or zoom run.
- Frames 20, 35 and 55 of that export were looked at: the red image
  letterboxed with the darkening band and the Hebrew caption over the black
  bar; the zoomed grid with its caption; the lime picture-in-picture
  rounded box in the top-right corner beside the speaker, caption unmoved.
- **Snapshot and cancellation** (`test_an_export_renders_its_snapshot_…`):
  a 30 s 1280×720 base; the placement was deleted right after approval, the
  running export's snapshot still had it; cancelling during rendering
  stopped it, left no MP4, and listed it as cancelled.
- Found by running it: the captions export (2A) already shifts fully
  saturated colours slightly relative to the FFmpeg-rendered input (pure red
  253,0,0 → 254,23,0, PSNR ≈ 24 dB on the test grid); the B-roll export does
  exactly the same, so comparisons are made against the captions export.
  Not a B-roll change; noted for whoever looks at colour next.

**Not verified here:** the interface in a browser (no browser automation
was available in this session — the panel was type-checked and built, and
its backend endpoints are covered by the tests; the walkthrough in the
README is for a person); real footage, long or 4K bases, and real screen
recordings (variable frame rate, odd sizes); EXIF-rotated photos; HEVC
detection on a real phone clip (the codec rule is tested with MPEG-4 Part
2); playback in browsers other than Chrome; the native file dialog (it is
the same Tk code path as the existing video dialog).

### Known limitations

See the README's B-roll section. In short: no automatic B-roll (3B); one
B-roll at a time; manual placement only through the minimal controls;
referenced files are read live during an export (changes are detected and
the result discarded, not prevented); plans are not remapped onto a re-cut
video; the library is per workspace.

### Deferred manual-editing ideas

Kept out on purpose; candidates only if 3B's automatic plans need a human
correction path: dragging placements on a timeline strip; trimming by
dragging edges; nudge buttons (± one frame); a per-asset default template;
stacking two layers (e.g. PiP over a full-frame); simple fades or a
slide-in; Ken Burns motion on stills; a blurred-fill background instead of
black bars; unmuted B-roll with ducking of the speaker.

### Walkthrough

One talking-head video, one image, one video asset: cut the takes → Zooms →
**Approve and continue** → in B-roll, **Asset library → Import image or
video…** twice → pause on a line, choose the image, **Full-frame cutaway**,
**Add at …** → pause later, choose the clip, **Picture-in-picture**, **Add**
→ watch the preview (speech continuous, B-roll muted, captions in place) →
**Approve and continue** → the B-roll export runs → **Download the finished
video**.

## Stage 3B — automatic B-roll selection and timing

Status: complete for the mechanics; editorial quality on real footage needs
your review (below).

### What I found first

- Still no CLAUDE.md or PRODUCT_PRINCIPLES.md. Stage 3A's code was in the
  working tree but **uncommitted** (all `broll*` files untracked); it was
  built on, not rewritten.
- 3A already had the pieces a planner needs: one placement validator,
  immutable plan revisions keyed by base video bytes, a catalog without
  paths or file names, composition props shared by preview and export, and
  approval by composition digest. 2B had the Gemini adapter, bounded retries
  and the job/reuse pattern.
- The library had no notion of *where* something happens inside a video
  asset, so "described source intervals" did not exist yet (added as
  optional user-typed **moments**).
- Baseline before changes: the suite stopped on one failure,
  `test_jobs.py::test_listing_is_newest_first_and_scoped_to_the_project`,
  after 272 passes. That run's wall time was over 15 hours (the machine
  slept); the test passes alone and in the full run below. It is a
  timestamp-ordering test and looked like a sleep/clock artefact, not a
  regression.

### What was built

**Backend:**

| File | Role |
| --- | --- |
| `broll_planner.py` | New. Catalog (described + available only, user words apart from measurements, source rules), shortlist, request, response schema, prompt, validation and renderer-identical normalisation, Gemini and demo planners, one-repair flow, provider status |
| `broll_planner_jobs.py` | New. The `broll_plan` job: immutable snapshot → transcript reuse (Whisper only if none) → cached or new plan → applied as a `planner` revision without overwriting edits; run records, reuse, readiness, `auto_start` |
| `broll_library.py` | Optional `moments` (user-described intervals of a video asset), `is_described`, moments in the catalog |
| `broll.py` | Planner preference setting; revisions carry `planner: {run_id}` (kept through corrections); `list_revisions`, `restore_revision` (`origin: restore`); a reviewed empty plan can be approved |
| `api_broll.py` | Planner state in the input view; `POST …/planner/runs`, `GET /planner/runs/{run}`, `POST …/planner/runs/{run}/apply`, `POST …/plan/restore/{rev}`; approving "no B-roll" continues like a skip |
| `api_zooms.py` | Approving zooms (B-roll enabled) starts B-roll planning |
| `capabilities.py`, `models.py`, `main.py` | Text, request bodies, job registration |
| `tests/test_broll_planner.py` | New. 46 tests |
| `tests/test_broll.py` | Two assertions updated for 3B (the planner is implemented; the zoom hand-over now reports planning). No check weakened |

**Frontend:** `BrollPanel.tsx` reorganised result-first (planning status →
preview → summary → approve; corrections, history and settings collapsed);
`BrollLibrary.tsx` shows *Not described* and edits moments;
`src/broll/moments.ts` (parser) with tests; types, API calls, CSS; the
Zooms hint. No new dependency.

### Decisions

**Described means the user said so.** An asset is offered only if it has a
description, tags or moments; a name alone is not enough (often a retyped
file name). Everything else stays manually usable. No vision tagging, no
upload — the request carries text only.

**Whole-asset videos play from their start.** Without moments nothing says
where an event is inside a video, so the planner may not choose an in-point
(`source_start` must be 0 — the documented default interval `[0, length]`).
With moments, the in-point and the whole placement must fit inside one.

**Hard limits are guards, not a pattern.** 1–15 s per planned placement and
at most 50 % coverage are validation rules (a violation gets the one repair
request); no count, no interval, no rhythm is imposed. Asset audio cannot be
unmuted by the planner; only `fit`, `position` and `size_percent` are
settable.

**Normalisation equals the renderer.** Times are stored as the frame the
composition shows them on (`ceil(t·fps − 1e-6)/fps`, as `timing.ts`),
in-points as `Math.round(source_start·fps)/fps` (as `layout.ts`). Found by
testing: the first version rounded to the *nearest* frame, which disagreed
with the renderer for times after a frame's midpoint (and Python's
banker's rounding broke ties differently from JavaScript). The tolerance is
documented: under one frame for times, half a frame for in-points, half a
frame (+1 ms) past the end clamped.

**Failures stay failures; empty is a result.** A provider error, a quota or
an invalid answer fails the job with **Retry**; a valid `placements: []`
with a summary is a plan revision that can be approved ("no B-roll"), which
continues exactly like a skip.

**Never overwrite.** Plans are revisions. An existing plan is replaced only
on an explicit, confirmed **Make a new plan**; the auto-start after zoom
approval never runs if a plan exists. A plan finished while the user
edited is kept, not applied, with **Apply the new plan**.

**Cache key** = prompt + rules version, plan schema, base bytes, transcript
id + revision, offered catalog (views + asset digests), template version,
provider + model, preference, caption band. Caption font/size/colour are
deliberately out; caption *text* is in (via the revision).

**Same provider settings as zooms.** One key, one model setting, one demo
switch; documented. No new environment variables.

### Checks performed

Automated, on this machine:

- `.venv\Scripts\python.exe -m pytest backend\tests -q` → **503 passed**
  (457 after 3A + 46 new in `test_broll_planner.py`), 11.5 minutes,
  including three real Remotion renders.
- `npm.cmd test` → **13 passed** (11 existing + 2 moments).
- `npm.cmd run build` → succeeded (the existing >500 kB chunk warning).
- `npm.cmd run lint` → the same nine `set-state-in-effect` warnings as
  after 3A; none new.

Real media (`test_a_planned_composition_renders_at_the_planned_times`): a
planner answer (controlled) put a red 16:9 image full-frame at 0.59–1.6 s
and a 4:3 H.264 clip picture-in-picture at 2.0–3.2 s from its described
0.5 s moment, on a 360×640, 25 fps, 6 s base with Hebrew captions from the
stand-in Whisper. The approved export: FFprobe one H.264 + one AAC stream,
360×640, 150 frames, 6.0 s; frame 14 the speaker, frames 15–39 the
letterboxed image (0.59 s → frame 15, stored as 0.6), frame 40 the speaker;
lime PiP top-right on frames 50–79, not 49 or 80; caption text visible over
the image; speech RMS within ±1 dB of the source; the asset's 300 Hz tone
absent; Whisper ran once, the planner was asked once. I looked at frames
14, 20, 39 and 60: speaker with caption "שלום לכולם,"; the red image with
the caption readable on the black bar; the image again with "זה מבחן של";
the PiP in the top-right corner beside the speaker with the caption below.

**Not verified here:** a live Gemini request for B-roll (not authorised in
this session; the request shape is the zoom planner's, which was verified
live in 2B, with a new schema using only the same features — object, array,
string enum, number, required); editorial quality on real Hebrew footage;
the panel in a browser (no browser automation in this session — it was
type-checked and built, and its endpoints are covered by tests); long
transcripts near the size budget; libraries over 40 described assets with
real descriptions.

### Known limitations

See the README's *Known limitations (automatic B-roll)*. In short: quality
unverified on real footage; the planner knows only the user's words; no
vision tagging; whole-asset videos start at 0; limits fixed in code.

### Deferred improvements (grounded in what was seen)

- The shortlist is plain word overlap. Hebrew morphology beyond one prefix
  letter (plural/construct forms) will miss matches in large libraries; a
  stemmer or the model itself ranking descriptions would help — only if
  large libraries turn out to be common.
- Descriptions in a different language from the speech match poorly in the
  shortlist (not in the model). Worth watching once real libraries exist.
- Moments are typed by hand; a "mark moment at playhead" button in the
  asset preview would make them cheaper to write.
- The 1–15 s / 50 % limits may need to move into settings once real plans
  are judged.

### Walkthrough

Describe three assets (one screen recording with a moment) and leave one
undescribed → cut, zoom, **Approve and continue** → B-roll plans by itself →
watch the composed preview and read the summary and each placement's reason
→ (optionally correct one placement; the plan history keeps both) →
**Approve and continue** → download. See README steps 53–59.

## Real-world trial — small fixes

These are small changes made while producing the first real video. The trial log
is [REAL_WORLD_TRIAL.md](REAL_WORLD_TRIAL.md).

- The project editor shows the total footage length under the clips list.
  Checked with `tsc -b`. No new lint warnings.
- Cutting shows each active job with a spinner, progress bar and **Cancel**.
  A preview being rendered says so on its own slot. A cut run creating
  subtitles shows a spinner and **Cancel**. Backend: the cutting state's job
  summaries include `samples` (the preview requests). Checked with `tsc -b`,
  lint and `pytest test_boundaries.py -k "state or sample or cancel"`
  (6 passed).

## Roadmap

| Stage | Scope | Status |
| --- | --- | --- |
| 0A / 0B | Projects; background jobs; structured AI plans | Done |
| 1A / 1B | Cutting (full-clip, boundary-only), previews, AI-assisted settings | Done |
| 2A | Transcription, editable captions, Remotion preview and export | Done |
| 2B | Automatic zooms (Gemini plan, FFmpeg render), review and approval | Done |
| **3A** | **B-roll foundation, not a manual editor:** a local asset library, a versioned and validated placement plan on the edited timeline, two templates (full-frame, picture-in-picture), composition with zooms and captions, review, approval and export. Manual placement is limited to one "add at playhead" action plus remove and small adjustments — enough to produce and review real results. | Done |
| **3B** | **AI-driven B-roll selection and timing:** Gemini chooses described assets, times, in-points and templates from the transcript; validated, saved as a plan revision, composed and reviewed as a result; starts on its own after zoom approval. | Done |
| **4** | **Background music and sound effects** (next): music bed and SFX chosen and mixed under the speaker, reviewed like B-roll. | Next |
| Later | Final export / OBS | Not started |
