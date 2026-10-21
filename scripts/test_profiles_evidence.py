#!/usr/bin/env python3
"""Temp-only regression for conservative profile dedupe and source status."""
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import profile_store as p
import evidence as e
import status


class ProfilesEvidenceTest(unittest.TestCase):
    def test_latest_duplicate_and_provenance(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual(p.append_profile([{'text':'不要动数据库。','ts':'2026-10-19','session_id':'first'}], 'constraints.md', root),1)
            self.assertEqual(p.append_profile([{'text':'不要动数据库！','ts':'2026-10-20','session_id':'second'}], 'constraints.md', root),0)
            body=(root/'constraints.md').read_text()
            self.assertEqual(len(body.splitlines()),1)
            self.assertIn('first',body);self.assertIn('second',body)
            self.assertIn('2026-10-20',body)
            self.assertEqual(p.dedupe_profile_file(root/'constraints.md'),0)
            self.assertEqual((root/'constraints.md').read_text(),body)

    def test_no_silent_semantic_merge(self):
        self.assertNotEqual(p.profile_key('不要动生产数据库'),p.profile_key('不要动测试数据库'))
        self.assertNotEqual(p.profile_key('x != 0'),p.profile_key('x == 0'))
        self.assertNotEqual(p.profile_key('不许动数据库'),p.profile_key('不要动数据库'))

    def test_preserve_markdown_and_metadata(self):
        lines=['# 约束','','- 普通规则','- 普通规则。']
        actual=p.unique_lines(lines)
        self.assertEqual(actual,['# 约束','','- 普通规则。'])
        self.assertEqual(p.entry_text('- [date] 不许重启 (session: full-id, sha1:deadbeef)'), '不许重启')

    def test_status_display_succinct(self):
        with tempfile.TemporaryDirectory() as td:
            original=status.CONSTRAINTS_PATH
            status.CONSTRAINTS_PATH=Path(td)/'constraints.md'
            try:
                p.append_profile([{'text':'不许重启','session_id':'one'},{'text':'不许重启。','session_id':'two'}],'constraints.md',Path(td))
                shown=status.format_recent_constraints()
                self.assertEqual(shown.count('不许重启'),1)
                self.assertNotIn('sha1',shown);self.assertNotIn('sources',shown)
            finally:status.CONSTRAINTS_PATH=original

    def test_negative_and_latest_claim(self):
        for text in ['测试未通过，无法交付','tests not passed','验证失败']:
            self.assertNotEqual(e._status([text]),'completed',text)
        self.assertEqual(e._status(['早上已完成修复','最终回归失败，仍然阻塞']),'blocked')
        self.assertEqual(e._status(['之前失败','最终已修复，测试通过']),'completed')
        self.assertEqual(e._status(['计划完成测试']),'in_progress')

    def test_user_request_not_completion_and_claim_metadata(self):
        rec=e.build_evidence([{'session_id':'s','role':'user','text':'把测试通过之后再交付','ts':'2026-10-20'}])[0]
        self.assertEqual(rec['status'],'in_progress')
        self.assertFalse(rec['semantic_verified'])
        self.assertEqual(rec['status_basis'],'latest_source_claim_unverified')

    def test_chronological_status_not_input_order(self):
        rec=e.build_evidence([
          {'session_id':'s','role':'assistant','text':'最终回归失败','ts':'2026-10-20T12:00'},
          {'session_id':'s','role':'assistant','text':'已完成修复','ts':'2026-10-20T11:00'}])[0]
        self.assertEqual(rec['status'],'blocked')

if __name__=='__main__':unittest.main()
