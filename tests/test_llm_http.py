from __future__ import annotations

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from unittest.mock import patch

from paicli.llm_client import OpenAICompatibleClient


class RecordingHandler(BaseHTTPRequestHandler):
    """记录真实 HTTP 请求的本地测试服务器。"""

    request_payload: ClassVar[dict[str, object] | None] = None
    authorization: ClassVar[str] = ""

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        type(self).request_payload = json.loads(self.rfile.read(length))
        type(self).authorization = self.headers.get("Authorization", "")
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-http-1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path":"README.md"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 21,
                    "completion_tokens": 4,
                    "prompt_tokens_details": {"cached_tokens": 7},
                },
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return None


class LlmHttpTest(unittest.TestCase):
    def test_real_http_transport_serializes_and_parses_tool_call(self) -> None:
        """不使用 FakeClient，真正经过 HTTP socket 发送消息并解析 tool_call。"""

        # Arrange：本地端口只代替外部模型服务，客户端 HTTP 代码是真实执行的。
        server = ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address[:2]
        client = OpenAICompatibleClient(
            "secret",
            "test-model",
            f"http://{host}:{port}/v1",
            timeout_seconds=2,
        )

        try:
            # Act：发起真实 POST /v1/chat/completions。
            response = client.chat(
                [{"role": "user", "content": "Read README"}],
                [
                    {
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "description": "Read one file",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        # Assert：请求体、鉴权头和服务端返回的工具调用都经过真实协议边界。
        self.assertEqual("test-model", RecordingHandler.request_payload["model"])  # type: ignore[index]
        self.assertEqual("Bearer secret", RecordingHandler.authorization)
        self.assertEqual("read_file", response.tool_calls[0].name)
        self.assertEqual('{"path":"README.md"}', response.tool_calls[0].arguments)
        self.assertEqual(21, response.input_tokens)
        self.assertEqual(4, response.output_tokens)
        self.assertEqual(7, response.cached_input_tokens)

    def test_loopback_endpoint_ignores_environment_proxy(self) -> None:
        """SSH 隧道的 127.0.0.1 请求必须直连，不能被系统代理接管。"""

        server = ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address[:2]

        # 故意设置一个不可达的代理。如果客户端没有绕过它，请求会立即失败。
        proxy_environment = {
            "HTTP_PROXY": "http://127.0.0.1:1",
            "http_proxy": "http://127.0.0.1:1",
            "ALL_PROXY": "http://127.0.0.1:1",
            "all_proxy": "http://127.0.0.1:1",
        }
        try:
            with patch.dict(os.environ, proxy_environment, clear=False):
                client = OpenAICompatibleClient(
                    "secret",
                    "test-model",
                    f"http://{host}:{port}/v1",
                    timeout_seconds=2,
                )
                response = client.chat(
                    [{"role": "user", "content": "Read README"}],
                    [],
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual("read_file", response.tool_calls[0].name)
