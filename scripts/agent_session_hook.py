#!/usr/bin/env python3
"""hubmemory agent_session_hook — Claude Code SessionStart hook.

Claude Code feeds the hook a JSON object on stdin, e.g.::

    {"session_id": "...", "model": "...", "source": "startup|resume|clear|compact",
     "transcript_path": "...", "cwd": "...", "hook_event_name": "SessionStart"}

This wrapper is a pure identity/forwarding shim — it never calls a model
and never invokes an LLM subprocess. It reads the JSON, extracts the real
session/model identity Claude itself reported, and runs::

    session_start.py --handoff --agent claude --session <session_id>
                     --model <model>

Model precedence: hook payload ``model`` first, ``ANTHROPIC_MODEL``
environment second; when neither is available the ``--model`` flag is
omitted (session_start.py treats it as optional). The payload model is
always forwarded as-is — it is never overwritten by the environment.

Every ``source`` value (startup / resume / clear / compact, or unknown)
takes the same path; the hook does not discriminate.

If the hook payload is malformed or carries no session identity, nothing
is fabricated or claimed: a direct, actionable instruction is printed on
stdout telling the agent to run session_start.py itself with the real
values, and the hook still exits 0 so the session is never blocked.

Stdout contract: only session_start.py's own output and (on failure) the
fallback instruction are emitted. Raw hook payload — transcript paths,
credentials, or any other field — is never echoed to stdout.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SESSION_START = Path(__file__).resolve().parent / "session_start.py"
SUBPROCESS_TIMEOUT_S = 60  # generous: session_start may decompress cold data

AGENT = "claude"


def _clean(value):
    """Return value as a non-empty stripped string, else None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def fallback_instruction(session_id=None, model=None):
    """Direct actionable instruction for the agent — no fabricated identity."""
    model_part = model if model else "'<actual model>'"
    session_part = session_id if session_id else "'<actual session>'"
    return (
        "hubmemory handoff was not completed automatically.\n"
        "Run this now with the real values from this session:\n"
        "  /usr/bin/python3 {script} --handoff --agent claude "
        "--model {model} --session {session}".format(
            script=SESSION_START, model=model_part, session=session_part,
        )
    )


def build_command(session_id, model):
    cmd = [
        sys.executable,
        str(SESSION_START),
        "--handoff",
        "--agent", AGENT,
        "--session", session_id,
    ]
    if model:
        cmd += ["--model", model]
    return cmd


def resolve_model(payload, env):
    """Payload model wins; ANTHROPIC_MODEL is fallback only, never override."""
    model = _clean(payload.get("model"))
    if model:
        return model
    return _clean(env.get("ANTHROPIC_MODEL"))


def run_session_start(cmd, timeout=SUBPROCESS_TIMEOUT_S):
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def main(stdin=None, env=None):
    stdin = sys.stdin if stdin is None else stdin
    env = os.environ if env is None else env

    try:
        raw = stdin.read()
    except Exception:
        raw = ""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    session_id = _clean(payload.get("session_id"))
    model = resolve_model(payload, env)

    if not session_id:
        # Do not fabricate or claim an identity we were not given.
        print(fallback_instruction(session_id=None, model=model))
        return 0

    try:
        result = run_session_start(build_command(session_id, model))
    except (subprocess.TimeoutExpired, OSError):
        print(fallback_instruction(session_id=session_id, model=model))
        return 0

    # Forward only session_start.py's own output — never the hook payload.
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    if result.returncode != 0:
        print(fallback_instruction(session_id=session_id, model=model))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
