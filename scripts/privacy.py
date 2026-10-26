"""Conservative credential masking for retrieval DISPLAY, never source mutation.

Not a general DLP guarantee: unknown secret formats may not be recognized.
Rules are independently implemented; inspired by deja-vu's output safety model.
"""
import re

_MARK = '[redacted:credential]'
_PATTERNS = [
    (re.compile(r'-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)', re.S), _MARK),
    (re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})\b'), _MARK),
    (re.compile(r'(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{12,}'), lambda m: m[1]+_MARK),
    (re.compile(r'''(?i)((?:[\w.-]*(?:api[_-]?key|secret|access[_-]?token|password|passwd)|密码)\s*["']?\s*[:=]\s*["']?)([^\s"'`,;&<>]{8,})'''), lambda m: m[1]+_MARK),
    (re.compile(r'(https?://[^\s/:@]+:)[^\s/@]+(@)'), lambda m: m[1]+_MARK+m[2]),
]


def redact_value(value):
    """Mask nested presentation metadata without changing the input object."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    return value


def redact(text):
    """Apply before clipping: a truncated key must not escape detection."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
