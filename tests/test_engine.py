import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from corpus.db import Corpus, canonical, digest
from corpus.ingest import import_manifest
from corpus.pipeline import register_media, transcribe, render, _model_source

class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.c=Corpus(self.root)
        self.vid=self.c.add('https://www.douyin.com/video/1234567890123456789','test')
    def tearDown(self):
        self.c.db.close();self.tmp.cleanup()
    def media(self,vid=None,data=b'audio'):
        p=self.root/('input'+str(vid or self.vid)+'.wav');p.write_bytes(data)
        with patch('corpus.pipeline.probe',return_value=100): register_media(self.c,vid or self.vid,p)
        return p
    def model(self,batch=2):
        import yaml
        p=self.root/'local-model';p.mkdir(exist_ok=True)
        for f in ('model.bin','config.json','tokenizer.json'): (p/f).write_bytes(b'fake test only')
        (self.root/'config.yaml').write_text(yaml.safe_dump({'asr':{'model':str(p),'language':'zh','device':'cpu','compute_type':'int8','beam_size':5,'batch_size':batch}}),encoding='utf8')
        return p
    def completed(self):
        self.media();self.c.set_segments(self.vid,[dict(start=0,end=1,text='完整文字',words=[dict(start=0,end=1,word='完整文字',probability=.9)])])
        self.c.update(self.vid,transcript_method='test',quality='machine_unverified');render(self.c,self.vid)
    def test_safe_id_and_host(self):
        self.assertNotEqual(canonical('https://evildouyin.com/video/1234567890123456789'),'https://www.douyin.com/video/1234567890123456789')
        vid=self.c.add('https://example.com/video/../../bad?x=1')
        self.assertRegex(vid,r'^[a-f0-9]{20}$')
    def test_idempotent_registration_preserves_completion(self):
        self.completed();row=dict(self.c.video(self.vid));p=Path(row['local_path'])
        with patch('corpus.pipeline.probe',side_effect=AssertionError('no repeated probe')):
            self.assertFalse(register_media(self.c,self.vid,p))
        self.assertEqual(dict(self.c.video(self.vid)),row)
    def test_hash_checked_before_completed_return(self):
        self.completed();Path(self.c.video(self.vid)['local_path']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'changed'):transcribe(self.c,self.vid)
    def test_repairs_missing_exports_preserves_words(self):
        self.completed();p=Path(self.c.video(self.vid)['transcript_path']);p.unlink();p.with_suffix('.json').unlink()
        transcribe(self.c,self.vid)
        self.assertEqual(json.loads(p.with_suffix('.json').read_text(encoding='utf8'))['segments'][0]['words'][0]['word'],'完整文字')
    def test_tamper_json_rejected(self):
        self.completed();p=Path(self.c.video(self.vid)['transcript_path']).with_suffix('.json');j=json.loads(p.read_text(encoding='utf8'));j['segments'][0]['text']='changed';p.write_text(json.dumps(j),encoding='utf8')
        with self.assertRaisesRegex(ValueError,'mismatch'): transcribe(self.c,self.vid)
    def test_duplicate_never_creates_cycle(self):
        self.media();other=self.c.add('https://www.douyin.com/video/2234567890123456789','other');self.media(other)
        self.media();self.assertIsNone(self.c.video(self.vid)['duplicate_of']);self.assertEqual(self.c.video(other)['duplicate_of'],self.vid)
    def test_duplicate_reuses_whichever_completed(self):
        self.media();other=self.c.add('https://www.douyin.com/video/2234567890123456789','other');self.media(other)
        self.c.set_segments(other,[dict(start=0,end=1,text='文字')]);self.c.update(other,quality='machine_unverified');render(self.c,other)
        transcribe(self.c,self.vid);self.assertEqual(self.c.video(self.vid)['transcript_method'],'exact_media_reuse:'+other)
    def test_nonfinite_timestamp_rejected(self):
        for value in (float('nan'),float('inf'),-1):
            with self.assertRaises(ValueError):self.c.set_segments(self.vid,[dict(start=value,end=2,text='text')])
    def test_local_model_required(self):
        with self.assertRaisesRegex(ValueError,'local ASR'): _model_source(self.c,{'model':'large-v3-turbo'})
        p=self.model();self.assertEqual(_model_source(self.c,{'model':str(p)})[0],str(p))
    def test_manifest_atomic(self):
        p=self.root/'manifest.json';p.write_text(json.dumps({'account':{'name':'name','url':'https://www.douyin.com/user/id'},'observed_at':'today','evidence':'visible','videos':[{'id':'2234567890123456789','title':'a'},{'id':'3234567890123456789','title':'b'}]}),encoding='utf8')
        original=self.c.add
        def add(url,*args,**kwargs):
            if '3234567890123456789' in url:raise RuntimeError('simulated interruption')
            return original(url,*args,**kwargs)
        with patch.object(self.c,'add',side_effect=add):
            with self.assertRaises(RuntimeError):import_manifest(self.c,p)
        self.assertEqual(self.c.db.execute('SELECT count(*) FROM videos').fetchone()[0],1)
    def test_batch_resume_commits_whole_groups_and_no_download(self):
        import numpy as np
        self.media();self.model();calls=[];alignment_states=[];state={'fail':True}
        class FakeModel:
            def __init__(self,*args,**kwargs):
                if kwargs.get('local_files_only') is not True:raise AssertionError('network allowed')
        class Batch:
            def __init__(self,model): self.last_speech_timestamp=0.0
            def transcribe(self,samples,**kwargs):
                clips=kwargs['clip_timestamps'];calls.append(clips)
                alignment_states.append(self.last_speech_timestamp)
                def segments():
                    self.last_speech_timestamp+=10
                    for clip in clips:
                        if state['fail'] and clip['start']>50:
                            state['fail']=False;raise RuntimeError('interruption mid batch')
                        yield SimpleNamespace(start=clip['start'],end=clip['end'],text=str(clip['start']),avg_logprob=-.1,no_speech_prob=.1,compression_ratio=1,words=[])
                    self.last_speech_timestamp=0.0
                return segments(),SimpleNamespace(duration=100)
        fake=SimpleNamespace(WhisperModel=FakeModel,BatchedInferencePipeline=Batch)
        decoded=SimpleNamespace(stdout=np.zeros(16000*100,dtype=np.int16).tobytes())
        with patch.dict(sys.modules,{'faster_whisper':fake}),patch('corpus.pipeline.subprocess.run',return_value=decoded):
            with self.assertRaises(RuntimeError):transcribe(self.c,self.vid)
            checkpoint=self.root/'data/transcripts'/f'{self.vid}.checkpoint.jsonl'
            first=json.loads(checkpoint.read_text(encoding='utf8'));self.assertEqual(len(first['segments']),2)
            with checkpoint.open('ab') as f:f.write(b'{torn')
            transcribe(self.c,self.vid)
        self.assertEqual(calls[2][0]['start'],first['resume_end'])
        self.assertEqual(first['last_speech_timestamp'],10.0)
        self.assertEqual(alignment_states,[0.0,10.0,10.0])
        segs=self.c.db.execute('SELECT start,end,text FROM segments WHERE video_id=? ORDER BY start',(self.vid,)).fetchall()
        self.assertEqual(len(segs),4);self.assertEqual(segs[-1]['end'],100)
        self.assertTrue(all(segs[i]['end']==segs[i+1]['start'] for i in range(len(segs)-1)))

if __name__=='__main__':unittest.main()
