"""Persistent argument-aware tool permission rules."""

from __future__ import annotations

import fnmatch
import glob
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping


class PermissionAction(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass(frozen=True)
class PermissionRule:
    id: str
    tool_name: str
    action: PermissionAction
    argument_patterns: dict[str, str]
    created_at: float
    description: str = ""

    @classmethod
    def create(
        cls,
        tool_name: str,
        action: PermissionAction | str,
        argument_patterns: Mapping[str, object] | None = None,
        *,
        description: str = "",
    ) -> "PermissionRule":
        name = str(tool_name).strip()
        if not name:
            raise ValueError("permission rule tool_name cannot be empty")
        resolved_action = (
            action if isinstance(action, PermissionAction) else PermissionAction(action)
        )
        return cls(
            "rule_" + uuid.uuid4().hex,
            name,
            resolved_action,
            {str(key): str(value) for key, value in dict(argument_patterns or {}).items()},
            time.time(),
            str(description).strip(),
        )

    def matches(self, tool_name: str, arguments: Mapping[str, object]) -> bool:
        if not fnmatch.fnmatchcase(str(tool_name), self.tool_name):
            return False
        return all(
            key in arguments and fnmatch.fnmatchcase(str(arguments[key]), pattern)
            for key, pattern in self.argument_patterns.items()
        )


class PermissionStore:
    VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._rules: list[PermissionRule] = []
        self.reload()

    @property
    def rules(self) -> tuple[PermissionRule, ...]:
        return tuple(self._rules)

    def reload(self) -> None:
        if not self.path.is_file():
            self._rules = []
            return
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("permission file root must be an object")
        if int(payload.get("version", self.VERSION)) != self.VERSION:
            raise ValueError("unsupported permission file version")
        raw_rules = payload.get("rules", [])
        if not isinstance(raw_rules, list):
            raise ValueError("permission rules must be an array")
        parsed: list[PermissionRule] = []
        for index, raw in enumerate(raw_rules):
            if not isinstance(raw, dict):
                raise ValueError(f"permission rules[{index}] must be an object")
            patterns = raw.get("argument_patterns", {})
            if not isinstance(patterns, dict):
                raise ValueError(
                    f"permission rules[{index}].argument_patterns must be an object"
                )
            parsed.append(
                PermissionRule(
                    str(raw.get("id") or "rule_" + uuid.uuid4().hex),
                    str(raw["tool_name"]),
                    PermissionAction(str(raw["action"])),
                    {str(key): str(value) for key, value in patterns.items()},
                    float(raw.get("created_at", 0.0)),
                    str(raw.get("description", "")),
                )
            )
        self._rules = parsed

    def resolve(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> tuple[PermissionAction, PermissionRule | None]:
        for rule in reversed(self._rules):
            if rule.matches(tool_name, arguments):
                return rule.action, rule
        return PermissionAction.ASK, None

    def add(self, rule: PermissionRule) -> PermissionRule:
        self._rules.append(rule)
        self.save()
        return rule

    def add_exact(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
        *,
        action: PermissionAction | str = PermissionAction.ALLOW,
    ) -> PermissionRule:
        """Persist a literal call without granting accidental glob authority."""

        return self.add(
            PermissionRule.create(
                glob.escape(str(tool_name)),
                action,
                {
                    str(key): glob.escape(str(value))
                    for key, value in arguments.items()
                },
                description="remembered exact interactive decision",
            )
        )

    def remove(self, rule_id: str) -> bool:
        kept = [rule for rule in self._rules if rule.id != rule_id]
        if len(kept) == len(self._rules):
            return False
        self._rules = kept
        self.save()
        return True

    def clear(self) -> None:
        self._rules.clear()
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.VERSION,
            "rules": [
                {**asdict(rule), "action": rule.action.value}
                for rule in self._rules
            ],
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass


def default_permission_path(project_root: str | Path) -> Path:
    return Path(project_root).resolve() / ".paicli" / "permissions.json"


__all__ = [
    "PermissionAction",
    "PermissionRule",
    "PermissionStore",
    "default_permission_path",
]
