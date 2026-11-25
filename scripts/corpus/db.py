import csv
import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qs


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(url):
    p = urlsplit(url.strip())
    if p.hostname in ('douyin.com', 'www.douyin.com') and re.fullmatch(r'/video/\d{19}/?', p.path):
        return 'https://www.douyin.com' + p.path.rstrip('/')
    if p.hostname in ('youtube.com', 'www.youtube.com') and p.path == '/watch' and parse_qs(p.query).get('v'):
        return 'https://www.youtube.com/watch?v=' + parse_qs(p.query)['v'][0]
    return urlunsplit((p.scheme, p.netloc, p.path.rstrip('/'), p.query, ''))


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


class Corpus:
    def __init__(self, root):
        self.root = Path(root).resolve()
        for d in ('sources', 'database', 'logs', 'data/metadata', 'data/videos', 'data/audio',
                  'data/subtitles', 'data/transcripts', 'analysis/topics'):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / 'database/corpus.sqlite', timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS videos(
          id TEXT PRIMARY KEY, url TEXT UNIQUE NOT NULL, title TEXT, platform TEXT,
          author TEXT, account_url TEXT, published TEXT, duration REAL, description TEXT,
          content_type TEXT DEFAULT 'unknown', original_status TEXT DEFAULT 'unknown',
          full_video TEXT DEFAULT 'unknown', subtitle_status TEXT DEFAULT 'unknown',
          in_profile INTEGER DEFAULT 0, discovery_evidence TEXT, identity_status TEXT,
          status TEXT DEFAULT 'discovered', local_path TEXT, media_sha256 TEXT,
          duplicate_of TEXT REFERENCES videos(id), transcript_path TEXT,
          transcript_method TEXT, quality TEXT, last_error TEXT,
          created_at TEXT, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS links(
          url TEXT PRIMARY KEY, video_id TEXT REFERENCES videos(id), evidence TEXT, discovered_at TEXT);
        CREATE TABLE IF NOT EXISTS events(
          id INTEGER PRIMARY KEY, video_id TEXT, stage TEXT, status TEXT, detail TEXT, at TEXT);
        CREATE TABLE IF NOT EXISTS segments(
          id INTEGER PRIMARY KEY, video_id TEXT REFERENCES videos(id), start REAL, end REAL,
          speaker TEXT DEFAULT '未知', text TEXT NOT NULL, uncertain INTEGER DEFAULT 0,
          confidence REAL, topics TEXT DEFAULT '', UNIQUE(video_id,start,end,text));
        CREATE VIRTUAL TABLE IF NOT EXISTS segment_fts USING fts5(text, tokenize='trigram');
        CREATE TABLE IF NOT EXISTS claims(
          id TEXT PRIMARY KEY, video_id TEXT REFERENCES videos(id), segment_id INTEGER REFERENCES segments(id),
          proposition TEXT, quote TEXT, attribution TEXT, level TEXT, rationale TEXT,
          assumptions TEXT, scenario TEXT, advice TEXT, case_text TEXT, review_status TEXT DEFAULT 'pending');
        ''')
        # Keep word timestamps and ASR diagnostics, including after file repair.
        if 'details' not in {r['name'] for r in self.db.execute('PRAGMA table_info(segments)')}:
            self.db.execute("ALTER TABLE segments ADD COLUMN details TEXT DEFAULT '{}'")
            self.db.commit()

    @contextmanager
    def atomic(self):
        name='corpus_'+str(id(object()))
        self.db.execute('SAVEPOINT '+name)
        try:
            yield
        except BaseException:
            self.db.execute('ROLLBACK TO '+name)
            self.db.execute('RELEASE '+name)
            raise
        else:
            self.db.execute('RELEASE '+name)

    def add(self, url, title='', **metadata):
        normalized = canonical(url)
        p = urlsplit(normalized)
        vid = p.path.rsplit('/', 1)[-1] if p.hostname == 'www.douyin.com' and re.fullmatch(r'/video/\d{19}', p.path) else hashlib.sha256(normalized.encode()).hexdigest()[:20]
        fields = {'title': title, 'platform': urlsplit(url).hostname, **metadata}
        with self.atomic():
            self.db.execute('INSERT OR IGNORE INTO videos(id,url,created_at,updated_at) VALUES(?,?,?,?)',
                            (vid, normalized, now(), now()))
            self.db.execute('INSERT OR IGNORE INTO links VALUES(?,?,?,?)',
                            (url, vid, fields.get('discovery_evidence', ''), now()))
            values = {k: v for k, v in fields.items() if v is not None and v != ''}
            allowed = {r['name'] for r in self.db.execute('PRAGMA table_info(videos)')} - {'id', 'url', 'created_at'}
            if not set(values) <= allowed: raise ValueError('Unknown metadata fields')
            current = self.video(vid)
            values = {k:v for k,v in values.items() if current[k] != v}
            if values:
                values['updated_at'] = now()
                self.db.execute('UPDATE videos SET '+','.join(f'{k}=?' for k in values)+' WHERE id=?', [*values.values(),vid])
        return vid

    def update(self, vid, **fields):
        allowed = {r['name'] for r in self.db.execute('PRAGMA table_info(videos)')} - {'id', 'url', 'created_at'}
        if not set(fields) <= allowed:
            raise ValueError('Unknown metadata fields')
        fields['updated_at'] = now()
        with self.db:
            self.db.execute('UPDATE videos SET ' + ','.join(f'{k}=?' for k in fields) + ' WHERE id=?',
                            [*fields.values(), vid])

    def event(self, vid, stage, status, detail=''):
        with self.db:
            self.db.execute('INSERT INTO events(video_id,stage,status,detail,at) VALUES(?,?,?,?,?)',
                            (vid, stage, status, detail, now()))
        with (self.root / 'logs/events.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(dict(video_id=vid,stage=stage,status=status,detail=detail,at=now()),ensure_ascii=False)+'\n')

    def video(self, vid):
        row = self.db.execute('SELECT * FROM videos WHERE id=?', (vid,)).fetchone()
        if row is None:
            raise ValueError(f'Unknown video: {vid}')
        return row

    def set_segments(self, vid, segments):
        segments = list(segments)
        self.video(vid)
        if not segments: raise ValueError('Cannot replace a transcript with empty segments')
        previous = 0
        for s in segments:
            if not isinstance(s['text'], str) or not s['text'].strip() or not all(isinstance(s[k], (int,float)) and math.isfinite(s[k]) for k in ('start','end')) or s['start'] < previous or s['end'] < s['start']:
                raise ValueError('Empty text or invalid/nonmonotonic timestamps')
            previous = s['start']
        with self.db:
            old = self.db.execute('SELECT id FROM segments WHERE video_id=?', (vid,)).fetchall()
            # Preserve research decisions: explicit archival is required before replacement.
            reviewed = self.db.execute("SELECT count(*) FROM claims WHERE video_id=? AND review_status<>'pending'", (vid,)).fetchone()[0]
            if reviewed:
                raise ValueError('Archive reviewed claims before replacing their source transcript')
            self.db.execute('DELETE FROM claims WHERE video_id=?', (vid,))
            for row in old:
                self.db.execute('DELETE FROM segment_fts WHERE rowid=?', (row['id'],))
            self.db.execute('DELETE FROM segments WHERE video_id=?', (vid,))
            for s in segments:
                details = {k:s[k] for k in ('words','avg_logprob','no_speech_prob','compression_ratio') if k in s}
                if not details and s.get('details'): details = json.loads(s['details'])
                cur = self.db.execute('INSERT INTO segments(video_id,start,end,speaker,text,uncertain,confidence,topics,details) VALUES(?,?,?,?,?,?,?,?,?)',
                    (vid,s['start'],s['end'],s.get('speaker','未知'),s['text'],int(s.get('uncertain',False)),s.get('confidence'),s.get('topics',''),json.dumps(details,ensure_ascii=False)))
                self.db.execute('INSERT INTO segment_fts(rowid,text) VALUES(?,?)', (cur.lastrowid,s['text']))

    def search(self, keyword, platform=None, speaker=None, topic=None, after=None, before=None, limit=50):
        # Literal LIKE supports Chinese one/two-character terms; FTS trigram supports longer phrases.
        escaped = keyword.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
        where = ["s.text LIKE ? ESCAPE '\\'"]
        values = ['%' + escaped + '%']
        for field, value, op in [('v.platform',platform,'='),('s.speaker',speaker,'='),('v.published',after,'>='),('v.published',before,'<=')]:
            if value:
                where.append(field+op+'?'); values.append(value)
        if topic:
            where.append('s.topics LIKE ?'); values.append('%'+topic+'%')
        return [dict(r) for r in self.db.execute('SELECT s.*,v.url,v.title,v.published,v.platform FROM segments s JOIN videos v ON v.id=s.video_id WHERE '
                                                +' AND '.join(where)+' ORDER BY v.published,s.start LIMIT ?', [*values,limit])]

    def export(self):
        rows = self.db.execute('SELECT * FROM videos ORDER BY id DESC').fetchall()
        with (self.root/'sources/video_index.csv').open('w',newline='',encoding='utf-8-sig') as f:
            if rows:
                w = csv.DictWriter(f,fieldnames=rows[0].keys()); w.writeheader(); w.writerows(map(dict,rows))
        with (self.root/'data/metadata/videos.jsonl').open('w',encoding='utf-8') as f:
            for row in rows:
                f.write(json.dumps(dict(row),ensure_ascii=False)+'\n')
        stats = {'video_records':len(rows),'profile_records':sum(r['in_profile'] for r in rows),
                 'media_acquired':sum(bool(r['local_path']) for r in rows),
                 'transcripts_completed':sum(bool(r['transcript_path']) for r in rows),
                 'transcribed_media_seconds':sum((r['duration'] or 0) for r in rows if r['transcript_path']),
                 'exact_media_duplicates':sum(bool(r['duplicate_of']) for r in rows),
                 'source_links':self.db.execute('SELECT count(*) FROM links').fetchone()[0],
                 'platforms':{r[0]:r[1] for r in self.db.execute('SELECT platform,count(*) FROM videos GROUP BY platform')},
                 'states':{r[0]:r[1] for r in self.db.execute('SELECT status,count(*) FROM videos GROUP BY status')},
                 'generated_at':now()}
        (self.root/'analysis/status.json').write_text(json.dumps(stats,ensure_ascii=False,indent=2),encoding='utf-8')
        pending=[dict(r) for r in rows if not r['transcript_path'] or r['quality'] != 'human_verified']
        (self.root/'analysis/manual_review.json').write_text(json.dumps(pending,ensure_ascii=False,indent=2),encoding='utf-8')
        failures=[dict(r) for r in self.db.execute("SELECT * FROM events WHERE status IN ('failed','blocked')")]
        (self.root/'logs/failures.json').write_text(json.dumps(failures,ensure_ascii=False,indent=2),encoding='utf-8')
        return stats
