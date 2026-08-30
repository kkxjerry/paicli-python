from __future__ import annotations

import tempfile
import threading
import unittest

from paicli.tools import (
    ConcurrencyPolicy,
    ToolRegistry,
    ToolRisk,
    ToolSideEffect,
    ToolSpec,
)


class ParallelToolsTest(unittest.TestCase):
    def test_execute_many_runs_calls_concurrently_in_stable_order(self) -> None:
        """同时验证两件事：工具真的并发执行，返回结果仍保持输入顺序。"""

        with tempfile.TemporaryDirectory() as directory:
            # Arrange：Barrier(2) 要求两个线程都到达 wait 后才一起继续。
            # 如果 execute_many 是串行的，第一个调用会因等不到第二个而超时。
            registry = ToolRegistry(directory)
            barrier = threading.Barrier(2)

            def synchronized(arguments: dict[str, object]) -> str:
                barrier.wait(timeout=1)
                return str(arguments["value"])

            # 两次调用使用同一个 handler，但传入不同 value。
            registry.register(
                ToolSpec(
                    "synchronized",
                    "Test concurrent execution.",
                    registry.object_schema(
                        {"value": {"type": "string"}},
                        required=["value"],
                    ),
                    synchronized,
                    risk=ToolRisk.SAFE,
                    side_effect=ToolSideEffect.READ_ONLY,
                    concurrency=ConcurrencyPolicy.PARALLEL,
                )
            )

            # Act：一次提交两个工具调用。
            results = registry.execute_many(
                [
                    ("synchronized", '{"value":"first"}'),
                    ("synchronized", '{"value":"second"}'),
                ]
            )

            # Assert：Barrier 没超时证明它们并发；顺序仍是 first、second。
            self.assertEqual(["first", "second"], results)


if __name__ == "__main__":
    unittest.main()
