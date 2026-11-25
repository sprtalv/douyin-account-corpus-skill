"""Account-independent CLI. Every content command requires an explicit workspace."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
from contextlib import contextmanager

VERSION='1.0.0'


def nonnegative(value):
    number=int(value)
    if number<0:raise argparse.ArgumentTypeError('Must be zero or greater')
    return number


@contextmanager
def project_lock(root):
    lock=root/'logs/workflow.lock';lock.parent.mkdir(parents=True,exist_ok=True)
    try:fd=os.open(str(lock),os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    except FileExistsError:raise ValueError('Workspace locked. Check PID in logs/workflow.lock; remove only after confirming the process ended.')
    try:
        with os.fdopen(fd,'w',encoding='utf8') as f:json.dump({'pid':os.getpid()},f)
        yield
    finally:lock.unlink(missing_ok=True)


def main():
    p=argparse.ArgumentParser(description='Local Douyin account corpus; no paid services')
    p.add_argument('--root',type=Path)
    sp=p.add_subparsers(dest='command',required=True)
    sp.add_parser('doctor')
    s=sp.add_parser('init');s.add_argument('--name',required=True);s.add_argument('--account',required=True)
    s.add_argument('--review-policy',choices=['full','none'],default='full')
    s.add_argument('--device',choices=['cpu','cuda'],default='cuda');s.add_argument('--model',default='large-v3-turbo')
    s.add_argument('--model-dir',type=Path)
    for name in ('import-index','observe','review-apply'):
        s=sp.add_parser(name);s.add_argument('file',type=Path)
    for name in ('import-media','import-subtitles'):
        s=sp.add_parser(name);s.add_argument('id');s.add_argument('file',type=Path)
    s=sp.add_parser('run');s.add_argument('--retry-failed',action='store_true');s.add_argument('--id');s.add_argument('--limit',type=nonnegative,default=0)
    s=sp.add_parser('review-prepare');s.add_argument('--limit',type=nonnegative,default=0)
    sp.add_parser('status')
    s=sp.add_parser('verify');s.add_argument('--media',action='store_true')
    s=sp.add_parser('export');s.add_argument('--allow-partial',action='store_true')
    s=sp.add_parser('search');s.add_argument('keyword');s.add_argument('--speaker');s.add_argument('--limit',type=nonnegative,default=20)
    a=p.parse_args()
    if a.command=='doctor':
        modules={m:importlib.util.find_spec(m) is not None for m in ['yaml','requests','faster_whisper','numpy']}
        result={'version':VERSION,'python':sys.executable,'python_supported':sys.version_info>=(3,11),
                'ffmpeg':shutil.which('ffmpeg'),'ffprobe':shutil.which('ffprobe'),'modules':modules,
                'browser':'Use current session browser tools; not bundled or accessed by this CLI'}
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if all(modules.values()) and result['ffmpeg'] and result['ffprobe'] and result['python_supported'] else 2
    if not a.root:p.error('--root is required for content operations')
    root=a.root.resolve()
    if a.command=='init':
        from urllib.parse import urlsplit
        u=urlsplit(a.account)
        if u.scheme!='https' or u.hostname!='www.douyin.com' or not u.path.startswith('/user/') or not u.path[len('/user/'):].strip('/'):
            raise ValueError('Expected https://www.douyin.com/user/... account URL')
    from corpus.db import Corpus,digest
    from corpus.workflow import read_json,write_json,config,status,verify,observations,prepare_reviews,apply_reviews,fingerprint
    from corpus.pipeline import register_media,parse_subtitles,render,fetch_observed,transcribe
    from corpus.ingest import import_manifest
    if a.command!='init' and not (root/'project.json').is_file():raise ValueError('Initialize this workspace first; legacy corpora need an explicit isolated migration')
    c=Corpus(root)
    try:
        with project_lock(root):
            if a.command=='init':
                from urllib.parse import urlsplit
                u=urlsplit(a.account)
                if u.scheme!='https' or u.hostname!='www.douyin.com' or not u.path.startswith('/user/'):
                    raise ValueError('Expected https://www.douyin.com/user/... account URL')
                if (root/'project.json').exists():raise ValueError('Workspace already initialized; use status/run')
                if c.db.execute('SELECT count(*) FROM videos').fetchone()[0]:raise ValueError('Refusing to initialize over an existing corpus')
                write_json(root/'project.json',{'version':VERSION,'name':a.name,'account_url':a.account,'review_policy':a.review_policy})
                import yaml
                cfg={'asr':{'model':a.model,'language':'zh','device':a.device,'compute_type':'int8_float16' if a.device=='cuda' else 'int8','beam_size':5,'batch_size':1}}
                if a.model_dir:cfg['asr']['model']=str(a.model_dir.resolve())
                (root/'config.yaml').write_text(yaml.safe_dump(cfg,allow_unicode=True),encoding='utf8')
                result={'initialized':str(root),'review_policy':a.review_policy}
            elif a.command=='import-index':
                manifest=read_json(a.file)
                if manifest['account']['url']!=config(c)['account_url']:raise ValueError('Manifest belongs to another account')
                result=import_manifest(c,a.file)
                write_json(root/'sources/snapshots'/(fingerprint(manifest)+'.json'),manifest)
            elif a.command=='observe':result=observations(c,a.file)
            elif a.command=='import-media':
                changed=register_media(c,a.id,a.file)
                result={'unchanged':True} if changed is False else {'imported':a.id}
            elif a.command=='import-subtitles':
                row=c.video(a.id);segs=parse_subtitles(a.file)
                current=[dict(r) for r in c.db.execute('SELECT start,end,text FROM segments WHERE video_id=? ORDER BY start,id',(a.id,))]
                proposed=[{k:s[k] for k in ('start','end','text')} for s in segs]
                if current==proposed and row['transcript_path']:
                    transcribe(c,a.id)
                    result={'unchanged':True}
                elif current:raise ValueError('Source transcript already exists; preserve it and use a revision workspace')
                else:
                    c.set_segments(a.id,segs);c.update(a.id,transcript_method='imported_subtitles',quality='machine_unverified');render(c,a.id)
                    write_json(root/'data/metadata'/f'{a.id}.subtitle_source.json',{'path':str(a.file.resolve()),'sha256':digest(a.file),'coverage':'not_audio_verified'})
                    result={'imported':a.id,'segments':len(segs)}
            elif a.command=='run':
                rows=[c.video(a.id)] if a.id else c.db.execute('SELECT * FROM videos ORDER BY id DESC').fetchall()
                cache_path=root/'logs/attempts.json';attempts=read_json(cache_path) if cache_path.exists() else {}
                processed=0;model=None
                for row in rows:
                    vid=row['id'];obs=root/'data/metadata'/f'{vid}.browser.json'
                    if row['transcript_path']:
                        try:model=transcribe(c,vid,model)
                        except Exception as e:
                            c.update(vid,last_error=str(e),status='processing_failed');c.event(vid,'run','failed',str(e))
                        continue
                    signature=fingerprint({'media':row['media_sha256'],'observation':read_json(obs) if obs.exists() else None,'asr':(root/'config.yaml').read_text(encoding='utf8')})
                    if not a.retry_failed and attempts.get(vid)==signature:continue
                    if not row['local_path'] and not obs.exists():continue
                    try:
                        if not row['local_path'] or not Path(row['local_path']).is_file():
                            if not obs.exists():raise ValueError('Media missing; import it again or supply a fresh browser observation')
                            fetch_observed(c,vid,read_json(obs)['audio_url'])
                        if c.video(vid)['local_path']:
                            if digest(c.video(vid)['local_path'])!=c.video(vid)['media_sha256']:raise ValueError('Media changed on disk')
                            model=transcribe(c,vid,model)
                        if c.video(vid)['transcript_path']:attempts.pop(vid,None)
                        else:attempts[vid]=signature
                    except Exception as e:
                        attempts[vid]=signature;c.update(vid,last_error=str(e),status='processing_failed');c.event(vid,'run','failed',str(e))
                    write_json(cache_path,attempts);processed+=1
                    if a.limit and processed>=a.limit:break
                if config(c)['review_policy']=='full':prepare_reviews(c)
                result={'processed':processed,**{k:v for k,v in status(c).items() if k!='uncertain'}}
                if not result['failures'] and not result['pending_transcripts'] and (config(c)['review_policy']=='none' or not result['pending_reviews']):
                    from corpus.delivery import deliver
                    result['export']=deliver(c)
            elif a.command=='review-prepare':result=prepare_reviews(c,a.limit)
            elif a.command=='review-apply':result=apply_reviews(c,a.file)
            elif a.command=='status':result={k:v for k,v in status(c).items() if k!='uncertain'}
            elif a.command=='verify':
                result=verify(c,a.media);write_json(root/'analysis/verification.json',result)
            elif a.command=='export':
                from corpus.delivery import deliver
                result=deliver(c,a.allow_partial)
            elif a.command=='search':
                if a.speaker:
                    exists=c.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='speaker_turns'").fetchone()
                    if not exists:result=[]
                    else:
                        term=a.keyword.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
                        result=[dict(r) for r in c.db.execute("SELECT t.*,v.url,v.title FROM speaker_turns t JOIN videos v ON v.id=t.video_id WHERE t.text LIKE ? ESCAPE '\\' AND t.speaker=? LIMIT ?",('%'+term+'%',a.speaker,a.limit))]
                else:result=c.search(a.keyword,limit=a.limit)
            print(json.dumps(result,ensure_ascii=False,indent=2))
            return 2 if (a.command=='verify' and not result['ok']) or (a.command=='run' and result['failures']) else 0
    finally:c.db.close()


if __name__=='__main__':
    try:sys.exit(main())
    except (ValueError,KeyError,OSError) as e:
        print(json.dumps({'error':str(e)},ensure_ascii=False),file=sys.stderr);sys.exit(2)
