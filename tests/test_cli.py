"""Offline CLI contracts. Every corpus and input lives under pytest tmp_path."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

CLI = Path(__file__).resolve().parents[1] / 'scripts' / 'run.py'
VID = '1234567890123456789'
ACCOUNT = 'https://www.douyin.com/user/test-account'


def cli(root, *args, ok=True):
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    result = subprocess.run([sys.executable, str(CLI), '--root', str(root), *map(str, args)],
                            capture_output=True, text=True, encoding='utf-8', env=env)
    if ok:
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    assert result.returncode != 0, result.stdout
    return result


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    return path


@pytest.fixture
def corpus(tmp_path):
    root = tmp_path / '全新账号'
    cli(root, 'init', '--name', '临时测试账号', '--account', ACCOUNT, '--device', 'cpu')
    manifest = write_json(tmp_path / 'inventory.json', {
        'account': {'name': '临时测试账号', 'url': ACCOUNT},
        'observed_at': '2026-11-17', 'evidence': 'Synthetic test fixture',
        'videos': [{'id': VID, 'title': '合成测试对话'}],
    })
    assert cli(root, 'import-index', manifest)['new'] == 1
    return root, manifest


def subtitles(tmp_path):
    path = tmp_path / 'source.srt'
    path.write_text('1\n00:00:00,000 --> 00:00:02,000\n你好，请介绍情况。我来咨询。\n\n'
                    '2\n00:00:02,000 --> 00:00:03,500\n嗯。\n', encoding='utf-8')
    return path


def test_full_review_export_is_lossless_and_cached(corpus, tmp_path):
    root, manifest = corpus
    assert cli(root, 'import-index', manifest)['existing'] == 1
    assert cli(root, 'status')['videos'] == 1
    sub = subtitles(tmp_path)
    assert cli(root, 'import-subtitles', VID, sub)['segments'] == 2
    original = {p: p.read_bytes() for folder in ('data/transcripts', 'data/subtitles')
                for p in (root / folder).glob('*') if p.is_file()}
    assert cli(root, 'import-subtitles', VID, sub)['unchanged'] is True
    cli(root, 'export', ok=False)
    assert cli(root, 'review-prepare')['pending'] == 1
    packet = json.loads((root / 'review/packets' / f'{VID}.json').read_text(encoding='utf-8'))
    raw = ''.join(segment['text'] for segment in packet['segments'])
    assert packet['source_sha256'] == hashlib.sha256(raw.encode()).hexdigest()
    review = {VID: {'source_sha256': packet['source_sha256'], 'reviewed_full_text': True,
                    '_boundaries': [[0, '主播'], [raw.index('我来'), '连麦者'], [raw.index('嗯'), '不确定']],
                    'uncertain_reasons': {str(raw.index('嗯')): '短答缺少声音线索，文字不能确定归属。'},
                    'review_note': '已顺序阅读全部两个段落，按问答语义标注，未作音频听校。'}}
    review_file = write_json(tmp_path / 'review.json', review)
    assert cli(root, 'review-apply', review_file)['applied'] == 1
    assert cli(root, 'review-apply', review_file)['unchanged'] == 1
    assert json.loads((root / 'review/accepted' / f'{VID}.json').read_text(encoding='utf-8')) == review[VID]
    assert cli(root, 'verify')['ok'] is True
    state = cli(root, 'status')
    assert state['semantic_reviewed'] == 1 and state['audio_verified'] == 0
    assert cli(root, 'export')['unchanged'] is False
    outputs = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (root / 'exports').iterdir()}
    assert cli(root, 'export')['unchanged'] is True
    assert all((p.read_bytes(), p.stat().st_mtime_ns) == data for p, data in outputs.items())
    assert all(p.read_bytes() == data for p, data in original.items())
    ann = json.loads((root / 'data/annotated' / f'{VID}.json').read_text(encoding='utf-8'))
    assert ''.join(turn['text'] for turn in ann['turns']) == raw
    assert cli(root, 'search', '咨询', '--speaker', '连麦者')
    sub.write_text(sub.read_text(encoding='utf-8').replace('咨询', '修改'), encoding='utf-8')
    cli(root, 'import-subtitles', VID, sub, ok=False)
    assert all(p.read_bytes() == data for p, data in original.items())


def test_reject_invalid_inputs_and_release_lock(corpus, tmp_path):
    root, manifest = corpus
    data = json.loads(manifest.read_text(encoding='utf-8'))
    data['account']['url'] = 'https://www.douyin.com/user/another'
    cli(root, 'import-index', write_json(tmp_path / 'wrong.json', data), ok=False)
    data['account']['url'] = ACCOUNT
    data['videos'].append(dict(data['videos'][0]))
    cli(root, 'import-index', write_json(tmp_path / 'duplicates.json', data), ok=False)
    cli(root, 'import-subtitles', '0000000000000000000', subtitles(tmp_path), ok=False)
    cli(root, 'init', '--name', 'Overwrite', '--account', ACCOUNT, ok=False)
    assert cli(root, 'status')['videos'] == 1
    assert not (root / 'logs/workflow.lock').exists()


@pytest.mark.parametrize('change', ['hash', 'attestation', 'reason', 'boundary'])
def test_reject_incomplete_reviews(corpus, tmp_path, change):
    root, _ = corpus
    cli(root, 'import-subtitles', VID, subtitles(tmp_path))
    cli(root, 'review-prepare')
    packet = json.loads((root / 'review/packets' / f'{VID}.json').read_text(encoding='utf-8'))
    item = {'source_sha256': packet['source_sha256'], 'reviewed_full_text': True,
            '_boundaries': [[0, '不确定']], 'uncertain_reasons': {'0': '无法确定'}, 'review_note': '阅读全文'}
    if change == 'hash': item['source_sha256'] = 'stale'
    elif change == 'attestation': item['reviewed_full_text'] = False
    elif change == 'reason': item['uncertain_reasons'] = {}
    else: item['_boundaries'] = [[1, '主播']]
    cli(root, 'review-apply', write_json(tmp_path / 'bad-review.json', {VID: item}), ok=False)
    assert cli(root, 'status')['semantic_reviewed'] == 0
    assert not (root / 'review/accepted' / f'{VID}.json').exists()


def test_verify_corruption_returns_nonzero(corpus, tmp_path):
    root, _ = corpus
    cli(root, 'import-subtitles', VID, subtitles(tmp_path))
    (root / 'data/transcripts' / f'{VID}.json').unlink()
    result = cli(root, 'verify', ok=False)
    assert json.loads(result.stdout)['ok'] is False


def test_uninitialized_workspace_rejected(tmp_path):
    root = tmp_path / 'absent'
    cli(root, 'status', ok=False)
    assert not root.exists()


@pytest.mark.parametrize('account', ['https://example.com/user/test', 'https://www.douyin.com/user/'])
def test_invalid_init_does_not_create_corpus(tmp_path, account):
    root = tmp_path / 'invalid'
    cli(root, 'init', '--name', 'Test', '--account', account, ok=False)
    assert not root.exists()


@pytest.mark.parametrize('command', ['run', 'review-prepare', 'search'])
def test_negative_limits_rejected(corpus, command):
    root, _ = corpus
    args = [command] + (['word'] if command == 'search' else []) + ['--limit', '-1']
    cli(root, *args, ok=False)


def test_run_missing_media_is_failure(corpus):
    import sqlite3
    root, _ = corpus
    with sqlite3.connect(root / 'database/corpus.sqlite') as db:
        db.execute('UPDATE videos SET local_path=?,media_sha256=? WHERE id=?',
                   (str(root / 'missing.wav'), 'missing', VID))
    result = cli(root, 'run', ok=False)
    assert json.loads(result.stdout)['failures'][0]['video_id'] == VID


def test_run_checks_completed_subtitles_without_loading_model(corpus, tmp_path):
    root, _ = corpus
    cli(root, 'import-subtitles', VID, subtitles(tmp_path))
    result = cli(root, 'run')
    assert result['processed'] == 0
    assert result['pending_reviews'] == 1
    target = root / 'data/transcripts' / f'{VID}.json'
    data = json.loads(target.read_text(encoding='utf-8'))
    data['segments'][0]['text'] = 'tampered'
    write_json(target, data)
    cli(root, 'run', ok=False)
