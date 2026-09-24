ZOOM PLANNER INPUT GENERATOR
============================

Recommended location:
OBS-Takes\videoFactory\ZoomPlanner

What it does:
- Receives a folder of separate trimmed clips.
- Transcribes every clip separately in Hebrew.
- Creates zoom_planning_input.json inside that clips folder.
- Does not edit, merge, move, or delete videos.

Installation:
1. Extract ZoomPlanner into videoFactory.
2. Run install_zoom_planner.bat once.

Use:
1. Run AutoEditor.
2. Drag the folder containing the separate trimmed clips onto create_zoom_input.bat.
3. Upload the generated zoom_planning_input.json to the Zoom Planning Agent.
4. The agent returns zoom_plan.json.

Recommended AutoEditor output:
Project_Output\
  Clips\
    01.mp4
    02.mp4
    03.mp4
  combined.mp4

Drag only the Clips folder.

The tool also ignores merged/master files whose names contain:
combined, merged, master, full_video, joined, concatenated

Current pipeline:
Separate clips -> ZoomPlanner -> zoom_planning_input.json -> GPT Agent
Merged video   -> Whisper -> SRT -> Premiere
