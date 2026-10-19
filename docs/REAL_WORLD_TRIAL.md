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
- **2026-10-18: the trial continues in a new project, "1000shekel-budget-test2"**
  (`58a03a48…`, 8 clips). Footage, Cutting (cut run `f2bf1e`), Captions
  (127 captions, revision 5, with the user's text fixes) and Zooms (approved
  run `802e07`) are **accepted**. **Current stage: B-roll**, not reviewed yet.
  The first project, "1000 shekels budget", is left as it was.
- **2026-10-17:** Footage and Cutting are still **not accepted**. The new
  stage lock sends the editor back to Footage. Accept Footage, then Cutting.
  Captions then transcribes the merged cut by itself, and the user fixes the
  spelling in the text table.
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
4. **Cutting slide decluttered (2026-10-17, the user's request).**
   - The mode cards show only their titles. Each card's description, tool,
     "default" and the old explanation paragraph (pauses kept / pauses
     removed) moved into an ⓘ tooltip on hover.
   - The explanation paragraph under the mode choice is removed.
   - The **Join with the next clip** preview is removed from every clip.
     **Render all previews** now renders openings and endings only, so it
     doesn't waste time on joins. The backend still supports join previews.
   - Verified with `tsc -b`, lint (no new warnings) and `test_boundaries.py`
     plus `test_pipeline.py` (98 passed).
5. **Boundaries are found automatically (Cutting).** When the clips' boundaries
   are unknown for the current detection settings and clip list, detection
   (**Find boundaries**) starts by itself. So the clips show where they will
   be cut, and the user can adjust the settings before rendering. It runs
   once per settings and clip-list combination, so a failure or cancel does
   not loop. The button stays as the manual retry. It runs when the editor
   loads, because all slides stay mounted, not only when the Cutting slide
   is opened. Verified by type-check. The user still has to see it on screen.
6. **VLC button on the preview panel.** The preview header has a **VLC**
   button that opens the file currently previewed (footage clip, merged cut
   or zoomed video) in VLC. VLC opens the plain file, so captions and B-roll
   aren't drawn on it. New endpoint: `POST /projects/{id}/captions/inputs/{key}/open`,
   which resolves the key on the backend, never a path from the request.
   It shares `player.open_file` with the footage VLC button. Verified by a new
   test in `test_projects.py` (opens the resolved path, 404 for an unknown
   key) and a live probe of the route.

7. **Shorter boundary setting labels** (the user's wording): Threshold,
   Before start, After end, Ignore. The labels are in the backend catalog
   (`boundaries.py`), and the README was updated to match. The longer meaning
   stays in each field's ⓘ. Verified live through `/cutting/settings` after a
   backend restart (see T-04).

8. **Captions slide simplified (2026-10-17, the user's request).**
   - **No engine options.** Model, language, device, precision, transcript
     picker and revision picker are gone. Transcription uses the catalog
     defaults: ivrit.ai large-v3 turbo, Hebrew, CPU, int8.
   - **No video picker or import.** The video is always the accepted cut's
     merged video, read from `/pipeline`. Before the cut is accepted, the
     slide says to accept it first.
   - **Transcription starts by itself** when that video has no transcript.
     It runs once per video and does not loop. **Transcribe** remains as the
     retry, and **Transcribe again** appears only for an out-of-date transcript.
   - **Text-only correction table** (`CaptionTextTable.tsx`) to the right of
     the slide's own player. Each row is an editable text cell and a
     read-only `start–end` time. Clicking the time or focusing the text seeks
     the player, and the caption on screen is highlighted. Use **Save text
     fixes** or **Discard**. Splitting, merging, timing edits and removal are
     gone from the UI.
   - Style, grouping and the separate captions export moved into collapsed
     sections below the table.
   - Verified with `tsc -b`, lint (no new warnings) and `npm test`. Not yet
     seen on screen.
9. **Stages are locked until the earlier ones are done.** The stage bar and
   the › button can't open a stage past the first stage that isn't done. If
   an upstream change reopens a stage, the carousel goes back to it. The
   side preview is hidden on the Captions slide, which has its own live
   player, so only one player is shown there.

10. **Captions slide, second pass (2026-10-17, the user's feedback).**
    - **One preview, in its usual place.** The slide's own player is removed
      (`CaptionPreview.tsx` deleted), and the shared side preview is shown on
      every slide again.
    - A small bridge (`previewBridge.ts`) lets the slide do three things:
      draw its unsaved text and style fixes on the shared preview, labelled
      "showing unsaved edits"; seek it from the table; and follow its frame
      to highlight the current row. The frames go through a listener, so
      playback does not re-render the slides.
    - **Action buttons at the top.** Every stage's Accept bar moved from the
      bottom of the slide to the top, under the slide title. Captions has
      **Transcribe** / **Save text fixes** / **Discard** in a row at the top
      of the slide.
    - **Default words per caption is 2** (was 5), in `captions.GROUPING_SPEC`.
      This is a product default (the user's request). Existing saved
      groupings are unchanged. The trial project had none saved. The test
      fixture now pins 5 for the rule tests, and a new test asserts the
      default.
    - **Style and grouping are always open:** plain sections, not collapsible.
      The separate captions export stays collapsed.
    - **Table box = preview panel height** (measured with a ResizeObserver).
      It scrolls with a sticky header. ↑/↓ move between rows and seek the
      player. While playing, the box scrolls itself, not the page, to follow
      the current caption, unless a row is being edited.
    - Verified with `tsc -b`, lint (one real warning fixed: unmemoised preview
      props), `npm test`, and `test_captions.py` (61 passed). The zoom,
      B-roll and pipeline tests gave 160 passed and 1 failed. The failure was
      `test_captions_keep_their_size_and_position_over_the_zoomed_video`,
      which hard-coded frames that had captions only with 5-word grouping. It
      now takes its frames from the middle of the rendered captions, and it
      passes. After a backend
      restart, `/captions/inputs` reports `max_words: 2`. Not yet seen on
      screen.

11. **Library windows on the B-roll slide (2026-10-18, the user's request).**
    - Two buttons at the top of the slide: **This video's assets (n)** and
      **General library (n)**, plus a badge when some assets need a
      description.
    - Each opens a modal window (`LibraryWindow.tsx`, a `<dialog>`) with
      **Import image or video…** first, then a list of every asset in that
      library. Each row shows its thumbnail and file information next to
      the always-open editor: name, description, tags, moments for videos,
      **Save details**, **Remove from library**, and moving the asset between
      the general library and this video's own. Rows without a description
      have a warning border.
    - Closing the window reloads the B-roll stage, so the planner sees the
      new descriptions.
    - The old collapsible "Asset library" section on the slide is removed.
      **Describe assets in the library** (shown when the planner has no
      described assets) opens the window that has undescribed assets.
    - The home-page library tab is unchanged.
    - Verified with `tsc -b`, lint (no new kinds of warning) and `npm test`
      (14 passed). Confirmed in the code that the video's route can edit
      general assets (`broll_library._find`). Not yet seen on screen.

12. **B-roll slide: one player, plus a read-only timeline table (2026-10-18).**
    - The slide's own Remotion player is removed. The shared side preview
      already plays the same composition, from the approved zoom with the
      saved B-roll plan and the captions. The slide seeks the preview (▶ on
      a placement, clicks in the table) and follows its playhead (for
      **Add at m:ss** and the table) through `previewBridge`. Edits are saved
      as plan revisions, and the preview shows them on its next 4-second
      refresh.
    - `BrollTimelineTable.tsx`, beside the preview and as tall as it (the
      shared `usePreviewHeight` hook). It has one row per caption (text and
      start–end time) and three narrow layer columns L1–L3. Each B-roll is a
      rectangle coloured per asset. A row covers from its caption's start to
      the next caption's start, and the rectangle's top and bottom are placed
      at the placement's exact start and end within the rows, so its height
      is its time on screen. Hover shows the asset, template and times. A
      click seeks the player. It follows the playhead.
    - Layers are filled greedily: the first layer free at a placement's start.
      Today's renderer and validator allow one B-roll at a time, so a valid
      plan only uses L1. L2 and L3 stay empty until overlapping B-roll
      exists.
    - It's read-only, and the existing correction controls are unchanged.
    - Verified with `tsc -b`, lint (no new warnings) and `npm test`. The
      trial project has no B-roll plan yet (planning has not run), so the
      layers are empty for now.
    - **Bug found on screen (the user's screenshot):** the Time column
      wrapped "start–end" onto two lines, and the fixed 22px row height drew
      them on top of each other. Cause: the no-wrap rule targeted
      `td:last-child`, which in this table is a layer column, not Time.
      Fixed with a `td.time` class (nowrap, narrow) used by both tables.
      Verified with `tsc -b`.

13. **Library window simplified (2026-10-18, the user's request).** A `compact`
    option on `AssetEditor`, `AssetImport` and the move button, used only in
    the library window. The home-page library tab keeps the full layout.
    - Each row is a small thumbnail with the name, type and length, plus the
      status only when there's a problem.
    - The fields are one line each with short titles: Description (widest),
      Name, Tags. Moments, for videos only, gets its own one-line row that
      can be resized.
    - **Save** (also with Enter in Description), **Remove**, and **Share with
      all videos** or **This video only**.
    - Removed: the explanation paragraphs, the "measured from the file" line,
      the storage and format note under Import, and the window's intro
      sentence.
    - Verified with `tsc -b`, lint (no new warnings) and `npm test`.

14. **Picture-in-picture position, size and angle (2026-10-18).**
    - **Feedback:** "the picture-in-picture default position is in the
      middle above the captions."
    - **Diagnosis:** the plan has `top_right`, so the box *is* in the corner.
      But the first asset is portrait (924×2048) at 40%. That would be
      432×958 px, which is over the 45% height limit, so the box becomes
      390×864 and reaches down to the middle of the 1080×1920 frame, just
      above the captions at 80%. The same limit made **Size** ineffective
      for tall assets.
    - **Added (manual corrections, in each placement's Adjust):**
      - **Position: custom**, with **Horizontal** and **Vertical** (the box's
        centre as % of the frame, kept inside the frame; shown only for
        custom).
      - **Angle** from −45° to 45°, applied around the centre with any
        position.
      - **Size** widened from 20–60 to 10–90. The 45% height limit holds up
        to size 60 and grows proportionally above it (67.5% at 90), so a
        large manual size actually enlarges tall assets.
    - **The planner is unchanged.** It still chooses only auto or a corner,
      with sizes 20–60, so its boxes keep the 45% limit. Its prompt shows
      those limits (`_planner_view_of_setting`), and its validator rejects
      `custom`, x/y and sizes outside 20–60.
    - Plans saved earlier get the new settings' defaults and look the same.
      The Adjust form shows defaults for missing values.
    - Verified:
      - `npm test` (16 passed, 2 new geometry tests).
      - `test_broll_planner.py` (46 passed, then the new rejection cases and
        a template test passed).
      - `test_broll.py`: 2 failures that asserted the old range and default
        set were updated, and those tests now pass.
      - The live `/broll/catalog` lists the new settings after a backend
        restart.
    - Not yet seen on screen.

15. **B-roll planner instructions: more B-roll, humour, own assets used
    (2026-10-18, the user's request).** Prompt version `broll-plan-v2`, so
    cached v1 answers aren't reused.
    - **More B-roll:** "fast short-form video … use it often. Place an asset
      wherever it relates to what is said, either by showing it or by
      reacting to it." An empty plan is right only when nothing relates. The
      speaker is kept on camera for the hook and the call to action only.
    - **Humour allowed:** "Memes and funny visuals may react to a line,
      exaggerate it or play on it, as long as the joke connects to what is
      said … Nothing hateful or demeaning toward people." This replaces
      "No jokes, memes or humorous readings."
    - **This video's own assets:** marked `added_for_this_video: true` in the
      catalog, with an explanation in `assets_note`. The rule: "must be
      placed at least once, at the moment where it fits best, unless it truly
      relates to nothing in the speech (then say so in the summary)." The
      shortlist for a large library always keeps them. It's an
      instruction, not a validation rule. Undescribed assets are still left
      out, because the planner can't know what they show.
    - **Default preference** changed from Restrained to **Balanced**, as a
      product default. This video had Restrained saved explicitly and was
      set to Balanced through `PUT /broll/settings`.
    - Unchanged hard limits: 1–15 s per placement, at most 50% coverage, no
      overlaps.
    - Verified with `test_broll_planner.py` (49 passed, with one assertion
      updated for the new default) and a new test that own assets are marked
      and survive the shortlist. The backend was restarted. Plan quality
      still needs the user's review after a new plan.

16. **Picture-in-picture: centred above the captions, bigger, plain (2026-10-18).**
    - **Position `above_captions`, which is what `auto` now resolves to:** the
      box is centred across the frame with its bottom 1.5% of the frame
      height above the caption line. If captions sit at the top, it goes
      just below them instead. The caption line is computed the same way
      `CaptionLayer` lays out a one-line caption (`captionLine` in
      `broll/layout.ts`), from a new `caption_layout` in the B-roll props
      (position, safe margin, font size). Without captions, the box sits
      above the bottom margin. A caption that wraps to two lines can
      overlap the box's bottom edge. Captions are drawn on top, so they
      stay readable.
    - **Bigger:** the default `size_percent` changed from 40 to 70. The
      planner's range changed from 20–60 to 40–80 ("Big is good"). The
      height limit is 45% up to size 60 and grows above it.
    - **No border:** the white outline, drop shadow and rounded corners are
      removed. Rotation still works.
    - The planner is told that `auto` / `above_captions` is the recommended
      position, and a corner should be used only when the centre would hide
      something. The prompt version is now `broll-plan-v3`.
    - **Existing plans:** placements that the planner gave an explicit
      corner, like this video's current `top_right` ones, stay in that
      corner. Only `auto` placements and new plans move. Make a new plan,
      or set Position to auto in Adjust.
    - Verified with `npm test` (17 passed, with a new above-captions geometry
      test) and `tsc -b`. Backend tests: see below. The README template row
      is updated.

17. **Full-frame only for video-shaped assets (2026-10-18, the user's rule).**
    - **The check:** `broll_templates.fits_full_frame`, mirrored by
      `fitsFullFrame` in `brollFormat.ts`. An asset fits when its aspect
      ratio is within ×1.25 of the frame's, which is 0.45–0.70 on a 9:16
      video: phone screenshots and phone recordings.
    - **Planner:** each offered asset carries `fits_full_frame`, and the
      prompt says to use full-frame only when it's true. A full-frame answer
      for an asset that doesn't fit is turned into picture-in-picture with
      default settings and noted in the normalisation notes, so no repair
      call is needed. Prompt version `broll-plan-v4`.
    - **Hand placement:** the validator refuses full-frame for an asset that
      doesn't fit ("not the video's shape … Use picture-in-picture"). The
      check runs last, so other problems are reported first. **Add at
      m:ss** switches to PiP for such assets, and both template pickers
      disable full-frame for them.
    - **This video:** 3 of the 8 assets fit (push-ups video, Havruta home
      screenshot, counting-money video). Plan revision 18's B-roll 2 (the
      Fortnite lobby, 1920×1080, full-frame at 0:12.5) is now flagged.
      Fix it by switching it to picture-in-picture, or by making a new plan.
    - **Test fallout, fixed:** the shared test image changed from 640×360
      to 360×640 (video-shaped for the 360×640 test videos). Updated: the
      import-measurement assertions, two overlap tests (the 320×240 clip is
      now placed as PiP), and two real-render tests ("letterboxed, 202 red
      rows" became "the top 300 rows are red"). The two render tests that
      check PiP pixels are pinned to top-right at 40% (see item 16).
    - Verified: `test_broll.py`, `test_broll_planner.py` and
      `test_pipeline.py` gave 91 passed, then 3 new tests passed (the check
      itself, a planner full-frame becoming PiP, the hand-placement
      refusal). Also `npm test` (17) and `tsc -b`. The backend was
      restarted, and the live plan shows the expected issue.

18. **Every B-roll plan must include a hook (2026-10-18, the user's rule).**
    - **Interpretation (mine; easy to change):** a hook is a placement that
      starts within the first **3.0 s** (`HOOK_SECONDS`).
    - **Prompt (`broll-plan-v5`):** "Open with a hook: one placement must
      start within the first 3 seconds, using the most attention-grabbing
      asset that relates to the opening words (a striking or funny one is
      ideal). This is required." This replaces "keep the speaker on camera
      for the opening hook". The line "A plan is never empty" replaces "an
      empty list is right when nothing relates". The rules list and the
      summary field now mention the hook.
    - **Validation:** a planner answer with no placement starting before 3 s
      is rejected with the reason. It gets one repair request, and the job
      fails if the repaired answer still has no hook. This means **an empty
      automatic plan is no longer valid.** Removing every placement by hand
      and approving "no B-roll", or **Skip B-roll**, still work.
    - **Demo planner:** adds a 2.5 s PiP hook at 0:00 when nothing starts in
      time.
    - **Tests:** the fake answers that started later or were empty now have
      a hook. "an empty plan is valid" became "every plan needs a hook".
      "a valid no-B-roll result can be approved" became "a plan without a
      hook is sent back once and then fails". `test_broll_planner.py`: 53
      passed. The backend was restarted.

## Decisions

- **Join previews are dropped from the Cutting UI** (the user's decision,
  2026-10-17). This applies to the product, not only this video.
- **Boundary detection runs automatically** in boundary-only mode (the user's
  decision). This is a product default.
- **Captions always use the accepted merged cut.** Captions engine settings
  are not user-facing, and corrections are text-only. The tool is only for
  fixing the transcription's spelling, not timing (the user's decision,
  product-wide). The backend still accepts timing changes. Only the UI
  dropped them.
- **A stage can't be opened before the earlier stages are done** (the user's
  decision, product-wide).
- **B-roll editorial direction is product-wide:** more B-roll, humour and memes
  allowed, the default preference is Balanced, and assets in a video's own
  library are expected in that video (the user's decision, 2026-10-18).
- **Picture-in-picture defaults to centred above the captions at 70%, plain
  with no border. Full-frame is only for assets shaped like the video;
  everything else is PiP** (the user's decisions, product-wide, 2026-10-18).

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

### T-04 — `uvicorn --reload` stops picking up backend changes

- **Problem:** this has happened twice (2026-10-16 and 2026-10-17). After an
  edit, the running backend kept serving the old code: a new route returned
  404, and renamed labels were still old. Stopping the reloader also leaves
  its multiprocessing worker holding port 8000 unless it is stopped too.
- **Stage:** development tooling, not a production stage.
- **Desired:** backend edits take effect reliably, or a documented one-command
  restart that stops the whole process tree.
- **Workaround:** stop the reloader *and* its `spawn_main` worker, then start
  the backend again from a shell that has `GEMINI_API_KEY`, with no job
  running.
- **Scope:** developer experience. **Priority:** low. **Status:** open.

### T-05 — Place and size B-roll by dragging on the preview

- **Feedback:** "add some options to play with the position and size and
  angle of the B-rolls."
- **Done now (small fix 14):** number fields in Adjust for a custom position,
  size and angle. Each change is saved as a plan revision, and the preview
  updates on its next refresh (about 4 s).
- **Still clumsy for "playing":** each tweak is a number, a save and a short
  wait.
- **Desired (speculative, not decided):** drag, resize and rotate handles on
  the PiP box in the shared preview, drawn live from a draft (like caption
  text fixes) and saved once.
- **Stage:** B-roll. **Scope:** general product. **Priority:** medium.
  **Status:** open.

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
