"""Create a private Python environment; never installs into system Python."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import venv


def main():
    p=argparse.ArgumentParser();p.add_argument('--with-tests',action='store_true');p.add_argument('--gpu-libs',action='store_true')
    a=p.parse_args();root=Path(__file__).resolve().parents[1]
    if sys.version_info<(3,11):raise SystemExit('Python 3.11+ required')
    target=root/'.venv'
    exe=target/('Scripts/python.exe' if sys.platform=='win32' else 'bin/python')
    if not exe.exists():venv.EnvBuilder(with_pip=True).create(target)
    subprocess.run([str(exe),'-m','pip','install','-r',str(root/'requirements.txt')],check=True)
    if a.with_tests:subprocess.run([str(exe),'-m','pip','install','-r',str(root/'requirements-test.txt')],check=True)
    if a.gpu_libs:
        subprocess.run([str(exe),'-m','pip','install','nvidia-cublas-cu12','nvidia-cudnn-cu12'],check=True)
    result=subprocess.run([str(exe),str(root/'scripts/run.py'),'doctor'])
    print(json.dumps({'python':str(exe),'doctor_exit':result.returncode,'model_downloaded':False},ensure_ascii=False))
    return result.returncode


if __name__=='__main__':sys.exit(main())
