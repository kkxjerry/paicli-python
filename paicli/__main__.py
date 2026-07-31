"""Phase 22：学习版 Agent 的命令行入口与交互循环。

这个文件负责“组装”：读配置、创建 Client/Tools/Agent/Renderer，然后进入
单次 -p 模式或持续 REPL。业务逻辑尽量留在各自模块，便于单元测试。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .agent import Agent, AgentLoopError
from .images import ImageAttachment, ImageProcessor
from .interaction import CliCommand, CliCommandParser, PaiCliHistory, normalize_input
from .llm_client import LlmClientFactory, LlmError, OpenAICompatibleClient
from .rendering import StatusInfo, create_renderer
from .runtime import CancelledError
from .tools import ToolRegistry


def load_dotenv(path: Path = Path(".env")) -> None:
    """加载简单 KEY=VALUE，但不覆盖进程环境中已存在的值。"""

    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        # 忽略空行、注释和不符合 KEY=VALUE 格式的内容。
        if not line or line.startswith("#") or "=" not in line:
            continue
        # 只分割第一个 =，所以 value 中可继续包含 =。这不是完整 dotenv 解析器。
        key, value = line.split("=", 1)
        # setdefault 保证真正的进程环境变量比 .env 优先。
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def env_flag(name: str, default: bool = False) -> bool:
    """把常见的真值字符串转成 bool，环境变量缺失时用默认值。"""

    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def build_parser() -> argparse.ArgumentParser:
    """定义 CLI 启动参数，argparse 负责类型转换和 --help。"""

    parser = argparse.ArgumentParser(description="PaiCLI Python learning agent")
    parser.add_argument("-p", "--prompt", help="Run one prompt and exit")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="Directory exposed to file tools",
    )
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument(
        "--provider",
        choices=sorted(LlmClientFactory.PROVIDERS),
        help="Use a configured GLM, DeepSeek, StepFun, or Kimi provider",
    )
    parser.add_argument(
        "--renderer",
        choices=("plain", "inline"),
        default="inline",
    )
    parser.add_argument(
        "--allow-shell",
        action="store_true",
        help="Allow the model to execute shell commands",
    )
    return parser


def main() -> int:
    """组装所有依赖，运行单次 prompt 或持续交互循环。"""

    # 先读 .env，再解析参数，provider 工厂后面才能读到密钥和模型配置。
    load_dotenv()
    args = build_parser().parse_args()

    try:
        # --provider 选中预置 provider；未指定时使用通用 OpenAI-compatible 环境变量。
        client = (
            LlmClientFactory.create(args.provider)
            if args.provider
            else OpenAICompatibleClient.from_env()
        )
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        return 2

    # project_root 决定文件、图片等工具能访问的最大目录边界。
    # Shell 必须通过命令行或环境变量显式开启，默认只暴露低风险工具。
    tools = ToolRegistry(
        args.project_root,
        allow_shell=args.allow_shell or env_flag("PAICLI_ALLOW_SHELL"),
    )
    renderer = create_renderer(args.renderer)
    # 依赖在 CLI 组装后注入 Agent，所以单元测试可以使用 FakeClient/FakeTool。
    agent = Agent(
        client,
        tools,
        max_steps=args.max_steps,
        # Agent.run 会自己返回 answer，run_once 统一打印，因此事件回调不重复渲染 answer。
        on_event=lambda kind, text: (
            renderer.event(kind, text) if kind != "answer" else None
        ),
    )
    image_processor = ImageProcessor(args.project_root)

    if args.prompt:
        # 非交互模式：解析 @image 后只运行一轮，退出码交给 shell/CI。
        try:
            prompt, images = image_processor.from_prompt(args.prompt)
        except ValueError as exc:
            print(f"Input error: {exc}")
            return 2
        return run_once(agent, prompt, tuple(images))

    # 没有 -p 时才进入交互式 REPL，并加载本地输入历史。
    provider = getattr(client, "provider", "custom")
    renderer.status(
        StatusInfo(provider, client.model, "react", "phase 22")
    )
    print("\nCommands: /help, /tools, /model, /context, /history, /clear, /exit")
    history = PaiCliHistory(Path.home() / ".paicli" / "history.json")
    # 交互模式：命令在本地处理，普通输入才发给 Agent。
    while True:
        try:
            prompt = normalize_input(input("\n> "))
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not prompt:
            continue
        try:
            command = CliCommandParser.parse(prompt)
        except ValueError as exc:
            print(f"Error: {exc}")
            continue
        if command:
            if handle_command(command, agent, tools, history):
                return 0
            continue
        # 只持久化真正的用户 prompt，斜杠命令不进入对话历史文件。
        history.add(prompt)
        try:
            clean_prompt, images = image_processor.from_prompt(prompt)
        except ValueError as exc:
            print(f"Input error: {exc}")
            continue
        run_once(agent, clean_prompt, tuple(images))


def handle_command(
    command: CliCommand,
    agent: Agent,
    tools: ToolRegistry,
    history: PaiCliHistory,
) -> bool:
    """执行一条本地斜杠命令；只有 /exit 返回 True 结束 REPL。"""

    if command.name == "exit":
        return True
    if command.name == "clear":
        agent.clear_history()
        print("History cleared.")
    elif command.name == "tools":
        print("\n".join(f"- {name}" for name in tools.names()))
    elif command.name == "history":
        print("\n".join(history.recent()) or "(empty)")
    elif command.name in {"context", "config"}:
        client = agent.client
        print(
            f"model={getattr(client, 'model', 'unknown')} "
            f"provider={getattr(client, 'provider', 'custom')} "
            f"messages={len(agent.history)} max_steps={agent.max_steps}"
        )
    elif command.name == "model":
        # 切模型只替换 client，保留已有对话历史和工具注册表。
        if len(command.arguments) != 1:
            print("Usage: /model <glm|deepseek|stepfun|kimi>")
        else:
            try:
                agent.client = LlmClientFactory.create(command.arguments[0])
                print(f"Switched to {agent.client.model}.")
            except ValueError as exc:
                print(f"Configuration error: {exc}")
    elif command.name == "help":
        print("/tools /model /context /config /history /clear /exit")
    return False


def run_once(
    agent: Agent,
    prompt: str,
    images: tuple[ImageAttachment, ...] = (),
) -> int:
    """运行一轮 Agent，将预期内错误转换为用户可读信息和退出码。"""

    try:
        answer = agent.run(prompt, images=images)
        print(f"\n{answer}")
        return 0
    except (AgentLoopError, CancelledError, LlmError, ValueError) as exc:
        print(f"\nError: {exc}")
        return 1


if __name__ == "__main__":
    # python -m paicli 最终从这里进入 main()，并将返回值交给 shell。
    raise SystemExit(main())
