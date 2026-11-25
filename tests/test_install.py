import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile


def test_portable_package_and_backup_install(tmp_path):
    script=Path(__file__).resolve().parents[1]/'scripts/deploy.py'
    target=tmp_path/'home/skills/douyin-account-corpus'
    target.mkdir(parents=True);(target/'SKILL.md').write_text('old skill',encoding='utf8')
    archive=tmp_path/'skill.zip'
    env={**os.environ,'CODEX_HOME':str(tmp_path/'home'),'PYTHONUTF8':'1'}
    r=subprocess.run([sys.executable,str(script),'--target',str(target),'--archive',str(archive)],env=env,capture_output=True,text=True,encoding='utf8')
    assert r.returncode==0,r.stderr
    result=json.loads(r.stdout)
    assert (Path(result['backup'])/'SKILL.md').read_text(encoding='utf8')=='old skill'
    assert (target/'scripts/run.py').exists()
    with ZipFile(archive) as z:
        assert z.testzip() is None
        assert all('.venv' not in n and '__pycache__' not in n for n in z.namelist())
        manifest=json.loads(z.read('douyin-account-corpus/package-manifest.json'))
        for name,sha in manifest.items():
            assert hashlib.sha256(z.read('douyin-account-corpus/'+name)).hexdigest()==sha
            assert hashlib.sha256((target/name).read_bytes()).hexdigest()==sha
