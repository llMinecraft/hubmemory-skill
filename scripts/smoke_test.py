#!/usr/bin/env python3
"""smoke_test — fast regression checks for hubmemory's invariants.

    python3 smoke_test.py     # exit 0 = all pass
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS))

import hybrid_query as hq  # noqa: E402
import lockutil  # noqa: E402
import manifest  # noqa: E402

FAILURES = []


def check(name, ok, detail=""):
    print("[%s] %s %s" % ("PASS" if ok else "FAIL", name, detail))
    if not ok:
        FAILURES.append(name)


with tempfile.TemporaryDirectory() as td:
    lock_path = Path(td) / ".lock"
    with lockutil.file_lock(lock_path) as fh:
        fh.write(b"x")
    check("lockutil.file_lock", lock_path.exists())

check("lockutil.pid_alive(self)", lockutil.pid_alive(os.getpid()))
check("lockutil.pid_alive(bad)", not lockutil.pid_alive(-1))

toks = hq.tokenize("日报生成 report generation")
check("tokenizer cjk bigram", any(len(t) == 2 for t in toks))
check("tokenizer english lower+plural", "report" in toks and "generation" in toks)

filters, rest = hq.extract_filters("agent:codex date:2026-10-13 日报")
check("filters parsed", filters.get("agent") == "codex" and filters.get("date") == "2026-10-13")
check("filters stripped", "agent:" not in rest and "日报" in rest)

expanded = hq.expand_synonyms(["日记"])
check("synonym expand", "diary" in expanded)

FENCE = chr(96) * 3
DIARY = (
    "# 2026-10-13 工作日记\n"
    "> TL;DR: x\n"
    "### 工作事项 · Session aaa\n"
    "- 工作目标：should be ignored\n- 当前状态：已完成\n"
    "## Agent-readable summary\n\n" + FENCE + "json\n"
    + json.dumps({"date": "2026-10-13", "task_count": 1, "done": 0,
                  "open_loops": ["loop"], "tasks": [
                      {"goal": "real goal", "status": "in_progress",
                       "conclusion": "c"}]}, ensure_ascii=False)
    + "\n" + FENCE + "\n"
)
parsed = manifest.parse_diary(DIARY)
check("manifest prefers json block",
      parsed["goals"] == ["real goal"] and parsed["statuses"].get("wip") == 1,
      str(parsed["goals"]))

parsed2 = manifest.parse_diary(DIARY.split("## Agent-readable summary")[0])
check("manifest prose fallback", parsed2["goals"] == ["should be ignored"])

check("status icons", manifest.norm_status("已完成") == "done"
      and manifest.norm_status("blocked") == "blocked"
      and manifest.norm_status("???") == "?")

print("")
print("checks done, %d failure(s)" % len(FAILURES))
sys.exit(0 if not FAILURES else 1)

