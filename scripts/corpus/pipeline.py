import html
import json
import math
import os
import re
import shutil
import subprocess
import time
import sys
from pathlib import Path
from urllib.parse import urlsplit

import requests
import yaml

from .db import digest, now

_DLL_HANDLES=[]
def setup_cuda():
    if os.name=='nt':
        for d in (Path(sys.prefix)/'Lib/site-packages/nvidia').glob('*/bin'):
            os.environ['PATH']=str(d)+os.pathsep+os.environ.get('PATH','')
            _DLL_HANDLES.append(os.add_dll_directory(str(d)))


def stamp(seconds, srt=False):
    ms = round(seconds * 1000)
    h, rem = divmod(ms, 3600000); m, rem = divmod(rem, 60000); s, rem = divmod(rem,1000)
    return f'{h:02}:{m:02}:{s:02}' + (f',{rem:03}' if srt else '')


def probe(path):
    r = subprocess.run(['ffprobe','-v','error','-show_format','-show_streams','-of','json',str(path)],
                       capture_output=True, text=True, encoding='utf-8', check=True)
    j = json.loads(r.stdout)
    if not any(s['codec_type']=='audio' for s in j['streams']):
        raise ValueError('Media has no audio stream')
    duration=float(j['format']['duration'])
    if not math.isfinite(duration) or duration<=0: raise ValueError('Invalid media duration')
    return duration


def register_media(c, vid, path, copy=True):
    path = Path(path).resolve(); sha = digest(path)
    previous=c.video(vid)
    if previous['media_sha256'] and previous['media_sha256']!=sha and (previous['transcript_path'] or (c.root/'data/transcripts'/f'{vid}.checkpoint.jsonl').exists()):
        raise ValueError('Media changed after transcription; use a new revision workspace')
    if previous['media_sha256']==sha and previous['local_path'] and Path(previous['local_path']).is_file() and digest(previous['local_path'])==sha:
        return False
    duration = probe(path)
    duplicate = next((r for r in c.db.execute('SELECT id,local_path FROM videos WHERE media_sha256=? AND id<>? AND duplicate_of IS NULL ORDER BY id',(sha,vid))
                      if r['local_path'] and Path(r['local_path']).is_file() and digest(r['local_path'])==sha),None)
    target = c.root/'data/audio'/f'{vid}{path.suffix.lower()}'
    if duplicate:
        target = Path(duplicate['local_path'])
    elif copy and path != target:
        temp=target.with_suffix(target.suffix+'.importing')
        shutil.copy2(path,temp)
        if digest(temp)!=sha: raise ValueError('Media changed during copy')
        temp.replace(target)
    else:
        target = path
    c.update(vid,local_path=str(target),media_sha256=sha,duration=duration,
             duplicate_of=duplicate['id'] if duplicate else None,status=previous['status'] if previous['transcript_path'] else 'media_ready',last_error=None)
    c.event(vid,'media','complete',f'SHA256 {sha}; duration {duration}; duplicate {bool(duplicate)}')
    if previous['transcript_path'] and not previous['media_sha256']: render(c,vid)
    return True


