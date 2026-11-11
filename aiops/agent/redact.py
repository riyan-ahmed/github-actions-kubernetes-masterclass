"""Redaction and sanitisation of untrusted text (logs, alert payloads, pod specs)."""
from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

_SENSITIVE_KEY = r"(?:pass(?:word|wd)?|secret|token|api[_-]?key|apikey|authorization|credential|private[_-]?key|access[_-]?key)"

_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S), REDACTED),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"), REDACTED),  # JWT
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), REDACTED),                              # AWS key id
    (re.compile(r"\b(?:ghp|gho|ghs|ghu|github_pat)_[A-Za-z0-9_]{20,}"), REDACTED),         # GitHub tokens
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), REDACTED),                                    # API keys
    (re.compile(r"(://[^/\s:@]+):[^/\s@]+@"), r"\1:" + REDACTED + "@"),                    # url creds
    (re.compile(r"\b([\w.-]+):([^\s:@/]+)@tcp\("), r"\1:" + REDACTED + "@tcp("),          # go mysql DSN
    (re.compile(rf"(?i)(\b[\w.-]*{_SENSITIVE_KEY}[\w.-]*\b\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&\"']+)"),
     r"\1" + REDACTED),
]

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Phrases that indicate someone is trying to steer an LLM/agent through log or alert text.
_INJECTION = [
    re.compile(p, re.I) for p in (
        r"ignore (?:all |any )?(?:previous|prior|above) (?:instructions|prompts?)",
        r"disregard (?:the |all )?(?:previous|above|system)",
        r"you are now\b",
        r"\bsystem prompt\b",
        r"\bnew instructions?\b",
        r"(?:run|execute|please run)\s+(?:the following )?(?:command|shell|kubectl|bash|sh)\b",
        r"\bkubectl\s+(?:delete|exec|apply|patch|edit|scale)\b",
        r"\brm\s+-rf\b",
        r"\bdelete\s+(?:the\s+)?(?:pvc|persistentvolume|database|namespace|secret)",
        r"\bcurl\s+[^|]*\|\s*(?:sh|bash)\b",
    )
]


def redact(text: Any) -> str:
    s = "" if text is None else str(text)
    for pat, repl in _PATTERNS:
        s = pat.sub(repl, s)
    return s


def redact_obj(obj: Any) -> Any:
    """Recursively redact every string; values under sensitive-looking keys are dropped entirely."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, (str, bytes)) and re.search(_SENSITIVE_KEY, str(k), re.I):
                out[k] = REDACTED
            else:
                out[k] = redact_obj(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj


def sanitize_untrusted(text: Any, max_len: int = 300) -> str:
    """Strip control/ANSI sequences, redact secrets and truncate. Always use for log/alert text."""
    s = redact(text)
    s = _ANSI.sub("", s)
    s = _CTRL.sub("", s)
    s = s.replace("\r", " ")
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s


def detect_injection(text: str) -> list[str]:
    """Return the patterns that matched; presence is recorded as a finding, never obeyed."""
    return [p.pattern for p in _INJECTION if p.search(text or "")]
