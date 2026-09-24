from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path
from typing import Any
import av
from faster_whisper import WhisperModel

VIDEO_EXTENSIONS={'.mp4','.mov','.mkv','.avi','.m4v','.webm'}
IGNORE_NAME_PARTS={'combined','merged','master','full_video','full-video','joined','concatenated'}

def natural_key(path: Path):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r'(\d+)', path.name)]

def should_ignore(path: Path):
    stem=path.stem.lower()
    return any(x in stem for x in IGNORE_NAME_PARTS)

def inspect_video(path: Path):
    with av.open(str(path)) as c:
        streams=[s for s in c.streams if s.type=='video']
        if not streams: raise RuntimeError(f'No video stream found in {path.name}')
        s=streams[0]
        if c.duration is not None: dur=float(c.duration/av.time_base)
        elif s.duration is not None and s.time_base is not None: dur=float(s.duration*s.time_base)
        else: raise RuntimeError(f'Could not determine duration of {path.name}')
        fps=float(s.average_rate) if s.average_rate is not None else None
        return {'duration':round(dur,3),'width':int(s.width),'height':int(s.height),'fps':round(fps,3) if fps else None}

def clean_text(t:str): return ' '.join(t.strip().split())

def main():
    p=argparse.ArgumentParser()
    p.add_argument('clips_folder')
    p.add_argument('--model',default='medium')
    p.add_argument('--device',choices=('cpu','cuda'),default='cpu')
    p.add_argument('--language',default='he')
    p.add_argument('--project-name',default=None)
    p.add_argument('--output',default='zoom_planning_input.json')
    a=p.parse_args()
    folder=Path(a.clips_folder).expanduser().resolve()
    if not folder.is_dir():
        print(f'ERROR: Folder not found: {folder}',file=sys.stderr); return 1
    clips=sorted([x for x in folder.iterdir() if x.is_file() and x.suffix.lower() in VIDEO_EXTENSIONS and not should_ignore(x)],key=natural_key)
    if not clips:
        print('ERROR: No separate video clips found.',file=sys.stderr); return 1
    print(f'Found {len(clips)} clips:')
    for i,c in enumerate(clips,1): print(f'  {i:02}. {c.name}')
    compute='float16' if a.device=='cuda' else 'int8'
    model=WhisperModel(a.model,device=a.device,compute_type=compute)
    out_clips=[]; first=None
    for i,clip in enumerate(clips,1):
        print(f'\n[{i}/{len(clips)}] {clip.name}')
        info=inspect_video(clip)
        if first is None: first=info
        segs,_=model.transcribe(str(clip),language=a.language,task='transcribe',beam_size=5,vad_filter=True,condition_on_previous_text=False,word_timestamps=False)
        transcript=[]
        for seg in segs:
            text=clean_text(seg.text)
            if not text: continue
            start=max(0.0,float(seg.start)); end=min(float(seg.end),info['duration'])
            if end<=start: continue
            transcript.append({'start':round(start,3),'end':round(end,3),'text':text})
        out_clips.append({'clip_id':f'{i:02}','filename':clip.name,'duration':info['duration'],'transcript':transcript})
        print(f'Created {len(transcript)} transcript segments.')
    orientation='vertical_short' if first['height']>first['width'] else 'horizontal_video'
    data={'schema_version':'1.0','project':{'name':a.project_name or folder.name,'format':orientation,'resolution':f"{first['width']}x{first['height']}",'fps':first['fps'],'source_folder':str(folder)},'clips':out_clips}
    output_path=folder/a.output
    output_path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'\nCreated: {output_path}')
    return 0

if __name__=='__main__': raise SystemExit(main())
