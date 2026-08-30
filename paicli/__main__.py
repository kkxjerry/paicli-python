"""PaiCLI command-line assembly for ReAct, Plan, and Team modes."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .agent import Agent, AgentLoopError
from .agents.budget import AgentBudget
from .bootstrap import ApplicationRuntime, build_application_runtime
from .images import ImageAttachment, ImageProcessor
from .interaction import CliCommand, CliCommandParser, PaiCliHistory, normalize_input
from .llm_client import LlmClientFactory, LlmError, OpenAICompatibleClient
from .model_probe import ProbeMode, probe_model
from .orchestration import (
    OrchestrationResult,
    OrchestrationStatus,
    PlanReviewDecision,
    PlanReviewHandler,
)
from .planning import ExecutionPlan, PlanGenerationError
from .rendering import StatusInfo, create_renderer
from .runtime import CancelledError

PlanApproval = PlanReviewHandler


def load_dotenv(path: Path = Path(".env")) -> None:
    """Load simple KEY=VALUE lines without overriding the process environment."""

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


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def stagnation_window(value: str) -> int:
    parsed = positive_int(value)
    if parsed < 2:
        raise argparse.ArgumentTypeError("must be at least 2")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PaiCLI Python learning agent")
    parser.add_argument("-p", "--prompt", help="Run one prompt and exit")
    parser.add_argument(
        "--mode",
        choices=("react", "plan", "team"),
        default="react",
        help="Execution mode for ordinary prompts",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="Directory exposed to file tools",
    )
    parser.add_argument(
        "--max-steps",
        type=positive_int,
        default=AgentBudget.DEFAULT_HARD_MAX_ITERATIONS,
    )
    parser.add_argument(
        "--subagent-max-steps",
        type=positive_int,
        default=12,
        help="Maximum model iterations for each worker/reviewer sub-agent turn",
    )
    parser.add_argument(
        "--stagnation-window",
        type=stagnation_window,
        default=AgentBudget.DEFAULT_STAGNATION_WINDOW,
        help="Stop after this many identical action/observation rounds",
    )
    parser.add_argument(
        "--token-budget",
        type=positive_int,
        help="Optional per-agent input+output token budget",
    )
    parser.add_argument(
        "--plan-workers",
        type=positive_int,
        default=4,
        help="Maximum parallel read-only workers in plan mode",
    )
    parser.add_argument(
        "--plan-revisions",
        type=non_negative_int,
        default=2,
        help="Maximum pre-execution plan revisions requested by the user",
    )
    parser.add_argument(
        "--team-workers",
        type=positive_int,
        default=2,
        help="Maximum parallel read-only workers in team mode",
    )
    parser.add_argument(
        "--review-retries",
        type=non_negative_int,
        default=2,
        help="Local worker retries after changes_requested",
    )
    parser.add_argument(
        "--provider",
        choices=sorted(LlmClientFactory.PROVIDERS),
        help="Use a configured cloud provider or OpenAI-compatible vLLM server",
    )
    parser.add_argument(
        "--renderer",
        choices=("plain", "inline"),
        default="inline",
    )
    parser.add_argument(
        "--allow-shell",
        action="store_true",
        help="Allow scoped workers to execute shell commands",
    )
    parser.add_argument(
        "--memory-file",
        type=Path,
        help="Long-term memory JSONL path (default: ~/.paicli/memory.jsonl)",
    )
    parser.add_argument(
        "--no-memory",
        action="store_true",
        help="Disable context compaction, retrieval, and save_memory",
    )
    parser.add_argument(
        "--check-model",
        choices=("chat", "tools"),
        help="Call the configured real model once and exit",
    )
    return parser


def main() -> int:
    load_dotenv()
    args = build_parser().parse_args()

    try:
        client = (
            LlmClientFactory.create(args.provider)
            if args.provider
            else OpenAICompatibleClient.from_env()
        )
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        return 2

    if args.check_model:
        return run_model_probe(client, args.check_model)

    renderer = create_renderer(args.renderer)
    runtime = build_application_runtime(
        client,
        args.project_root,
        allow_shell=args.allow_shell or env_flag("PAICLI_ALLOW_SHELL"),
        enable_memory=not args.no_memory,
        memory_path=args.memory_file,
        max_steps=args.max_steps,
        subagent_max_steps=args.subagent_max_steps,
        token_budget=args.token_budget,
        stagnation_window=args.stagnation_window,
        plan_workers=args.plan_workers,
        plan_revisions=args.plan_revisions,
        team_workers=args.team_workers,
        review_retries=args.review_retries,
        # Final answers are printed exactly once by run_selected_mode.
        on_event=lambda kind, text: (
            renderer.event(kind, text) if kind != "answer" else None
        ),
    )
    image_processor = ImageProcessor(args.project_root)

    if args.prompt:
        try:
            prompt, images = image_processor.from_prompt(args.prompt)
        except ValueError as exc:
            print(f"Input error: {exc}")
            return 2
        return run_selected_mode(runtime, args.mode, prompt, tuple(images))

    provider = getattr(client, "provider", "custom")
    renderer.status(
        StatusInfo(
            provider,
            client.model,
            args.mode,
            f"window={runtime.settings.window} memory={'off' if args.no_memory else 'on'}",
        )
    )
    print(
        "\nCommands: /help, /plan <task>, /team <task>, /tools, /model, "
        "/context, /history, /clear, /exit"
    )
    history = PaiCliHistory(Path.home() / ".paicli" / "history.json")
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
            if handle_command(command, runtime, history):
                return 0
            continue

        history.add(prompt)
        try:
            clean_prompt, images = image_processor.from_prompt(prompt)
        except ValueError as exc:
            print(f"Input error: {exc}")
            continue
        run_selected_mode(
            runtime,
            args.mode,
            clean_prompt,
            tuple(images),
            plan_approval=(
                interactive_plan_approval if args.mode == "plan" else None
            ),
        )


def handle_command(
    command: CliCommand,
    runtime: ApplicationRuntime,
    history: PaiCliHistory,
    *,
    plan_approval: PlanApproval | None = None,
) -> bool:
    """Execute one local slash command; only /exit returns True."""

    agent = runtime.react.agent
    if command.name == "exit":
        return True
    if command.name == "clear":
        agent.clear_history()
        print("ReAct history cleared. Plan/Team sub-agents are isolated per run.")
    elif command.name == "tools":
        print("\n".join(f"- {name}" for name in runtime.tools.names()))
    elif command.name == "history":
        print("\n".join(history.recent()) or "(empty)")
    elif command.name in {"context", "config"}:
        client = agent.client
        print(
            f"model={getattr(client, 'model', 'unknown')} "
            f"provider={getattr(client, 'provider', 'custom')} "
            f"messages={len(agent.history)} max_steps={agent.max_steps} "
            f"subagent_max_steps={runtime.subagents.max_steps} "
            f"stagnation_window={agent.stagnation_window} "
            f"token_budget={agent.token_budget or 'unlimited-per-agent'} "
            f"context_window={runtime.settings.window} "
            f"plan_workers={runtime.plan.concurrency.max_workers} "
            f"plan_revisions={runtime.plan.max_plan_revisions} "
            f"team_workers={runtime.team.concurrency.max_workers} "
            f"review_retries={runtime.team.max_review_retries} "
            f"memory={agent.memory.status() if agent.memory else 'disabled'}"
        )
    elif command.name == "model":
        if len(command.arguments) != 1:
            print("Usage: /model <glm|deepseek|stepfun|kimi|vllm>")
        else:
            try:
                runtime.set_client(LlmClientFactory.create(command.arguments[0]))
                print(f"Switched to {runtime.react.agent.client.model}.")
            except ValueError as exc:
                print(f"Configuration error: {exc}")
    elif command.name == "plan":
        goal = " ".join(command.arguments).strip()
        if not goal:
            print("Usage: /plan <task>")
        else:
            run_selected_mode(
                runtime,
                "plan",
                goal,
                (),
                plan_approval=plan_approval or interactive_plan_approval,
            )
    elif command.name == "team":
        goal = " ".join(command.arguments).strip()
        if not goal:
            print("Usage: /team <task>")
        else:
            run_selected_mode(runtime, "team", goal, ())
    elif command.name == "help":
        print(
            "/plan <task> /team <task> /tools /model /context /config "
            "/history /clear /exit"
        )
    return False


def interactive_plan_approval(plan: ExecutionPlan) -> PlanReviewDecision:
    del plan  # The validated plan was already emitted by PlanModeRuntime.
    while True:
        try:
            value = input(
                "Execute this validated plan? [Enter/Y]es/[N]o/[E]dit: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return PlanReviewDecision.cancel()
        if value in {"", "y", "yes", "execute"}:
            return PlanReviewDecision.execute()
        if value in {"n", "no", "cancel"}:
            return PlanReviewDecision.cancel()
        if value in {"e", "edit", "i", "supplement"}:
            try:
                feedback = input("Plan changes> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return PlanReviewDecision.cancel()
            if feedback:
                return PlanReviewDecision.supplement(feedback)
            print("Plan changes cannot be empty.")
            continue
        print("Choose Y to execute, N to cancel, or E to revise the plan.")


def run_selected_mode(
    runtime: ApplicationRuntime,
    mode: str,
    prompt: str,
    images: tuple[ImageAttachment, ...] = (),
    *,
    plan_approval: PlanApproval | None = None,
) -> int:
    """Run one user request through the selected public execution mode."""

    if mode == "react":
        return run_once(runtime.react.agent, prompt, images)
    if images:
        print("Input error: Plan and Team modes currently accept text tasks only")
        return 2

    if mode not in {"plan", "team"}:
        print(f"Input error: unknown execution mode {mode!r}")
        return 2

    try:
        result = (
            runtime.plan.run(prompt, approval=plan_approval)
            if mode == "plan"
            else runtime.team.run(prompt)
        )
    except (PlanGenerationError, AgentLoopError, CancelledError, LlmError, ValueError) as exc:
        print(f"\nError: {exc}")
        return 1
    print(f"\n{result.answer}")
    return orchestration_exit_code(result)


def orchestration_exit_code(result: OrchestrationResult) -> int:
    if result.status in {
        OrchestrationStatus.SUCCEEDED,
        OrchestrationStatus.CANCELLED,
    }:
        return 0
    return 1


def run_once(
    agent: Agent,
    prompt: str,
    images: tuple[ImageAttachment, ...] = (),
) -> int:
    try:
        answer = agent.run(prompt, images=images)
        print(f"\n{answer}")
        return 0
    except (AgentLoopError, CancelledError, LlmError, ValueError) as exc:
        print(f"\nError: {exc}")
        return 1


def run_model_probe(client: OpenAICompatibleClient, mode: ProbeMode) -> int:
    try:
        result = probe_model(client, mode)
    except (LlmError, ValueError) as exc:
        print(f"Model check failed: {exc}")
        return 1
    label = "passed" if result.ok else "failed"
    print(f"Model check {label}: {result.detail}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
