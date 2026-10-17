# Real-world trial — log and backlog

The first end-to-end production of a real video with Video Factory. This log
holds what is needed to resume: the current stage, approved results, small fixes,
decisions and the deferred backlog. No transcripts, keys or media go in here.

## Resume here

- **Project:** "1000 shekels budget" (`94911a0d…`), 8 clips, boundary-only
  cutting.
- **Current stage:** 2 — cutting. The clips and merged video were rendered
  (13:13–13:16) and previews were rendered. The result is **not yet reviewed
  or accepted**. The old per-run subtitles were also created by accident
  (see T-01); they are harmless and not used.
- **Next action:** the user reviews the cut (clip order, every opening, ending
  and join) and confirms it, or names the clips to fix. Then go to
  **Captions**.
- **The editor is now the T-03 stage carousel.** In it: accept **Footage**
  (8 clips), then accept **Cutting**. The candidate there is cut run `837d1a`'s
  merged video, which the preview already shows. Then Captions opens by
  itself.

## Setup notes (2026-10-15)

- `CLAUDE.md` and `PRODUCT_PRINCIPLES.md` do not exist in the repository. The
  principles used for this trial are the ones in the trial brief: automatic
  processing with sensible defaults, review of each result, automatic
  continuation after approval, secondary correction controls, and new manual
  tools only when a real problem justifies them.
- **The backend that was already running could not see `GEMINI_API_KEY`**,
  because it was started before the user variable was set, so zoom and B-roll
  planning would have failed. No job was running. It was restarted from a
  shell that has the key. `/zooms/status` then reported Gemini as ready.
  - The backend now runs as a background process of the Claude session. If
    that session ends, restart it from a **new** terminal:
    `.\.venv\Scripts\python.exe -m uvicorn backend.main:app --reload --port 8000`.
  - 2026-10-16: both servers had stopped overnight (the log shows no error).
    Both were restarted from the Claude session (backend and `npm.cmd run dev`).
  - 2026-10-16, later: `--reload` had stopped noticing file changes, so the
    new `/pipeline` route returned 404. Stopping the reloader left its
    multiprocessing **server worker** orphaned and still holding port 8000.
    It had to be stopped too (its parent PID is the old reloader's). Then the
    backend was started again from the session with the Gemini key visible.
    No job was running at the time.
- The frontend (Vite, port 5173) was already running and was left alone. A
  second, stale Vite process also exists; it is harmless.
- The LLM plan provider is `mock` (demo mode). The trial does not need it,
  because cutting, captions, zooms and B-roll do not use it.
- **Uncommitted work exists at the start of the trial:** per-video asset
  libraries and the library page (14 files, about 780 lines changed after
  commit `6d8952c`). It is documented in the README but not yet in
  `docs/PROGRESS.md`. It was left as is.

## Stages available for this video

| # | Stage | Status |
| --- | --- | --- |
| 1 | Project and footage (folder import, ordered by name) | Implemented |
| 2 | Cutting: boundary-only trimming (default) or full silence removal | Implemented |
| 3 | Captions: local Whisper transcription, editing, style | Implemented |
| 4 | Zooms: Gemini plan and FFmpeg render | Implemented |
| 5 | B-roll: library, automatic plan, composed export | Implemented, but needs described assets |
| — | Music and sound effects (stage 4) | **Not implemented** |
| — | Final export / OBS | **Not implemented**. The B-roll export (or the captions export when B-roll is skipped) is the finished file |

## Approved results

_None yet._

## Small fixes made during the trial

1. **The total footage length is shown under the clips list** (project
   editor, `ProjectsScreen.tsx`). It sums the measured clip lengths, which are
   not rounded per clip, and says how many clips without a known length were
   left out. The footage list draft updates it live. Verified with `tsc -b`
   (passed) and lint (no new warnings). The user checks it on screen.
2. **Ongoing work shows that it is working (Cutting).** Every active cutting job
   (render clips and merged video, previews, finding boundaries,
   recommendation) is shown under the mode choice with a spinner, its title,
   the progress message and a progress bar. The bar is animated when the job
   gives no percentage. Each preview being rendered shows "Rendering this
   preview…" (or "Waiting to render…") on its own slot, and its button is
   disabled. The backend now includes `samples` (the preview requests) in the
   cutting state's job summaries. Verified with `tsc -b`, lint (no new
   warnings) and `pytest test_boundaries.py -k "state or sample or cancel"`
   (6 passed). The user still has to see it on screen.
3. **Cancel where the work is shown (Cutting).** Each active cutting job has
   **Cancel**. The cut run's "Creating subtitles…" spinner has **Cancel**,
   which replaces "Create subtitles" while it runs. This uses the existing
   job cancellation, which was only reachable in **Background jobs** before.
   Verified with `tsc -b` and lint. Not yet exercised on screen.

## Decisions

_None yet._

## Deferred backlog

Each item records: ID, title, feedback and the actual problem, the stage, the
desired behaviour, an example or reproduction, the scope, any workaround, the
priority and the status.

### T-01 — Two subtitle paths: the old "Create subtitles" competes with Captions

- **Feedback:** "I pressed 'create subtitles' without checking the settings,
  now I need to wait for the action to finish for no reason."
- **Problem:** the cut run card still has the pre-2A **Create subtitles**
  button (raw Whisper → SRT, no editing). It is prominent (a primary button) and
  sits right after cutting, so it looks like the next step. The real next step
  is the **Captions** section. It also starts a multi-minute Whisper job with
  one click and no visible settings.
