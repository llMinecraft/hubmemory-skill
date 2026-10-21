#!/usr/bin/env python3
"""Public numbered pack CLI must be read-only and refuse corrupt sources."""
import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import daily_input


class InputTest(unittest.TestCase):
    def test_closed_day_pack_is_read_only(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {'HUBMEMORY_HOME': td}):
            import cold_storage
            cold_storage.clear_caches()
            day = '2020-01-02'
            root = Path(td) / 'daily' / day
            root.mkdir(parents=True)
            source = root / 'sessions.jsonl'
            source.write_text(json.dumps({'event_id':'e1','session_id':'s1','agent':'dsh','role':'assistant','text':'工作已交付，测试结果见来源记录。'})+'\n')
            before = {str(p.relative_to(td)): p.read_bytes() for p in Path(td).rglob('*') if p.is_file()}
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(daily_input.main(['--date', day]), 0)
            self.assertIn('[1]', out.getvalue())
            after = {str(p.relative_to(td)): p.read_bytes() for p in Path(td).rglob('*') if p.is_file()}
            self.assertEqual(before, after)

    def test_corrupt_archive_refused_even_with_hot_data(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {'HUBMEMORY_HOME': td}):
            arc = Path(td) / 'archive' / '2020'
            arc.mkdir(parents=True)
            (arc / '2020-01.tar.gz').write_bytes(b'broken')
            (arc / '2020-01.index.json').write_text(json.dumps({'archive':{'sha256':'0'*64},'days':[{'date':'2020-01-02'}]}))
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                daily_input.main(['--date','2020-01-02'])
            self.assertNotEqual(raised.exception.code, 0)

    def test_bad_date_refused(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            daily_input.main(['--date','not-a-date'])


if __name__ == '__main__':
    unittest.main()
