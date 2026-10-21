#!/usr/bin/env python3
"""Query-intent parsing for hubmemory hybrid retrieval.

Deterministic and declarative — no ML, no per-case hardcoding:

  * temporal rules map natural zh/en time phrases (刚才/刚刚/昨天/今天/
    上次/最近/just now/yesterday/last time/recent ...) to recency hints:
    either a forward-looking half-life (relative kinds) or a target day
    (explicit dates and day offsets like 昨天/前天).
  * scaffold patterns strip conversational filler (politeness, imperative
    frames like 你看一下/帮我找, deixis 那个/这个, interrogatives, English
    pronouns/articles) while preserving technical terms — each removal is
    recorded so the cleaned query is auditable.
  * actor references (另一个agent/另外一个会话/another agent) and
    production scaffolds (写的/做的/改的/I wrote/created) become intent
    flags the ranker uses for scope and delivered-vs-planned weighting.

The module is pure stdlib and has no I/O — parse_intent() is the only API.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta


# ---------------------------------------------------------------------------
# Temporal rules — first match wins. `day_offset` anchors the recency window
# at a fixed day (symmetric distance from that day's noon); `half_life_minutes`
# decays forward-only from `now` (how stale "just now" content may be).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TemporalRule:
    pattern: "re.Pattern[str]"
    kind: str
    half_life_minutes: float | None = None
    day_offset: int | None = None


_TEMPORAL_RULES: tuple[TemporalRule, ...] = (
    TemporalRule(
        re.compile(
            r"刚才|刚刚|刚(?=[写做生发提创记说改删弄修])"
            r"|just\s+now|a\s+moment\s+ago|a\s+second\s+ago|\bjust\b",
            re.I,
        ),
        "just_now", 30.0,
    ),
    TemporalRule(re.compile(r"今天|今日|today", re.I), "today", 360.0),
    TemporalRule(
        re.compile(r"前天|the\s+day\s+before\s+yesterday", re.I),
        "day_before_yesterday", day_offset=-2,
    ),
    TemporalRule(re.compile(r"昨天|昨日|yesterday", re.I), "yesterday", day_offset=-1),
    TemporalRule(
        re.compile(r"上次|上一次|前次|之前|last\s+time|previously|earlier", re.I),
        "last", 1440.0,
    ),
    TemporalRule(
        re.compile(r"最近几天|最近|这几天|近期|these\s+days|recent(?:ly)?|lately", re.I),
        "recent", 4320.0,
    ),
    TemporalRule(re.compile(r"这周|本周|this\s+week", re.I), "this_week", 4320.0),
    TemporalRule(re.compile(r"上周|last\s+week", re.I), "last_week", 10080.0),
)

# Half-life used when recency is measured as distance from a target day's
# noon (explicit dates and day-offset kinds).
SYMMETRIC_HALF_LIFE_MINUTES = 1200.0

_EXPLICIT_DATE_RE = re.compile(
    r"(?:(?P<y>20\d{2})\s*[-/年.]\s*)?(?P<m>\d{1,2})\s*[-/月.]\s*(?P<d>\d{1,2})\s*[日号]"
    r"|(?P<y2>20\d{2})-(?P<m2>\d{2})-(?P<d2>\d{2})"
)

# ---------------------------------------------------------------------------
# Actor / artifact scaffolds — matched on the query BEFORE generic filler
# removal so the flags reflect the user's wording, not the cleaned topic.
# Multi-char verbs precede single-char ones so 修改的/整理成 match whole.
# ---------------------------------------------------------------------------

_OTHER_AGENT_RE = re.compile(
    r"(?:另(?:外)?|别的?|其他)一?个?\s*(?:agent|会话|session|智能体)"
    r"|(?:another|other)\s+(?:agent|session)",
    re.I,
)

# Production scaffolds: a "made thing" is being referenced -> artifact intent.
# The verb+particle span is conversational noise, so it is also removed.
_ARTIFACT_SCAFFOLD_RE = re.compile(
    r"(?:新建|创建|生成|修改|整理|产出|起草|撰写|提交|写|做|改|修|弄)[的了过出]"
    r"|\b(?:i|you|we)\s+(?:wrote|created|generated|made|drafted|produced|added|built)\b",
    re.I,
)

# Reporting scaffolds: conversational verbs that are filler but do NOT imply
# the user wants a produced artifact (发现的/提到的/说的).
_REPORTING_SCAFFOLD_RE = re.compile(r"(?:发现|提到|说|问|聊)[的了过]")

# ---------------------------------------------------------------------------
# Generic conversational filler. Each pattern is intentionally narrow:
# imperative frames require the verb+一下/下 shape or a leading address,
# politeness is boundary-anchored, articles/pronouns are English \b-bounded —
# so technical terms (检索/搜索/检查 as topic nouns, i/o, file names) survive.
# ---------------------------------------------------------------------------

_SCAFFOLD_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("politeness", re.compile(
        r"^(?:请|麻烦(?:你|您)?|打扰一下|hi|hello|hey|你好|您好)[，,。.!！?？\s]*", re.I)),
    ("politeness_mid", re.compile(
        r"(?<=[，,。.!！?？、\s])(?:请|麻烦|帮我|帮忙|给我)")),
    ("imperative", re.compile(
        r"(?:你|您|帮我|帮忙|给我|能否|可以|可否|能不能)?\s*"
        r"(?:看|查|找|搜|读|打开|检查|回顾|翻阅|翻|浏览|总结|盘点)"
        r"(?:一下|下下|下(?=个|的|我|你|那|这|[，,。.!！?？\s]|$))", re.I)),
    ("imperative_repeated", re.compile(r"^(?:帮我|帮忙|给我)?(?:把\s*)?(?:看看|查查|找找|读读)\s*")),
    ("request_connector", re.compile(r"^(?:帮我|帮忙|给我)(?:把)?\s*")),
    ("imperative_addr", re.compile(
        r"^(?:你|您|能不能|可以|可否)(?:看|查|找|搜|读|打开|检查|回顾|帮我|帮忙|给我)", re.I)),
    ("request_frame_en", re.compile(
        r"^(?:please\s+)?(?:(?:can|could|would|will)\s+you\s+)?"
        r"(?:help\s+me\s+|i\s+(?:want|need|would\s+like|'d\s+like)\s+to\s+)?"
        r"(?:look\s+at|check\s+(?:out\s+)?|check|show\s+me|show|find|read|open"
        r"|review|go\s+over|search\s+for|pull\s+up)\b[\s,]*", re.I)),
    ("pronoun_en", re.compile(r"\b(?:i|me|my|you|your|we|the|a|an|that|this)\b", re.I)),
    ("thanks", re.compile(r"(?:please|thanks|thank\s+you)\b[.!！。,，\s]*$", re.I)),
    ("deixis", re.compile(
        r"那个的|这个的|那份的|那个|这个|那份|一件|一份|一条"
        r"|(?<![第另这那哪前后上下])一(?:个|份|条|件)"
        r"|(?:这|那|几|好几)?(?:几|一|二|两|三|四|五|六|七|八|九|十|\d+)个?天"
        r"|(?:几|一|二|两|三|四|五|\d+)个?(?:周|星期|个月)")),
    ("interrogative", re.compile(
        r"什么|啥|怎么样|如何|哪些|哪个|哪里|在哪|\b(?:what|which|how|where)\b", re.I)),
    # A connector orphaned at a boundary by scaffold removal is filler;
    # mid-string 的 keeps both neighbours in the topic (unigram weight ~0).
    ("connector_edge", re.compile(r"^(?:的|之|地|得)+|(?:的|之|地|得)+$")),
    ("particle_tail", re.compile(r"[吗呢吧啊呀嘛哦哈了~～!.?？。，,、\s]+$")),
)


def parse_intent(query: str, now: datetime | None = None) -> dict:
    """Parse a natural zh/en retrieval query into topic + intent hints.

    Returns a JSON-friendly dict:
      raw, clean, removed, temporal, other_agent, artifact_request
    `temporal` is None or {kind, half_life_minutes, symmetric, target_date?}.
    """
    ref = now or datetime.now().astimezone()
    text = (query or "").strip()
    removed: list[dict] = []

    def _cut(match, kind):
        nonlocal text
        removed.append({"kind": kind, "text": match.group(0).strip()})
        text = (text[:match.start()] + " " + text[match.end():]).strip()

    # --- temporal -----------------------------------------------------------
    temporal = None
    match = _EXPLICIT_DATE_RE.search(text)
    if match:
        try:
            year = int(match["y"] or match["y2"] or ref.year)
            target = date(year, int(match["m"] or match["m2"]),
                          int(match["d"] or match["d2"]))
        except (ValueError, TypeError):
            target = None
        if target:
            temporal = {
                "kind": "explicit_date",
                "target_date": target.isoformat(),
                "half_life_minutes": SYMMETRIC_HALF_LIFE_MINUTES,
                "symmetric": True,
            }
            _cut(match, "temporal")
    if temporal is None:
        for rule in _TEMPORAL_RULES:
            match = rule.pattern.search(text)
            if not match:
                continue
            _cut(match, "temporal")
            if rule.day_offset is not None:
                target = ref.date() + timedelta(days=rule.day_offset)
                temporal = {
                    "kind": rule.kind,
                    "target_date": target.isoformat(),
                    "half_life_minutes": SYMMETRIC_HALF_LIFE_MINUTES,
                    "symmetric": True,
                }
            else:
                temporal = {
                    "kind": rule.kind,
                    "half_life_minutes": rule.half_life_minutes,
                    "symmetric": False,
                }
            break

    # --- actor --------------------------------------------------------------
    other_agent = False
    match = _OTHER_AGENT_RE.search(text)
    if match:
        other_agent = True
        _cut(match, "actor")

    # --- artifact / reporting scaffolds (to fixpoint; spans are removed) -----
    artifact_request = False
    for _ in range(4):
        match = _ARTIFACT_SCAFFOLD_RE.search(text)
        if not match:
            break
        artifact_request = True
        _cut(match, "artifact_scaffold")
    for _ in range(4):
        match = _REPORTING_SCAFFOLD_RE.search(text)
        if not match:
            break
        _cut(match, "reporting_scaffold")

    # --- generic filler (to fixpoint) ---------------------------------------
    for _ in range(8):
        matched = False
        for name, pattern in _SCAFFOLD_PATTERNS:
            match = pattern.search(text)
            if match and (match.group(0).strip() or name == "particle_tail"):
                _cut(match, name)
                matched = True
                break
        if not matched:
            break

    clean = " ".join(text.split())
    return {
        "raw": query,
        "clean": clean,
        "removed": removed,
        "temporal": temporal,
        "other_agent": other_agent,
        "artifact_request": artifact_request,
    }


if __name__ == "__main__":  # tiny manual probe
    import json
    import sys

    probe = " ".join(sys.argv[1:]) or "你看一下刚才写的项目立项书"
    print(json.dumps(parse_intent(probe), ensure_ascii=False, indent=2))
