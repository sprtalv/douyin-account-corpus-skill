"""Portable workflow primitives; no model-generated summaries or silent revisions."""
import hashlib
import json
import math
import sqlite3
from pathlib import Path
from .db import digest, now
from .speakers import source, save, ROLES


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, data):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    text=json.dumps(data,ensure_ascii=False,indent=2)+'\n'
    if path.exists() and path.read_text(encoding='utf8')==text:return False
    temp=path.with_suffix(path.suffix+'.tmp');temp.write_text(text,encoding='utf8');temp.replace(path)
    return True


def fingerprint(data):
    return hashlib.sha256(json.dumps(data,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def config(c):
    return read_json(c.root/'project.json')


def observations(c, path):
    items=read_json(path)
    if not isinstance(items,list):raise ValueError('Observations must be a JSON array')
    allowed={'id','page_url','audio_url','duration','published','declaration','observed_at'}
    from urllib.parse import urlsplit
    for item in items:
        if not isinstance(item,dict) or not set(item)<=allowed:raise ValueError('Unexpected observation field')
        row=c.video(item['id'])
        if item['page_url']!=row['url']:raise ValueError('Observation page/video mismatch')
        u=urlsplit(item['audio_url'])
        if u.scheme!='https' or not u.hostname or not u.hostname.endswith(('.zjcdn.com','.douyinvod.com')):
            raise ValueError('Only normally observed public Douyin media URLs are accepted')
        if not item.get('observed_at'):raise ValueError('Observation timestamp required')
    changed=0
    for item in items:
        changed+=write_json(c.root/'data/metadata'/f"{item['id']}.browser.json",item)
        updates={k:item[k] for k in ('published',) if item.get(k)}
        if item.get('declaration'):updates['description']=item['declaration']
        if updates:c.update(item['id'],**updates)
    return {'observations':len(items),'changed':changed}


def review_current(c, vid):
    path=c.root/'data/annotated'/f'{vid}.json'
    accepted=c.root/'review/accepted'/f'{vid}.json'
    if not path.is_file() or not accepted.is_file():return False
    try:
        j=read_json(path);item=read_json(accepted);_,raw,sha=source(c,vid)
        validate_review(c,vid,item)
        turns=j['turns'];bounds=item['_boundaries']
        if (j['source_sha256']!=sha or not j['method'].startswith('agent')
                or j['video_id']!=vid or j['url']!=c.video(vid)['url']
                or len(turns)!=len(bounds)):return False
        for i,(a,role) in enumerate(bounds):
            end=bounds[i+1][0] if i+1<len(bounds) else len(raw)
            t=turns[i]
            if (t['char_start']!=a or t['char_end']!=end or t['speaker']!=role
                    or t['text']!=raw[a:end]):return False
        return True
    except (KeyError,ValueError,TypeError,IndexError,OSError):return False


def prepare_reviews(c, limit=0):
    """Write full texts once to files; return paths only to avoid token-heavy logs."""
    packets=[]
    for row in c.db.execute('SELECT * FROM videos ORDER BY id DESC').fetchall():
        vid=row['id']
        if not row['transcript_path'] or review_current(c,vid):continue
        segs,raw,sha=source(c,vid)
        if not raw:continue
        offset=0;parts=[]
        for s in segs:
            parts.append({'segment_id':s['id'],'char_start':offset,'char_end':offset+len(s['text']),
                          'start':s['start'],'end':s['end'],'text':s['text']});offset+=len(s['text'])
        data={'video_id':vid,'title':row['title'],'url':row['url'],'source_sha256':sha,
              'characters':len(raw),'segments':parts,
              'instructions':'Read every segment in order. A segment may contain multiple speakers. Return exact character boundaries; do not rewrite text. Supply a reason per uncertain boundary.'}
        path=c.root/'review/packets'/f'{vid}.json';write_json(path,data)
        packets.append({'video_id':vid,'characters':len(raw),'packet':str(path)})
        if limit and len(packets)>=limit:break
    write_json(c.root/'review/queue.json',packets)
    return {'pending':len(packets),'queue':str(c.root/'review/queue.json')}


def validate_review(c, vid, item):
    c.video(vid)
    if not isinstance(item,dict):raise ValueError('Review must be an object: '+vid)
    _,raw,sha=source(c,vid)
    if not raw or item.get('source_sha256')!=sha:raise ValueError('Review source hash mismatch: '+vid)
    if item.get('reviewed_full_text') is not True:raise ValueError('Full-text review attestation required: '+vid)
    bs=item.get('_boundaries',[])
    if not isinstance(bs,list) or not bs:raise ValueError('Boundaries required: '+vid)
    reasons=item.get('uncertain_reasons',{})
    if not isinstance(reasons,dict):raise ValueError('Uncertainty reasons must be an object')
    last=-1
    uncertain=set()
    for boundary in bs:
        if not isinstance(boundary,(list,tuple)) or len(boundary)!=2:raise ValueError('Invalid boundary: '+vid)
        a,r=boundary
        if type(a) is not int or not last<a<len(raw) or r not in ROLES:raise ValueError('Invalid boundary: '+vid)
        if last==-1 and a!=0:raise ValueError('First boundary must be zero: '+vid)
        last=a
        if r=='不确定':
            uncertain.add(str(a))
            reason=reasons.get(str(a))
            if not isinstance(reason,str) or not reason.strip():raise ValueError('Specific uncertainty reason required at '+str(a))
    if set(reasons)!=uncertain:raise ValueError('Uncertainty reasons must match uncertain boundaries')
    if not isinstance(item.get('review_note'),str) or not item['review_note'].strip():raise ValueError('Review note required')
    return sha


def apply_reviews(c, path):
    data=read_json(path)
    if not isinstance(data,dict):raise ValueError('Reviews must map video IDs to records')
    for vid,item in data.items():validate_review(c,vid,item)
    applied=skipped=0
    for vid,item in data.items():
        record_path=c.root/'review/accepted'/f'{vid}.json'
        if record_path.exists() and read_json(record_path)==item and review_current(c,vid):
            skipped+=1;continue
        result=dict(item)
        notes=[f"字符{a}：{reason}" for a,reason in item.get('uncertain_reasons',{}).items()]
        if notes:result['review_note']+='\n\n不确定原因：\n'+'\n'.join(notes)
        save(c,vid,result,method='agent semantic review of full transcript',review='agent_semantic_review_not_audio_verified')
        write_json(record_path,item);applied+=1
    return {'applied':applied,'unchanged':skipped}


def status(c):
    rows=c.db.execute('SELECT * FROM videos ORDER BY id DESC').fetchall()
    media=sum(bool(r['local_path']) and Path(r['local_path']).is_file() for r in rows)
    transcribed=sum(bool(r['transcript_path']) and Path(r['transcript_path']).is_file() for r in rows)
    reviewed=sum(bool(r['transcript_path']) and Path(r['transcript_path']).is_file() and review_current(c,r['id']) for r in rows)
    uncertain=[]
    for r in rows:
        if not review_current(c,r['id']):continue
        j=read_json(c.root/'data/annotated'/f"{r['id']}.json")
        accepted=c.root/'review/accepted'/f"{r['id']}.json"
        reasons=read_json(accepted).get('uncertain_reasons',{}) if accepted.exists() else {}
        for t in j['turns']:
            if t['speaker']=='不确定':uncertain.append({'video_id':r['id'],'url':r['url'],**t,
                'reason':reasons[str(t['char_start'])]})
    return {'videos':len(rows),'media':media,'transcripts':transcribed,'semantic_reviewed':reviewed,
            'pending_transcripts':len(rows)-transcribed,'pending_reviews':transcribed-reviewed,
            'transcribed_seconds':sum(r['duration'] or 0 for r in rows if r['transcript_path']),
            'audio_verified':sum(r['quality']=='human_verified' for r in rows),
            'uncertain_passages':len(uncertain),'uncertain':uncertain,
            'failures':[{'video_id':r['id'],'status':r['status'],'error':r['last_error']} for r in rows if r['last_error']]}


def verify(c, media=False):
    errors=[];checked=0
    for row in c.db.execute('SELECT * FROM videos').fetchall():
        try:
            vid=row['id'];segs,raw,sha=source(c,vid);checked+=1
            if row['local_path']:
                path=Path(row['local_path'])
                if not path.is_file():errors.append([vid,'missing media'])
                elif media and digest(path)!=row['media_sha256']:errors.append([vid,'media hash mismatch'])
            if row['transcript_path']:
                path=Path(row['transcript_path'])
                if not path.is_file() or not path.with_suffix('.json').is_file():errors.append([vid,'missing transcript']);continue
                j=read_json(path.with_suffix('.json'))
                if not raw or ''.join(s['text'] for s in j['segments'])!=raw:errors.append([vid,'source text mismatch'])
                if j['video']['id']!=vid or j['video']['url']!=row['url']:errors.append([vid,'source link mismatch'])
                previous=-1
                for s in segs:
                    if not math.isfinite(s['start']) or not math.isfinite(s['end']) or not 0<=s['start']<=s['end'] or s['start']<previous:errors.append([vid,'invalid source timing'])
                    previous=s['start']
                if len(j['segments'])!=len(segs) or any(any(a[k]!=b[k] for k in ('start','end','text')) for a,b in zip(j['segments'],segs)):
                    errors.append([vid,'source segment/database mismatch'])
            ann=c.root/'data/annotated'/f'{vid}.json'
            if ann.exists():
                j=read_json(ann);turns=j['turns']
                if j['source_sha256']!=sha or ''.join(t['text'] for t in turns)!=raw:errors.append([vid,'annotation stale or text changed'])
                if j.get('video_id')!=vid or j.get('url')!=row['url']:errors.append([vid,'annotation source link mismatch'])
                if j.get('method','').startswith('agent') and not review_current(c,vid):errors.append([vid,'full review evidence invalid'])
                cursor=0
                for t in turns:
                    if t['char_start']!=cursor or not cursor<t['char_end']<=len(raw):errors.append([vid,'annotation boundary mismatch'])
                    if raw[t['char_start']:t['char_end']]!=t['text']:errors.append([vid,'annotation slice mismatch'])
                    if t['speaker'] not in ROLES:errors.append([vid,'invalid annotation speaker'])
                    if not math.isfinite(t['start']) or not math.isfinite(t['end']) or not 0<=t['start']<=t['end']:errors.append([vid,'annotation timing mismatch'])
                    cursor=t['char_end']
                stored=c.db.execute('SELECT * FROM speaker_turns WHERE video_id=? ORDER BY turn_index',(vid,)).fetchall()
                keys=('char_start','char_end','speaker','text','start','end')
                if any(s['source_sha256']!=sha for s in stored):errors.append([vid,'database annotation source hash mismatch'])
                if len(stored)!=len(turns) or any(any(s[k]!=t[k] for k in keys) for s,t in zip(stored,turns)):
                    errors.append([vid,'annotation/database mismatch'])
        except (KeyError,ValueError,TypeError,IndexError,OSError,sqlite3.Error) as exc:
            errors.append([row['id'],'invalid evidence: '+str(exc)])
    integrity=c.db.execute('PRAGMA integrity_check').fetchone()[0]
    if integrity!='ok':errors.append(['database',integrity])
    return {'checked':checked,'media_hashes_checked':media,'errors':errors,'ok':not errors}
