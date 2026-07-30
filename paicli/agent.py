"""The complete Phase 1 ReAct loop."""

from __future__ import annotations

from typing import Any, Callable

from .llm_client import LlmClient
from .memory import MemoryManager
from .runtime import CancellationToken
from .tools import ToolRegistry

EventHandler = Callable[[str, str], None]

SYSTEM_PROMPT = """You are a coding agent working inside one project directory.
Use tools when you need to inspect or modify the project.
After a tool result arrives, continue reasoning from that result.
When the task is complete, answer the user directly without calling a tool."""


class AgentLoopError(RuntimeError):
    """Raised when the model never reaches a final answer."""


class Agent:
    """Coordinates model calls and tool-result feedback."""

    def __init__(
        self,
        client: LlmClient,
        tools: ToolRegistry,
        *,
        max_steps: int = 20,
        on_event: EventHandler | None = None,
        memory: MemoryManager | None = None,
        cancellation: CancellationToken | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        self.client = client
        self.tools = tools
        self.max_steps = max_steps
        self.on_event = on_event or (lambda _kind, _text: None)
        # memory 是可选的：不传时保持 Phase 01 的全量 history 行为。
        self.memory = memory
        self.cancellation = cancellation or CancellationToken()
        self.history: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

    def run(self, user_input: str) -> str:
        """Run ReAct until the model returns an answer without tool calls."""

        if not user_input.strip():
            raise ValueError("user_input cannot be empty")
        self.history.append({"role": "user", "content": user_input})

        for _step in range(1, self.max_steps + 1):
            # history 始终保留完整对话；messages 是本轮真正传给 LLM 的上下文。
            # 启用 MemoryManager 后，messages 可能已压缩旧消息并注入长期记忆。
            self.cancellation.check()
            messages = (
                self.memory.prepare(self.history) if self.memory else self.history
            )
            response = self.client.chat(messages, self.tools.definitions())
            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": response.content,
            }

            if response.tool_calls:
                assistant_message["tool_calls"] = [
                    call.as_message_dict() for call in response.tool_calls
                ]
            self.history.append(assistant_message)

            if not response.tool_calls:
                self.on_event("answer", response.content)
                return response.content

            for call in response.tool_calls:
                # 先统一发出工具开始事件，真正执行放在后面批量处理。
                self.on_event("tool", f"{call.name} {call.arguments}")

            # 将 ToolCall 对象转为 ToolRegistry.execute_many 需要的 (name, JSON) 元组。
            calls = [
                (call.name, call.arguments) for call in response.tool_calls
            ]
            if hasattr(self.tools, "execute_many"):
                # 原生 ToolRegistry 支持并行。
                results = self.tools.execute_many(calls)
            else:
                # HitlToolRegistry 等包装器暂无 execute_many，退化为按顺序执行。
                results = [
                    self.tools.execute(name, arguments) for name, arguments in calls
                ]

            # execute_many 虽然并行，但保持输入顺序，所以可以通过 zip 正确回灌 call/result。
            for call, result in zip(response.tool_calls, results, strict=True):
                self.cancellation.check()
                self.on_event("result", result)
                self.history.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": result,
                    }
                )

        raise AgentLoopError(
            f"model did not return a final answer within {self.max_steps} steps"
        )

    def clear_history(self) -> None:
        """Start a new conversation while keeping the system prompt."""

        self.history = [self.history[0]]
