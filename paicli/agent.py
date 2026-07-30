"""第一期核心：完成一次“模型判断 -> 工具执行 -> 结果回灌”的 ReAct 循环。"""

from __future__ import annotations

from typing import Any, Callable

from .llm_client import LlmClient
from .tools import ToolRegistry

# 事件回调只负责把运行过程展示给 CLI，不参与 Agent 的业务判断。
EventHandler = Callable[[str, str], None]

# 系统提示词规定 Agent 的身份和结束条件。
# 它会成为消息历史的第一条记录，每次调用模型时都会一起发送。
SYSTEM_PROMPT = """You are a coding agent working inside one project directory.
Use tools when you need to inspect or modify the project.
After a tool result arrives, continue reasoning from that result.
When the task is complete, answer the user directly without calling a tool."""


class AgentLoopError(RuntimeError):
    """模型在最大循环次数内始终没有给出最终答案。"""


class Agent:
    """协调模型调用、工具执行和工具结果回灌。"""

    def __init__(
        self,
        client: LlmClient,
        tools: ToolRegistry,
        *,
        max_steps: int = 20,
        on_event: EventHandler | None = None,
    ) -> None:
        # 限制循环次数，防止模型一直要求调用工具而无法结束。
        if max_steps < 1:
            raise ValueError("max_steps must be positive")

        # Agent 只依赖 LlmClient 协议，而不关心具体接的是哪一家模型。
        self.client = client
        # ToolRegistry 保存模型当前可以使用的工具及其执行函数。
        self.tools = tools
        self.max_steps = max_steps
        self.on_event = on_event or (lambda _kind, _text: None)

        # history 是发送给模型的完整消息链。
        # 第一条永远是 system，之后按 user/assistant/tool 的顺序增长。
        self.history: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

    def run(self, user_input: str) -> str:
        """循环执行 ReAct，直到模型返回不含工具调用的普通答案。"""

        if not user_input.strip():
            raise ValueError("user_input cannot be empty")

        # 第一步：把本轮用户问题放进消息历史。
        self.history.append({"role": "user", "content": user_input})

        # 一轮循环对应一次模型请求。模型可能连续调用多轮工具。
        for _step in range(1, self.max_steps + 1):
            # 把“消息历史 + 工具 JSON Schema”交给模型。
            # 模型只能提出调用哪个工具，本身不会执行本地 Python 函数。
            response = self.client.chat(self.history, self.tools.definitions())

            # 模型的回复也必须保存。尤其是 tool_calls，下一轮模型需要看到
            # 自己之前发起了哪些调用，否则 tool 结果会失去对应关系。
            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": response.content,
            }

            if response.tool_calls:
                assistant_message["tool_calls"] = [
                    call.as_message_dict() for call in response.tool_calls
                ]
            self.history.append(assistant_message)

            # 没有 tool_calls 说明模型认为任务已经完成，可以直接结束循环。
            if not response.tool_calls:
                self.on_event("answer", response.content)
                return response.content

            # 有 tool_calls 时，Agent 逐个找到本地工具并执行。
            for call in response.tool_calls:
                self.on_event("tool", f"{call.name} {call.arguments}")
                result = self.tools.execute(call.name, call.arguments)
                self.on_event("result", result)

                # 工具结果使用 role=tool 回灌。
                # tool_call_id 必须与模型发起调用时的 id 相同，这样模型才能
                # 知道当前结果属于哪一次工具调用。
                self.history.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": result,
                    }
                )

        # 超过最大轮数仍在调用工具，主动停止，避免无限循环和费用失控。
        raise AgentLoopError(
            f"model did not return a final answer within {self.max_steps} steps"
        )

    def clear_history(self) -> None:
        """清空对话，但保留定义 Agent 身份的 system 消息。"""

        self.history = [self.history[0]]
