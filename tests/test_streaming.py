from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from paicli.agent import Agent
from paicli.llm_client import ChatResponse, OpenAICompatibleClient
from paicli.tools import ToolRegistry


class _StreamingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        assert request["stream"] is True
        chunks = [
            {"choices": [{"delta": {"reasoning_content": "inspect "}}]},
            {"choices": [{"delta": {"reasoning_details": [{"text": "file"}]}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"pa',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": 'th":"a.txt"}'},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 4,
                    "prompt_tokens_details": {"cached_tokens": 3},
                },
            },
        ]
        payload = "".join(
            "data: " + json.dumps(chunk) + "\n\n" for chunk in chunks
        ) + "data: [DONE]\n\n"
        encoded = payload.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)


class _Server:
    def __enter__(self):  # type: ignore[no-untyped-def]
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _StreamingHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.url = f"http://{host}:{port}/v1"
        return self

    def __exit__(self, *_args):  # type: ignore[no-untyped-def]
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class _LoopStreamingClient:
    model = "stream-model"
    provider = "test"
    context_window = 32_000
    supports_prompt_caching = False

    def chat(self, messages, tools):  # type: ignore[no-untyped-def]
        return ChatResponse("blocking")

    def chat_stream(self, messages, tools, handler):  # type: ignore[no-untyped-def]
        handler("reasoning", "checking")
        handler("content", "final")
        return ChatResponse(
            "final",
            input_tokens=5,
            output_tokens=1,
            reasoning="checking",
            streamed=True,
        )


class StreamingTest(unittest.TestCase):
    def test_openai_compatible_sse_reconstructs_reasoning_tool_and_usage(self) -> None:
        events: list[tuple[str, str]] = []
        with _Server() as server:
            client = OpenAICompatibleClient(
                api_key="test-key",
                model="stream-model",
                base_url=server.url,
                timeout_seconds=5,
            )
            response = client.chat_stream(
                [{"role": "user", "content": "read"}],
                [
                    {
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "description": "read",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
                lambda kind, value: events.append((kind, value)),
            )

        self.assertTrue(response.streamed)
        self.assertEqual("inspect file", response.reasoning)
        self.assertEqual(1, len(response.tool_calls))
        self.assertEqual("read_file", response.tool_calls[0].name)
        self.assertEqual('{"path":"a.txt"}', response.tool_calls[0].arguments)
        self.assertEqual((12, 4, 3), (
            response.input_tokens,
            response.output_tokens,
            response.cached_input_tokens,
        ))
        self.assertEqual(
            [("reasoning_delta", "inspect "), ("reasoning_delta", "file")],
            events,
        )

    def test_agent_loop_uses_streaming_client_and_marks_outcome(self) -> None:
        events: list[tuple[str, str]] = []
        agent = Agent(
            _LoopStreamingClient(),
            ToolRegistry("."),
            on_event=lambda kind, text: events.append((kind, text)),
        )

        outcome = agent.run_outcome("answer")

        self.assertTrue(outcome.succeeded)
        self.assertTrue(outcome.streamed)
        self.assertIn(("reasoning_delta", "checking"), events)
        self.assertIn(("content_delta", "final"), events)
        self.assertEqual("final", outcome.content)


if __name__ == "__main__":
    unittest.main()
