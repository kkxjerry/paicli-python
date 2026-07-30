"""第一期 CLI 外壳：读取配置、创建依赖，并把用户输入交给 Agent。"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .agent import Agent, AgentLoopError
from .llm_client import LlmError, OpenAICompatibleClient
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


def print_event(kind: str, text: str) -> None:
    """显示工具调用过程；最终答案由 run_once 统一输出。"""

    if kind == "tool":
        print(f"\n[tool] {text}")
    elif kind == "result":
        print(f"[result] {text}")


def build_parser() -> argparse.ArgumentParser:
    """定义第一期支持的命令行参数。"""

    parser = argparse.ArgumentParser(description="Minimal Phase 1 ReAct agent")
    parser.add_argument("-p", "--prompt", help="Run one prompt and exit")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="Directory exposed to file tools",
    )
    parser.add_argument("--max-steps", type=int, default=20)
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
        client = OpenAICompatibleClient.from_env()
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        return 2

    # project_root 决定文件工具能够访问的最大目录范围。
    tools = ToolRegistry(
        args.project_root,
        allow_shell=args.allow_shell or env_flag("PAICLI_ALLOW_SHELL"),
    )
    # 依赖由 CLI 创建并注入 Agent，测试时可以替换成 FakeClient。
    agent = Agent(
        client,
        tools,
        max_steps=args.max_steps,
        on_event=print_event,
    )

    # -p/--prompt 适合脚本调用：只执行一次，随后退出。
    if args.prompt:
        return run_once(agent, args.prompt)

    # 没有 -p 时进入最小 REPL。
    print("PaiCLI Python Phase 1")
    print("Commands: /tools, /clear, /exit")
    while True:
        try:
            prompt = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not prompt:
            continue
        # Slash 命令由 CLI 自己处理，不需要消耗一次模型请求。
        if prompt == "/exit":
            return 0
        if prompt == "/clear":
            agent.clear_history()
            print("History cleared.")
            continue
        if prompt == "/tools":
            print("\n".join(f"- {name}" for name in tools.names()))
            continue
        run_once(agent, prompt)


def run_once(agent: Agent, prompt: str) -> int:
    """执行一轮用户任务，并把领域异常转换成 CLI 退出码。"""

    try:
        answer = agent.run(prompt)
        print(f"\n{answer}")
        return 0
    except (AgentLoopError, LlmError, ValueError) as exc:
        print(f"\nError: {exc}")
        return 1


if __name__ == "__main__":
    # python -m paicli 最终会从这里进入 main()。
    raise SystemExit(main())
