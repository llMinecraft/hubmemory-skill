#!/usr/bin/env python3
"""Offline regression suite; all fixtures live in a temporary HUBMEMORY_HOME."""
from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path

import hybrid_query as hq
from context import build_payload, compact_hit, estimate_tokens, render

NOW = '2026-10-18T19:01:39+08:00'
CWD = '/projects/alpha'


class RetrievalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.records = [
            self.event('plan', '2026-10-18T18:56:00+08:00', '我会把合成舱整理成正式产品立项书，明确范围。'),
            self.event('delivery', '2026-10-18T18:59:00+08:00', '已完成[《合成舱产品立项书》](<docs/CHARTER.md>)。尚未启动开发。'),
            self.event('future', '2026-10-18T19:05:00+08:00', '已完成项目立项书 docs/WRONG.md'),
            self.event('echo', '2026-10-18T19:00:00+08:00', '你看一下刚才写的项目立项书', role='user'),
            self.event('noise', '2026-10-18T18:59:59+08:00', '你看一下刚才的修改，现在已经好了。'),
            self.event('other', '2026-10-18T19:00:00+08:00', '已完成[项目立项书](<docs/OTHER.md>)', cwd='/projects/beta'),
            self.event('yesterday', '2026-10-17T10:00:00+08:00', '已交付[项目立项书](<docs/YESTERDAY.md>)'),
            self.event('technical', '2026-10-18T18:40:00+08:00', '修复 subscribe_quote 回调行情订阅失败。'),
        ]
        self.flush()

    def event(self, event_id, ts, text, role='assistant', cwd=CWD):
        return dict(event_id=event_id, ts=ts, text=text, role=role, cwd=cwd,
                    agent='dsh', session_id='session-'+event_id)

    def flush(self):
        groups = {}
        for record in self.records:
            groups.setdefault(record['ts'][:10], []).append(record)
        for day, records in groups.items():
            directory = self.home/'daily'/day
            directory.mkdir(parents=True, exist_ok=True)
            (directory/'sessions.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in records))

    def search(self, q, **kw):
        return hq.search(self.home, q, cwd=CWD, as_of=NOW, **kw)

    def test_original_natural_question_finds_delivery_first(self):
        hits = self.search('你看一下刚才写的项目立项书')
        self.assertEqual(hits[0]['event_id'], 'delivery')

    def test_short_recent_question(self):
        self.assertEqual(self.search('刚才的立项书')[0]['event_id'], 'delivery')

    def test_cutoff_is_exclusive_and_anchors_time(self):
        self.records.append(self.event('boundary', NOW, '已完成[项目立项书](<docs/BOUNDARY.md>)'))
        self.flush()
        self.assertNotIn('boundary', [r['event_id'] for r in self.search('刚才的项目立项书')])

    def test_invalid_cutoff_is_rejected(self):
        with self.assertRaises(ValueError):
            hq.search(self.home, '项目立项书', as_of='not-a-date')

    def test_planned_file_is_not_delivered(self):
        self.records.append(self.event('planned-file', '2026-10-18T19:00:00+08:00',
                         '我将生成[项目立项书](<docs/PLANNED.md>)，尚未完成。'))
        self.flush()
        hit = next(r for r in self.search('项目立项书') if r['event_id']=='planned-file')
        compact = compact_hit(hit)
        self.assertNotEqual(compact['retrieval'].get('delivery_state'), 'delivered')

    def test_artifact_link_with_spaces_and_line_anchor(self):
        self.records.append(self.event('spaces', '2026-10-18T18:59:00+08:00',
                      '已完成[部署说明](<docs/Deploy Guide.md#L12>)'))
        self.flush()
        hits = self.search('部署说明')
        paths = [a['path'] for a in compact_hit(hits[0])['artifacts']]
        self.assertIn('/projects/alpha/docs/Deploy Guide.md', paths)

    def test_future_is_excluded(self):
        self.assertNotIn('future', [r['event_id'] for r in self.search('项目立项书')])

    def test_explicit_yesterday(self):
        self.assertEqual(self.search('昨天写的项目立项书')[0]['event_id'], 'yesterday')

    def test_historical_yesterday_uses_cutoff_day_not_wall_clock(self):
        self.records.append(self.event('old', '2025-01-02T10:00:00+08:00',
                           '已完成[部署说明](<docs/OLD.md>)'))
        self.flush()
        hits = hq.search(self.home, '昨天的部署说明', cwd=CWD,
                         as_of='2025-01-03T09:00:00+08:00')
        self.assertTrue(hits)
        self.assertEqual(hits[0]['event_id'], 'old')

    def test_filter_source(self):
        self.assertTrue(all(r['_source']=='session' for r in self.search('source:session 项目立项书')))

    def test_filter_agent_no_match(self):
        self.assertEqual(self.search('agent:nonexistent 项目立项书'), [])

    def test_filter_date(self):
        hits = self.search('date:2026-10-17 项目立项书')
        self.assertTrue(hits)
        self.assertTrue(all(r['ts'].startswith('2026-10-17') for r in hits))

    def test_english_recent_query(self):
        self.records.append(self.event('english', '2026-10-18T18:59:00+08:00',
                       'Completed [deployment guide](<docs/DEPLOY.md>).'))
        self.flush()
        self.assertEqual(self.search('Please review the deployment guide you just created')[0]['event_id'], 'english')

    def test_paths_with_secret_urls_are_not_artifacts(self):
        record = self.event('secret', '2026-10-18T18:59:00+08:00',
                    '已完成[说明](https://example.test/doc.md?token=not-a-real-secret)')
        self.assertEqual(hq.extract_artifacts(record, self.home), [])

    def test_secret_basenames_are_not_artifacts(self):
        record = self.event('secret-paths', '2026-10-18T18:59:00+08:00', '')
        record['files'] = ['auth.json', '.env.local', 'id_rsa', 'keys/client.key',
                           '.npmrc', 'docs/DEPLOY.md']
        record['text'] = '已保存[登录配置](<private/auth.json>)。'
        artifacts = hq.extract_artifacts(record, self.home)
        self.assertEqual([a['path'] for a in artifacts], ['docs/DEPLOY.md'])

    def test_artifact_request_does_not_boost_bulk_completed_summary(self):
        # Session-wide evidence may mention a topic and many unrelated files;
        # "写的" in the question is not evidence that this summary delivered it.
        summary = dict(session_id='bulk-summary', agent='dsh', cwd=CWD,
                       status='completed', ended_at='2026-10-18T18:59:00+08:00',
                       goals=['项目立项书'], results=['completed project work'],
                       files=['extensions.yml', 'SKILL.md', 'auth.json'] +
                             ['other/file%d.py' % i for i in range(20)])
        path = self.home/'daily'/'2026-10-18'/'evidence.jsonl'
        path.write_text(json.dumps(summary, ensure_ascii=False)+'\n')
        hits = self.search('你看一下刚才写的项目立项书')
        bulk = next(r for r in hits if r.get('session_id') == 'bulk-summary')
        self.assertEqual(bulk['_retrieval']['components']['delivery'], 0.0)
        self.assertFalse(any(a['path'].endswith('auth.json')
                             for a in bulk.get('artifacts', [])))
        delivery = next(r for r in hits if r.get('event_id') == 'delivery')
        self.assertEqual(delivery['_retrieval']['components']['delivery'],
                         hq.DELIVERED_BONUS)

    def test_exact_technical_identifier(self):
        self.assertEqual(self.search('subscribe_quote')[0]['event_id'], 'technical')

    def test_unrelated_topic_no_match(self):
        self.assertEqual(self.search('量子纠缠实验报告'), [])

    def test_cross_project_fallback(self):
        hits = hq.search(self.home, '刚才的项目立项书', cwd='/projects/unknown', as_of=NOW)
        self.assertTrue(hits)
        self.assertIn(hits[0]['event_id'], {'other','delivery'})

    def test_exclude_session(self):
        hits = self.search('项目立项书', exclude_session='session-delivery')
        self.assertNotIn('delivery', [r['event_id'] for r in hits])

    def test_common_prefix_does_not_erase_all_sessions(self):
        self.assertTrue(self.search('项目立项书', exclude_session='session-'))

    def test_precise_provenance_and_artifact(self):
        hit = compact_hit(self.search('刚才的立项书')[0])
        self.assertEqual(hit['session'], 'session-delivery')
        self.assertEqual(hit['event_id'], 'delivery')
        self.assertIsInstance(hit['source_line'], int)
        self.assertIn('#L', hit['locator'])
        self.assertIn('/projects/alpha/docs/CHARTER.md', [a['path'] for a in hit['artifacts']])

    def test_negated_english_completion_not_delivered(self):
        record = self.event('negative', NOW, 'Report not completed. Will write [report](<docs/R.md>).')
        self.assertNotEqual(hq._classify_delivery(record), 'delivered')

    def test_negated_chinese_completion_not_delivered(self):
        record = self.event('negative', NOW, '尚未完成[部署说明](<docs/R.md>)，准备下次做。')
        self.assertNotEqual(hq._classify_delivery(record), 'delivered')

    def test_plan_only_is_not_sufficient_delivery(self):
        raw = [dict(text='我将写报告', _score=100, _source='session', _path='/tmp/x',
                    _retrieval={'strong_coverage':1.0, 'delivery':'planned'})]
        self.assertEqual(build_payload('刚才写的报告', raw)['sufficiency'], 'thin')

    def test_raw_score_is_not_confidence(self):
        payload = build_payload('x', [dict(text='unrelated', _score=999, _source='session', _path='/tmp/x')])
        self.assertEqual(payload['sufficiency'], 'thin')

    def test_human_budget(self):
        payload = build_payload('刚才的立项书', self.search('刚才的立项书'), budget=800)
        self.assertLessEqual(estimate_tokens(render(payload)), 800)
        self.assertTrue(payload['results'])

    def test_json_budget(self):
        payload = build_payload('刚才的立项书', self.search('刚才的立项书'), budget=800, json_output=True)
        self.assertLessEqual(estimate_tokens(json.dumps(payload, ensure_ascii=False, indent=2)), 800)

    def test_tiny_budget_does_not_force_oversized_first_hit(self):
        raw = [dict(text='x'*1000, session_id='s'*1000, _path='/'+'p'*1000, _score=999)]
        payload = build_payload('x', raw, budget=160)
        self.assertFalse(payload['results'])
        self.assertEqual(payload['sufficiency'], 'thin')
        self.assertLessEqual(estimate_tokens(render(payload)), 160)

    def test_no_topic_after_polite_scaffold_is_not_guessed(self):
        self.assertEqual(self.search('请帮我看看刚才那个'), [])

    def test_empty_deictic_query_does_not_guess(self):
        self.assertEqual(self.search('看看刚才那个'), [])

    def test_future_derived_summary_does_not_leak(self):
        day = self.home/'daily'/'2026-10-18'
        (day/'evidence.jsonl').write_text(json.dumps(dict(
            period='2026-10-18', title='项目立项书已完成',
            text='项目立项书 docs/LEAK.md', cwd=CWD,
            session_id='session-leak'), ensure_ascii=False)+'\n')
        self.assertNotIn('session-leak', [r.get('session_id') for r in self.search('项目立项书')])

    def test_raw_records_are_not_changed(self):
        path = self.home/'daily'/'2026-10-18'/'sessions.jsonl'
        before = path.read_bytes()
        self.search('刚才的立项书')
        self.assertEqual(path.read_bytes(), before)

    def test_same_prefix_sessions_remain_distinguishable(self):
        a = compact_hit(dict(session_id='session-aaa'))
        b = compact_hit(dict(session_id='session-bbb'))
        self.assertNotEqual(a['session'], b['session'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
