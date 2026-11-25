import json
from corpus.db import Corpus
from corpus.speakers import char_times


def test_batch_checkpoint_word_alignment(tmp_path):
    c=Corpus(tmp_path)
    vid=c.add('https://www.douyin.com/video/1234567890123456789','测试')
    segment={'start':0,'end':4,'text':'你好','words':[{'word':'你','start':1,'end':2},{'word':'好','start':3,'end':4}]}
    c.set_segments(vid,[segment])
    (tmp_path/'data/transcripts'/f'{vid}.checkpoint.jsonl').write_text(json.dumps({'segments':[segment],'complete':True})+'\n',encoding='utf8')
    starts,ends,methods=char_times(c,vid,[segment])
    assert starts==[1,3] and ends==[2,4]
    assert methods==['asr_word_alignment','asr_word_alignment']
