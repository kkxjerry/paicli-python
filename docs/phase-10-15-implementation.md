# Phase 10–15 implementation record

Date: 2026-08-30  
Base: Phase 9 DashScope live integration  
Goal: close the project-level acceptance checklist with one production path,
explicit residual boundaries, and reproducible evidence.

## Phase 10 — safety and deterministic completion

Implemented:

- production CLI always builds a `HitlToolRegistry`;
- explicit `ask`, `deny`, and `allow` approval modes;
- unified diff preview before `write_file`;
- exact command and working-directory preview before `execute_command`;
- hard destructive-command policy before HITL;
- runtime JSON Schema validation before policy and after argument edits;
- typed policy/approval failures;
- redacted append-only approval audit;
- run-level BEFORE/AFTER snapshots;
- task-level snapshots before mutation-capable work;
- rollback policy `always|ask|never`;
- LLM-generated `FILE_WRITE` plans require a downstream `VERIFICATION` task.

Reviewer remains a separate read-only quality gate. Deterministic tests, Tool
Results, and snapshots do not depend on Reviewer agreement.

## Phase 11 — retries, parent budget, trace, and metrics

Implemented:

- transient model error classification for 408/409/425/429/5xx and transport
  errors;
- bounded exponential retry with `Retry-After` support;
- one parent `RunBudget` shared by Planner, Workers, Reviewer repairs,
  Aggregator, model retries, and Tool Calls;
- limits for total Tokens, configured CNY cost, wall time, model calls, and Tool
  Calls;
- SQLite TraceStore with run/span/model/tool/event tables;
- parent `run_id`, `span_id`, `parent_span_id`, Task ID, role, and Agent name;
- provider/model, input/output/cache Tokens, latency, Tool errors/timeouts,
  changed files, and termination state;
- pricing loaded from environment for the exact provider/model;
- missing pricing reported as unpriced rather than zero cost;
- secret redaction for common keys and Authorization forms.

The project deliberately does not persist hidden chain-of-thought.

## Phase 12 — durable state and safe resume

Implemented:

- SQLite RunStateStore with atomic current state and append-only checkpoints;
- full Plan/Task/Worker ToolResult/Reviewer serialization;
- checkpoint boundaries at plan creation, before Task, after review, and after
  Task;
- startup conversion of stale `running` rows to `interrupted`;
- CLI run listing and resume;
- restoration of a task-level snapshot before retrying an interrupted side
  effect;
- full BEFORE restore and DAG restart when the uncertain task boundary is not
  provable;
- failed/partial branch retry when the retained workspace is consistent;
- full DAG restart after a run-level rollback.

This is safe local-process recovery, not distributed ownership or exactly-once
external side effects.

## Phase 13 — fixed benchmark and comparison

Implemented:

- versioned JSON evaluation suites;
- disposable project workspace per case;
- real application runtime and RunCoordinator, not a separate demo loop;
- deterministic answer/file/command assertions;
- reports with Git commit, provider/model, Task status, assertion status,
  Tokens, latency, calls, failures, changed files, and configured cost;
- baseline/candidate comparison and per-Task success changes;
- a repository smoke suite covering ReAct, Plan, and Team.

The evaluation API accepts a fake executor for deterministic tests and a real
provider executor for opt-in live runs.

## Phase 14 — persistent hybrid retrieval and managed memory

Code retrieval:

- incremental file hashing and deletion reconciliation;
- Python AST chunks and bounded generic line chunks;
- persistent SQLite chunks;
- exact symbol, FTS5/BM25, lexical cosine, and optional dense channels;
- Reciprocal Rank Fusion;
- source file, line range, symbol, channel, and source text in each result;
- generic OpenAI-compatible `/embeddings` adapter, disabled unless explicitly
  configured.

Memory:

- SQLite IDs and normalized-content deduplication;
- tags, source, source hash, created/updated timestamps;
- unverified, verified, stale, and deleted lifecycle states;
- verified-result ranking bonus;
- source-version staleness invalidation;
- list, verify, and soft-delete tools;
- compatibility with explicitly selected legacy JSONL memory.

## Phase 15 — release and independent reproduction

Implemented:

- package version `1.0.0`;
- `paicli` and `paicli-eval` console scripts;
- complete `.env.example` without credentials;
- standalone install and DashScope probe instructions;
- ReAct/Plan/Team examples;
- architecture, security, recovery, evaluation, and implementation documents;
- normal-test, live-test, fixed-evaluation, clean-mainline, and fresh-install
  release gates.

## Single trusted runtime path

The production path is:

```text
CLI / evaluation
  -> build_application_runtime
  -> RunCoordinator
  -> ReAct | PlanModeRuntime | TeamModeRuntime
  -> shared AgentLoopEngine
  -> observed/retried model + observed/HITL Tool Gateway
  -> checkpoint + snapshot + trace + metrics
```

Older direct methods remain only as compatibility and focused-test interfaces.
They are not the documented production CLI path.

## Residual boundaries

The final project does not claim:

- OS-level shell sandboxing;
- safe parallel mutation without per-worker worktrees;
- distributed task ownership or high availability;
- exactly-once arbitrary remote side effects;
- Reviewer accuracy without a human-labelled calibration set;
- automatically current provider pricing;
- cloud-model confidentiality for source code sent in Tool Results.

These are explicit scope boundaries, not hidden implementation gaps.

## Release gates

```bash
python3.11 -m compileall -q paicli tests
python3.12 -m compileall -q paicli tests
python3.11 -m unittest discover -s tests -q
python3.12 -m unittest discover -s tests -q
python3.11 -m paicli --help
python3.11 -m paicli.evaluation --help
git diff --check
```

Credentialed gates:

```bash
python3.11 -m paicli --provider dashscope --check-model chat
python3.11 -m paicli --provider dashscope --check-model tools
PAICLI_RUN_DASHSCOPE_LIVE_TEST=1 \
  python3.11 -m unittest tests.test_dashscope_live -v
python3.11 -m paicli.evaluation run \
  --suite eval/suites/coding-smoke.json \
  --provider dashscope \
  --output reports/dashscope-current.json
```

Final release additionally requires a clean `develop`, no extra feature
worktree, and a fresh editable-install smoke test in a new virtual environment.