def fetch_observed(c, vid, url):
    """Download ONLY a public audio URL actually observed in normal browser playback."""
    p=urlsplit(url)
    if p.scheme != 'https' or not p.hostname or not (p.hostname.endswith('.zjcdn.com') or p.hostname.endswith('.douyinvod.com')):
        raise ValueError('Not an allowed observed Douyin media host')
    existing=c.video(vid)
    if existing['local_path'] and Path(existing['local_path']).is_file():
        if digest(existing['local_path'])!=existing['media_sha256']: raise ValueError('Media changed on disk')
        return
    target=c.root/'data/audio'/f'{vid}.m4a'; part=target.with_suffix('.m4a.part')
    binding=part.with_suffix(part.suffix+'.source.json')
    # Never append bytes from an expired/replaced playback URL to another source.
    if not binding.exists() or json.loads(binding.read_text(encoding='utf8')).get('url')!=url:
        part.unlink(missing_ok=True)
        binding.write_text(json.dumps({'url':url}),encoding='utf8')
    error=''
    for attempt in range(3):
        if shutil.disk_usage(c.root).free < 5*1024**3:
            raise RuntimeError('Less than 5 GB free disk space')
        try:
            offset=part.stat().st_size if part.exists() else 0
            headers={'Range':f'bytes={offset}-'} if offset else {}
            with requests.get(url,headers=headers,stream=True,timeout=(15,45)) as r:
                if r.status_code in (401,403,412,429):
                    c.update(vid,status='blocked',last_error=f'HTTP {r.status_code}: access limited or URL expired')
                    c.event(vid,'download','blocked',f'HTTP {r.status_code}'); return
                r.raise_for_status()
                mode='ab' if offset and r.status_code==206 else 'wb'
                if mode=='ab' and not r.headers.get('Content-Range','').startswith(f'bytes {offset}-'):
                    raise ValueError('Range response mismatch')
                with part.open(mode) as f:
                    for chunk in r.iter_content(256*1024):
                        f.write(chunk)
            probe(part)
            if existing['media_sha256'] and digest(part)!=existing['media_sha256'] and (existing['transcript_path'] or (c.root/'data/transcripts'/f'{vid}.checkpoint.jsonl').exists()):
                raise ValueError('Downloaded media differs from bound transcript/checkpoint')
            part.replace(target)
            register_media(c,vid,target,copy=False); return
        except (requests.RequestException,subprocess.SubprocessError,ValueError) as e:
            error=f'{type(e).__name__}: {str(e)[:180]}'
            if attempt<2: time.sleep(2**attempt)
    c.update(vid,status='download_failed',last_error=error); c.event(vid,'download','failed',error)


def download_ytdlp(c, vid):
    from yt_dlp import YoutubeDL
    row=c.video(vid)
    if row['local_path'] and Path(row['local_path']).is_file(): return
    opts={'outtmpl':str(c.root/'data/videos'/f'{vid}.%(ext)s'),'format':'bestaudio/best',
          'noplaylist':True,'retries':2,'socket_timeout':25,'continuedl':True,
          'writesubtitles':True,'writeautomaticsub':True,'subtitleslangs':['zh.*','en.*'],
          'writeinfojson':True,'quiet':True}
    try:
        with YoutubeDL(opts) as ydl:
            info=ydl.extract_info(row['url'],download=True)
            register_media(c,vid,ydl.prepare_filename(info),copy=False)
            c.update(vid,published=info.get('upload_date'),description=info.get('description'),
                     subtitle_status='available' if info.get('requested_subtitles') else 'not_found')
    except Exception as e:
        err=str(e)
        blocked=any(t in err.lower() for t in ('403','412','429','cookies','captcha','login','drm'))
        c.update(vid,status='blocked' if blocked else 'download_failed',last_error=err)
        c.event(vid,'download','blocked' if blocked else 'failed',err)
        return blocked


def parse_subtitles(path):
    text=Path(path).read_text(encoding='utf-8-sig')
    if Path(path).suffix == '.json':
        j=json.loads(text)
        if isinstance(j,dict) and 'body' in j:  # Bilibili subtitle JSON
            return [dict(start=s['from'],end=s['to'],text=s['content'],speaker='未知') for s in j['body']]
        return j['segments'] if isinstance(j,dict) else j
    rx=re.compile(r'(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})')
    def seconds(t):
        m=rx.search(t)
        if not m: raise ValueError('Invalid subtitle timestamp')
        h,mi,s,ms=m.groups(); return int(h or 0)*3600+int(mi)*60+int(s)+int(ms)/1000
    result=[]
    for block in re.split(r'\n\s*\n',text.replace('\r','')):
        lines=block.splitlines()
        for i,line in enumerate(lines):
            if '-->' in line:
                a,b=line.split('-->',1)
                raw='\n'.join(lines[i+1:]); spoken=html.unescape(re.sub('<[^>]+>','',raw))
                if spoken.strip(): result.append(dict(start=seconds(a),end=seconds(b),text=spoken,speaker='未知'))
                break
    if not result: raise ValueError('No timestamped subtitle segments')
    return result


