"""Semantic speaker annotations are a separate, reversible layer over unchanged ASR."""
import bisect
import difflib
import hashlib
import json
import re
from pathlib import Path
from .pipeline import stamp

ROLES=('主播','连麦者','连麦者（妻子）','连麦者（丈夫）','连麦者（男生）','连麦者（女生）','其他','不确定')


def normalize(text):
    return re.sub(r'\s+','',text)

def source(c,vid):
    segs=[dict(x) for x in c.db.execute('SELECT * FROM segments WHERE video_id=? ORDER BY start,id',(vid,))]
    raw=''.join(x['text'] for x in segs)
    return segs,raw,hashlib.sha256(raw.encode()).hexdigest()

def locate(raw,switches):
    positions=[i for i,ch in enumerate(raw) if not ch.isspace()]
    norm=''.join(raw[i] for i in positions)
    found=[];cursor=0;ambiguities=[]
    if not switches:raise ValueError('No speaker boundaries')
    for item in switches:
        if not isinstance(item,list) or len(item)!=2 or item[1] not in ROLES:raise ValueError('Bad speaker role or item')
        anchor=normalize(item[0])
        if not anchor:raise ValueError('Empty boundary anchor')
        pos=norm.find(anchor,cursor)
        if pos<0:
            # Recover only an exact, unique leading substring; never rewrite ASR.
            # Models sometimes corrupt the last character when hitting a length cap.
            recovered=None
            for size in range(len(anchor)-1,5,-1):
                prefix=anchor[:size];candidate=norm.find(prefix,cursor)
                if candidate>=0 and norm.find(prefix,candidate+1)<0:
                    recovered=candidate;break
            if recovered is None:raise ValueError('Anchor not found in order: '+anchor)
            pos=recovered
            ambiguities.append({'offset':positions[pos],'anchor':anchor,'reason':'anchor_suffix_mismatch_exact_prefix_used'})
        if pos>0 and norm.find(anchor,pos+1)>=0:ambiguities.append({'offset':positions[pos],'anchor':anchor,'reason':'repeated_anchor'})
        offset=positions[pos]
        found.append((offset,item[1]));cursor=pos+1
    if found[0][0]>0:
        if raw[:found[0][0]].strip():found.insert(0,(0,'不确定'))
        else:found[0]=(0,found[0][1])
    return found,ambiguities

def char_times(c,vid,segs):
    """Use retained word alignment; fall back explicitly to segment interpolation."""
    path=c.root/'data/transcripts'/f'{vid}.checkpoint.jsonl';lookup={}
    if path.exists():
        for line in path.read_text(encoding='utf8').splitlines():
            item=json.loads(line)
            segments=item.get('segments',[])
            if 'segment' in item:segments=[item['segment']]
            for s in segments:
                lookup[(s['start'],s['end'],s['text'])]=s.get('words',[])
    starts=[];ends=[];methods=[]
    for s in segs:
        text=s['text'];n=len(text)
        aa=[s['start']+(s['end']-s['start'])*i/max(n,1) for i in range(n)]
        bb=[s['start']+(s['end']-s['start'])*(i+1)/max(n,1) for i in range(n)]
        mm=['segment_interpolation']*n
        words=lookup.get((s['start'],s['end'],text),[])
        wt='';wa=[];wb=[]
        for w in words:
            st=w['word'];wt+=st
            wa += [w['start']]*len(st)
            wb += [w['end']]*len(st)
        for match in difflib.SequenceMatcher(None,text,wt,autojunk=False).get_matching_blocks():
            for j in range(match.size):
                aa[match.a+j]=wa[match.b+j];bb[match.a+j]=wb[match.b+j];mm[match.a+j]='asr_word_alignment'
        starts+=aa;ends+=bb;methods+=mm
    return starts,ends,methods

