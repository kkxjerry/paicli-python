# PaiCLI architecture

## System boundary

PaiCLI is a local single-process Agent harness. Repository state and tool
execution remain local. An external provider receives model messages, tool
schemas, and the Tool Results required for the next inference step.

```text
                         User / CI
                            |
                         PaiCLI CLI
                            |
                      RunCoordinator
          +-----------------+------------------+
          |                 |                  |
       ReAct             Plan               Team
          |                 |                  |
          |          LlmPlanner          LlmPlanner
          |                 |                  |
          |          ExecutionPlan       ExecutionPlan
          |                 |                  |
          |             Workers          Workers + Reviewer
          +-----------------+------------------+
                            |
                     AgentLoopEngine
                    /               \
             LlmClient             ToolRuntime
                 |                     |
      Retry + Observe             Observe + HITL
                 |                     |
       OpenAI-compatible          ToolRegistry
            provider                  |
                             local files/process/MCP

Cross-cutting:
RunBudget | TraceStore | RunStateStore | SnapshotService | Memory | CodeIndex
```

## Ownership rules

### Model-owned proposals

The model may propose:

- a plan's task descriptions, task types, dependencies, and acceptance criteria;
- the next tool call and its arguments;
- a Worker result;
- a Reviewer verdict;
- a final natural-language summary.

### Code-owned invariants

Deterministic code owns:

- JSON parsing and runtime Tool Schema validation;
- project-root path confinement;
- hard command policy and human approval;
- Tool capability scopes;
- DAG ID/dependency/cycle validation and topological readiness;
- the rule that write tasks require downstream verification;
- Reviewer retry limits and failure propagation;
- task/tool conflict scheduling;
- snapshot, rollback, checkpoint, and resume semantics;
- parent budgets, traces, metrics, and evaluation assertions.

This separation is deliberate: an LLM can recommend a state transition but
cannot directly mutate orchestration state.

## Shared Agent loop

`AgentLoopEngine` is the only model/tool feedback loop. ReAct, Workers,
Reviewers, and the final Aggregator all reuse it with different:

- System Prompts;
- exposed tools;
- Completion Policies;
- task/context packets;
- iteration/Token budgets.

Each loop appends the assistant Tool Call followed by the matching Tool Result,
preserving `tool_call_id`. A no-tool response terminates only after the assigned
Completion Policy accepts it.

## Planning and scheduling

`LlmPlanner` emits strict JSON for complex goals. `PlanValidator` rejects:

- empty or duplicate task IDs;
- unknown, duplicate, or self dependencies;
- cycles;
- write plans without downstream verification.

`DagScheduler` computes stable topological order, ready tasks, batches, and
blocked descendants. Only read-scoped tasks may execute concurrently in the
shared workspace. Mutating, command, and verification tasks remain serial.

## SubAgent isolation

A `TaskPacket` contains only:

- plan ID and original goal;
- current Task and acceptance criteria;
- direct dependency results and changed files;
- current attempt and Reviewer feedback.

Workers do not inherit the parent ReAct history or unrelated Worker histories.
Tool schemas are capability-scoped:

```text
read/analysis worker -> read-only tools
verification worker  -> read-only + execute_command
write/command worker  -> full configured tools
reviewer              -> read-only tools
aggregator             -> no tools
```

A hidden tool remains denied at execution time even if the model hallucinates
its name.

## Safety transaction

Production execution goes through `RunCoordinator`:

```text
create run + BEFORE snapshot
  -> start parent budget and root trace
  -> execute selected mode
  -> checkpoint plan/task/review transitions
  -> failure/partial: apply rollback policy
  -> AFTER snapshot
  -> finalize state and trace
```

Mutating tasks receive an additional pre-attempt snapshot. Resume restores the
uncertain boundary before retrying. When no safe task snapshot exists, PaiCLI
restores the complete run snapshot and restarts the DAG instead of replaying an
unknown side effect.

## Observability

SQLite trace data includes:

- one root run ID;
- nested spans with parent IDs, task IDs, roles, and Agent names;
- every provider attempt, including retries and failures;
- input/output/cache Tokens, latency, tool-schema count, and configured cost;
- every Tool Result, changed files, timeout/retryability, and redacted args;
- run status, error, snapshots, rollback, and parent budget.

Raw private chain-of-thought is not persisted. Tool arguments and error strings
pass through secret redaction.

## Persistence

```text
.paicli/runs.db        current run state + append-only checkpoints
.paicli/traces.db      runs, spans, model calls, tool calls, events
.paicli/code-index.db  incremental code chunks and FTS index
.paicli/snapshots/     compressed tree snapshots
.paicli/audit/         HITL decisions
~/.paicli/memory.db    default versioned long-term memory
```

SQLite uses WAL mode for local thread-safe operation. This is not a distributed
lease or multi-host scheduler.

## Retrieval and memory

Code retrieval fuses:

```text
exact/partial symbol rank
+ SQLite FTS5 BM25 rank
+ lexical cosine rank
+ optional dense embedding rank
-> Reciprocal Rank Fusion
```

Results contain source text, file path, symbol, and exact line span. Dense
embeddings are optional so a fresh install remains dependency-free.

Long-term memory is separate from code retrieval. Managed memory records an ID,
source, source hash, verification state, and lifecycle status. A source version
change can mark dependent memories stale; stale/deleted memories are excluded
from normal retrieval.

## Extension points

Stable protocols/interfaces include:

- `LlmClient`
- `ToolRuntime` and `ToolSpec`
- `CompletionPolicy`
- `Planner`
- `ResultAggregator`
- `OrchestrationObserver`
- `EmbeddingClient`
- evaluation `CaseExecutor`

New extensions should preserve code-owned invariants and fail closed when risk,
side effect, or concurrency metadata is unknown.
