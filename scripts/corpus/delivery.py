"""One verified snapshot produces every reading artifact; unchanged inputs skip export."""
import html
import json
import os
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
from .workflow import config, fingerprint, read_json, write_json, status, verify, review_current


def deliver(c, allow_partial=False):
    qa=verify(c)
    write_json(c.root/'analysis/verification.json',qa)
    if not qa['ok']:raise ValueError('Integrity errors; see analysis/verification.json')
    stats=status(c);cfg=config(c);require_review=cfg['review_policy']=='full'
    incomplete=stats['pending_transcripts'] or (require_review and stats['pending_reviews'])
    if incomplete and not allow_partial:
        raise ValueError('Batch incomplete; resolve queue or explicitly use --allow-partial')
    rows=c.db.execute('SELECT * FROM videos ORDER BY published DESC,id DESC').fetchall()
    inputs=[]
    for row in rows:
        vid=row['id'];annotated=require_review and review_current(c,vid)
        stem=c.root/'data/annotated'/vid if annotated else c.root/'data/transcripts'/vid
        if not row['transcript_path'] or not Path(row['transcript_path']).is_file():continue
        files=[stem.with_suffix('.md'),stem.with_suffix('.json')]
        srt=stem.with_suffix('.srt') if annotated else c.root/'data/subtitles'/f'{vid}.srt'
        files.append(srt)
        if not all(p.is_file() for p in files):raise ValueError('Missing delivery files: '+vid)
        inputs.append((dict(row),annotated,files))
    from .db import digest
    accepted=[c.root/'review/accepted'/f"{row['id']}.json" for row,annotated,_ in inputs if annotated]
    links=[dict(r) for r in c.db.execute('SELECT * FROM links ORDER BY url')]
    key=fingerprint({'revision':2,'project':cfg,'partial':bool(incomplete),
        'videos':[dict(r) for r in rows], 'stats':stats, 'links':links, 'verification':qa,
        'reviews':[(p.name,digest(p)) for p in accepted],
        'files':[(str(p.relative_to(c.root)),digest(p)) for _,_,ff in inputs for p in ff]})
    out=c.root/'exports';out.mkdir(exist_ok=True);marker=out/'snapshot.json'
    if marker.exists():
        previous=read_json(marker)
        if previous.get('input_hash')==key and all((out/n).is_file() and digest(out/n)==h for n,h in previous.get('outputs',{}).items()) and previous.get('outputs'):
            return {'unchanged':True,'directory':str(out/'index.html'),'videos':len(inputs)}
    label='部分交付' if incomplete else '批次完整交付'
    title=cfg['name'];md=f'# {title}：完整文字稿\n\n{label}。保留源字幕或转写原文；语义复核不等于音频听校。\n\n'
    table=[];portable=[];manifest=[]
    for row,annotated,files in inputs:
        text=files[0].read_text(encoding='utf8');md+='\n---\n\n'+text
        relative='../'+files[0].relative_to(c.root).as_posix()
        srt='../'+files[2].relative_to(c.root).as_posix()
        table.append(f'<tr><td>{html.escape(row["title"] or row["id"])}</td><td>{"全文语义复核（未听校）" if annotated else "原始转写"}</td><td><a href="{html.escape(row["url"],quote=True)}">来源</a> · <a href="{relative}">全文</a> · <a href="{srt}">字幕</a></td></tr>')
        portable.append(read_json(files[1]))
        manifest.append({'video_id':row['id'],'url':row['url'],'title':row['title'],
            'source_links':[link for link in links if link['video_id']==row['id']],
            'reviewed':annotated,'files':{p.relative_to(c.root).as_posix():digest(p) for p in files}})
    pending=[dict(r) for r in rows if not r['transcript_path'] or (require_review and not review_current(c,r['id']))]
    summary={k:v for k,v in stats.items() if k!='uncertain'};summary.update(delivery=label,review_policy=cfg['review_policy'])
    uncertain='# 不确定片段与原因\n\n'
    from .pipeline import stamp
    for t in stats['uncertain']:
        uncertain+=f"## {t['video_id']} [{stamp(t['start'])}]\n\n来源：{t['url']}\n\n原文：{t['text']}\n\n原因：{t['reason']}\n\n"
    (out/'transcripts.md').write_text(md,encoding='utf8')
    (out/'uncertain.md').write_text(uncertain,encoding='utf8')
    page='<!doctype html><html lang="zh"><meta charset="utf-8"><title>'+html.escape(title)+'</title><style>body{font:16px/1.7 system-ui;max-width:1100px;margin:40px auto}td{padding:12px;border-bottom:1px solid #ddd}</style><h1>'+html.escape(title)+'</h1><p>'+label+'；全文语义复核不等于音频听校。</p><p><a href="transcripts.md">合订稿</a> · <a href="uncertain.md">不确定及原因</a> · <a href="status.json">统计</a> · <a href="pending.json">未完成清单</a> · <a href="manifest.json">来源及文件清单</a></p><table>'+''.join(table)+'</table></html>'
    (out/'index.html').write_text(page,encoding='utf8')
    for name,data in [('status',summary),('pending',pending),('corpus',portable),('uncertain',stats['uncertain']),('manifest',manifest)]:write_json(out/(name+'.json'),data)
    names=['transcripts.md','uncertain.md','index.html','status.json','pending.json','corpus.json','uncertain.json','manifest.json']
    temp=out/'corpus.zip.tmp'
    with ZipFile(temp,'w',ZIP_DEFLATED) as z:
        for n in names:z.write(out/n,'exports/'+n)
        for _,_,files in inputs:
            for p in files:z.write(p,p.relative_to(c.root).as_posix())
        for p in accepted:z.write(p,p.relative_to(c.root).as_posix())
        z.write(c.root/'analysis/verification.json','analysis/verification.json')
    with ZipFile(temp) as z:
        if z.testzip():raise ValueError('Archive CRC verification failed')
        members={('exports/'+n):out/n for n in names}
        members.update({p.relative_to(c.root).as_posix():p for _,_,files in inputs for p in files})
        members.update({p.relative_to(c.root).as_posix():p for p in accepted})
        members['analysis/verification.json']=c.root/'analysis/verification.json'
        if set(z.namelist())!=set(members):raise ValueError('Archive inventory mismatch')
        for name,p in members.items():
            if z.read(name)!=p.read_bytes():raise ValueError('Archive mismatch: '+name)
    os.replace(temp,out/'corpus.zip');names.append('corpus.zip')
    write_json(marker,{'input_hash':key,'outputs':{n:digest(out/n) for n in names}})
    return {'unchanged':False,'directory':str(out/'index.html'),'videos':len(inputs),'delivery':label}
