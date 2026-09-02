from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from paicli.lsp import LspManager, LspSeverity


_SERVER = r'''
import json
import sys

def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        value = line.decode("ascii").strip()
        if not value:
            break
        key, item = value.split(":", 1)
        headers[key.lower()] = item.strip()
    body = sys.stdin.buffer.read(int(headers["content-length"]))
    return json.loads(body.decode("utf-8"))

def send(value):
    body = json.dumps(value, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()

while True:
    message = read_message()
    if message is None:
        break
    if message.get("method") == "initialize":
        send({"jsonrpc":"2.0","id":message["id"],"result":{"capabilities":{}}})
    elif message.get("method") == "textDocument/didOpen":
        document = message["params"]["textDocument"]
        send({
            "jsonrpc":"2.0",
            "method":"textDocument/publishDiagnostics",
            "params":{
                "uri":document["uri"],
                "diagnostics":[{
                    "range":{"start":{"line":1,"character":4},"end":{"line":1,"character":5}},
                    "severity":1,
                    "source":"fake-lsp",
                    "message":"example diagnostic"
                }]
            }
        })
    elif message.get("method") == "shutdown":
        send({"jsonrpc":"2.0","id":message["id"],"result":None})
    elif message.get("method") == "exit":
        break
'''


class StdioLspTest(unittest.TestCase):
    def test_real_stdio_protocol_diagnostics_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "server.py"
            script.write_text(textwrap.dedent(_SERVER), encoding="utf-8")
            source = root / "sample.py"
            source.write_text("x = 1\ny = 2\n", encoding="utf-8")
            manager = LspManager(
                root,
                python_lsp_command=(sys.executable, str(script)),
                lsp_timeout_seconds=2,
            )

            report = manager.diagnostics_for("sample.py")

        self.assertTrue(report.has_errors)
        self.assertEqual(1, len(report.diagnostics))
        diagnostic = report.diagnostics[0]
        self.assertEqual((2, 5), (diagnostic.line, diagnostic.column))
        self.assertEqual(LspSeverity.ERROR, diagnostic.severity)
        self.assertEqual("fake-lsp", diagnostic.source)

    def test_unavailable_server_falls_back_to_ast(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "broken.py"
            source.write_text("def broken(:\n", encoding="utf-8")
            manager = LspManager(
                root,
                python_lsp_command=(str(root / "missing-language-server"),),
                lsp_timeout_seconds=0.1,
            )

            report = manager.diagnostics_for("broken.py")

        self.assertTrue(report.has_errors)
        self.assertEqual("python-ast", report.diagnostics[0].source)


if __name__ == "__main__":
    unittest.main()
