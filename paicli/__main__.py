"""第一期 CLI 外壳：读取配置、创建依赖，并把用户输入交给 Agent。"""

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
    """读取简单的 KEY=VALUE 配置，但不覆盖系统中已经存在的环境变量。"""

    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        # 忽略空行、注释和不符合 KEY=VALUE 格式的内容。
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        # setdefault 保证真正的系统环境变量拥有更高优先级。
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def env_flag(name: str, default: bool = False) -> bool:
    """把常见的布尔环境变量写法转换为 bool。"""

    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def build_parser() -> argparse.ArgumentParser:
    """定义 CLI 支持的命令行参数。"""

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
    """组装 LLM、ToolRegistry 和 Agent，并启动单次或交互模式。"""

    # 先加载 .env，再解析和使用配置。
    load_dotenv()
    args = build_parser().parse_args()

    try:
        # LLM 客户端负责网络协议，Agent 不直接读取 API Key。
        # 指定 provider 时使用第 8 期工厂，否则保留第一期通用配置。
        client = (
            LlmClientFactory.create(args.provider)
            if args.provider
            else OpenAICompatibleClient.from_env()
        )
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        return 2

    # project_root 决定文件工具能够访问的最大目录范围。
    tools = ToolRegistry(
        args.project_root,
        allow_shell=args.allow_shell or env_flag("PAICLI_ALLOW_SHELL"),
    )
    renderer = create_renderer(args.renderer)

    # 依赖由 CLI 创建并注入 Agent，测试时可以替换成 FakeClient。
    agent = Agent(
        client,
        tools,
        max_steps=args.max_steps,
        on_event=lambda kind, text: (
            renderer.event(kind, text) if kind != "answer" else None
        ),
    )
    image_processor = ImageProcessor(args.project_root)

    # -p/--prompt 适合脚本调用：只执行一次，随后退出。
    if args.prompt:
        try:
            prompt, images = image_processor.from_prompt(args.prompt)
        except ValueError as exc:
            print(f"Input error: {exc}")
            return 2
        return run_once(agent, prompt, tuple(images))

    # 没有 -p 时进入交互式 REPL；下面的历史和状态栏来自后续阶段。
    provider = getattr(client, "provider", "custom")
    renderer.status(
        StatusInfo(provider, client.model, "react", "phase 22")
    )
    print("\nCommands: /help, /tools, /model, /context, /history, /clear, /exit")
    history = PaiCliHistory(Path.home() / ".paicli" / "history.json")
    while True:
        try:
            prompt = normalize_input(input("\n> "))
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not prompt:
            continue

        # Slash 命令由 CLI 自己处理，不需要消耗一次模型请求。
        try:
            command = CliCommandParser.parse(prompt)
        except ValueError as exc:
            print(f"Error: {exc}")
            continue
        if command:
            if handle_command(command, agent, tools, history):
                return 0
            continue
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
    """执行一轮用户任务，并把领域异常转换成 CLI 退出码。"""

    try:
        answer = agent.run(prompt, images=images)
        print(f"\n{answer}")
        return 0
    except (AgentLoopError, CancelledError, LlmError, ValueError) as exc:
        print(f"\nError: {exc}")
        return 1


if __name__ == "__main__":
    # python -m paicli 最终会从这里进入 main()。
    raise SystemExit(main())
