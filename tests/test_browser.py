from __future__ import annotations

import unittest

from paicli.browser import BrowserConnector, ChromeDevToolsMcpConfig


class BrowserTest(unittest.TestCase):
    def test_routes_browser_actions_to_mcp_tools(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []
        browser = BrowserConnector(
            lambda name, arguments: calls.append((name, arguments)) or "ok"
        )

        result = browser.navigate("https://example.com/docs")

        self.assertEqual("ok", result)
        self.assertEqual("navigate_page", calls[0][0])

    def test_sensitive_navigation_requires_approval(self) -> None:
        browser = BrowserConnector(lambda _name, _arguments: "unexpected")

        with self.assertRaises(PermissionError):
            browser.navigate("https://example.com/login")
        with self.assertRaises(ValueError):
            browser.navigate("file:///etc/passwd")

    def test_cdp_mcp_command_is_explicit_but_not_executed(self) -> None:
        command = ChromeDevToolsMcpConfig(
            browser_url="http://127.0.0.1:9333"
        ).command()

        self.assertIn("--browser-url=http://127.0.0.1:9333", command)


if __name__ == "__main__":
    unittest.main()
