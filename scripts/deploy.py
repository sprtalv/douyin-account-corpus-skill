"""Install only this skill's source files; preserve an existing installation as backup."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import uuid
from datetime import datetime
from zipfile import ZipFile,ZIP_DEFLATED


def files(root):
    for p in root.rglob('*'):
        if not p.is_file():continue
        rel=p.relative_to(root)
        if any(x.startswith('.') or x in ('__pycache__','dist') for x in rel.parts):continue
        if p.suffix in ('.pyc','.pyo'):continue
        if rel.parts[0] not in ('scripts','references','agents','tests') and rel.as_posix() not in ('SKILL.md','requirements.txt','requirements-test.txt'):
            continue
        yield p,rel


def main():
    p=argparse.ArgumentParser();p.add_argument('--target',type=Path);p.add_argument('--archive',type=Path);p.add_argument('--package-only',action='store_true')
    a=p.parse_args();src=Path(__file__).resolve().parents[1]
    entries=list(files(src));checks={r.as_posix():hashlib.sha256(f.read_bytes()).hexdigest() for f,r in entries}
    if not entries or 'SKILL.md' not in checks:raise ValueError('Invalid source skill')
    result={'files':len(entries)}
    if a.archive:
        archive=a.archive.resolve();archive.parent.mkdir(parents=True,exist_ok=True)
        with ZipFile(archive,'w',ZIP_DEFLATED) as z:
            for f,r in entries:z.write(f,'douyin-account-corpus/'+r.as_posix())
            z.writestr('douyin-account-corpus/package-manifest.json',json.dumps(checks,ensure_ascii=False,indent=2))
        with ZipFile(archive) as z:
            if z.testzip():raise ValueError('ZIP corrupt')
            for rel,h in checks.items():
                if hashlib.sha256(z.read('douyin-account-corpus/'+rel)).hexdigest()!=h:raise ValueError('ZIP content mismatch')
        result['archive']=str(archive)
    if not a.package_only:
        home=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))
        target=(a.target or home/'skills/douyin-account-corpus').resolve()
        if target==src or target in src.parents or src in target.parents:raise ValueError('Install source and target must be separate directories')
        target.parent.mkdir(parents=True,exist_ok=True)
        stage=target.parent/('.douyin-account-corpus-stage-'+uuid.uuid4().hex)
        stage.mkdir()
        for f,r in entries:
            out=stage/r;out.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(f,out)
        for rel,h in checks.items():
            if hashlib.sha256((stage/rel).read_bytes()).hexdigest()!=h:raise ValueError('Staged content mismatch')
        (stage/'package-manifest.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf8')
        backup=None
        if target.exists():
            backup=home/'skill-backups'/('douyin-account-corpus-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6])
            backup.parent.mkdir(parents=True,exist_ok=True);shutil.move(str(target),str(backup))
        try:stage.rename(target)
        except Exception:
            if backup and not target.exists():shutil.move(str(backup),str(target))
            raise
        result.update(installed=str(target),backup=str(backup) if backup else None)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
