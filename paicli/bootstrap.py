"""Single dependency graph for ReAct, Plan, Team, safety, and tracing."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .agent import Agent, SYSTEM_PROMPT
from .context import ContextController, ContextSettings
from .extensions import (
    ExtensionConfig,
    InstalledExtensions,
    install_extensions,
)
from .llm_client import LlmClient, RetryingLlmClient, unwrap_llm_client
from .lsp import LspManager
from .managed_memory import ManagedMemoryStore
from .memory import (
    ConversationHistoryCompactor,
    LongTermMemory,
    MemoryManager,
    register_memory_tool,
)
from .observability import (
    ObservedLlmClient,
    ObservedToolGateway,
    PricingCatalog,
    TraceStore,
)
from .orchestration import PlanModeRuntime, TeamModeRuntime
from .planning import LlmPlanner
from .prompts import PromptMode, assemble_system_prompt
from .rag import CodeIndex, IndexRefreshingToolGateway
from .permissions import PermissionStore, default_permission_path
from .policy import (
    ApprovalHandler,
    ApprovalMode,
    AuditLog,
    ConsoleApprovalHandler,
    HitlToolRegistry,
)
from .subagents import SubAgentFactory, SubAgentRole
from .tools import ToolRegistry

EventHandler = Callable[[str, str], None]


@dataclass(frozen=True)
class ReactRuntime:
    agent: Agent
    tools: Any
    context: ContextController
    memory: MemoryManager | None
    long_term_memory: Any | None
    code_index: CodeIndex | None = None

    @property
    def settings(self) -> ContextSettings:
        return self.context.settings


@dataclass
class ApplicationRuntime:
    """All public execution modes sharing one client/tool/memory graph."""

    react: ReactRuntime
    plan: PlanModeRuntime
    team: TeamModeRuntime
    subagents: SubAgentFactory
    trace_store: TraceStore | None = None
    pricing: PricingCatalog | None = None
    llm_max_attempts: int = 3
    llm_base_delay_seconds: float = 0.25
    llm_max_delay_seconds: float = 4.0
    role_clients: dict[str, LlmClient] | None = None
    extensions: InstalledExtensions | None = None

    @property
    def tools(self) -> Any:
        return self.react.tools

    @property
    def settings(self) -> ContextSettings:
        return self.react.settings

    @property
    def client(self) -> LlmClient:
        """Expose the active provider client, not transparent decorators."""

        return unwrap_llm_client(self.react.agent.client)

    def set_client(self, client: LlmClient) -> None:
        wrapped, pricing = _wrap_client(
            client,
            self.trace_store,
            pricing=None,
            max_attempts=self.llm_max_attempts,
            base_delay_seconds=self.llm_base_delay_seconds,
            max_delay_seconds=self.llm_max_delay_seconds,
        )
        self.pricing = pricing
        self.react.agent.set_client(wrapped)
        self.subagents.set_client(wrapped)
        self.plan.set_client(wrapped)
        self.team.set_client(wrapped)

    def begin_run(
        self,
        mode: str,
        prompt: str,
        *,
        metadata: Mapping[str, object] | None = None,
    ) -> str:
        run_id = "run_" + uuid.uuid4().hex
        if self.trace_store is None:
            return run_id
        provider = str(getattr(self.client, "provider", "custom"))
        model = str(getattr(self.client, "model", "unknown"))
        return self.trace_store.start_run(
            mode,
            prompt,
            provider=provider,
            model=model,
            metadata=metadata,
            run_id=run_id,
        )

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        error: str = "",
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        if self.trace_store is not None:
            self.trace_store.finish_run(
                run_id,
                status=status,
                error=error,
                metadata=metadata,
            )

    def capability_matrix(self) -> dict[str, object]:
        """Return only capabilities reachable through this assembled runtime."""

        tools = tuple(sorted(self.tools.names()))
        return {
            "modes": ("react", "plan", "team"),
            "tools": tools,
            "memory": self.react.long_term_memory is not None,
            "rag": self.react.code_index is not None,
            "trace": self.trace_store is not None,
            "extensions": self.extensions is not None
            and bool(self.extensions.installed_tools),
            "extension_tools": (
                self.extensions.installed_tools if self.extensions is not None else ()
            ),
            "role_models": tuple(sorted(self.role_clients or {})),
        }

    def close(self) -> None:
        resources = (
            self.react.code_index,
            self.react.long_term_memory,
            self.extensions,
            self.trace_store,
        )
        closed: set[int] = set()
        for resource in resources:
            if resource is None or id(resource) in closed:
                continue
            close = getattr(resource, "close", None)
            if callable(close):
                close()
            closed.add(id(resource))


def default_memory_path() -> Path:
    configured = os.getenv("PAICLI_MEMORY_FILE", "").strip()
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".paicli" / "memory.db"
    )


def default_rag_path(project_root: str | Path) -> Path:
    configured = os.getenv("PAICLI_RAG_FILE", "").strip()
    return (
        Path(configured).expanduser()
        if configured
        else Path(project_root).resolve() / ".paicli" / "code-index.db"
    )


def default_trace_path() -> Path:
    configured = os.getenv("PAICLI_TRACE_FILE", "").strip()
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".paicli" / "traces.db"
    )


def build_react_runtime(
    client: LlmClient,
    project_root: str | Path,
    *,
    allow_shell: bool = False,
    enable_memory: bool = True,
    memory_path: str | Path | None = None,
    enable_rag: bool = False,
    rag_path: str | Path | None = None,
    code_index: CodeIndex | None = None,
    python_lsp_command: tuple[str, ...] | str | None = None,
    lsp_timeout_seconds: float = 8.0,
    max_steps: int = 50,
    stagnation_window: int = 3,
    token_budget: int | None = None,
    on_event: EventHandler | None = None,
    tools: Any | None = None,
    system_prompt: str | None = None,
) -> ReactRuntime:
    """Build the default ReAct path; callers may supply a guarded gateway."""

    root = Path(project_root).resolve()
    settings = _settings_for(client)
    runtime_tools = tools or ToolRegistry(root, allow_shell=allow_shell)
    registry = _find_registry(runtime_tools)

    active_index = code_index
    if active_index is None and enable_rag:
        active_index = CodeIndex(
            root,
            rag_path or default_rag_path(root),
        )
        active_index.build()
    if active_index is not None:
        active_index.register_tool(registry)

    memory: MemoryManager | None = None
    long_term: Any | None = None
    if enable_memory:
        resolved = Path(memory_path or default_memory_path()).expanduser()
        if resolved.suffix.lower() == ".jsonl":
            if on_event is not None:
                on_event(
                    "warning",
                    "Legacy JSONL memory is deprecated and lacks the trust "
                    "lifecycle of ManagedMemoryStore; migrate it before PaiCLI 2.0.",
                )
            long_term = LongTermMemory(resolved)
        else:
            long_term = ManagedMemoryStore(resolved)
        memory = MemoryManager(
            max_tokens=settings.compression_trigger_tokens,
            long_term=long_term,
            history_compactor=ConversationHistoryCompactor(client),
            long_term_context_tokens=settings.memory_context_tokens,
        )
        # Register against the innermost registry, not a policy/trace decorator.
        if "save_memory" not in registry.names():
            register_memory_tool(registry, long_term)

    context = ContextController(settings)
    agent = Agent(
        client,
        runtime_tools,
        max_steps=max_steps,
        stagnation_window=stagnation_window,
        token_budget=token_budget,
        on_event=on_event,
        memory=memory,
        context=context,
        lsp=LspManager(
            root,
            python_lsp_command=python_lsp_command,
            lsp_timeout_seconds=lsp_timeout_seconds,
        ),
        system_prompt=system_prompt or assemble_system_prompt(
            SYSTEM_PROMPT,
            mode=PromptMode.REACT,
            project_root=root,
        ),
    )
    return ReactRuntime(
        agent,
        runtime_tools,
        context,
        memory,
        long_term,
        active_index,
    )


def build_application_runtime(
    client: LlmClient,
    project_root: str | Path,
    *,
    allow_shell: bool = False,
    enable_memory: bool = True,
    memory_path: str | Path | None = None,
    enable_rag: bool = True,
    rag_path: str | Path | None = None,
    python_lsp_command: tuple[str, ...] | str | None = None,
    lsp_timeout_seconds: float = 8.0,
    max_steps: int = 50,
    subagent_max_steps: int = 12,
    stagnation_window: int = 3,
    token_budget: int | None = None,
    plan_workers: int = 4,
    plan_revisions: int = 2,
    team_workers: int = 2,
    review_retries: int = 2,
    on_event: EventHandler | None = None,
    enable_hitl: bool | None = None,
    approval_mode: ApprovalMode | str | None = None,
    approval_handler: ApprovalHandler | None = None,
    audit_path: str | Path | None = None,
    permission_path: str | Path | None = None,
    enable_trace: bool = True,
    trace_path: str | Path | None = None,
    pricing: PricingCatalog | None = None,
    llm_max_attempts: int = 3,
    llm_base_delay_seconds: float = 0.25,
    llm_max_delay_seconds: float = 4.0,
    role_clients: Mapping[str, LlmClient] | None = None,
    skill_index: tuple[str, ...] = (),
    resource_index: tuple[str, ...] = (),
    runtime_notes: tuple[str, ...] = (),
    extension_config: ExtensionConfig | None = None,
) -> ApplicationRuntime:
    """Build the complete application from one shared infrastructure graph."""

    root = Path(project_root).resolve()
    trace_store = (
        TraceStore(trace_path or default_trace_path()) if enable_trace else None
    )
    wrapped_client, effective_pricing = _wrap_client(
        client,
        trace_store,
        pricing=pricing,
        max_attempts=llm_max_attempts,
        base_delay_seconds=llm_base_delay_seconds,
        max_delay_seconds=llm_max_delay_seconds,
    )
    wrapped_roles: dict[str, LlmClient] = {}
    allowed_roles = {"planner", "worker", "reviewer", "aggregator"}
    for raw_role, role_client in dict(role_clients or {}).items():
        role = str(raw_role).strip().lower()
        if role not in allowed_roles:
            raise ValueError(
                f"unknown role client {raw_role!r}; choose: "
                + ", ".join(sorted(allowed_roles))
            )
        wrapped_roles[role], _ = _wrap_client(
            role_client,
            trace_store,
            pricing=effective_pricing,
            max_attempts=llm_max_attempts,
            base_delay_seconds=llm_base_delay_seconds,
            max_delay_seconds=llm_max_delay_seconds,
        )

    registry = ToolRegistry(root, allow_shell=allow_shell)
    code_index: CodeIndex | None = None
    if enable_rag:
        code_index = CodeIndex(root, rag_path or default_rag_path(root))
        code_index.build()
        code_index.register_tool(registry)

    installed_extensions = (
        install_extensions(registry, root, extension_config)
        if extension_config is not None
        else InstalledExtensions()
    )
    skill_index = tuple((*skill_index, *installed_extensions.skill_index))
    resource_index = tuple((*resource_index, *installed_extensions.resource_index))

    gateway: Any = registry
    # The complete application is fail-closed for library callers as well as
    # the CLI. Callers that deliberately provide their own isolation may opt
    # out with enable_hitl=False.
    use_hitl = True if enable_hitl is None else enable_hitl
    if use_hitl:
        mode = (
            approval_mode
            if isinstance(approval_mode, ApprovalMode)
            else ApprovalMode(str(approval_mode or ApprovalMode.DENY.value).lower())
        )
        handler = approval_handler or ConsoleApprovalHandler(mode)
        gateway = HitlToolRegistry(
            registry,
            handler,
            enabled=True,
            audit_log=AuditLog(
                Path(audit_path).expanduser()
                if audit_path is not None
                else root / ".paicli" / "audit"
            ),
            permission_store=PermissionStore(
                Path(permission_path).expanduser()
                if permission_path is not None
                else default_permission_path(root)
            ),
        )
    if code_index is not None:
        gateway = IndexRefreshingToolGateway(gateway, code_index)
    gateway = ObservedToolGateway(gateway, trace_store)

    react_prompt = assemble_system_prompt(
        SYSTEM_PROMPT,
        mode=PromptMode.REACT,
        project_root=root,
        skill_index=skill_index,
        resource_index=resource_index,
        runtime_notes=runtime_notes,
    )
    planner_prompt = assemble_system_prompt(
        LlmPlanner.SYSTEM_PROMPT,
        mode=PromptMode.PLAN,
        project_root=root,
        skill_index=skill_index,
        resource_index=resource_index,
        runtime_notes=runtime_notes,
    )
    role_prompts = {
        SubAgentRole.WORKER: assemble_system_prompt(
            SubAgentFactory.WORKER_SYSTEM_PROMPT,
            mode=PromptMode.TEAM,
            project_root=root,
            skill_index=skill_index,
            resource_index=resource_index,
            runtime_notes=runtime_notes,
        ),
        SubAgentRole.REVIEWER: assemble_system_prompt(
            SubAgentFactory.REVIEWER_SYSTEM_PROMPT,
            mode=PromptMode.TEAM,
            project_root=root,
            skill_index=skill_index,
            resource_index=resource_index,
            runtime_notes=runtime_notes,
        ),
        SubAgentRole.AGGREGATOR: assemble_system_prompt(
            SubAgentFactory.AGGREGATOR_SYSTEM_PROMPT,
            mode=PromptMode.TEAM,
            project_root=root,
            skill_index=skill_index,
            resource_index=resource_index,
            runtime_notes=runtime_notes,
        ),
    }

    react = build_react_runtime(
        wrapped_client,
        root,
        allow_shell=allow_shell,
        enable_memory=enable_memory,
        memory_path=memory_path,
        enable_rag=enable_rag,
        rag_path=rag_path,
        code_index=code_index,
        python_lsp_command=python_lsp_command,
        lsp_timeout_seconds=lsp_timeout_seconds,
        max_steps=max_steps,
        stagnation_window=stagnation_window,
        token_budget=token_budget,
        on_event=on_event,
        tools=gateway,
        system_prompt=react_prompt,
    )
    subagents = SubAgentFactory(
        wrapped_client,
        react.tools,
        root,
        long_term_memory=react.long_term_memory,
        enable_memory=enable_memory,
        max_steps=subagent_max_steps,
        stagnation_window=stagnation_window,
        token_budget=token_budget,
        on_event=on_event,
        role_clients={
            SubAgentRole.WORKER: wrapped_roles.get("worker", wrapped_client),
            SubAgentRole.REVIEWER: wrapped_roles.get("reviewer", wrapped_client),
            SubAgentRole.AGGREGATOR: wrapped_roles.get("aggregator", wrapped_client),
        },
        system_prompts=role_prompts,
    )
    planner_client = wrapped_roles.get("planner", wrapped_client)
    plan = PlanModeRuntime(
        LlmPlanner(planner_client, system_prompt=planner_prompt),
        subagents,
        max_workers=plan_workers,
        max_plan_revisions=plan_revisions,
        on_event=on_event,
    )
    team = TeamModeRuntime(
        LlmPlanner(planner_client, system_prompt=planner_prompt),
        subagents,
        max_workers=team_workers,
        max_review_retries=review_retries,
        on_event=on_event,
    )
    return ApplicationRuntime(
        react=react,
        plan=plan,
        team=team,
        subagents=subagents,
        trace_store=trace_store,
        pricing=effective_pricing,
        llm_max_attempts=llm_max_attempts,
        llm_base_delay_seconds=llm_base_delay_seconds,
        llm_max_delay_seconds=llm_max_delay_seconds,
        role_clients={"react": wrapped_client, **wrapped_roles},
        extensions=installed_extensions,
    )


def _wrap_client(
    client: LlmClient,
    trace_store: TraceStore | None,
    *,
    pricing: PricingCatalog | None,
    max_attempts: int,
    base_delay_seconds: float,
    max_delay_seconds: float,
) -> tuple[LlmClient, PricingCatalog]:
    raw = unwrap_llm_client(client)
    provider = str(getattr(raw, "provider", "custom"))
    model = str(getattr(raw, "model", "unknown"))
    effective = pricing or PricingCatalog.from_env(provider, model)
    observed = ObservedLlmClient(raw, trace_store, effective)
    wrapped: LlmClient = RetryingLlmClient(
        observed,
        max_attempts=max_attempts,
        base_delay_seconds=base_delay_seconds,
        max_delay_seconds=max_delay_seconds,
    )
    return wrapped, effective


def _find_registry(gateway: Any) -> ToolRegistry:
    current = gateway
    seen: set[int] = set()
    while not isinstance(current, ToolRegistry):
        if id(current) in seen:
            raise TypeError("tool gateway decorator cycle detected")
        seen.add(id(current))
        if hasattr(current, "gateway"):
            current = current.gateway
        elif hasattr(current, "registry"):
            current = current.registry
        else:
            raise TypeError("tool gateway does not expose a ToolRegistry")
    return current


def _settings_for(client: LlmClient) -> ContextSettings:
    raw = getattr(client, "context_window", 128_000)
    try:
        window = max(8_000, int(raw))
    except (TypeError, ValueError):
        window = 128_000
    return ContextSettings.for_model(
        window,
        supports_prompt_caching=bool(
            getattr(client, "supports_prompt_caching", False)
        ),
    )


__all__ = [
    "ApplicationRuntime",
    "ReactRuntime",
    "build_application_runtime",
    "build_react_runtime",
    "default_memory_path",
    "default_rag_path",
    "default_trace_path",
]
