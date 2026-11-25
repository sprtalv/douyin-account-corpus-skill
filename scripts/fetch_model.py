"""Explicit opt-in download of public faster-whisper weights; never run by inference."""
import argparse
import json
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model',default='large-v3-turbo',help='Public faster-whisper model name or Hugging Face repository')
    parser.add_argument('--output',type=Path,required=True,help='Destination model directory for subsequent init --model-dir')
    args=parser.parse_args()
    output=args.output.expanduser().resolve()
    from faster_whisper.utils import download_model
    # download_model uses resumable Hugging Face downloads and an allowlist of
    # inference files; this script is the only explicit network opt-in.
    installed=Path(download_model(args.model,output_dir=str(output))).resolve()
    required=('model.bin','config.json','tokenizer.json')
    missing=[name for name in required if not (installed/name).is_file()]
    if missing: raise ValueError('Incomplete model download: '+', '.join(missing))
    print(json.dumps({'model':args.model,'model_dir':str(installed)},ensure_ascii=False,indent=2))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
