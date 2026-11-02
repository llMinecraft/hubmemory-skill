#!/usr/bin/env python3
"""hubmemory agent_session_hook — Claude Code / Codex lifecycle hook.

Usage (hook command)::

    python3 agent_session_hook.py                 # Claude (default agent)
    python3 agent_session_hook.py --agent codex   # Codex hooks.json

``SessionStart`` payloads run the daily handoff below. ``UserPromptSubmit``
payloads run the read-only ``session_start.py --remind`` check, which prints
nothing unless this session owns an unpublished lease or a recent daily is
waiting — so ordinary turns add no context.

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
KNOWN_AGENTS = ("claude", "codex")
MODEL_ENV = {"claude": ("ANTHROPIC_MODEL", "CLAUDE_MODEL"),
             "codex": ("CODEX_MODEL", "OPENAI_MODEL")}


def _clean(value):
    """Return value as a non-empty stripped string, else None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def fallback_instruction(session_id=None, model=None, agent=AGENT):
    """Direct actionable instruction for the agent — no fabricated identity."""
    model_part = model if model else "'<actual model>'"
    session_part = session_id if session_id else "'<actual session>'"
    return (
        "hubmemory handoff was not completed automatically.\n"
        "Run this now with the real values from this session:\n"
        "  /usr/bin/python3 {script} --handoff --agent {agent} "
        "--model {model} --session {session}".format(
            script=SESSION_START, model=model_part, session=session_part,
            agent=agent,
        )
    )


def build_command(session_id, model, agent=AGENT, remind=False):
    cmd = [
        sys.executable,
        str(SESSION_START),
        "--remind" if remind else "--handoff",
        "--agent", agent,
        "--session", session_id,
    ]
    if model:
        cmd += ["--model", model]
    return cmd


def resolve_model(payload, env, agent=AGENT):
    """Payload model wins; agent model env is fallback only, never override."""
    model = _clean(payload.get("model"))
    if model:
        return model
    for key in MODEL_ENV.get(agent, ()):
        if _clean(env.get(key)):
            return _clean(env.get(key))
    return None


def parse_agent(argv):
    if len(argv) >= 2 and argv[0] == "--agent" and argv[1] in KNOWN_AGENTS:
        return argv[1]
    return AGENT


def run_session_start(cmd, timeout=SUBPROCESS_TIMEOUT_S):
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


HOOK_LOG = Path(os.environ.get("HUBMEMORY_HOME", os.path.expanduser("~/hubmemory"))) / "logs" / "agent_hooks.log"


def log_invocation(agent, event, session_id, outcome):
    """One metadata line per hook run (no payload/prompt text) so doctor and
    humans can prove the host actually fired the hook."""
    try:
        from datetime import datetime
        HOOK_LOG.parent.mkdir(parents=True, exist_ok=True)
        with HOOK_LOG.open("a", encoding="utf-8") as stream:
            stream.write("%s agent=%s event=%s session=%s outcome=%s\n" % (
                datetime.now().astimezone().isoformat(timespec="seconds"),
                agent, event or "?", session_id or "-", outcome))
    except OSError:
        pass


def main(stdin=None, env=None, argv=None):
    stdin = sys.stdin if stdin is None else stdin
    env = os.environ if env is None else env
    agent = parse_agent(sys.argv[1:] if argv is None else argv)

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
    model = resolve_model(payload, env, agent)
    remind = payload.get("hook_event_name") == "UserPromptSubmit"

    event = _clean(payload.get("hook_event_name"))
    if env is os.environ or "HUBMEMORY_HOOK_LOG" in env:
        log_invocation(agent, event, session_id, "start")

    if not session_id:
        if remind:
            return 0  # per-turn check never nags without an identity
        # Do not fabricate or claim an identity we were not given.
        print(fallback_instruction(session_id=None, model=model, agent=agent))
        return 0

    try:
        result = run_session_start(build_command(session_id, model, agent, remind))
    except (subprocess.TimeoutExpired, OSError):
        if not remind:
            print(fallback_instruction(session_id=session_id, model=model, agent=agent))
        return 0

    # Forward only session_start.py's own output — never the hook payload.
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    if result.returncode != 0 and not remind:
        print(fallback_instruction(session_id=session_id, model=model, agent=agent))
    # Never block the host session: a failed handoff stays pending and the
    # fallback instruction above tells the agent what to run.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
