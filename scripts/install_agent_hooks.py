#!/usr/bin/env python3
"""Install hubmemory lifecycle hooks into Claude Code and Codex (idempotent).

    python3 install_agent_hooks.py            # install / repair
    python3 install_agent_hooks.py --check    # report only, exit 1 if missing

Claude: ~/.claude/settings.json  SessionStart (handoff) + UserPromptSubmit (remind)
Codex : ~/.codex/hooks.json      same two events (--agent codex), plus
        ~/.codex/config.toml     hooks.state trust for exactly these handlers and
                                 sandbox_workspace_write.writable_roots += HUB_HOME
DSH has no host hook surface; ~/.dsh/AGENTS.md carries the per-turn
``session_start.py --remind`` and sandbox-escalation instructions instead.

Codex trust: Codex refuses to run user hooks until their ``trusted_hash``
is recorded. The hash is read back from Codex itself (``codex app-server``
``hooks/list``), never computed here, so a Codex format change can only make
this installer report "untrusted" — it cannot trust a different command.
Backups are written before any file is modified.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
HUB_HOME = Path(os.environ.get("HUBMEMORY_HOME", HOME / "hubmemory"))
# Prefer the stable ~/.claude/skills symlink spelling used by existing hooks.
_STABLE = HOME / ".claude" / "skills" / "hubmemory" / "scripts" / "agent_session_hook.py"
_REAL = Path(__file__).resolve().parent / "agent_session_hook.py"
HOOK = _STABLE if _STABLE.exists() and _STABLE.resolve() == _REAL else _REAL
CLAUDE_SETTINGS = HOME / ".claude" / "settings.json"
CODEX_HOME = Path(os.environ.get("CODEX_HOME", HOME / ".codex"))
CODEX_HOOKS = CODEX_HOME / "hooks.json"
CODEX_CONFIG = CODEX_HOME / "config.toml"
CODEX_BINS = ("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
              "/Applications/Codex.app/Contents/Resources/codex")
MARK = "agent_session_hook.py"


def _backup(path):
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak_hubmemory_" + time.strftime("%Y%m%d%H%M%S")))


def _hook_groups(command, start_timeout, remind_timeout):
    return {
        "SessionStart": {"matcher": "startup|resume|clear|compact",
                         "hooks": [{"type": "command", "command": command,
                                    "timeout": start_timeout}]},
        "UserPromptSubmit": {"hooks": [{"type": "command", "command": command,
                                        "timeout": remind_timeout}]},
    }


def _same_agent(command, wanted_command):
    return ("--agent codex" in command) == ("--agent codex" in wanted_command)


def _merge(hooks, wanted):
    """Ensure each event has exactly one hubmemory group; keep foreign hooks.

    An existing hubmemory handler for the same agent is accepted as-is (path
    spelling / timeout differences are not churned: rewriting would change
    Codex's trusted hash for no behavioural gain)."""
    changed = False
    for event, group in wanted.items():
        groups = hooks.setdefault(event, [])
        ours = [g for g in groups if any(MARK in h.get("command", "") for h in g.get("hooks", []))]
        want_cmd = group["hooks"][0]["command"]
        if len(ours) == 1 and all(_same_agent(h.get("command", ""), want_cmd)
                                  for h in ours[0].get("hooks", [])) \
                and (event != "SessionStart" or ours[0].get("matcher") == group.get("matcher")):
            continue
        hooks[event] = [g for g in groups if g not in ours] + [group]
        changed = True
    return changed


def install_claude(check):
    data = json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8")) if CLAUDE_SETTINGS.exists() else {}
    command = "/usr/bin/python3 " + str(HOOK)
    wanted = _hook_groups(command, 90, 20)
    hooks = data.setdefault("hooks", {})
    if not _merge(hooks, wanted):
        return "claude: ok"
    if check:
        return "claude: MISSING hooks"
    _backup(CLAUDE_SETTINGS)
    CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_SETTINGS.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return "claude: installed"


def codex_bin():
    for candidate in (shutil.which("codex"), *CODEX_BINS):
        if candidate and Path(candidate).exists():
            return candidate
    return None


def codex_hooks_list(binary):
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"clientInfo": {"name": "hubmemory-installer", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "hooks/list", "params": {"cwds": [str(HOME)]}},
    ]
    proc = subprocess.Popen([binary, "app-server"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        proc.stdin.write("".join(json.dumps(m) + "\n" for m in msgs))
        proc.stdin.flush()
        deadline = time.time() + 30
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == 2:
                return [h for entry in msg.get("result", {}).get("data", [])
                        for h in entry.get("hooks", [])]
        return None
    finally:
        proc.kill()
        proc.wait()


def _toml_has(text, needle):
    return needle in text


def install_codex(check):
    if not CODEX_HOME.exists():
        return "codex: not installed (skipped)"
    notes = []
    command = "/usr/bin/python3 %s --agent codex" % HOOK
    data = json.loads(CODEX_HOOKS.read_text(encoding="utf-8")) if CODEX_HOOKS.exists() else {}
    hooks = data.setdefault("hooks", {})
    wanted = _hook_groups(command, 90, 20)
    wanted["SessionStart"]["hooks"][0]["statusMessage"] = "hubmemory daily handoff"
    if _merge(hooks, wanted):
        if check:
            return "codex: MISSING hooks.json entries"
        _backup(CODEX_HOOKS)
        CODEX_HOOKS.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        notes.append("hooks.json written")
    config = CODEX_CONFIG.read_text(encoding="utf-8") if CODEX_CONFIG.exists() else ""
    additions = []
    root = str(HUB_HOME)
    if root not in config:
        if "[sandbox_workspace_write]" in config:
            notes.append("WARNING: add %s to existing [sandbox_workspace_write].writable_roots manually" % root)
        else:
            additions.append('\n# hubmemory: agents claim/publish reports under %s\n'
                             '[sandbox_workspace_write]\nwritable_roots = ["%s"]\n' % (root, root))
    binary = codex_bin()
    listed = codex_hooks_list(binary) if binary else None
    if listed is None:
        notes.append("WARNING: could not query Codex hooks/list; trust hooks via Codex /hooks")
    else:
        for h in listed:
            if MARK not in h.get("command", "") or h.get("trustStatus") == "trusted":
                continue
            key, digest = h.get("key"), h.get("currentHash")
            if not key or not digest:
                continue
            if check:
                return "codex: hook %s untrusted" % key
            additions.append('\n[hooks.state."%s"]\ntrusted_hash = "%s"\n' % (key, digest))
    if additions and not check:
        _backup(CODEX_CONFIG)
        with CODEX_CONFIG.open("a", encoding="utf-8") as stream:
            stream.write("\n# hubmemory hooks (install_agent_hooks.py)" + "".join(additions))
        notes.append("config.toml updated")
    elif additions:
        return "codex: config incomplete"
    return "codex: " + (", ".join(notes) if notes else "ok")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    results = [install_claude(args.check), install_codex(args.check)]
    print("\n".join(results))
    bad = any(("MISSING" in r or "untrusted" in r or "incomplete" in r) for r in results)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
