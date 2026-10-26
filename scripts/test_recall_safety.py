#!/usr/bin/env python3
import copy
import json
import unittest
from privacy import redact
from context import build_payload, render, estimate_tokens


class RecallSafetyTest(unittest.TestCase):
    def test_recognized_credentials_masked_idempotently(self):
        secrets = ['ghp_'+'x'*36, 'sk-'+'a'*30, 'Bearer '+'z'*25,
                   'API_KEY="'+'q'*30+'"', 'password=MySecret!123',
                   'https://user:MySecretPassword@example.org/path',
                   '-----BEGIN RSA PRIVATE KEY-----\nabc123\n-----END RSA PRIVATE KEY-----']
        for text in secrets:
            out = redact(text)
            self.assertNotEqual(out,text)
            self.assertEqual(redact(out),out)
        self.assertEqual(redact('token budget 800; docs/design.md#L12; session-123'),
                         'token budget 800; docs/design.md#L12; session-123')

    def test_summary_and_query_masked_before_clipping_without_source_mutation(self):
        secret='ghp_'+'x'*36
        raw=[{'_source':'session','text':'a'*230+' '+secret,
              'session_id':'s1','event_id':'e1','_path':'/hub/sessions.jsonl',
              '_source_line':7,'_retrieval':{'coverage':1,'query_clean':secret,
                                          'intent':{'raw':secret}}}]
        original=copy.deepcopy(raw)
        for mode in [False,True]:
            payload=build_payload(secret,raw,1200,json_output=mode)
            output=json.dumps(payload) if mode else render(payload)
            self.assertNotIn(secret,output)
            self.assertNotIn('ghp_xxx',output)
            self.assertIn('e1',output)
            self.assertIn('sessions.jsonl',output)
        self.assertEqual(raw,original)

    def test_pending_backlog_does_not_starve_evidence(self):
        pending=[{'kind':'daily','period':'2026-06-%02d'%n,'status':'pending'} for n in range(1,70)]
        raw=[{'_source':'session','text':'项目交付结果','_path':'/hub/sessions.jsonl',
              '_source_line':1,'session_id':'s','event_id':'e','_retrieval':{'coverage':1}}]
        for mode in [False, True]:
            p=build_payload('项目',raw,800,pending,mode)
            self.assertEqual(p['pending_reports_total'],69)
            self.assertEqual(p['pending_reports_omitted'],66)
            self.assertEqual(len(p['results']),1)
            encoded=json.dumps(p,ensure_ascii=False,indent=2) if mode else render(p)
            self.assertLessEqual(estimate_tokens(encoded),800)

class ReadExchangeTest(unittest.TestCase):
    def test_cold_exchange_exact_locator(self):
        import tempfile
        from pathlib import Path
        from test_cold_storage import make_archive, jsonl
        import cold_storage
        from recall_read import read_exchange
        with tempfile.TemporaryDirectory() as td:
            home=Path(td);cold_storage.clear_caches()
            make_archive(home,'2026-06',{'2026-06-01':{'sessions.jsonl':jsonl([
                {'session_id':'s','event_id':'e','role':'assistant','text':'冷存储结果',
                 'ts':'2026-06-01T10:00:00+08:00'}])}})
            p=read_exchange(home,'2026-06-01','s','e')
            self.assertIn('.tar.gz!',p['events'][0]['ref'])
            self.assertFalse((home/'daily').exists())

    def test_integrity_failure_refuses_context(self):
        from unittest.mock import patch
        from pathlib import Path
        from recall_read import read_exchange
        with patch('recall_read.iter_day_records', return_value=[]), \
             patch('recall_read.last_issues', return_value=['corrupt']):
            with self.assertRaisesRegex(ValueError,'integrity'):
                read_exchange(Path('/unused'),'2026-10-20','s','e')

    def test_exact_anchor_exchange_omissions_budget_and_source_unchanged(self):
        import tempfile
        from pathlib import Path
        from recall_read import read_exchange
        with tempfile.TemporaryDirectory() as td:
            home=Path(td);day=home/'daily/2026-10-20';day.mkdir(parents=True)
            records=[{'session_id':'s','event_id':'e'+str(i),'ts':'2026-10-20T10:%02d:00'%i,
                      'role':'user' if i%2==0 else 'assistant','text':'工作证据'+str(i)} for i in range(12)]
            records[5]['text']='Bearer '+'x'*30
            source=day/'sessions.jsonl';source.write_text(''.join(json.dumps(r)+'\n' for r in records))
            before=source.read_bytes()
            out=read_exchange(home,'2026-10-20','s','e5',1000)
            ids=[r['event_id'] for r in out['events']]
            self.assertIn('e4',ids);self.assertIn('e5',ids)
            self.assertGreater(out['omitted_events'],0)
            self.assertEqual(out['shown_events']+out['omitted_events'],12)
            self.assertNotIn('x'*30,json.dumps(out))
            self.assertLessEqual(estimate_tokens(json.dumps(out,ensure_ascii=False,indent=2)),1000)
            self.assertEqual(before,source.read_bytes())
            with self.assertRaises(ValueError):read_exchange(home,'2026-10-20','prefix','e5')
            with self.assertRaises(ValueError):read_exchange(home,'2026-10-20','s','missing')
            records.append(dict(records[5],text='conflicting same ID'))
            source.write_text(''.join(json.dumps(r)+'\n' for r in records))
            with self.assertRaises(ValueError):read_exchange(home,'2026-10-20','s','e5')

if __name__=='__main__':unittest.main()
