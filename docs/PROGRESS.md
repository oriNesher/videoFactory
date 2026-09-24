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

## Next: milestone 1A — manual Auto-Editor processing through the interface

Planned scope, not started:

- Run Auto-Editor on a selected source through the job queue: explicit settings
  in the interface (threshold, margin, output name), streamed progress derived
  from the tool's own output, and cancellation that actually terminates the
  child process.
- Write results into `intermediates\` with a record of which source, which
  settings and which job produced them, so a cut can be reproduced or discarded.
- Register the first *editing* capability in the catalog once it genuinely runs,
  so a plan can reference cutting rather than only diagnostics.
- Handle real failure modes: a missing tool, a corrupt source, a file already
  open elsewhere, and a run that produces nothing.
- Still out of scope: Whisper transcription, zoom rendering, Remotion, B-roll,
  audio and OBS control.
