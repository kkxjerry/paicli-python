from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from paicli.bootstrap import build_application_runtime
from paicli.extensions import (
    ExtensionConfig,
    ExtensionConfigurationError,
    SafeWebFetcher,
    SkillExtensionConfig,
    install_extensions,
    load_extension_config,
)
from paicli.llm_client import ChatResponse
from paicli.mcp import McpError, StdioTransport
from paicli.tools import ToolRegistry


class _Client:
    model = "fake-model"
    provider = "fake"
    context_window = 32_000
    supports_prompt_caching = False

    def chat(self, messages, tools):  # type: ignore[no-untyped-def]
        return ChatResponse("done")


class _Response:
    def __init__(self, url: str, body: bytes, content_type: str = "text/html") -> None:
        self._url = url
        self._body = body
        self.headers = Message()
        self.headers["Content-Type"] = content_type + "; charset=utf-8"

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *_args):  # type: ignore[no-untyped-def]
        return False

    def geturl(self) -> str:
        return self._url

    def read(self, _limit: int) -> bytes:
        return self._body


class ExtensionTest(unittest.TestCase):
    def test_skill_extension_is_reachable_from_product_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill_root = root / "skills" / "review"
            skill_root.mkdir(parents=True)
            (skill_root / "SKILL.md").write_text(
                "---\n"
                "name: review-code\n"
                "description: Review changed Python code\n"
                "allowed-tools: [read_file]\n"
                "---\n"
                "Inspect the changed file before making a claim.\n",
                encoding="utf-8",
            )
            runtime = build_application_runtime(
                _Client(),
                root,
                enable_memory=False,
                enable_rag=False,
                enable_trace=False,
                enable_hitl=False,
                extension_config=ExtensionConfig(
                    skills=SkillExtensionConfig((str(root / "skills"),))
                ),
            )
            try:
                self.assertIn("load_skill", runtime.tools.names())
                prompt = str(runtime.react.agent.history[0]["content"])
                self.assertIn("review-code", prompt)
                result = runtime.tools.execute_result(
                    "load_skill",
                    json.dumps({"name": "review-code"}),
                )
                self.assertTrue(result.ok, result.content)
                self.assertIn("Inspect the changed file", result.content)
            finally:
                runtime.close()

    def test_config_is_explicit_and_rejects_unsafe_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "extensions.json"
            config.write_text(
                json.dumps(
                    {
                        "skills": {"roots": ["skills"]},
                        "mcp": {"servers": []},
                        "web": {"enabled": False},
                    }
                ),
                encoding="utf-8",
            )
            loaded = load_extension_config(config, root)
            self.assertEqual((str((root / "skills").resolve()),), loaded.skills.roots)

            config.write_text(
                json.dumps({"mcp": {"servers": [{"name": "../bad"}]}}),
                encoding="utf-8",
            )
            with self.assertRaises(ExtensionConfigurationError):
                load_extension_config(config, root)

    def test_web_fetcher_requires_allow_list_and_rejects_private_dns(self) -> None:
        with self.assertRaises(ExtensionConfigurationError):
            SafeWebFetcher(())
        fetcher = SafeWebFetcher(("example.com",))
        with patch(
            "paicli.extensions.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("127.0.0.1", 443))],
        ):
            with self.assertRaisesRegex(ValueError, "non-public"):
                fetcher.fetch("https://example.com/private")

    def test_web_fetcher_validates_public_host_and_strips_active_html(self) -> None:
        fetcher = SafeWebFetcher(("example.com",))
        response = _Response(
            "https://example.com/page",
            b"<html><script>secret()</script><body>Hello <b>world</b></body></html>",
        )
        with patch(
            "paicli.extensions.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
        ), patch.object(fetcher._opener, "open", return_value=response):
            text = fetcher.fetch("https://example.com/page")
        self.assertIn("Hello", text)
        self.assertIn("world", text)
        self.assertNotIn("secret()", text)

    def test_mcp_stdio_timeout_does_not_hang_gateway(self) -> None:
        transport = StdioTransport(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            timeout_seconds=0.05,
        )
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(McpError, "timed out"):
                transport.request({"jsonrpc": "2.0", "id": 1, "method": "x"})
        finally:
            transport.close()
        self.assertLess(time.monotonic() - started, 3)

    def test_mcp_stdio_ignores_notification_before_matching_response(self) -> None:
        script = (
            "import json,sys; "
            "request=json.loads(sys.stdin.readline()); "
            "print(json.dumps({'jsonrpc':'2.0','method':'notifications/tools/list_changed','params':{}}), flush=True); "
            "print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'ok':True}}), flush=True)"
        )
        transport = StdioTransport(
            [sys.executable, "-c", script],
            timeout_seconds=2,
        )
        try:
            response = transport.request(
                {"jsonrpc": "2.0", "id": 7, "method": "tools/list"}
            )
        finally:
            transport.close()

        self.assertEqual(7, response["id"])
        self.assertEqual({"ok": True}, response["result"])
        self.assertEqual(
            "notifications/tools/list_changed",
            transport.notifications[0]["method"],
        )

    def test_install_extensions_leaves_default_registry_unchanged_when_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)
            before = tuple(registry.names())
            installed = install_extensions(registry, directory, ExtensionConfig())
            self.assertEqual(before, tuple(registry.names()))
            self.assertEqual((), installed.installed_tools)
            installed.close()


if __name__ == "__main__":
    unittest.main()