def render(c, vid):
    row=c.video(vid)
    segments=[dict(s) for s in c.db.execute('SELECT * FROM segments WHERE video_id=? ORDER BY start,id',(vid,))]
    if not segments: raise ValueError('Cannot render an empty transcript')
    for s in segments:
        s.update(json.loads(s.pop('details','{}') or '{}'))
    md=f"# {row['title']}\n\n平台：{row['platform']}\n原始链接：{row['url']}\n发布日期：{row['published'] or '未知'}\n时长：{row['duration'] or '未知'} 秒\n作者：{row['author'] or '未知'}\n内容类型：{row['content_type']}\n转写方式：{row['transcript_method']}\n转写质量：{row['quality']}；未校对，不可靠说话人标为未知。\n\n## 完整逐字稿\n\n"
    for s in segments:
        md+=f"[{stamp(s['start'])}] {s['speaker']}：{s['text']}"+(' 【识别存疑】' if s['uncertain'] else '')+'\n\n'
    md+='## 涉及主题\n\n待上下文复核。\n\n## 关键观点\n\n待人工核对说话人及完整论述，不从标题生成观点。\n\n## 论证过程\n\n待复核。\n'
    base=c.root/'data/transcripts'/vid
    def atomic(path, text):
        temp=path.with_suffix(path.suffix+'.tmp');temp.write_text(text,encoding='utf-8');temp.replace(path)
    atomic(base.with_suffix('.md'),md)
    metadata=dict(row);metadata.update(transcript_path=str(base.with_suffix('.md')),status='transcribed')
    atomic(base.with_suffix('.json'),json.dumps({'video':metadata,'segments':segments},ensure_ascii=False,indent=2))
    srt='\n\n'.join(f"{i}\n{stamp(s['start'],True)} --> {stamp(s['end'],True)}\n{s['text']}" for i,s in enumerate(segments,1))+'\n'
    atomic(c.root/'data/subtitles'/f'{vid}.srt',srt)
    c.update(vid,transcript_path=str(base.with_suffix('.md')),status='transcribed')


_MODEL_SIGNATURES = {}


def _model_source(c, cfg):
    """Resolve an installed model only: inference must never download implicitly."""
    name = str(cfg['model'])
    direct = Path(name).expanduser()
    candidates = [direct] if direct.is_absolute() else [c.root/direct]
    cache = c.root/'data/models'
    for repository in sorted(cache.glob('models--*--faster-whisper-'+name)) if '/' not in name and '\\' not in name else []:
        ref = repository/'refs/main'
        if ref.is_file(): candidates.append(repository/'snapshots'/ref.read_text().strip())
        candidates.extend(sorted((repository/'snapshots').glob('*')))
    for path in candidates:
        if all((path/f).is_file() for f in ('model.bin','config.json','tokenizer.json')):
            files = sorted(f for f in path.iterdir() if f.is_file())
            stat_key = tuple((str(f.resolve()),f.stat().st_size,f.stat().st_mtime_ns) for f in files)
            if stat_key not in _MODEL_SIGNATURES:
                import hashlib
                _MODEL_SIGNATURES[stat_key] = hashlib.sha256(json.dumps([(f.name,digest(f)) for f in files]).encode()).hexdigest()
            return str(path.resolve()), _MODEL_SIGNATURES[stat_key]
    raise ValueError('Complete local ASR model not found (model.bin, config.json, tokenizer.json required); install explicitly or use --model-dir')


