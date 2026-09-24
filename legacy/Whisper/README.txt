WHISPER SUBTITLES FOR WINDOWS
=============================

What this does
--------------
Drag a video onto a BAT file and receive an SRT subtitle file beside the video.

First-time installation
-----------------------
1. Extract this folder.
2. Double-click install_whisper.bat.
3. Wait until "Installation completed successfully" appears.

Create Hebrew subtitles
-----------------------
1. Drag the final edited video onto transcribe_video.bat.
2. The first run downloads the selected Whisper model.
3. A file with the same name and .srt extension will be created beside the video.

Example:
    video_final.mp4
    video_final.srt

Speed versus accuracy
---------------------
transcribe_video.bat:
    Uses the medium model. Better Hebrew accuracy, but slower.

transcribe_video_fast.bat:
    Uses the small model. Faster, but may make more transcription mistakes.

Premiere Pro
------------
Import the SRT file into the Project panel, then drag it to the timeline.
Premiere will create a caption track.

Recommended workflow
--------------------
1. Record approved takes.
2. Run Auto-Editor to remove unwanted silence.
3. Run Whisper on the edited output.
4. Import the video and SRT into Premiere.
5. Correct words and apply the Hebrew caption style.

Notes
-----
- Everything runs locally on the computer.
- The first run requires internet access to download the model.
- Later runs can work locally using the downloaded model.
- CPU mode is enabled by default because it is the most compatible.
- Keep the folder intact; its private .venv environment is created during installation.
