# Java parity implementation notes — Phases 3, 4, and 5

> **Historical phase snapshot.** This file records the Phase 3–5 migration state. It is not the current `develop` contract; use `README.md`, `ARCHITECTURE.md`, and `docs/final-acceptance.md` for the 1.0 implementation.

Date: 2026-08-30
Python base commit: `880fc1b`
Java behavior reference: local `paicli-java` develop inspected during Phase 0

This migration line is separate from the original 22 educational tags. It
synchronizes public behavior without translating Java classes line by line.

## Phase 3 — validated tool gateway

### New contracts

- `ToolResult`: tool-call ID, success/failure, error category, retryability,
  timeout, elapsed time, changed files, and resource claims.
- `ToolSpec`: keeps the original four positional fields and adds risk,
  side-effect, concurrency, timeout, and resource-resolver metadata.
- `ResourceAccess`: read/write claim over a project-relative resource.

### Runtime path

```text
model arguments JSON
  -> JSON parse
  -> execution-time schema validation
  -> policy / HITL
  -> resource conflict scheduling
  -> handler
  -> structured ToolResult
  -> legacy string facade when required
```

The validator enforces the common object/array/string/number constraints used
by built-in and MCP schemas. Unknown JSON Schema keywords are ignored so newer
external schemas do not fail merely because the learning implementation does
not know every draft keyword.

Calls are grouped into stable execution waves. Read/read may share a wave;
read/write, write/write, recursive directory overlap, global claims, and
`SERIAL` tools cannot. Extensions fail closed as unknown-risk/unknown-effect/
serial until their author opts into weaker metadata. External MCP tools remain
unknown-risk and serial because the base MCP descriptor does not declare side
effects or idempotency.

### Intentional boundary

Python threads cannot be forcibly killed safely. Each call has an independent
submission deadline; a batch timeout returns a typed timeout result and cancels
work that has not started. Only read-only timeouts are marked retryable. For
write/process/unknown tools the result says not to retry blindly, because a
partial side effect may already exist. While such a timed-out Future remains
alive, conflicting resources are quarantined and later calls receive a typed
`RESOURCE_CONFLICT`; the quarantine clears automatically after the Future ends.
Process/network handlers must additionally enforce an underlying timeout;
`execute_command` does so for its subprocess.

## Phase 4 — context and memory on the real CLI path

`build_react_runtime()` is the single default assembly point for:

- `ToolRegistry`
- `ContextSettings` and `ContextController`
- `ConversationHistoryCompactor`
- JSONL `LongTermMemory`
- `save_memory`
- post-write `LspManager`
- `Agent`

The CLI enables this path by default and supports `--no-memory` and
`--memory-file` for explicit control.

### Compaction behavior

- Trigger: 90% of the already-reserved usable input budget.
- Primary boundary: retain the latest three user rounds in full.
- Long single-turn fallback: retain a recent suffix without beginning in the
  middle of contiguous tool results.
- Summary: model-generated factual coding summary; image base64 payloads are
  replaced by attachment placeholders rather than copied into the summary call.
- Availability fallback: deterministic summary when the summary call fails or
  returns empty.
- Repeated preparation: LRU cache keyed by the complete old-history prefix.
- Long-term memory injection: separately capped by a model-window-derived token
  budget.

The parent Agent history remains the source of truth. Compression prepares the
actual message list sent to the model but does not destructively overwrite that
history.

## Phase 5 — LLM Planner and one DAG contract

### Ownership boundary

```text
LLM Planner owns:
- candidate task descriptions
- task types
- candidate dependency IDs
- acceptance criteria

PlanValidator / DagScheduler own:
- non-empty IDs and descriptions
- unique IDs
- known dependencies
- no duplicate or self dependencies
- cycle detection
- topological order
- execution batches
- runtime readiness
- failed-dependency propagation
```

`LlmPlanner` bypasses the model only for a narrow set of obvious one-action
read/list goals. Complex goals request strict JSON. Invalid JSON, unknown task
types, missing dependencies, and cyclic plans receive the exact failure reason
and one repair request by default.

`StaticPlanner` remains for deterministic tests. `PlanExecuteAgent` now lets an
independent ready branch continue after another branch fails. A replacement
plan can inherit a completed result only when task ID, description, type, and
acceptance criteria still match.

### Not yet connected

Phase 5 exposes the planner and scheduler as library capabilities. It does not
add `/plan` or `/team` to the Python CLI. It also does not yet turn each plan
node into a shared-loop LLM Worker or add Reviewer verdicts; those are the next
orchestration phases.

## Verification commands

```bash
python3.11 -m unittest discover -s tests -v
python3.12 -m unittest discover -s tests -v
python3.11 -m compileall -q paicli tests
python3.11 -m paicli --help

git diff --check
```

Focused suites:

```bash
python3.11 -m unittest tests.test_tool_gateway -v
python3.11 -m unittest tests.test_context_memory_runtime -v
python3.11 -m unittest tests.test_llm_planning -v
```

Final verification on 2026-08-30:

```text
Python 3.11: 133 tests passed, 1 real-vLLM test skipped by configuration
Python 3.12: 133 tests passed, 1 real-vLLM test skipped by configuration
Java-parity focused suite: 6 tests passed
compileall, CLI --help, package import smoke, and git diff --check: passed
```
