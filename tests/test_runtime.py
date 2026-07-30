from __future__ import annotations

import json
import tempfile
import unittest
import urllib.request
from pathlib import Path

from paicli.runtime import DurableTaskManager, RuntimeApiServer, TaskStatus


class RuntimeTest(unittest.TestCase):
    def test_background_task_persists_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory, "tasks.json")
            manager = DurableTaskManager(
                store,
                lambda prompt, _token: prompt.upper(),
            )
            task = manager.submit("build it")

            completed = manager.wait(task.id, timeout=2)
            manager.close()

            self.assertEqual(TaskStatus.SUCCEEDED, completed.status)
            self.assertEqual("BUILD IT", completed.result)
            self.assertIn(task.id, store.read_text(encoding="utf-8"))

    def test_local_runtime_api_accepts_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = DurableTaskManager(
                Path(directory, "tasks.json"),
                lambda prompt, _token: prompt,
            )
            server = RuntimeApiServer(manager)
            server.start()
            host, port = server.address
            request = urllib.request.Request(
                f"http://{host}:{port}/tasks",
                data=json.dumps({"prompt": "hello"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            with urllib.request.urlopen(request, timeout=2) as response:
                payload = json.loads(response.read())
            completed = manager.wait(payload["id"], timeout=2)
            server.close()
            manager.close()

            self.assertEqual(TaskStatus.SUCCEEDED, completed.status)


if __name__ == "__main__":
    unittest.main()
