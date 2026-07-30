from __future__ import annotations

import tempfile
import threading
import unittest

from paicli.tools import ToolRegistry, ToolSpec


class ParallelToolsTest(unittest.TestCase):
    def test_execute_many_runs_calls_concurrently_in_stable_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ToolRegistry(directory)
            barrier = threading.Barrier(2)

            def synchronized(arguments: dict[str, object]) -> str:
                barrier.wait(timeout=1)
                return str(arguments["value"])

            registry.register(
                ToolSpec(
                    "synchronized",
                    "Test concurrent execution.",
                    registry.object_schema(
                        {"value": {"type": "string"}},
                        required=["value"],
                    ),
                    synchronized,
                )
            )

            results = registry.execute_many(
                [
                    ("synchronized", '{"value":"first"}'),
                    ("synchronized", '{"value":"second"}'),
                ]
            )

            self.assertEqual(["first", "second"], results)


if __name__ == "__main__":
    unittest.main()
