import hashlib
import json

import pytest

from export_text import export


def fixture(tmp_path):
    source = tmp_path/'source.json'
    source.write_text(json.dumps({'video': {'id': '7000000000000000001', 'title': '测试/标题'},
                                 'segments': [{'start': 0, 'end': 1, 'text': '你好。'},
                                              {'start': 1, 'end': 2, 'text': '好！'}]}), encoding='utf-8')
    manifest = tmp_path/'selection.json'
    spec = {'entries': [{'author': '博主甲', 'id': '7000000000000000001', 'source': source.name,
                         'issues': ['内部疑点'], 'selection_reason': '原稿'}]}
    manifest.write_text(json.dumps(spec), encoding='utf-8')
    return manifest, spec, source


def test_plain_export_preserves_text_and_keeps_issues_internal(tmp_path):
    manifest, _, source = fixture(tmp_path)
    before = source.read_bytes()
    output, audit = tmp_path/'out', tmp_path/'audit.json'
    result = export(manifest, output, audit)
    assert result['files'] == 1
    files = list(output.rglob('*'))
    text_files = [p for p in files if p.is_file()]
    assert len(text_files) == 1 and text_files[0].suffix == '.txt'
    text = text_files[0].read_text(encoding='utf-8-sig')
    assert '你好。好！' in text and '00:00' not in text and '内部疑点' not in text
    assert json.loads(audit.read_text(encoding='utf-8'))['entries'][0]['issues'] == ['内部疑点']
    assert source.read_bytes() == before
    with pytest.raises(ValueError, match='already exists'):
        export(manifest, output, audit)


def test_labels_require_exact_source_and_stale_annotation_is_rejected(tmp_path):
    manifest, spec, _ = fixture(tmp_path)
    annotation = tmp_path/'annotation.json'
    ann = {'video_id': '7000000000000000001',
           'source_sha256': hashlib.sha256('你好。好！'.encode()).hexdigest(),
           'turns': [{'speaker': '主播', 'text': '你好。'}, {'speaker': '连麦者', 'text': '好！'}]}
    annotation.write_text(json.dumps(ann), encoding='utf-8')
    spec['entries'][0]['annotation'] = annotation.name
    manifest.write_text(json.dumps(spec), encoding='utf-8')
    export(manifest, tmp_path/'out', tmp_path/'audit.json')
    text = next((tmp_path/'out').rglob('*.txt')).read_text(encoding='utf-8-sig')
    assert '主播：你好。\n\n连麦者：好！' in text
    ann['turns'][1]['text'] = '改写'
    annotation.write_text(json.dumps(ann), encoding='utf-8')
    with pytest.raises(ValueError, match='changes source'):
        export(manifest, tmp_path/'rejected', tmp_path/'audit2.json')
    assert not (tmp_path/'rejected').exists()


def test_reject_duplicate_and_internal_audit_in_delivery(tmp_path):
    manifest, spec, _ = fixture(tmp_path)
    with pytest.raises(ValueError, match='outside'):
        export(manifest, tmp_path/'out', tmp_path/'out/audit.json')
    spec['entries'].append(spec['entries'][0])
    manifest.write_text(json.dumps(spec), encoding='utf-8')
    with pytest.raises(ValueError, match='Duplicate'):
        export(manifest, tmp_path/'out', tmp_path/'audit.json')
    assert not (tmp_path/'out').exists()