def transcribe(c, vid, model=None, device=None):
    row=c.video(vid)
    if row['local_path']:
        if not Path(row['local_path']).is_file(): raise ValueError('Bound media file missing')
        if not row['media_sha256'] or digest(row['local_path'])!=row['media_sha256']: raise ValueError('Media changed on disk')
    if row['transcript_path']:
        segs=[dict(s) for s in c.db.execute('SELECT * FROM segments WHERE video_id=? ORDER BY start,id',(vid,))]
        if not segs: raise ValueError('Completed transcript has no database segments')
        path=Path(row['transcript_path'])
        if path.with_suffix('.json').is_file():
            saved=json.loads(path.with_suffix('.json').read_text(encoding='utf8'))
            keys=('start','end','text','speaker','uncertain')
            if saved['video']['id']!=vid or saved['video'].get('media_sha256')!=row['media_sha256'] or len(saved['segments'])!=len(segs) or any(any(a.get(k)!=b.get(k) for k in keys) for a,b in zip(saved['segments'],segs)):
                raise ValueError('Completed transcript binding or database mismatch')
        if not path.is_file() or not path.with_suffix('.json').is_file() or not (c.root/'data/subtitles'/f'{vid}.srt').is_file(): render(c,vid)
        if row['last_error'] or row['status']!='transcribed': c.update(vid,last_error=None,status='transcribed')
        return model
    if not row['local_path']: raise ValueError('No local media')
    # Find a completed exact match afresh; duplicate_of is only a storage hint.
    for other in c.db.execute('SELECT * FROM videos WHERE media_sha256=? AND id<>? AND transcript_path IS NOT NULL ORDER BY id',(row['media_sha256'],vid)).fetchall():
        transcribe(c,other['id'],model,device)
        segs=[dict(s) for s in c.db.execute('SELECT * FROM segments WHERE video_id=? ORDER BY start,id',(other['id'],))]
        c.set_segments(vid,segs)
        c.update(vid,transcript_method='exact_media_reuse:'+other['id'],quality=other['quality'])
        render(c,vid)
        return model
    cfg=yaml.safe_load((c.root/'config.yaml').read_text(encoding='utf-8'))['asr']
    model_source, model_hash=_model_source(c,cfg)
    checkpoint=c.root/'data/transcripts'/f'{vid}.checkpoint.jsonl'
    import hashlib
    settings={k:cfg.get(k) for k in ('language','compute_type','beam_size','batch_size')}
    settings.update(model_hash=model_hash,device=device or cfg['device'],vad_filter=False,word_timestamps=True,pipeline_revision=4)
    config_key=hashlib.sha256(json.dumps(settings,sort_keys=True).encode()).hexdigest()
    # A checkpoint line commits an entire decoding batch, never a partial segment.
    # Continuous decoding keeps full prior context; an interrupted pass restarts.
    existing=[]; resume=0; complete=False; last_speech_timestamp=0.0
    if checkpoint.exists():
        lines=checkpoint.read_bytes().splitlines()
        valid=[]
        for index,line in enumerate(lines):
            try:
                line=line.decode('utf-8');item=json.loads(line)
            except (json.JSONDecodeError,UnicodeDecodeError):
                if index != len(lines)-1: raise ValueError('Corrupt checkpoint before final line')
                break
            if item.get('config_key')!=config_key or item.get('media_sha256')!=row['media_sha256']: raise ValueError('Checkpoint source/model mismatch')
            if complete: raise ValueError('Checkpoint data after completion')
            end=item['resume_end']
            if not math.isfinite(end) or end<resume: raise ValueError('Invalid checkpoint boundary')
            existing.extend(item['segments']);resume=end;complete=item['complete'];valid.append(line)
            last_speech_timestamp=item['last_speech_timestamp']
            if not isinstance(last_speech_timestamp,(int,float)) or not math.isfinite(last_speech_timestamp) or last_speech_timestamp<0:
                raise ValueError('Invalid checkpoint word-alignment state')
        temp=checkpoint.with_suffix('.tmp');temp.write_text(''.join(line+'\n' for line in valid),encoding='utf8');temp.replace(checkpoint)
    started=time.monotonic()
    if not complete:
        if model is None or getattr(model,'_corpus_config_key',None)!=config_key:
            setup_cuda()
            from faster_whisper import WhisperModel
            model=WhisperModel(model_source,device=device or cfg['device'],compute_type=cfg['compute_type'],
                               local_files_only=True,cpu_threads=8,num_workers=1)
            model._corpus_config_key=config_key
        c.update(vid,status='transcribing')
        import numpy as np
        decoded=subprocess.run(['ffmpeg','-v','error','-i',row['local_path'],'-f','s16le','-ac','1','-ar','16000','pipe:1'],capture_output=True,check=True)
        samples=np.frombuffer(decoded.stdout,dtype=np.int16).astype(np.float32)/32768.0
        duration=len(samples)/16000
        if duration<=0 or resume>duration+0.001: raise ValueError('Invalid decoded duration/checkpoint')
        batch_size=cfg.get('batch_size',1)
        if not isinstance(batch_size,int) or batch_size<1: raise ValueError('batch_size must be a positive integer')
        groups=[]
        if batch_size>1:
            from faster_whisper import BatchedInferencePipeline
            decoder=BatchedInferencePipeline(model)
            clips=[];begin=resume
            while begin<duration:
                end=min(begin+29.5,duration)
                if end<duration:
                    lo=int((begin+25)*16000);hi=int(end*16000)
                    window=samples[lo:hi];frames=len(window)//320
                    energy=np.mean(window[:frames*320].reshape(-1,320)**2,axis=1)
                    end=(lo+int(np.argmin(energy))*320+160)/16000
                clips.append({'start':begin,'end':end});begin=end
            groups=[clips[i:i+batch_size] for i in range(0,len(clips),batch_size)]
        else:
            if resume: raise ValueError('Unexpected partial continuous-decoding checkpoint')
            decoder=model;groups=[None]
        with checkpoint.open('a',encoding='utf-8',buffering=1) as f:
            for clips in groups:
                options=dict(language=cfg.get('language','zh'),beam_size=cfg.get('beam_size',5),vad_filter=False,word_timestamps=True)
                if clips is not None: options.update(clip_timestamps=clips,batch_size=batch_size)
                else: options.update(condition_on_previous_text=True)
                if clips is not None: decoder.last_speech_timestamp=last_speech_timestamp
                it,info=decoder.transcribe(samples,**options)
                pending=[]
                for s in it:
                    # The batched generator resets this field at exhaustion.
                    # Capture it while yielded segments still expose the state.
                    if clips is not None: last_speech_timestamp=decoder.last_speech_timestamp
                    if not s.text.strip(): continue
                    uncertain=s.avg_logprob < -0.8 or s.no_speech_prob > 0.5 or s.compression_ratio>2.4
                    pending.append(dict(start=s.start,end=s.end,text=s.text,speaker='未知',uncertain=uncertain,
                        confidence=math.exp(s.avg_logprob),avg_logprob=s.avg_logprob,no_speech_prob=s.no_speech_prob,
                        compression_ratio=s.compression_ratio,
                        words=[dict(start=w.start,end=w.end,word=w.word,probability=w.probability) for w in (s.words or [])]))
                end=clips[-1]['end'] if clips is not None else duration
                complete=end>=duration
                item=dict(config_key=config_key,media_sha256=row['media_sha256'],segments=pending,resume_end=end,complete=complete,last_speech_timestamp=last_speech_timestamp)
                f.write(json.dumps(item,ensure_ascii=False)+'\n');f.flush();os.fsync(f.fileno())
                existing.extend(pending)
                print(f'{vid} {stamp(end)} / {stamp(duration)}',flush=True)
    if not existing: raise ValueError('ASR returned no speech; not a completed transcript')
    for i in range(1,len(existing)):
        if existing[i]['start']<existing[i-1]['start']:
            existing[i]['uncertain']=True;existing[i-1]['uncertain']=True
    existing.sort(key=lambda s:s['start'])
    c.set_segments(vid,existing)
    c.update(vid,transcript_method=f"faster-whisper {cfg['model']} {device or cfg['device']} / {cfg['compute_type']} / batch={cfg.get('batch_size',1)}; resumed={resume:.2f}s; model_sha256={model_hash}",quality='machine_unverified',last_error=None)
    render(c,vid); c.event(vid,'asr','complete',f'{len(existing)} segments; elapsed {time.monotonic()-started:.1f}s')
    return model
