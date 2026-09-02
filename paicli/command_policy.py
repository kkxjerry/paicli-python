"""Hard command policy shared by every execution path.

Human approval is deliberately not part of this module.  A destructive command
must remain blocked when PaiCLI is embedded as a library, when HITL is disabled,
or when a caller supplies an allow-all approval handler.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandPolicyMatch:
    """One deterministic hard-policy match."""

    rule_id: str
    reason: str
    pattern: str


class CommandGuard:
    """Reject high-confidence destructive shell patterns before execution.

    This is an application-level last line of defence, not an OS sandbox.  The
    list intentionally contains only commands whose common meaning is globally
    destructive or privilege-escalating; project-specific allow/deny choices
    belong to the persistent permission layer.
    """

    BLOCKED_RULES: tuple[tuple[str, str, str], ...] = (
        ("privilege-escalation", r"\bsudo\b", "privilege escalation is blocked"),
        (
            "recursive-force-delete",
            r"(?:^|[;&|]\s*)rm\s+(?:-[A-Za-z]*r[A-Za-z]*f[A-Za-z]*|-[A-Za-z]*f[A-Za-z]*r[A-Za-z]*)\b",
            "recursive forced deletion is blocked",
        ),
        ("format-filesystem", r"\bmkfs(?:\.\w+)?\b", "filesystem formatting is blocked"),
        (
            "raw-device-write",
            r"\bdd\b[^\n]*\bof\s*=\s*/dev/",
            "raw device writes are blocked",
        ),
        (
            "fork-bomb",
            r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",
            "shell fork bombs are blocked",
        ),
        (
            "download-pipe-shell",
            r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:sh|bash|zsh|dash)\b",
            "download-and-execute pipelines are blocked",
        ),
        (
            "power-control",
            r"\b(?:shutdown|reboot|poweroff|halt)\b",
            "host power-control commands are blocked",
        ),
        (
            "world-writable-root",
            r"\bchmod\s+(?:-[A-Za-z]+\s+)*-?R\s+777\s+/(?:\s|$)",
            "recursive world-writable permissions on root are blocked",
        ),
        (
            "recursive-root-owner-change",
            r"\bchown\s+(?:-[A-Za-z]+\s+)*-?R\b[^\n]*\s+/(?:\s|$)",
            "recursive ownership changes on root are blocked",
        ),
    )

    @classmethod
    def match(cls, command: str) -> CommandPolicyMatch | None:
        normalized = str(command or "").strip()
        for rule_id, pattern, reason in cls.BLOCKED_RULES:
            if re.search(pattern, normalized, flags=re.IGNORECASE):
                return CommandPolicyMatch(rule_id, reason, pattern)
        return None

    @classmethod
    def reject_reason(cls, command: str) -> str | None:
        match = cls.match(command)
        if match is None:
            return None
        return f"command rejected by hard policy ({match.rule_id}): {match.reason}"


__all__ = ["CommandGuard", "CommandPolicyMatch"]