def save(c,vid,result,method='Qwen3-4B-Q4_K_M semantic inference',review='automatic_semantic'):
    segs,raw,sha=source(c,vid)
    if result.get('source_sha256',sha)!=sha:
        raise ValueError('Review source changed; re-review required')
    if '_boundaries' in result:
        bounds=[tuple(x) for x in result['_boundaries']];issues=result.get('_issues',[])
        if not bounds or bounds[0][0]!=0 or any(not isinstance(x,int) or x<0 or x>=len(raw) or role not in ROLES for x,role in bounds):raise ValueError('Invalid source offsets')
    else:bounds,issues=locate(raw,result['switches'])
    starts,ends,timing=char_times(c,vid,segs)
    turns=[]
    for i,(a,role) in enumerate(bounds):
        b=bounds[i+1][0] if i+1<len(bounds) else len(raw)
        if a>=b:raise ValueError('Nonincreasing boundary')
        text=raw[a:b]
        flags=[]
        if role=='主播' and re.search(r'^(?:小白老师|白哥|小白你好|老师你好)',text.strip()):flags.append('host_addressed_as_host_check_quote')
        flags.extend(x['reason'] for x in issues if a<=x['offset']<b)
        if role=='不确定':flags.append('role_uncertain')
        start=starts[a];end=max(start,ends[b-1])
        turns.append(dict(index=i+1,char_start=a,char_end=b,start=start,end=end,speaker=role,text=text,
                          attribution='semantic_inference_not_voice_verified',timing=timing[a],review=review,flags=flags))
    assert ''.join(x['text'] for x in turns)==raw
    v=dict(c.video(vid));out=c.root/'data/annotated';out.mkdir(exist_ok=True)
    record=dict(video_id=vid,url=v['url'],title=v['title'],source_sha256=sha,method=method,mode=result.get('mode','不确定'),turns=turns,issues=issues,review_note=result.get('review_note',''))
    (out/f'{vid}.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf8')
    level='逐段语义复核（未听校）' if review.startswith('agent_') else '自动候选（语义参照＋音频辅助，待复核）' if 'audio_supported' in review else '自动语义候选（待复核）'
    md=f"# {v['title']}\n\n原始链接：{v['url']}\n发布日期：{v['published'] or '未知'}\n标注级别：{level}\n\n连麦者标签不证明跨视频为同一人。保留源字幕或转写原文，转述仍归当前叙述者。时间来自源字幕、ASR词级对齐或分段估计；未确认身份和精确换人时刻。\n\n## 标注逐字稿\n\n"
    for t in turns:
        marker=' 【语义无法确定，待听校】' if review.startswith('agent_') and t['speaker']=='不确定' else ' 【归属待复核】' if t['flags'] else ''
        md+=f"[{stamp(t['start'])}] **{t['speaker']}**：{t['text']}"+marker+'\n\n'
    if record['review_note']:
        md+='## 语义复核说明\n\n'+record['review_note']+'\n'
    (out/f'{vid}.md').write_text(md,encoding='utf8')
    srt='\n\n'.join(f"{t['index']}\n{stamp(t['start'],True)} --> {stamp(t['end'],True)}\n{t['speaker']}：{t['text']}" for t in turns)+'\n'
    (out/f'{vid}.srt').write_text(srt,encoding='utf8')
    c.db.execute('''CREATE TABLE IF NOT EXISTS speaker_turns(video_id TEXT REFERENCES videos(id),turn_index INTEGER,char_start INTEGER,char_end INTEGER,start REAL,end REAL,speaker TEXT,text TEXT,source_sha256 TEXT,method TEXT,review TEXT,flags TEXT,PRIMARY KEY(video_id,turn_index))''')
    with c.db:
        c.db.execute('DELETE FROM speaker_turns WHERE video_id=?',(vid,))
        c.db.executemany('INSERT INTO speaker_turns VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',[(vid,t['index'],t['char_start'],t['char_end'],t['start'],t['end'],t['speaker'],t['text'],sha,method,review,json.dumps(t['flags'],ensure_ascii=False)) for t in turns])
    return record
