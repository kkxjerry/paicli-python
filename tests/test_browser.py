from __future__ import annotations

import unittest

from paicli.browser import BrowserConnector, ChromeDevToolsMcpConfig


class BrowserTest(unittest.TestCase):
    def test_routes_browser_actions_to_mcp_tools(self) -> None:
        """验证公开 HTTPS 导航会映射为 navigate_page MCP 工具调用。"""

        # Arrange：假 call_tool 记录工具名/参数并返回 ok。
        calls: list[tuple[str, dict[str, object]]] = []
        browser = BrowserConnector(
            lambda name, arguments: calls.append((name, arguments)) or "ok"
        )

        # Act：普通 docs 页无需审批。
        result = browser.navigate("https://example.com/docs")

        # Assert：返回底层结果，并且工具名映射正确。
        self.assertEqual("ok", result)
        self.assertEqual("navigate_page", calls[0][0])

    def test_sensitive_navigation_requires_approval(self) -> None:
        """验证登录页默认被用户审批拒绝，file URL 被硬策略拒绝。"""

        # approval 未传，默认返回 False。
        browser = BrowserConnector(lambda _name, _arguments: "unexpected")

        with self.assertRaises(PermissionError):
            browser.navigate("https://example.com/login")
        with self.assertRaises(ValueError):
            browser.navigate("file:///etc/passwd")

    def test_cdp_mcp_command_is_explicit_but_not_executed(self) -> None:
        """验证 CDP endpoint 可被编译进 npx 参数，但测试不启动任何进程。"""

        # Act：command() 只返回 list[str]。
        command = ChromeDevToolsMcpConfig(
            browser_url="http://127.0.0.1:9333"
        ).command()

        # Assert：指定 endpoint 原样出现在命令参数中。
        self.assertIn("--browser-url=http://127.0.0.1:9333", command)


if __name__ == "__main__":
    unittest.main()
