ZOOM RENDERER FOR WINDOWS
=========================

What it does
------------
The tool reads zoom_plan.json and permanently applies the planned zooms to each
separate video clip.

It creates a new folder named:

ZOOMED_OUTPUT

The original clips are never changed.

Recommended workflow
--------------------
1. AutoEditor creates the separate trimmed clips.
2. ZoomPlanner creates zoom_planning_input.json.
3. The GPT Zoom Planning Agent creates zoom_plan.json.
4. Place zoom_plan.json inside the folder containing the trimmed clips.
5. Drag that folder onto render_zoom_plan.bat.
6. Import the files from ZOOMED_OUTPUT into Premiere.
7. Continue using the merged video only for the SRT subtitle workflow.

Required folder structure
-------------------------
trimmed
├── 0001_1_trimmed.mp4
├── 0002_2_trimmed.mp4
├── ...
└── zoom_plan.json

After rendering:

trimmed
├── original clips...
├── zoom_plan.json
└── ZOOMED_OUTPUT
    ├── 0001_1_trimmed.mp4
    ├── 0002_2_trimmed.mp4
    └── ...

FFmpeg requirement
------------------
The tool searches for ffmpeg.exe and ffprobe.exe in this order:

1. Inside the ZoomRenderer folder
2. Inside the sibling videoFactory\AutoEditor folder
3. Anywhere inside AutoEditor subfolders
4. Windows PATH

If the tool cannot find them, copy ffmpeg.exe and ffprobe.exe into ZoomRenderer.

Current zoom behavior
---------------------
NORMAL       = 100%
PUNCH_LIGHT  = 108%
PUNCH_MEDIUM = 112%
PUNCH_STRONG = 120%

Transitions are immediate hard cuts, not animated zooms.

FACE_CENTER currently means a fixed crop around the center of the frame.
It does not yet perform face detection or tracking.

Quality
-------
Video is rendered as H.264 with CRF 18.
Audio is rendered as AAC at 192 kbps.
The original files remain untouched.

Important
---------
The filenames in zoom_plan.json must exactly match the clip filenames.

The final effect of every clip must end at the actual clip duration.
A small tolerance is allowed because container durations can differ slightly.
