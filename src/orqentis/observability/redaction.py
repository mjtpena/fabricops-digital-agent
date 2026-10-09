"""Secret redaction applied before anything is logged, persisted as evidence, or sent to a model."""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

_SENSITIVE_KEY = re.compile(
    r"(secret|password|passwd|pwd|token|api[_-]?key|access[_-]?key|account[_-]?key|credential|"
    r"authorization|connection[_-]?string|sas|signature|private[_-]?key|client[_-]?assertion)",
    re.IGNORECASE,
)
# Keys that look sensitive but carry non-secret metadata.
_SAFE_KEYS = frozenset({"token_count", "input_tokens", "output_tokens", "total_tokens", "secret_expires_at"})

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bbearer\s+[a-z0-9\-._~+/]+=*"), f"Bearer {REDACTED}"),
    (re.compile(r"\beyJ[a-zA-Z0-9_-]{8,}\.[a-zA-Z0-9_-]{8,}\.[a-zA-Z0-9_-]{8,}\b"), REDACTED),  # JWT
    (
        re.compile(
            r"(?i)\b(client[_-]?secret|password|pwd|api[_-]?key|access[_-]?key|account[_-]?key|"
            r"shared[_-]?access[_-]?key|token)\s*[:=]\s*(\"[^\"]*\"|'[^']*'|[^\s;,&]+)"
        ),
        rf"\1={REDACTED}",
    ),
    (re.compile(r"(?i)\b(sig|sv|se)=[a-z0-9%+/=_-]{8,}"), rf"\1={REDACTED}"),  # SAS query params
    (re.compile(r"(?i)\bAccountKey=[^;\s]+"), f"AccountKey={REDACTED}"),
    (re.compile(r"(?i)\bInstrumentationKey=[0-9a-f-]{36}"), f"InstrumentationKey={REDACTED}"),
)


def redact_text(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact secrets in strings, mappings and sequences."""
    if _depth > 32:
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() not in _SAFE_KEYS and _SENSITIVE_KEY.search(key):
                out[key] = REDACTED if item not in (None, "") else item
            else:
                out[key] = redact(item, _depth=_depth + 1)
        return out
    if isinstance(value, list | tuple):
        return [redact(v, _depth=_depth + 1) for v in value]
    return value
