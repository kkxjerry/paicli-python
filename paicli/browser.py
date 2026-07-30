"""Phase 13：Chrome DevTools MCP 浏览器控制的类型化边界。

BrowserConnector 不直接控制 Chrome，只把 navigate/snapshot/click 转换成
Chrome DevTools MCP 的工具名和参数，同时在调用前执行 URL 策略。
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol


class BrowserMode(str, Enum):
    """浏览器会话是独立创建，还是复用现有 CDP endpoint。"""

    ISOLATED = "isolated"
    REUSE = "reuse"


@dataclass
class BrowserSession:
    """记录浏览器会话的身份、连接方式和活跃时间。"""

    id: str
    endpoint: str
    mode: BrowserMode
    created_at: float
    last_used_at: float

    def touch(self) -> None:
        # 后续可根据 last_used_at 清理长时间未使用的会话。
        self.last_used_at = time.time()


@dataclass(frozen=True)
class BrowserCheckResult:
    """URL 策略的三态结果：拒绝、直接允许、需审批后允许。"""

    allowed: bool
    reason: str
    requires_approval: bool = False


class SensitivePagePolicy:
    """拦截危险 scheme，并将登录/付费等页面升级为人工审批。"""

    SENSITIVE_PATTERNS = (
        r"/login(?:/|$)",
        r"/signin(?:/|$)",
        r"/checkout(?:/|$)",
        r"/billing(?:/|$)",
        r"/password(?:/|$)",
    )

    def check(self, url: str) -> BrowserCheckResult:
        lowered = url.lower()
        # file/javascript/data/chrome 可访问本地资源或执行脚本，直接禁止。
        if lowered.startswith(("file:", "javascript:", "data:", "chrome:")):
            return BrowserCheckResult(False, "browser scheme is blocked")
        if not lowered.startswith(("http://", "https://")):
            return BrowserCheckResult(False, "URL must use http or https")
        # 敏感页面不立即拒绝，而是要求 BrowserConnector 调用 approval。
        if any(re.search(pattern, lowered) for pattern in self.SENSITIVE_PATTERNS):
            return BrowserCheckResult(
                True,
                "sensitive page requires approval",
                requires_approval=True,
            )
        return BrowserCheckResult(True, "public web page")


class BrowserToolCaller(Protocol):
    """底层可以是 McpClient.call_tool，测试中也可以是普通假函数。"""

    def __call__(self, name: str, arguments: dict[str, object]) -> str:
        """Call one browser tool exposed by Chrome DevTools MCP."""


BrowserApproval = Callable[[str, dict[str, object]], bool]


class BrowserConnector:
    """将业务友好方法映射为 Chrome DevTools MCP 工具名的外观层。"""

    def __init__(
        self,
        call_tool: BrowserToolCaller,
        *,
        policy: SensitivePagePolicy | None = None,
        approval: BrowserApproval | None = None,
    ) -> None:
        self.call_tool = call_tool
        self.policy = policy or SensitivePagePolicy()
        # 未配置审批器时默认拒绝敏感操作，避免“无 UI 等于自动允许”。
        self.approval = approval or (lambda _action, _arguments: False)

    def navigate(self, url: str) -> str:
        # 先策略，再审批，最后才调远端工具。
        check = self.policy.check(url)
        if not check.allowed:
            raise ValueError(check.reason)
        arguments: dict[str, object] = {"url": url}
        if check.requires_approval and not self.approval("navigate", arguments):
            raise PermissionError("navigation denied by user")
        return self.call_tool("navigate_page", arguments)

    def snapshot(self) -> str:
        # 快照与截图不同，通常返回可供 Agent 理解的页面结构。
        return self.call_tool("take_snapshot", {})

    def click(self, selector: str) -> str:
        if not selector.strip():
            raise ValueError("selector cannot be empty")
        return self.call_tool("click", {"selector": selector})


@dataclass(frozen=True)
class ChromeDevToolsMcpConfig:
    """只生成命令配置；构造对象或调用 command() 都不会启动本地进程。"""

    browser_url: str = "http://127.0.0.1:9222"
    package: str = "chrome-devtools-mcp@latest"

    def command(self) -> list[str]:
        # 返回 list 而非 Shell 字符串，方便 StdioTransport 安全交给 subprocess。
        return [
            "npx",
            "-y",
            self.package,
            f"--browser-url={self.browser_url}",
        ]


class BrowserSessionManager:
    """管理已连接的 CDP 会话，优先复用而不是反复创建。

    本类只管理 BrowserSession 元数据，不会真正启动 Chrome，也不检测
    endpoint 是否真的可连接。
    """

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
            # REUSE 模式下，同 endpoint 且未超过空闲 TTL 就返回同一个对象。
            existing = self._sessions.get(endpoint)
            if existing and now - existing.last_used_at <= self.idle_ttl_seconds:
                existing.touch()
                return existing

        # ISOLATED 模式，或者旧会话已过期，都会生成新 id。
        session = BrowserSession(
            id=uuid.uuid4().hex,
            endpoint=endpoint,
            mode=mode,
            created_at=now,
            last_used_at=now,
        )
        # 同一 endpoint 只记录最新的会话；这不是多会话池。
        self._sessions[endpoint] = session
        return session

    def disconnect(self, endpoint: str) -> None:
        # pop(..., None) 使“重复断开”也是安全的。
        self._sessions.pop(endpoint, None)

    def active_sessions(self) -> list[BrowserSession]:
        # 返回新 list，避免调用者直接改写内部字典。
        return list(self._sessions.values())
