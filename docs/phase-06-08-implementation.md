# Java parity implementation notes — Phases 6, 7, and 8

> **Historical phase snapshot.** This file records the Phase 6–8 migration state. It is not the current `develop` contract; use `README.md`, `ARCHITECTURE.md`, and `docs/final-acceptance.md` for the 1.0 implementation.

Date: 2026-08-30
Python base commit: `2107eab`
Java behavior reference: local `paicli-java` develop inspected during Phase 0

This migration line connects the Phase 0–5 kernels into user-visible Plan and
Team modes. It preserves the original 22 educational tags and keeps the old
callback-based `multi_agent.py` example for historical reading.

## Phase 6 — Plan mode on the real CLI path

Public entry points:

```bash
python3 -m paicli --mode plan -p "inspect, edit, and verify the project"
```

```text
/plan inspect, edit, and verify the project
```

The interactive slash command emits the validated plan before any task runs.
Enter/Y executes it, N cancels, and E accepts supplemental requirements. A
supplement causes `LlmPlanner` to generate and validate a complete replacement
plan, then presents that revision again. Revisions are bounded (default 2) so
human review cannot become an unbounded planning loop. Non-interactive
`--mode plan` executes automatically so it can be used by scripts and CI.

Runtime path:

```text
user goal
  -> LlmPlanner
  -> strict JSON parsing and one bounded repair
  -> PlanValidator
  -> unified ExecutionPlan / DagScheduler
  -> real task-scoped LLM workers
  -> deterministic task states
  -> no-tool LLM final aggregator
  -> deterministic aggregation fallback
```

Each worker receives a `TaskPacket` containing only:

- the overall goal and plan ID;
- the current task, type, attempt, and acceptance criteria;
- direct dependency results and their changed files;
- reviewer feedback when the same task is retried.

It does not receive unrelated task output, another worker's conversation, or the
parent ReAct history.

Typed workers also have a task completion gate. A final text claim is
insufficient for `FILE_READ`, `FILE_WRITE`, `COMMAND`, or `VERIFICATION`: the
current attempt must contain a successful read observation, file/directory
mutation, process result, or concrete read/process verification respectively.
Only pure `ANALYSIS` may finish from dependency evidence without another tool.

## Phase 7 — real SubAgents and reviewer gate

`SubAgentFactory` creates independent LLM Agents with:

- their own System Prompt and conversation history;
- their own AgentBudget and stagnation detector;
- their own context controller and history compactor;
- a capability-scoped view of the shared ToolRegistry;
- shared durable long-term memory and cancellation state.

Team runtime:

```text
LlmPlanner
  -> validated DAG
  -> Worker for current task
  -> Reviewer reads task evidence and changed files
     -> APPROVED: task COMPLETED
     -> CHANGES_REQUESTED: same Worker retries current task
     -> REJECTED: current task FAILED
     -> ERROR: current task FAILED
  -> failed descendants SKIPPED; independent branches continue
  -> final aggregator
```

Reviewer output is a strict JSON contract:

```json
{
  "verdict": "approved | changes_requested | rejected",
  "summary": "factual assessment",
  "issues": [],
  "suggestions": [],
  "evidence": []
}
```

Malformed reviewer JSON receives one repair request by default. Reviewer model
failure, invalid output after repair, explicit rejection, or unresolved changes
after two local retries never silently marks a task complete.

This intentionally improves the Java behavior, where reviewer failure or retry
exhaustion can retain the latest worker result and continue. Python treats the
reviewer as a real quality gate.

Reviewers receive read-only tools. They can inspect changed files instead of
trusting only the worker's narrative, but they cannot modify the workspace.
Verification tasks may receive `execute_command`; normal reviewers do not.

## Phase 8 — bounded concurrency and conflict prevention

Defaults:

```text
Plan: at most 4 parallel read-scoped tasks
Team: at most 2 parallel read-scoped tasks
```

Only `FILE_READ` and `ANALYSIS` tasks may share a ready-task wave. Their workers
receive only tools whose metadata declares `READ_ONLY`. A hallucinated call to a
hidden write tool is rejected with typed `POLICY_DENIED`, even though the model
never saw that schema.

`FILE_WRITE`, `COMMAND`, and `VERIFICATION` tasks run alone. Therefore two
workers cannot concurrently edit the same file in the current architecture.
Within one worker response, the Phase 3 Tool Gateway still applies resource
claims and separates read/write or write/write conflicts into stable waves.

This policy is deliberately conservative. Safe parallel mutation requires a
stronger isolation mechanism such as per-worker Git worktrees plus merge and
verification; that is not claimed by this phase.

## CLI controls

```text
--mode react|plan|team
--subagent-max-steps 12
--plan-workers 4
--plan-revisions 2
--team-workers 2
--review-retries 2
```

`--token-budget` is currently a per-Agent limit. `OrchestrationResult.usage`
aggregates Planner, Worker, Reviewer, and final-aggregator usage for reporting,
but Phase 8 does not yet enforce one atomic total budget across concurrent
workers.

Plan and Team modes currently accept text tasks only. ReAct retains multimodal
`@image:` support.

## Structured results

`OrchestrationResult` exposes:

```text
mode
status: succeeded | partial | failed | cancelled
validated final plan
task records
worker outcomes
review runs
planner usage
aggregation outcome
combined token usage
changed files
final answer
```

Provider exceptions inside a SubAgent become a structured `INTERNAL_ERROR`
outcome, so orchestration state remains complete and diagnosable.

## Known boundaries after Phase 8

Not yet implemented:

- separate model/provider selection per role;
- Team-level replanning after a reviewer rejects a plan assumption;
- per-worker Git worktrees, patch merge, or semantic conflict resolution;
- automatic turn snapshots and rollback around Plan/Team runs;
- durable plan/task/review checkpoints and crash continuation;
- default CLI HITL assembly for mutation tools;
- one global orchestration token/cost/time budget;
- an Agent task benchmark and reviewer-to-human calibration set.

## Verification

Focused suites:

```bash
python3.11 -m unittest tests.test_orchestration -v
python3.11 -m unittest tests.test_orchestration_cli -v
python3.11 -m unittest tests.parity.test_java_phase_6_8 -v
```

Full gates:

```bash
python3.11 -m unittest discover -s tests -v
python3.12 -m unittest discover -s tests -v
python3.11 -m compileall -q paicli tests
python3.12 -m compileall -q paicli tests
python3.11 -m paicli --help
git diff --check
```
