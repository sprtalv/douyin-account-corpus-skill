"""Offline workflow and delivery regression checks using temporary corpora only."""
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from corpus.db import Corpus
from corpus.speakers import source
from corpus.workflow import apply_reviews, prepare_reviews, read_json, review_current, status, validate_review, verify, write_json
from corpus.delivery import deliver


@pytest.fixture
def corpus(tmp_path):
    c = Corpus(tmp_path)
    write_json(tmp_path / 'project.json', {'name': '测试账号', 'review_policy': 'full'})
    vid = c.add('https://www.douyin.com/video/7000000000000000123', title='原始标题')
    segments = [{'start': 0., 'end': 2., 'text': '你好。'}, {'start': 2., 'end': 4., 'text': '嗯，不知道。'}]
    c.set_segments(vid, segments)
    stem = tmp_path / 'data/transcripts' / vid
    stem.with_suffix('.md').write_text('# 原始标题\n\n你好。嗯，不知道。', encoding='utf8')
    write_json(stem.with_suffix('.json'), {'video': dict(c.video(vid)), 'segments': segments})
    (tmp_path / 'data/subtitles' / (vid + '.srt')).write_text('1\n00:00:00,000 --> 00:00:04,000\n你好。嗯，不知道。\n', encoding='utf8')
    c.update(vid, transcript_path=str(stem.with_suffix('.md')), duration=4)
    yield c
    c.db.close()


def review(c):
    return {'source_sha256': source(c, '7000000000000000123')[2], 'reviewed_full_text': True,
            '_boundaries': [[0, '主播'], [3, '不确定']], 'uncertain_reasons': {'3': '短答缺少可靠身份线索'},
            'review_note': '逐段通读，未回听。'}


def accept(c):
    path = c.root / 'reviews.json'
    write_json(path, {'7000000000000000123': review(c)})
    return apply_reviews(c, path)


def test_full_review_is_cached_and_preserves_text(corpus):
    c = corpus
    assert prepare_reviews(c)['pending'] == 1
    assert accept(c) == {'applied': 1, 'unchanged': 0}
    assert accept(c) == {'applied': 0, 'unchanged': 1}
    assert prepare_reviews(c)['pending'] == 0
    ann = read_json(c.root / 'data/annotated/7000000000000000123.json')
    assert ''.join(t['text'] for t in ann['turns']) == source(c, '7000000000000000123')[1]
    assert status(c)['uncertain'][0]['reason'] == '短答缺少可靠身份线索'
    assert verify(c)['ok']


@pytest.mark.parametrize('change', [
    {'source_sha256': 'stale'}, {'reviewed_full_text': False},
    {'_boundaries': [[True, '主播']]}, {'_boundaries': [[1, '主播']]},
    {'_boundaries': [[0, '主播'], [0, '不确定']]}, {'_boundaries': [[0]]},
    {'_boundaries': [[0, 'invented']]}, {'_boundaries': [[0, '主播'], [100, '不确定']]},
    {'uncertain_reasons': {}}, {'uncertain_reasons': {'3': '  '}},
    {'uncertain_reasons': {'3': 9}}, {'uncertain_reasons': {'3': '原因', '4': '无对应片段'}},
    {'review_note': None},
])
def test_invalid_review_rejected(corpus, change):
    item = review(corpus)
    item.update(change)
    with pytest.raises(ValueError):
        validate_review(corpus, '7000000000000000123', item)


def test_all_reviews_prevalidated_before_mutation(corpus):
    path = corpus.root / 'reviews.json'
    write_json(path, {'7000000000000000123': review(corpus), 'unknown': review(corpus)})
    with pytest.raises(ValueError):
        apply_reviews(corpus, path)
    assert not (corpus.root / 'data/annotated/7000000000000000123.json').exists()


@pytest.mark.parametrize('mutation', ['missing_accepted', 'false_attestation', 'role', 'boundary', 'reason'])
def test_current_review_requires_complete_evidence(corpus, mutation):
    accept(corpus)
    path = corpus.root / ('review/accepted/7000000000000000123.json' if mutation in ('missing_accepted', 'false_attestation', 'reason') else 'data/annotated/7000000000000000123.json')
    if mutation == 'missing_accepted':
        path.unlink()
    else:
        item = read_json(path)
        if mutation == 'false_attestation': item['reviewed_full_text'] = False
        if mutation == 'reason': item['uncertain_reasons'] = {}
        if mutation == 'role': item['turns'][0]['speaker'] = '连麦者'
        if mutation == 'boundary': item['turns'][0]['char_end'] = 2
        write_json(path, item)
    assert not review_current(corpus, '7000000000000000123')
    assert not verify(corpus)['ok']
    with pytest.raises(ValueError): deliver(corpus)


def test_incomplete_refuses_final_but_partial_is_explicit(corpus):
    with pytest.raises(ValueError, match='incomplete'): deliver(corpus)
    result = deliver(corpus, allow_partial=True)
    assert result['delivery'] == '部分交付'
    assert len(read_json(corpus.root / 'exports/pending.json')) == 1
    accept(corpus)
    corpus.add('https://www.douyin.com/video/7000000000000000456')
    with pytest.raises(ValueError, match='incomplete'): deliver(corpus)


def test_directory_zip_manifest_and_cache(corpus):
    c = corpus
    accept(c)
    assert not deliver(c)['unchanged']
    assert deliver(c)['unchanged']
    out = c.root / 'exports'
    manifest = read_json(out / 'manifest.json')
    assert manifest[0]['url'] == c.video('7000000000000000123')['url']
    assert manifest[0]['source_links'][0]['video_id'] == '7000000000000000123'
    with ZipFile(out / 'corpus.zip') as archive:
        for name in archive.namelist():
            assert archive.read(name) == (c.root / name).read_bytes()
    # Output corruption must invalidate cache and rebuild every delivery artifact.
    (out / 'manifest.json').write_text('{}', encoding='utf8')
    assert not deliver(c)['unchanged']
    assert deliver(c)['unchanged']
    # Source aliases and review records are inputs even if transcript bytes stay fixed.
    with c.db:
        c.db.execute('INSERT INTO links VALUES(?,?,?,?)', ('https://v.douyin.com/example', '7000000000000000123', 'public share', 'today'))
    assert not deliver(c)['unchanged']
    accepted = c.root / 'review/accepted/7000000000000000123.json'
    item = read_json(accepted); item['review_note'] += '额外记录'
    write_json(accepted, item)
    assert not deliver(c)['unchanged']
    assert deliver(c)['unchanged']


def test_malformed_evidence_is_reported(corpus):
    (corpus.root / 'data/transcripts/7000000000000000123.json').write_text('{', encoding='utf8')
    assert not verify(corpus)['ok']
    with pytest.raises(ValueError, match='Integrity'): deliver(corpus)



def test_source_change_invalidates_review(corpus):
    accept(corpus)
    with corpus.db:
        corpus.db.execute("UPDATE segments SET text=text || '改' WHERE video_id=?", ('7000000000000000123',))
    assert not review_current(corpus, '7000000000000000123')
    assert not verify(corpus)['ok']
    with pytest.raises(ValueError): apply_reviews(corpus, corpus.root / 'reviews.json')


def test_transcript_timestamp_drift_rejected(corpus):
    path = corpus.root / 'data/transcripts/7000000000000000123.json'
    data = read_json(path)
    data['segments'][0]['end'] = 1.5
    write_json(path, data)
    assert not verify(corpus)['ok']
