from __future__ import annotations

import unittest
from typing import Any

from paicli.llm_client import ChatResponse, LlmError, RetryingLlmClient


class SequenceClient:
    def __init__(self, values: list[ChatResponse | BaseException]) -> None:
        self.values = list(values)
        self.calls = 0
        self.model = "fake"
        self.provider = "fake"

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ChatResponse:
        del messages, tools
        self.calls += 1
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class LlmRetryTest(unittest.TestCase):
    def test_retries_only_explicit_transient_failures(self) -> None:
        client = SequenceClient(
            [
                LlmError("rate limited", status_code=429, retryable=True),
                ChatResponse("ok"),
            ]
        )
        sleeps: list[float] = []
        resilient = RetryingLlmClient(
            client,
            max_attempts=3,
            base_delay_seconds=0.1,
            sleep=sleeps.append,
        )

        response = resilient.chat([], [])

        self.assertEqual("ok", response.content)
        self.assertEqual(2, client.calls)
        self.assertEqual([0.1], sleeps)
        self.assertEqual(2, resilient.last_attempts)

    def test_honors_retry_after_and_stops_at_bound(self) -> None:
        error = LlmError(
            "busy",
            status_code=503,
            retryable=True,
            retry_after_seconds=0.75,
        )
        client = SequenceClient([error, error])
        sleeps: list[float] = []
        resilient = RetryingLlmClient(
            client,
            max_attempts=2,
            base_delay_seconds=0.1,
            sleep=sleeps.append,
        )

        with self.assertRaises(LlmError):
            resilient.chat([], [])

        self.assertEqual(2, client.calls)
        self.assertEqual([0.75], sleeps)

    def test_permanent_failure_is_not_retried(self) -> None:
        client = SequenceClient(
            [LlmError("bad request", status_code=400, retryable=False)]
        )
        resilient = RetryingLlmClient(
            client,
            max_attempts=3,
            sleep=lambda _delay: self.fail("sleep must not be called"),
        )

        with self.assertRaises(LlmError):
            resilient.chat([], [])

        self.assertEqual(1, client.calls)

    def test_proxy_exposes_provider_metadata(self) -> None:
        client = SequenceClient([ChatResponse("ok")])
        resilient = RetryingLlmClient(client)

        self.assertEqual("fake", resilient.model)
        self.assertEqual("fake", resilient.provider)


if __name__ == "__main__":
    unittest.main()
