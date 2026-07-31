"""核心 ReAct 循环：模型判断 -> 工具执行 -> 结果回灌 -> 继续判断。

后续的记忆、上下文、并行工具、LSP 和图片都围绕这条主链路扩展，
但结束条件始终一样：模型返回不含 tool_calls 的 assistant 消息。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from .context import ContextController
from .images import ImageAttachment, multimodal_user_message
from .llm_client import LlmClient
from .lsp import LspDiagnosticFormatter, LspManager
from .memory import MemoryManager
from .runtime import CancellationToken
from .tools import ToolRegistry

# 事件回调只负责展示运行过程，不参与 Agent 的业务判断。
EventHandler = Callable[[str, str], None]

# 系统提示词规定 Agent 身份、工具用法和结束条件。
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
        memory: MemoryManager | None = None,
        cancellation: CancellationToken | None = None,
        context: ContextController | None = None,
        lsp: LspManager | None = None,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        # 限制循环次数，防止模型一直要求工具而无法结束。
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        # Agent 仅依赖 LlmClient 协议，测试可换成 FakeClient。
        self.client = client
        # ToolRegistry 保存模型能看到的 schema 和真正的 Python handler。
        self.tools = tools
        self.max_steps = max_steps
        self.on_event = on_event or (lambda _kind, _text: None)
        # memory 是可选的：不传时保持 Phase 01 的全量 history 行为。
        self.memory = memory
        # 未传入时为该 Agent 创建独立令牌，避免多 Agent 意外共享取消状态。
        self.cancellation = cancellation or CancellationToken()
        # ContextController 是 MemoryManager 之上的自适应策略层。
        self.context = context
        # lsp 只在 write_file 成功后做诊断，不参与工具是否成功的判定。
        self.lsp = lsp
        # history 是完整消息链；system_prompt 始终放在首位。
        self.history: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]

    def run(
        self,
        user_input: str,
        *,
        images: tuple[ImageAttachment, ...] = (),
    ) -> str:
        """循环执行 ReAct，直到模型返回不含工具调用的普通答案。

        images 已由 ImageProcessor 完成安全校验，这里只负责组装多模态用户消息。
        """

        if not user_input.strip() and not images:
            raise ValueError("user_input and images cannot both be empty")
        # 允许“只发图片”；有图时 content 是 parts 列表，无图时仍是普通字符串。
        self.history.append(multimodal_user_message(user_input, images))

        # 一次循环就是一次模型请求；模型可能连续调用多轮工具。
        for _step in range(1, self.max_steps + 1):
            # history 始终保留完整对话；messages 是本轮真正传给 LLM 的上下文。
            # 启用 MemoryManager 后，messages 可能已压缩旧消息并注入长期记忆。
            # 每次调模型前检查，取消后不再发新请求。
            self.cancellation.check()
            if self.context:
                # 有 Controller 时由它决定是否调用 memory，并执行预算检查。
                messages = self.context.prepare(self.history, self.memory)
            else:
                messages = (
                    self.memory.prepare(self.history) if self.memory else self.history
                )
            # 模型只能“提出”调用哪个工具，真正的 Python 函数由 Agent 后面执行。
            response = self.client.chat(messages, self.tools.definitions())
            # assistant 发出的 tool_calls 也必须保存，否则下一轮 tool 结果无法对应。
            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": response.content,
            }

            if response.tool_calls:
                assistant_message["tool_calls"] = [
                    call.as_message_dict() for call in response.tool_calls
                ]
            self.history.append(assistant_message)

            # 没有 tool_calls 就是唯一的正常结束分支。
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
                # 工具可能运行很久，回灌结果前再给取消一次生效机会。
                self.cancellation.check()
                self.on_event("result", result)
                # tool_call_id 必须与 assistant 发起调用时的 id 相同。
                self.history.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": result,
                    }
                )
                self._report_diagnostics(call.name, call.arguments)

        # 超过上限仍在调工具，主动停止，避免无限循环和费用失控。
        raise AgentLoopError(
            f"model did not return a final answer within {self.max_steps} steps"
        )

    def clear_history(self) -> None:
        """开始新对话，但保留定义 Agent 身份的 system 消息。"""

        self.history = [self.history[0]]

    def _report_diagnostics(self, tool_name: str, arguments_json: str) -> None:
        """write_file 成功后检查该文件，并用 diagnostics 事件回报问题。"""

        # 只有写文件会改变源码；未配置 LSP 时保持以前的行为。
        if self.lsp is None or tool_name != "write_file":
            return
        try:
            # arguments 是模型产生的 JSON，必须容错，诊断不应让主 Agent 循环崩溃。
            path = str(json.loads(arguments_json)["path"])
            report = self.lsp.diagnostics_for(path)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return
        # 没有问题时不产生噪声事件。
        if report.diagnostics:
            self.on_event("diagnostics", LspDiagnosticFormatter.format(report))
