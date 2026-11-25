"""Import an observed account inventory without account-specific seed data."""
import json
import re
from pathlib import Path
from urllib.parse import urlsplit


def import_manifest(c, path):
    manifest = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    account = manifest['account']
    profile = account['url']
    p = urlsplit(profile)
    if p.scheme != 'https' or p.netloc != 'www.douyin.com' or not re.fullmatch(r'/user/[^/]+',p.path) or p.query or p.fragment:
        raise ValueError('Expected a canonical Douyin account URL')
    if not all(isinstance(v,str) and v.strip() for v in (account.get('name'),manifest.get('observed_at'),manifest.get('evidence'))):
        raise ValueError('Account name, observation date and evidence are required')
    rows = manifest['videos']
    project=c.root/'project.json'
    if project.exists() and json.loads(project.read_text(encoding='utf-8-sig'))['account_url'] != profile:
        raise ValueError('Manifest belongs to another account')
    if not isinstance(rows, list) or not rows:
        raise ValueError('Expected a nonempty observed video inventory')
    seen = set()
    # Validate the complete inventory before inserting any records.
    for row in rows:
        vid = row['id']
        if not isinstance(vid, str) or not re.fullmatch(r'\d{19}', vid):
            raise ValueError('Video IDs must be 19 digit strings')
        if vid in seen:
            raise ValueError('Duplicate video ID in inventory: '+vid)
        seen.add(vid)
        if not isinstance(row.get('title'), str) or not row['title'].strip():
            raise ValueError('Missing observed title: '+vid)
        if row.get('url', 'https://www.douyin.com/video/'+vid) != 'https://www.douyin.com/video/'+vid:
            raise ValueError('URL and video ID mismatch: '+vid)
        old = c.db.execute('SELECT account_url FROM videos WHERE id=?', (vid,)).fetchone()
        if old and old['account_url'] and old['account_url'] != profile:
            raise ValueError('Existing account attribution conflicts: '+vid)
    new = sum(c.db.execute('SELECT 1 FROM videos WHERE id=?', (r['id'],)).fetchone() is None for r in rows)
    with c.atomic():
        for row in rows:
            c.add('https://www.douyin.com/video/'+row['id'], row['title'],
                  author=account['name'], account_url=profile, in_profile=1,
                  discovery_evidence=str(manifest['observed_at'])+'; '+manifest['evidence'])
    # Inventory observation alone does not establish identity or original authorship.
    return {'observed': len(rows), 'new': new, 'existing': len(rows)-new}
