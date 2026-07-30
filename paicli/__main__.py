"""Command-line entry point for the learning agent."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .agent import Agent, AgentLoopError
from .llm_client import LlmError, OpenAICompatibleClient
from .tools import ToolRegistry


def load_dotenv(path: Path = Path(".env")) -> None:
    """Load simple KEY=VALUE entries without overriding the real environment."""

    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def print_event(kind: str, text: str) -> None:
    if kind == "tool":
        print(f"\n[tool] {text}")
    elif kind == "result":
        print(f"[result] {text}")


def build_parser() -> argparse.ArgumentParser:
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
    load_dotenv()
    args = build_parser().parse_args()

    try:
        client = OpenAICompatibleClient.from_env()
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        return 2

    tools = ToolRegistry(
        args.project_root,
        allow_shell=args.allow_shell or env_flag("PAICLI_ALLOW_SHELL"),
    )
    agent = Agent(
        client,
        tools,
        max_steps=args.max_steps,
        on_event=print_event,
    )

    if args.prompt:
        return run_once(agent, args.prompt)

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
    try:
        answer = agent.run(prompt)
        print(f"\n{answer}")
        return 0
    except (AgentLoopError, LlmError, ValueError) as exc:
        print(f"\nError: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

