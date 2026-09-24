# Video Factory

A local application for producing talking-head videos through one interface,
instead of driving separate command-line tools by hand.

Everything runs on this machine. There is no cloud storage, no authentication
and no upload of any kind. The one exception is deliberate and opt-in: if you
configure a real AI provider, asking for an editing plan sends text and
metadata — never media — to that provider. See
[AI plans](#ai-plans-mock-mode-and-the-real-provider).

Current state: **milestone 0B** — projects, background jobs, and structured
editing plans proposed by AI and approved by you. See
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
backend stopped is marked **נקטעה** (interrupted) on the next start — never
silently re-run — and you decide whether to retry it. **הרץ שוב** always starts
a *new* job from the original input snapshot; the old record is kept.

Cancellation is reported honestly: a queued job is cancelled immediately, while
a running job is *asked* to stop and stays "running, cancellation requested"
until it actually stops.

The only work this milestone can run is the processing-tool check — the same
FFmpeg / FFprobe / Auto-Editor checks as the **כלים** tab, but queued, with
progress, results, cancellation and retry.

## AI plans, mock mode and the real provider

An editing plan is a structured, versioned document: a summary plus actions that
name a backend-registered capability, project resource ids and validated
parameters. A plan can never contain a shell command, a script or any
executable expression — there is no field that could hold one. The model
proposes; the backend validates; **you** approve. Nothing runs without an
explicit approval and an explicit **הרץ** press.

Right now exactly one capability is registered — `diagnostics.tool_check`,
classified as a **diagnostic** operation, not video editing. Cuts, captions,
zooms, B-roll and audio are not registered, so a request for them is answered
with an explanation rather than an invented plan.

### Mock mode (the default)

With `VIDEO_FACTORY_LLM_PROVIDER=mock` nothing needs a key and nothing touches
the network: plans come from fixed local rules. The interface labels this
clearly (**מצב הדגמה (ללא AI)**) on the panel and on every plan it produced. It
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
`בדוק אילו כלי עיבוד מותקנים`. The job in **משימות רקע** will show either a
plan or a specific provider error.

## Tests and build

```powershell
cd C:\Users\orinesher\Documents\videoFactory
.\.venv\Scripts\python.exe -m pytest backend\tests -q
cd .\frontend
npm.cmd run build
```

The Python tests use a throwaway workspace and a stubbed provider: they never
touch `workspace\`, your footage, or the network.

## Where files live

| What | Where |
| --- | --- |
| Project metadata | `workspace\projects\<project-id>\project.json` |
| Background jobs | `workspace\projects\<project-id>\jobs\<job-id>.json` |
| Editing plans | `workspace\projects\<project-id>\plans\<plan-id>\rev-000N.json` |
| Plan approvals | `workspace\projects\<project-id>\plans\<plan-id>\rev-000N.approval.json` |
| Generated intermediates | `workspace\projects\<project-id>\intermediates\` |
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
  still opens; the file is marked `קובץ חסר` and everything else keeps working.
- Removing a source in the interface removes only the reference. The file on
  disk is untouched.
- The project file is not portable to another machine unless the same paths
  exist there.

Paths are typed or pasted as text. A browser file input cannot hand a web page a
real absolute path, so it would be a false convenience here. Paths copied with
Explorer's "Copy as path" (wrapped in double quotes) are accepted as-is.

## Manual verification

With both services running, at <http://localhost:5173>:

### Projects (milestone 0A)

1. **Tools** tab → **בדוק כלים**. FFmpeg, FFprobe and Auto-Editor should each
   show זמין = כן with a version and a `C:\tools\...` path.
2. **פרויקטים** tab → type a Hebrew project name → **צור**. The editor opens on
   the right and the project appears in the list.
3. Paste the full path of a real video (a path with spaces is a good test) →
   **הוסף קובץ**. The filename, size and full path appear.
4. Add a second and a third file. Reorder them with ↑ / ↓ and change the project
   name. A `שינויים לא שמורים` badge appears.
5. Press **שמור שינויים**. A green confirmation with the save time appears and
   the badge disappears.
6. Stop both terminals (Ctrl+C) and start them again. Reopen the project: the
   name and the source order are exactly as saved.
7. Rename one of the source files in Explorer, then reload the project. It is
   marked `קובץ חסר`, the other sources are unaffected, and the project still
   opens and saves. Rename the file back and reload to clear it.
8. Paste a nonsense path such as `not\a\path.mp4` → a Hebrew error explains that
   a full path is required. Clear the project name and press save → an error
   explains that the name cannot be empty, and nothing is written.
9. Remove a source with **הסר** and save. The file still exists in Explorer.

### Jobs and plans (milestone 0B)

With a project open, scroll to **משימות רקע** and **תוכנית עריכה (AI)**:

10. **הרץ בדיקת כלים**. The job appears as בתור/פועלת and finishes as הסתיימה
    with a tool table. While it runs, switching projects and editing the name
    still work.
11. Press it again and immediately press **בטל**. Either the job is cancelled
    outright (it was still queued) or it shows `התבקש ביטול` and then בוטלה.
    The status shown is always what actually happened.
12. Press **הרץ שוב** on a finished job: a new job appears with the
    `הרצה חוזרת` badge, and the original record stays as it was.
13. Restart the backend (Ctrl+C, then the uvicorn command again) and reload the
    page. Finished jobs and their results are still listed. Anything that was
    mid-run is now נקטעה with an explanation.
14. In the AI panel, confirm the badge says **מצב הדגמה (ללא AI)** when no key
    is configured. Type `בדוק אילו כלי עיבוד מותקנים` → **בקש תוכנית**. A
    generation job runs, and the plan appears below with a summary and one
    action.
15. Type instead `תחתוך את כל השתיקות ותוסיף כתוביות`. The job succeeds with a
    plain explanation that no implemented capability covers it — and no plan is
    created.
16. Open the plan, untick a tool, edit the summary, and press
    **שמור כגרסה חדשה**. Revision 2 appears, needs approval again, and
    **גרסה 1** is still openable and unchanged.
17. Press **אשר תוכנית**, then **הרץ תוכנית מאושרת**. An execution job runs and
    reports the tool table for exactly the tools you left ticked.
18. Add another source file to the project. The plan is immediately marked
    `לא מעודכנת`, and both approval and execution are refused with a reason.
    Asking for a new plan produces a current one.
