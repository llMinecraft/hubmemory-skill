#!/usr/bin/env python3
"""Install hubmemory lifecycle hooks into Claude Code and Codex (idempotent).

    python3 install_agent_hooks.py            # install / repair
    python3 install_agent_hooks.py --check    # report only, exit 1 if missing

Claude: ~/.claude/settings.json  SessionStart (handoff) + UserPromptSubmit (remind)
Codex : ~/.codex/hooks.json      same two events (--agent codex), plus
        ~/.codex/config.toml     hooks.state trust for exactly these handlers and
                                 sandbox_workspace_write.writable_roots += HUB_HOME
Global instructions: ~/.dsh/AGENTS.md (DSH has no host hooks) and
~/.codex/AGENTS.md get one managed hubmemory block rendered from
templates/agents_<agent>.md between BEGIN/END markers. Only that block is
ever rewritten; an older unmarked "## Cross-agent shared memory (hubmemory)"
section is migrated in place. User text outside the block is untouched.

session_start.py calls ensure_integration() so a fresh install wires itself
up the first time any agent uses the skill (opt out: HUBMEMORY_NO_AUTO_INSTALL=1).

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
SKILL_DIR = Path(__file__).resolve().parent.parent
TEMPLATES = SKILL_DIR / "templates"
STABLE_SKILL = HOME / ".claude" / "skills" / "hubmemory"
SKILL_REF = ("~/.claude/skills/hubmemory" if STABLE_SKILL.exists()
             and STABLE_SKILL.resolve() == SKILL_DIR else str(SKILL_DIR))
BEGIN = "<!-- hubmemory:begin (managed by hubmemory install_agent_hooks.py; edits inside are overwritten) -->"
END = "<!-- hubmemory:end -->"
LEGACY_HEADING = "## Cross-agent shared memory (hubmemory)"
INSTRUCTION_TARGETS = {"dsh": HOME / ".dsh" / "AGENTS.md", "codex": CODEX_HOME / "AGENTS.md"}
STAMP = HUB_HOME / "state" / "agent_integration.json"


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


def render_block(agent):
    body = (TEMPLATES / ("agents_%s.md" % agent)).read_text(encoding="utf-8")
    body = body.replace("{PY}", "/usr/bin/python3" if Path("/usr/bin/python3").exists()
                        else sys.executable).replace("{SKILL}", SKILL_REF)
    return "%s\n%s\n%s" % (BEGIN, body.rstrip(), END)


def merge_instructions(text, block):
    """Return text with exactly one managed block (pure; tested)."""
    if BEGIN in text and END in text:
        start = text.index(BEGIN)
        stop = text.index(END, start) + len(END)
        return text[:start] + block + text[stop:]
    if LEGACY_HEADING in text:
        start = text.index(LEGACY_HEADING)
        nxt = start + len(LEGACY_HEADING)
        stop = len(text)
        for marker in ("\n## ", "\n# "):
            pos = text.find(marker, nxt)
            if pos != -1:
                stop = min(stop, pos + 1)
        tail = text[stop:]
        return text[:start] + block + ("\n\n" + tail.lstrip("\n") if tail.strip() else "\n")
    sep = "" if not text else ("\n" if text.endswith("\n") else "\n\n")
    if text and not text.endswith("\n\n"):
        sep = "\n" if text.endswith("\n") else "\n\n"
    return text + sep + block + "\n"


def install_instructions(agent, check):
    path = INSTRUCTION_TARGETS[agent]
    if not path.parent.exists():
        return "%s instructions: not installed (skipped)" % agent
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    new = merge_instructions(old, render_block(agent))
    if new == old:
        return "%s instructions: ok" % agent
    if check:
        return "%s instructions: MISSING/outdated" % agent
    _backup(path)
    path.write_text(new, encoding="utf-8")
    return "%s instructions: updated %s" % (agent, path)


def install_all(check=False):
    results = [install_claude(check), install_codex(check),
               install_instructions("dsh", check), install_instructions("codex", check)]
    return results


def _fingerprint():
    import hashlib
    digest = hashlib.sha256()
    for path in sorted(TEMPLATES.glob("agents_*.md")) + [Path(__file__).resolve()]:
        digest.update(path.read_bytes())
    for path in (CLAUDE_SETTINGS, CODEX_HOOKS, *INSTRUCTION_TARGETS.values()):
        digest.update(str(path.exists()).encode())
    return digest.hexdigest()[:16]


def ensure_integration():
    """Cheap self-heal called by session_start. Returns human-readable notes
    only when something changed or needs attention; [] on the fast path."""
    if os.environ.get("HUBMEMORY_SKIP_INTEGRATION") or "unittest" in sys.modules:
        return []
    try:
        fp = _fingerprint()
        if STAMP.exists() and json.loads(STAMP.read_text(encoding="utf-8")).get("fp") == fp:
            return []
        if os.environ.get("HUBMEMORY_NO_AUTO_INSTALL"):
            missing = [r for r in install_all(check=True) if "MISSING" in r or "untrusted" in r]
            return ["hubmemory: agent integration incomplete (%s); run python3 %s"
                    % ("; ".join(missing), Path(__file__).resolve())] if missing else []
        results = install_all(check=False)
        STAMP.parent.mkdir(parents=True, exist_ok=True)
        STAMP.write_text(json.dumps({"fp": _fingerprint(), "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                     "results": results}, ensure_ascii=False), encoding="utf-8")
        changed = [r for r in results if any(w in r for w in ("installed", "written", "updated", "WARNING"))]
        return ["hubmemory: wired agent integration (backups *.bak_hubmemory_*): " + "; ".join(changed)] if changed else []
    except Exception as exc:  # never break session start
        return ["hubmemory: agent integration check failed: %s" % exc]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    results = install_all(args.check)
    print("\n".join(results))
    if not args.check:
        STAMP.parent.mkdir(parents=True, exist_ok=True)
        STAMP.write_text(json.dumps({"fp": _fingerprint(), "results": results}, ensure_ascii=False), encoding="utf-8")
    bad = any(("MISSING" in r or "untrusted" in r or "incomplete" in r) for r in results)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
