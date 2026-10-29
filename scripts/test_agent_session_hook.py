#!/usr/bin/env python3
"""Tests for agent_session_hook — identity forwarding and failure contract."""
import contextlib
import io
import json
import subprocess
import unittest
from unittest.mock import patch

import agent_session_hook as hook


class FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def run_hook(payload, env=None, fake=None):
    """Run main() with a fake subprocess; return (rc, stdout, stderr, cmds)."""
    env = {} if env is None else env
    cmds = []
    if fake is None:
        def fake(cmd, timeout=hook.SUBPROCESS_TIMEOUT_S):
            cmds.append(cmd)
            return FakeResult(stdout="session_start output\n")
    stdin = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
    out, err = io.StringIO(), io.StringIO()
    with patch.object(hook, "run_session_start", fake), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = hook.main(stdin=stdin, env=env)
    return rc, out.getvalue(), err.getvalue(), cmds


class ForwardingTest(unittest.TestCase):
    def test_forwards_handoff_identity(self):
        rc, out, _, cmds = run_hook({
            "session_id": "sess-abc",
            "model": "claude-sonnet-4-5",
            "source": "startup",
        })
        self.assertEqual(rc, 0)
        self.assertEqual(len(cmds), 1)
        cmd = cmds[0]
        self.assertIn("--handoff", cmd)
        self.assertIn("--agent", cmd)
        self.assertIn("claude", cmd)
        self.assertIn("--session", cmd)
        self.assertIn("sess-abc", cmd)
        self.assertIn("--model", cmd)
        self.assertIn("claude-sonnet-4-5", cmd)
        self.assertIn("session_start output", out)

    def test_payload_model_beats_env(self):
        _, _, _, cmds = run_hook(
            {"session_id": "s1", "model": "claude-opus-4", "source": "startup"},
            env={"ANTHROPIC_MODEL": "claude-haiku"},
        )
        self.assertIn("claude-opus-4", cmds[0])
        self.assertNotIn("claude-haiku", cmds[0])

    def test_env_model_fallback_when_payload_missing(self):
        _, _, _, cmds = run_hook(
            {"session_id": "s1", "source": "startup"},
            env={"ANTHROPIC_MODEL": "claude-haiku-3"},
        )
        self.assertIn("claude-haiku-3", cmds[0])

    def test_no_model_anywhere_omits_flag(self):
        _, _, _, cmds = run_hook({"session_id": "s1", "source": "startup"}, env={})
        self.assertNotIn("--model", cmds[0])

    def test_all_sources_forwarded(self):
        for source in ("startup", "resume", "clear", "compact", "unknown-x"):
            with self.subTest(source=source):
                rc, _, _, cmds = run_hook({"session_id": "s", "source": source})
                self.assertEqual(rc, 0)
                self.assertEqual(len(cmds), 1)
                self.assertIn("--handoff", cmds[0])

    def test_compaction_source_still_runs(self):
        rc, out, _, cmds = run_hook({
            "session_id": "sess-compact",
            "model": "m1",
            "source": "compact",
        })
        self.assertEqual(rc, 0)
        self.assertIn("sess-compact", cmds[0])
        self.assertIn("session_start output", out)


class FailureContractTest(unittest.TestCase):
    def test_malformed_json_no_subprocess(self):
        rc, out, _, cmds = run_hook("{not json at all")
        self.assertEqual(rc, 0)
        self.assertEqual(cmds, [])
        self.assertIn("session_start.py", out)
        self.assertIn("--handoff", out)
        self.assertIn("--agent claude", out)

    def test_empty_stdin_no_subprocess(self):
        rc, out, _, cmds = run_hook("")
        self.assertEqual(rc, 0)
        self.assertEqual(cmds, [])
        self.assertIn("--session", out)

    def test_missing_session_id_no_subprocess(self):
        rc, out, _, cmds = run_hook({"model": "m", "source": "startup"})
        self.assertEqual(rc, 0)
        self.assertEqual(cmds, [])
        self.assertIn("--agent claude", out)

    def test_non_dict_payload_no_subprocess(self):
        rc, out, _, cmds = run_hook(json.dumps(["a", "b"]))
        self.assertEqual(rc, 0)
        self.assertEqual(cmds, [])
        self.assertIn("--handoff", out)

    def test_timeout_falls_back_to_instruction(self):
        def fake(cmd, timeout=hook.SUBPROCESS_TIMEOUT_S):
            raise subprocess.TimeoutExpired(cmd, timeout)
        rc, out, _, _ = run_hook(
            {"session_id": "sess-t", "model": "m1", "source": "resume"}, fake=fake)
        self.assertEqual(rc, 0)
        self.assertIn("sess-t", out)
        self.assertIn("--session", out)

    def test_spawn_failure_falls_back_to_instruction(self):
        def fake(cmd, timeout=hook.SUBPROCESS_TIMEOUT_S):
            raise OSError("no such file")
        rc, out, _, _ = run_hook({"session_id": "sess-e"}, fake=fake)
        self.assertEqual(rc, 0)
        self.assertIn("session_start.py", out)

    def test_child_failure_propagates_and_instructs(self):
        def fake(cmd, timeout=hook.SUBPROCESS_TIMEOUT_S):
            return FakeResult(returncode=3, stdout="partial\n", stderr="boom\n")
        rc, out, err, _ = run_hook({"session_id": "s1"}, fake=fake)
        self.assertEqual(rc, 3)
        self.assertIn("partial", out)
        self.assertIn("boom", err)
        self.assertIn("session_start.py", out)


class StdoutHygieneTest(unittest.TestCase):
    def test_payload_never_echoed_to_stdout(self):
        secret = "sk-ant-SECRET-DO-NOT-LOG"
        rc, out, err, _ = run_hook({
            "session_id": "s1",
            "source": "startup",
            "transcript_path": "/private/path/" + secret,
            "api_key": secret,
        })
        self.assertEqual(rc, 0)
        self.assertNotIn(secret, out)
        self.assertNotIn(secret, err)

    def test_malformed_input_not_echoed(self):
        secret = "credential-BLOB-123"
        rc, out, err, _ = run_hook('{"broken": "%s"' % secret)
        self.assertEqual(rc, 0)
        self.assertNotIn(secret, out)
        self.assertNotIn(secret, err)


if __name__ == "__main__":
    unittest.main()
