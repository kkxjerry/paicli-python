"""PaiCLI command-line entry point over one coordinated execution path."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Callable

from .agent import Agent, AgentLoopError
from .agents.budget import AgentBudget
from .bootstrap import ApplicationRuntime, build_application_runtime
from .execution import CoordinatedRun, RunCoordinator
from .images import ImageAttachment, ImageProcessor
from .interaction import CliCommand, CliCommandParser, PaiCliHistory, normalize_input
from .llm_client import LlmClientFactory, LlmError, OpenAICompatibleClient
from .model_probe import ProbeMode, probe_model
from .observability import RunLimits
from .orchestration import OrchestrationStatus, PlanApproval, PlanReviewDecision
from .planning import ExecutionPlan
from .policy import ApprovalMode
from .rendering import StatusInfo, create_renderer
from .runtime import CancelledError
from .safety import RollbackPolicy
from .state import RunStateStore


def load_dotenv(path: Path = Path(".env")) -> None:
    """Load simple KEY=VALUE entries without overriding process environment."""

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
    return default if value is None else value.strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


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


def positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def non_negative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def stagnation_window(value: str) -> int:
    parsed = positive_int(value)
    if parsed < 2:
        raise argparse.ArgumentTypeError("must be at least 2")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PaiCLI Python coding agent")
    parser.add_argument("-p", "--prompt", help="Run one prompt and exit")
    parser.add_argument(
        "--mode",
        choices=("react", "plan", "team"),
        default="react",
        help="Execution mode for ordinary prompts",
    )
    parser.add_argument("--resume", metavar="RUN_ID", help="Resume a persisted run")
    parser.add_argument(
        "--list-runs",
        action="store_true",
        help="List recent persisted runs without connecting to a model",
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
    parser.add_argument("--subagent-max-steps", type=positive_int, default=12)
    parser.add_argument(
        "--stagnation-window",
        type=stagnation_window,
        default=AgentBudget.DEFAULT_STAGNATION_WINDOW,
    )
    parser.add_argument(
        "--token-budget",
        type=positive_int,
        help="Per-Agent input+output token limit",
    )
    parser.add_argument("--plan-workers", type=positive_int, default=4)
    parser.add_argument("--plan-revisions", type=non_negative_int, default=2)
    parser.add_argument("--team-workers", type=positive_int, default=2)
    parser.add_argument("--review-retries", type=non_negative_int, default=2)
    parser.add_argument(
        "--provider",
        choices=sorted(LlmClientFactory.PROVIDERS),
        help="Configured cloud provider or OpenAI-compatible vLLM",
    )
    parser.add_argument(
        "--renderer",
        choices=("plain", "inline"),
        default="inline",
    )
    parser.add_argument("--allow-shell", action="store_true")
    parser.add_argument(
        "--approval-mode",
        choices=tuple(item.value for item in ApprovalMode),
        default=ApprovalMode.ASK.value,
        help="ASK for side effects, DENY them, or explicitly ALLOW them",
    )
    parser.add_argument(
        "--rollback-on-failure",
        choices=tuple(item.value for item in RollbackPolicy),
        default=RollbackPolicy.ALWAYS.value,
    )
    parser.add_argument("--no-snapshot", action="store_true")
    parser.add_argument("--memory-file", type=Path)
    parser.add_argument("--no-memory", action="store_true")
    parser.add_argument("--state-path", type=Path)
    parser.add_argument("--trace-path", type=Path)
    parser.add_argument("--audit-path", type=Path)
    parser.add_argument("--no-trace", action="store_true")
    parser.add_argument("--llm-max-attempts", type=positive_int, default=3)
    parser.add_argument("--llm-base-delay", type=non_negative_float, default=0.25)
    parser.add_argument("--llm-max-delay", type=non_negative_float, default=4.0)
    parser.add_argument("--max-run-tokens", type=positive_int)
    parser.add_argument("--max-run-cost-cny", type=positive_float)
    parser.add_argument("--max-run-seconds", type=positive_float)
    parser.add_argument("--max-model-calls", type=positive_int)
    parser.add_argument("--max-tool-calls", type=positive_int)
    parser.add_argument(
        "--check-model",
        choices=("chat", "tools"),
        help="Call the configured real model once and exit",
    )
    return parser


def main() -> int:
    load_dotenv()
    args = build_parser().parse_args()
    root = args.project_root.expanduser().resolve()
    # A project-local .env is useful when PaiCLI is launched from another cwd.
    load_dotenv(root / ".env")
    state_path = args.state_path or root / ".paicli" / "runs.db"

    if args.list_runs:
        return print_recent_runs(state_path)

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

    def event(kind: str, text: str) -> None:
        if kind != "answer":
            renderer.event(kind, text)

    try:
        runtime = build_application_runtime(
            client,
            root,
            allow_shell=args.allow_shell or env_flag("PAICLI_ALLOW_SHELL"),
            enable_memory=not args.no_memory,
            memory_path=args.memory_file,
            max_steps=args.max_steps,
            subagent_max_steps=args.subagent_max_steps,
            stagnation_window=args.stagnation_window,
            token_budget=args.token_budget,
            plan_workers=args.plan_workers,
            plan_revisions=args.plan_revisions,
            team_workers=args.team_workers,
            review_retries=args.review_retries,
            on_event=event,
            enable_hitl=True,
            approval_mode=args.approval_mode,
            audit_path=args.audit_path,
            enable_trace=not args.no_trace,
            trace_path=args.trace_path or root / ".paicli" / "traces.db",
            llm_max_attempts=args.llm_max_attempts,
            llm_base_delay_seconds=args.llm_base_delay,
            llm_max_delay_seconds=args.llm_max_delay,
        )
        coordinator = RunCoordinator(
            runtime,
            root,
            state_store=RunStateStore(state_path),
            enable_snapshots=not args.no_snapshot,
            limits=RunLimits(
                max_tokens=args.max_run_tokens,
                max_cost_cny=args.max_run_cost_cny,
                max_seconds=args.max_run_seconds,
                max_model_calls=args.max_model_calls,
                max_tool_calls=args.max_tool_calls,
            ),
            rollback_policy=args.rollback_on_failure,
            rollback_handler=interactive_rollback_decision,
        )
    except (ValueError, OSError) as exc:
        print(f"Runtime configuration error: {exc}")
        return 2

    try:
        if args.resume:
            result = coordinator.resume(
                args.resume,
                plan_approval=(
                    interactive_plan_approval if args.mode == "plan" else None
                ),
            )
            return print_coordinated_result(runtime, result)

        image_processor = ImageProcessor(root)
        if args.prompt:
            try:
                prompt, images = image_processor.from_prompt(args.prompt)
            except ValueError as exc:
                print(f"Input error: {exc}")
                return 2
            result = coordinator.execute(
                args.mode,
                prompt,
                images=tuple(images),
                # Non-interactive -p executes a validated Plan automatically.
                plan_approval=None,
            )
            return print_coordinated_result(runtime, result)

        return interactive_loop(runtime, coordinator, image_processor, renderer)
    finally:
        coordinator.close()


def interactive_loop(
    runtime: ApplicationRuntime,
    coordinator: RunCoordinator,
    image_processor: ImageProcessor,
    renderer: object,
) -> int:
    client = runtime.client
    renderer.status(  # type: ignore[attr-defined]
        StatusInfo(
            str(getattr(client, "provider", "custom")),
            str(getattr(client, "model", "unknown")),
            "react",
            f"window={runtime.settings.window} memory={'on' if runtime.react.memory else 'off'}",
        )
    )
    print(
        "\nCommands: /plan <goal>, /team <goal>, /runs, /resume <run_id>, "
        "/tools, /model, /context, /history, /clear, /exit"
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
        if command is not None:
            exit_requested = handle_runtime_command(
                command,
                runtime,
                coordinator,
                history,
            )
            if exit_requested:
                return 0
            continue

        history.add(prompt)
        try:
            clean_prompt, images = image_processor.from_prompt(prompt)
            result = coordinator.execute(
                "react",
                clean_prompt,
                images=tuple(images),
            )
            print_coordinated_result(runtime, result)
        except (ValueError, LlmError) as exc:
            print(f"Error: {exc}")


def handle_runtime_command(
    command: CliCommand,
    runtime: ApplicationRuntime,
    coordinator: RunCoordinator,
    history: PaiCliHistory,
) -> bool:
    if command.name == "exit":
        return True
    if command.name == "clear":
        runtime.react.agent.clear_history()
        print("History cleared.")
    elif command.name == "tools":
        print("\n".join(f"- {name}" for name in runtime.tools.names()))
    elif command.name == "history":
        print("\n".join(history.recent()) or "(empty)")
    elif command.name in {"context", "config"}:
        print_runtime_context(runtime)
    elif command.name == "model":
        if len(command.arguments) != 1:
            print("Usage: /model <dashscope|glm|deepseek|stepfun|kimi|vllm>")
        else:
            try:
                runtime.set_client(LlmClientFactory.create(command.arguments[0]))
                print(f"Switched to {getattr(runtime.client, 'model', 'unknown')}.")
            except ValueError as exc:
                print(f"Configuration error: {exc}")
    elif command.name in {"plan", "team"}:
        goal = " ".join(command.arguments).strip()
        if not goal:
            print(f"Usage: /{command.name} <task>")
        else:
            history.add(goal)
            result = coordinator.execute(
                command.name,
                goal,
                plan_approval=(
                    interactive_plan_approval if command.name == "plan" else None
                ),
            )
            print_coordinated_result(runtime, result)
    elif command.name == "runs":
        print_runs(coordinator.recent_runs())
    elif command.name == "resume":
        if len(command.arguments) != 1:
            print("Usage: /resume <run_id>")
        else:
            try:
                result = coordinator.resume(command.arguments[0])
            except (KeyError, ValueError) as exc:
                print(f"Resume error: {exc}")
            else:
                print_coordinated_result(runtime, result)
    elif command.name == "help":
        print(
            "/plan <goal> /team <goal> /runs /resume <run_id> /tools "
            "/model /context /config /history /clear /exit"
        )
    return False


# Compatibility command helper retained for earlier tests/library examples.
def handle_command(
    command: CliCommand,
    agent: Agent,
    tools: object,
    history: PaiCliHistory,
) -> bool:
    if command.name == "exit":
        return True
    if command.name == "clear":
        agent.clear_history()
        print("History cleared.")
    elif command.name == "tools":
        print("\n".join(f"- {name}" for name in tools.names()))  # type: ignore[attr-defined]
    elif command.name == "history":
        print("\n".join(history.recent()) or "(empty)")
    elif command.name in {"context", "config"}:
        print(
            f"model={getattr(agent.client, 'model', 'unknown')} "
            f"messages={len(agent.history)} max_steps={agent.max_steps}"
        )
    elif command.name == "model":
        if len(command.arguments) != 1:
            print("Usage: /model <provider>")
        else:
            agent.set_client(LlmClientFactory.create(command.arguments[0]))
    elif command.name == "help":
        print("/tools /model /context /config /history /clear /exit")
    return False


def interactive_plan_approval(plan: ExecutionPlan) -> PlanReviewDecision:
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
        if value in {"e", "edit", "supplement"}:
            try:
                feedback = input("Plan changes> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return PlanReviewDecision.cancel()
            if feedback:
                return PlanReviewDecision.supplement(feedback)
            print("Plan changes cannot be empty.")
        else:
            print("Choose Y to execute, N to cancel, or E to revise the plan.")


def interactive_rollback_decision(message: str) -> bool:
    try:
        value = input(f"{message} [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return True
    return value not in {"n", "no", "keep"}


def run_selected_mode(
    runtime: ApplicationRuntime,
    mode: str,
    prompt: str,
    images: tuple[ImageAttachment, ...] = (),
    *,
    plan_approval: PlanApproval | None = None,
) -> int:
    """Direct compatibility adapter; production CLI uses RunCoordinator."""

    if mode == "react":
        return run_once(runtime.react.agent, prompt, images)
    if images:
        print("Input error: Plan and Team modes currently accept text tasks only")
        return 2
    if mode == "plan":
        result = runtime.plan.run(prompt, approval=plan_approval)
    elif mode == "team":
        result = runtime.team.run(prompt)
    else:
        print(f"Input error: unknown execution mode {mode!r}")
        return 2
    print(f"\n{result.answer}")
    return orchestration_exit_code(result)


def orchestration_exit_code(result: object) -> int:
    """Map orchestration status to a stable CLI return code."""

    status = getattr(result, "status", None)
    return 0 if status in {
        OrchestrationStatus.SUCCEEDED,
        OrchestrationStatus.CANCELLED,
    } else 1


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


def print_coordinated_result(
    runtime: ApplicationRuntime,
    result: CoordinatedRun,
) -> int:
    print(f"\n{result.answer or '(no final answer)'}")
    print(
        f"\n[run] id={result.run_id} mode={result.mode} status={result.status} "
        f"rolled_back={str(result.rolled_back).lower()}"
    )
    if result.error:
        print(f"[run] error={result.error}")
    if runtime.trace_store is not None:
        try:
            summary = runtime.trace_store.run_summary(result.run_id)
        except KeyError:
            pass
        else:
            print(
                "[metrics] "
                f"tokens={summary['input_tokens'] + summary['output_tokens']} "
                f"model_calls={summary['model_calls']} "
                f"tool_calls={summary['tool_calls']} "
                f"model_errors={summary['model_errors']} "
                f"tool_errors={summary['tool_errors']} "
                f"elapsed_ms={summary['elapsed_ms']} "
                f"estimated_cost_cny={summary['estimated_cost_cny']:.6f} "
                f"unpriced_calls={summary['unpriced_model_calls']}"
            )
    return result.exit_code


def print_runtime_context(runtime: ApplicationRuntime) -> None:
    agent = runtime.react.agent
    print(
        f"model={getattr(runtime.client, 'model', 'unknown')} "
        f"provider={getattr(runtime.client, 'provider', 'custom')} "
        f"messages={len(agent.history)} max_steps={agent.max_steps} "
        f"stagnation_window={agent.stagnation_window} "
        f"token_budget={agent.token_budget or 'unlimited'} "
        f"context_window={runtime.settings.window} "
        f"memory={agent.memory.status() if agent.memory else 'disabled'}"
    )


def print_recent_runs(path: Path) -> int:
    store = RunStateStore(path)
    try:
        print_runs(store.recent())
    finally:
        store.close()
    return 0


def print_runs(runs: list[object]) -> None:
    if not runs:
        print("(no persisted runs)")
        return
    for run in runs:
        print(
            f"{run.run_id}\t{run.status}\t{run.mode}\t"
            f"{run.updated_at:.3f}\t{run.goal[:80]}"  # type: ignore[attr-defined]
        )


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
