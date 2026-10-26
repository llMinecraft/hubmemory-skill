#!/usr/bin/env python3
"""End-to-end decision visibility + index parity; isolated synthetic memories."""
import json
import tempfile
import unittest
from pathlib import Path
from hybrid_query import search
from context import build_payload,render
from decision_state import mark_decision


class DecisionRetrievalTest(unittest.TestCase):
    def test_rejected_demoted_retained_and_historical_cutoff(self):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td);(home/'memory').mkdir()
            records=[{'id':'old','title':'连接池方案','content':'连接池方案 old retry',
                      'created_at':'2026-10-01T10:00:00+08:00','status':'active'},
                     {'id':'new','title':'连接池方案','content':'连接池方案 new backoff',
                      'created_at':'2026-10-01T10:00:00+08:00','status':'active'}]
            source=home/'memory/memories.jsonl';source.write_text(''.join(json.dumps(x)+'\n' for x in records))
            before=source.read_bytes()
            mark_decision(home,'old',decision='rejected',reason='遇到真实重试风暴后撤回',
                          source_session_ids=['source-session-full'],source_event_ids=['source-event'],
                          ts='2026-10-02T10:00:00+08:00')
            sig=[]
            for enabled in (False,True):
                hits=search(home,'连接池方案',as_of='2026-10-03T10:00:00+08:00',use_index=enabled)
                self.assertEqual(hits[0]['id'],'new')
                old=next(x for x in hits if x.get('id')=='old')
                self.assertFalse(old['_decision']['recommended'])
                payload=build_payload('连接池方案',[old],1200)
                self.assertEqual(payload['sufficiency'],'thin')
                self.assertIn('source-event',render(payload))
                sig.append([(h['id'],h['_score']) for h in hits])
            self.assertEqual(sig[0],sig[1])
            history=search(home,'连接池方案',as_of='2026-10-02T10:00:00+08:00',use_index=True)
            self.assertFalse(any(h.get('_decision',{}).get('state')=='rejected' for h in history))
            self.assertEqual(before,source.read_bytes())

if __name__=='__main__':unittest.main()
