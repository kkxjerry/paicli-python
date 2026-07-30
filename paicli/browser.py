"""Phase 13: Chrome DevTools MCP browser control boundaries."""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol


class BrowserMode(str, Enum):
    ISOLATED = "isolated"
    REUSE = "reuse"


@dataclass
class BrowserSession:
    id: str
    endpoint: str
    mode: BrowserMode
    created_at: float
    last_used_at: float

    def touch(self) -> None:
        self.last_used_at = time.time()


@dataclass(frozen=True)
class BrowserCheckResult:
    allowed: bool
    reason: str
    requires_approval: bool = False


class SensitivePagePolicy:
    SENSITIVE_PATTERNS = (
        r"/login(?:/|$)",
        r"/signin(?:/|$)",
        r"/checkout(?:/|$)",
        r"/billing(?:/|$)",
        r"/password(?:/|$)",
    )

    def check(self, url: str) -> BrowserCheckResult:
        lowered = url.lower()
        if lowered.startswith(("file:", "javascript:", "data:", "chrome:")):
            return BrowserCheckResult(False, "browser scheme is blocked")
        if not lowered.startswith(("http://", "https://")):
            return BrowserCheckResult(False, "URL must use http or https")
        if any(re.search(pattern, lowered) for pattern in self.SENSITIVE_PATTERNS):
            return BrowserCheckResult(
                True,
                "sensitive page requires approval",
                requires_approval=True,
            )
        return BrowserCheckResult(True, "public web page")


class BrowserToolCaller(Protocol):
    def __call__(self, name: str, arguments: dict[str, object]) -> str:
        """Call one browser tool exposed by Chrome DevTools MCP."""


BrowserApproval = Callable[[str, dict[str, object]], bool]


class BrowserConnector:
    """Typed façade over Chrome DevTools MCP tool names."""

    def __init__(
        self,
        call_tool: BrowserToolCaller,
        *,
        policy: SensitivePagePolicy | None = None,
        approval: BrowserApproval | None = None,
    ) -> None:
        self.call_tool = call_tool
        self.policy = policy or SensitivePagePolicy()
        self.approval = approval or (lambda _action, _arguments: False)

    def navigate(self, url: str) -> str:
        check = self.policy.check(url)
        if not check.allowed:
            raise ValueError(check.reason)
        arguments: dict[str, object] = {"url": url}
        if check.requires_approval and not self.approval("navigate", arguments):
            raise PermissionError("navigation denied by user")
        return self.call_tool("navigate_page", arguments)

    def snapshot(self) -> str:
        return self.call_tool("take_snapshot", {})

    def click(self, selector: str) -> str:
        if not selector.strip():
            raise ValueError("selector cannot be empty")
        return self.call_tool("click", {"selector": selector})


@dataclass(frozen=True)
class ChromeDevToolsMcpConfig:
    """Configuration only; constructing it never launches a local process."""

    browser_url: str = "http://127.0.0.1:9222"
    package: str = "chrome-devtools-mcp@latest"

    def command(self) -> list[str]:
        return [
            "npx",
            "-y",
            self.package,
            f"--browser-url={self.browser_url}",
        ]


class BrowserSessionManager:
    """Reuses an existing CDP session instead of launching a new browser."""

    def __init__(self, *, idle_ttl_seconds: float = 900) -> None:
        self.idle_ttl_seconds = idle_ttl_seconds
        self._sessions: dict[str, BrowserSession] = {}

    def connect(
        self,
        endpoint: str,
        *,
        mode: BrowserMode = BrowserMode.REUSE,
    ) -> BrowserSession:
        now = time.time()
        if mode is BrowserMode.REUSE:
            existing = self._sessions.get(endpoint)
            if existing and now - existing.last_used_at <= self.idle_ttl_seconds:
                existing.touch()
                return existing

        session = BrowserSession(
            id=uuid.uuid4().hex,
            endpoint=endpoint,
            mode=mode,
            created_at=now,
            last_used_at=now,
        )
        self._sessions[endpoint] = session
        return session

    def disconnect(self, endpoint: str) -> None:
        self._sessions.pop(endpoint, None)

    def active_sessions(self) -> list[BrowserSession]:
        return list(self._sessions.values())
