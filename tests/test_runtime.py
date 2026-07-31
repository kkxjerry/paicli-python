from __future__ import annotations

import json
import tempfile
import unittest
import urllib.request
from pathlib import Path

from paicli.runtime import DurableTaskManager, RuntimeApiServer, TaskStatus


class RuntimeTest(unittest.TestCase):
    def test_background_task_persists_result(self) -> None:
        """后台 runner 的结果既出现在任务对象中，也写入 JSON 文件。"""

        with tempfile.TemporaryDirectory() as directory:
            # Arrange：runner 将 prompt 转大写，便于看出它真的被执行。
            store = Path(directory, "tasks.json")
            manager = DurableTaskManager(
                store,
                lambda prompt, _token: prompt.upper(),
            )
            # Act：提交后等待完成，再关闭线程池。
            task = manager.submit("build it")

            completed = manager.wait(task.id, timeout=2)
            manager.close()

            # Assert：验证终态、runner 输出和磁盘中的任务 id。
            self.assertEqual(TaskStatus.SUCCEEDED, completed.status)
            self.assertEqual("BUILD IT", completed.result)
            self.assertIn(task.id, store.read_text(encoding="utf-8"))

    def test_local_runtime_api_accepts_task(self) -> None:
        """POST /tasks 可提交任务，返回的 id 能在 manager 中等到成功。"""

        with tempfile.TemporaryDirectory() as directory:
            # Arrange：以 port=0 启动仅测试期间存活的本地服务。
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

            # Act：真正发出 HTTP POST，再按响应 id 等待后台任务。
            with urllib.request.urlopen(request, timeout=2) as response:
                payload = json.loads(response.read())
            completed = manager.wait(payload["id"], timeout=2)
            server.close()
            manager.close()

            # Assert：HTTP 层不仅接收请求，底层 runner 也确实完成。
            self.assertEqual(TaskStatus.SUCCEEDED, completed.status)


if __name__ == "__main__":
    unittest.main()
