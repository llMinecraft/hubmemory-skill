#!/usr/bin/env python3
"""Conversation views suppress empty/tool noise without deleting source facts."""
import json
import unittest
import test_watcher


class ConversationTests(unittest.TestCase):
    setUp = test_watcher.WatcherDurabilityTests.setUp
    tearDown = test_watcher.WatcherDurabilityTests.tearDown

    def render(self, rows):
        day = '2026-09-23'
        root = self.hub / 'daily' / day
        root.mkdir(parents=True, exist_ok=True)
        events = []
        for index, row in enumerate(rows):
            event = {'session_id':'s1', 'agent':'dsh', 'model':'test',
                     'ts':'2026-09-23T09:35:00+08:00', 'role':'assistant',
                     'event_id':str(index), 'text':'', 'tool_uses':[]}
            event.update(row)
            events.append(event)
        source = root / 'sessions.jsonl'
        source.write_text(''.join(json.dumps(e)+'\n' for e in events))
        original = source.read_bytes()
        self.daily._rebuild_conversations(day)
        view = (root / 'conversations.md').read_text()
        self.assertEqual(original, source.read_bytes())
        self.daily._rebuild_conversations(day)
        self.assertEqual(view, (root / 'conversations.md').read_text())
        return view

    def test_empty_and_tool_only_events_have_no_heading(self):
        view = self.render([{}, {'text':'  \n'},
            {'tool_uses':[{'name':'bash','input_summary':'secret command'}]},
            {'role':'tool','text':'tool result'},
            {'role':'system','text':'internal instruction'},
            {'text':'实际回复'}])
        self.assertEqual(view.count('**[2026-09-23 09:35] AI**'), 1)
        self.assertIn('实际回复', view)
        self.assertNotIn('secret command',view)
        self.assertNotIn('tool result',view)
        self.assertNotIn('internal instruction',view)
        self.assertIn('1 条 AI 回复',view)

    def test_same_minute_different_messages_and_legitimate_repeats_survive(self):
        view = self.render([{'text':'第一条'}, {'text':'第二条'},
                            {'text':'第一条','ts':'2026-09-23T09:36:00+08:00'}])
        self.assertEqual(view.count('**[2026-09-23 09:35] AI**'),2)
        self.assertEqual(view.count('第一条'),2)

    def test_exact_duplicate_removed_but_conflicting_id_preserved(self):
        a={'event_id':'same','text':'原回复'}
        view = self.render([a,a,{'event_id':'same','text':'不同回复'}])
        self.assertEqual(view.count('原回复'),1)
        self.assertEqual(view.count('不同回复'),1)

    def test_empty_only_session_not_rendered(self):
        view = self.render([{'session_id':'empty'}, {'session_id':'real','text':'有内容'}])
        self.assertNotIn('`empty',view)
        self.assertIn('`real',view)


if __name__ == '__main__':
    unittest.main()
