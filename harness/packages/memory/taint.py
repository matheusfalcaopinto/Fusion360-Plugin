"""Content classification for persisted memory.

Memory is data, never an instruction channel.  These checks intentionally
target high-signal prompt/tool injection and secret shapes instead of trying to
interpret arbitrary prose.
"""

from __future__ import annotations

import re


class MemoryContentRejected(ValueError):
    """Raised when content is not safe to persist as reusable memory."""

    def __init__(self, flags: list[str]) -> None:
        self.flags = sorted(set(flags))
        super().__init__(f"memory content rejected: {', '.join(self.flags)}")


_INSTRUCTION_PATTERNS = (
    re.compile(
        r"\bignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above)\s+instructions?\b",
        re.I,
    ),
    re.compile(r"\b(?:system|developer)\s+(?:prompt|message|instructions?)\b", re.I),
    re.compile(
        r"\b(?:reveal|print|repeat|exfiltrate)\s+(?:the\s+)?(?:system|developer)\s+prompt\b",
        re.I,
    ),
    re.compile(
        r"\b(?:run|execute)\s+(?:this|the following)\s+(?:command|script|tool)\b", re.I
    ),
)
_TOOL_DIRECTIVE_PATTERNS = (
    re.compile(r"<\/?(?:tool_call|function_call|assistant|system)(?:\s|>)", re.I),
    re.compile(r"\b(?:assistant|model)\s+to\s*=\s*[A-Za-z0-9_.:-]+", re.I),
    re.compile(r"\btool_choice\s*[:=]", re.I),
)
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(
        r"\b(?:api[_ -]?key|access[_ -]?token|bearer|password)\s*[:=]\s*['\"]?[A-Za-z0-9_./+\-=]{16,}",
        re.I,
    ),
    re.compile(r"\b(?:ghp|github_pat|sk)-[A-Za-z0-9_-]{20,}\b"),
)


def inspect_memory_content(content: str) -> list[str]:
    """Return deterministic taint flags for unsafe reusable content."""

    flags: list[str] = []
    if any(pattern.search(content) for pattern in _INSTRUCTION_PATTERNS):
        flags.append("instruction_injection")
    if any(pattern.search(content) for pattern in _TOOL_DIRECTIVE_PATTERNS):
        flags.append("tool_directive")
    if any(pattern.search(content) for pattern in _SECRET_PATTERNS):
        flags.append("possible_secret")
    if "\x00" in content:
        flags.append("binary_content")
    return flags


def validate_memory_content(content: str) -> None:
    """Reject instructions, tool directives, secrets, and binary payloads."""

    flags = inspect_memory_content(content)
    if flags:
        raise MemoryContentRejected(flags)