- **Stage:** Cutting → Captions hand-over.
- **Desired:** one obvious next step after cutting, toward Captions.
  The old per-run subtitles are hidden, demoted or removed.
- **Repro:** render clips and merged video, then look at the cut run card.
- **Scope:** general product.
- **Workaround:** ignore the button. A job started by mistake can now be
  cancelled on the card (small fix 3).
- **Priority:** medium. **Status:** **done with T-03 (2026-10-16).** The
  Subtitles (Whisper) section, the per-run and per-clip **Create subtitles**
  buttons, the subtitle badges and the SRT track on the cut players are
  removed from Cutting. Cut runs stay in the Cutting slide. The backend
  subtitles API is untouched.

### T-03 — Stage layout: one slide per stage, with a progress bar on top

- **Feedback:** "Right now the layout is quite confusing. There is a lot of
  layout overlapping between the different parts of the factory even though
  there isn't a lot of real logical overlapping. The layout will be a
  horizontal carousel, each stage gets a slide: uploading clips, cutting
  silence and merging clips, subtitles, zooms, B-roll. At the top, a bar shows
  where the video is in the process and how many stages are left. This layout
  will emphasise the hard separation between the stages."
- **Problem (seen in the code):** the project editor (`ProjectsScreen.tsx`)
  stacks everything on one long page: Footage, `CuttingPanel` (which also
  holds the cut runs and the old per-run subtitles, see T-01),
  `CaptionsPanel`, `ZoomsPanel`, `BrollPanel`, then `JobsPanel` and
  `PlanPanel`. Panels repeat each other's video pickers, status and outputs,
  so the page reads as overlapping even though the stages are sequential.
- **Stage:** all, the project editor shell.
- **Desired (user's decision):**
  1. A horizontal carousel with exactly one slide per stage, in this order:
     Footage (upload clips) → Cutting & merging → Captions → Zooms → B-roll.
  2. A stage bar at the top with the current stage, the completed stages and
     the remaining ones.
  3. A hard visual separation: a stage's controls live only on its slide.
  4. **A persistent preview to the right of the carousel** (added 2026-10-16)
     shows the video being made. It stays visible on every slide and always
     shows the video's current state: the latest result of the furthest
     stage reached (raw footage → cut/merged → captioned → zoomed → B-roll).
- **Decisions (2026-10-16):**
  - A stage is **done only when the user accepts it**. Zooms and B-roll
    already have approval. Footage, Cutting and Captions get a new explicit
    **Accept**. Each acceptance is tied to the exact result it covers (saved
    clip list, cut run's merged output, caption revision and style), so it
    becomes outdated when that result changes.
  - Accepting a stage **moves to the next slide by itself**. Any slide can
    still be opened from the stage bar.
  - Background jobs go into a collapsible drawer. The AI-plan panel is
    **removed** from the editor (its code and API stay).
  - T-01 is resolved here: the old per-run **Create subtitles** is removed
    from the cut run card. Cutting ends with one step toward Captions.
  - The preview shows the furthest result reached. Captions and B-roll use the
    live Remotion composition, so edits appear without rendering. A new
    read-only `GET /projects/{id}/pipeline` returns the stage statuses and
    the preview descriptor.
- **Scope:** general product. **Priority:** high, because it is the main
  clarity issue the user has raised so far. **Status:** **implemented
  (2026-10-16), awaiting the user's check on screen.**
- **What was built:**
  - Backend `pipeline.py` and `api_pipeline.py`:
    - `GET /projects/{id}/pipeline` returns the stages, the current stage,
      the number of stages left and the preview.
    - `POST /projects/{id}/pipeline/{stage}/accept` (footage, cutting,
      captions) takes the candidate's token, so only exactly what was shown
      is accepted.
    - Acceptances are stored in `project.settings["pipeline"]`. A stage is
      done only on top of a done stage. Zooms count as done when an approved
      zoom run of the accepted cut exists. B-roll counts as done when it is
      approved or skipped on that zoomed video, or when B-roll is switched
      off.
    - The preview is the zoomed video if one exists, else the accepted (or
      newest) merged cut, else the first clip. It is composed with
      `broll.compose`, the same props as the B-roll review and export.
  - Frontend:
    - `StageCarousel.tsx`: the stage bar, one slide per stage (all stay
      mounted), an Accept footer and automatic continuation.
    - `VideoPreview.tsx`: a sticky Remotion player on the right that polls
      every 4 s.
    - The footage list is slide 1. Background jobs sit in a collapsible
      drawer. `PlanPanel` is no longer shown.
  - Verified:
    - `tsc -b` passed. Lint shows no new kinds of warning.
    - The new `test_pipeline.py` passed (4 tests). The full suite passed
      (510 tests).
    - `/pipeline` on the trial project gave the expected state.
- **Known limits:**
  - The preview shows *saved* edits. Unsaved caption or B-roll drafts appear
    in their own slide's player, not in the preview.
  - The stage panels still have their own video pickers. Choosing a video
    other than the accepted cut works, but the stage bar follows only the
    accepted cut.

### T-02 — Running work shows a spinner and Cancel only in Cutting

- **Feedback:** show a loading animation on ongoing actions; add an option
  to cancel ongoing actions.
- **Problem:** small fixes 2–3 cover Cutting only. Captions, Zooms and B-roll
  have their own progress displays and partly their own Cancel buttons, but
  none of this is consistent or shared.
- **Stage:** all.
- **Desired:** one shared "working" component (spinner, title, progress, Cancel)
  used by every stage.
- **Scope:** general product. **Priority:** low (polish). **Status:** open.
  Extend it during the trial only where a stage turns out to have no
  visible progress.
