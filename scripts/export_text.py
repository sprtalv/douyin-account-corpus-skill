"""Export explicitly selected transcripts; keep provenance outside the delivery."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def safe_name(text, limit=70):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', text).strip().rstrip('. ')[:limit].rstrip('. ')
    if not name or name.upper().split('.')[0] in {'CON', 'PRN', 'AUX', 'NUL', *('COM'+str(n) for n in range(1,10)), *('LPT'+str(n) for n in range(1,10))}:
        name = '_' + (name or '未命名')
    return name


def paragraphs(items, labelled):
    """Only add whitespace and existing role labels; never infer punctuation."""
    result = []
    buffer = ''
    role = None
    for item in items:
        text = item['text']
        current = item.get('speaker', '未知') if labelled else None
        if buffer and (current != role or len(buffer) >= 260):
            result.append((role, buffer))
            buffer = ''
        buffer += text
        role = current
        if len(buffer) >= 160 and re.search(r'[。！？.!?][”’"\']?\s*$', buffer):
            result.append((role, buffer))
            buffer = ''
    if buffer:
        result.append((role, buffer))
    return '\n\n'.join((r+'：' if r else '')+t.strip() for r,t in result)


def export(manifest, output, audit):
    manifest, output, audit = map(lambda p: Path(p).resolve(), (manifest, output, audit))
    if output.exists():
        raise ValueError('Output already exists; choose a new snapshot directory')
    if audit == output or output in audit.parents:
        raise ValueError('Audit must stay outside the delivery')
    spec = read(manifest)
    prepared, seen, folders = [], set(), {}
    for entry in spec['entries']:
        author, vid = entry['author'], str(entry['id'])
        if not re.fullmatch(r'\d{19}', vid):
            raise ValueError('Expected 19-digit work ID')
        folder = safe_name(author)
        key = folder.casefold()
        if key in folders and folders[key] != author:
            raise ValueError('Author folder collision')
        folders[key] = author
        if (key,vid) in seen:
            raise ValueError('Duplicate author/work')
        seen.add((key,vid))
        source = Path(entry['source'])
        if not source.is_absolute(): source = manifest.parent/source
        raw = read(source)
        video = raw['video']
        if str(video['id']) != vid:
            raise ValueError('Source ID mismatch')
        segments = raw['segments']
        source_text = ''.join(s['text'] for s in segments)
        if not source_text.strip():
            raise ValueError('Empty transcript must not become a body file')
        items, labelled, annotation_hash = segments, False, None
        if entry.get('annotation'):
            annotation = Path(entry['annotation'])
            if not annotation.is_absolute(): annotation = manifest.parent/annotation
            ann = read(annotation)
            items = ann['turns']
            if str(ann['video_id']) != vid or ''.join(t['text'] for t in items) != source_text:
                raise ValueError('Annotation changes source text or work ID')
            if ann['source_sha256'] != hashlib.sha256(source_text.encode()).hexdigest():
                raise ValueError('Stale annotation')
            labelled, annotation_hash = True, sha(annotation)
        title = video.get('title') or vid
        rel = Path(folder)/(safe_name(title)+'_'+vid+'.txt')
        body = paragraphs(items, labelled)
        # Compare all original non-whitespace characters before adding role labels.
        unlabelled = paragraphs(items, False)
        if re.sub(r'\s+', '', unlabelled) != re.sub(r'\s+', '', source_text):
            raise ValueError('Body text changed')
        content = f"标题：{title}\n博主：{author}\n作品ID：{vid}\n原视频：{video.get('url') or 'https://www.douyin.com/video/'+vid}\n\n{body}\n"
        record = dict(entry, file=rel.as_posix(), source_sha256=sha(source), annotation_sha256=annotation_hash,
                      text_sha256=hashlib.sha256(source_text.encode()).hexdigest(), characters=len(source_text))
        prepared.append((rel, content, record))
    output.mkdir(parents=True)
    for rel, content, record in prepared:
        target = output/rel
        target.parent.mkdir(exist_ok=True)
        target.write_text(content, encoding='utf-8-sig')
        if target.read_text(encoding='utf-8-sig') != content:
            raise ValueError('Output verification failed')
        record['output_sha256'] = sha(target)
    result = {'files': len(prepared), 'accounts': dict(Counter(r['author'] for _,_,r in prepared)),
              'policy': spec.get('policy'), 'excluded': spec.get('excluded', []),
              'entries': [r for _,_,r in prepared]}
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'files': result['files'], 'accounts': result['accounts'], 'output': str(output), 'audit': str(audit)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--audit', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export(args.manifest, args.output, args.audit), ensure_ascii=True, indent=2))
