# Java → Python behavior parity ledger

This file is the Phase 0–5 source of truth for porting PaiCLI behavior. The goal
is **behavioral parity at public boundaries**, not line-by-line Java
translation.  Intentional Python improvements are recorded explicitly so a
future change cannot silently reintroduce a Java limitation.

## Baseline inspected

- Python baseline: `develop` at `718411e`
- Java reference: local `develop` at `13eff7f`
- Python baseline tests: 64 passed, 1 opt-in vLLM test skipped
- Java focused core tests: 84 passed

## Phase 0–2 parity matrix

| Capability | Java reference behavior | Python before this work | Python Phase 0–2 target | Decision |
|---|---|---|---|---|
| Execution kernel | `Agent` and `SubAgent` each own similar loops | ReAct loop embedded in `Agent` | Shared `AgentLoopEngine`; public `Agent` is a facade | Improve architecture |
| Normal finish | no tool calls means final answer | same | no tool calls **and completion policy accepts it** | Deliberate improvement |
| Empty final answer | accepted | accepted | rejected and fed back to the model | Deliberate improvement |
| Hard iteration limit | 50 | 20 | 50 by default, configurable | Match Java |
| Stagnation | 3 identical tool-call rounds | absent | 3 identical normalized tool + observation rounds | Improve Java detection |
| Token budget | optional; unlimited by default | absent from loop | optional hard input + output budget; unlimited by default | Match Java |
| Provider usage | input/output/cached tokens parsed | not parsed | parsed into every `ChatResponse` and accumulated | Match Java |
| Controlled stop result | user-facing string | exception only | structured `AgentOutcome`; legacy `run()` still raises | Improve architecture |
| Cancellation | cooperative checks | cooperative checks | preserved inside shared loop | Preserve |
| Tool feedback protocol | assistant call → tool result → next model call | same | preserved inside shared loop | Preserve |
| Tool-call signature | raw argument JSON string | absent | canonical JSON plus result hash | Deliberate improvement |
| Shell default | callable; HITL may be disabled | disabled unless explicitly enabled | keep disabled by default | Intentional safety divergence |

## Phase 3–5 parity matrix

| Capability | Java reference behavior | Python before Phase 3–5 | Python Phase 3–5 result | Decision |
|---|---|---|---|---|
| Tool result | mostly user-facing strings | strings only | typed `ToolResult` plus compatible string facade | Improve architecture |
| Runtime argument validation | schemas sent to model, limited handler checks | JSON object check only | dependency-free JSON Schema subset enforced before policy and handler | Deliberate improvement |
| Tool scheduling | same-response calls may run concurrently | every call ran concurrently | read/write resource claims create conflict-free waves | Deliberate improvement |
| External MCP side effects | MCP tools require approval in Java CLI | treated like ordinary tools | unknown risk, serial scheduling, HITL classification | Match safety intent |
| Default context wiring | memory/compaction active in main Agent | modules existed but CLI omitted them | default bootstrap wires context, LLM compaction, LSP and `save_memory` | Match Java |
| History compaction | summarize old history, retain 3 recent user rounds | deterministic per-message truncation | LLM summary with protocol-safe split, cache and deterministic fallback | Match and improve |
| Long-window memory | still managed | LONG profile bypassed memory | every profile uses the same memory pipeline | Deliberate fix |
| Planner source | LLM for complex goals, rule fast-path for obvious simple goals | `StaticPlanner` only | `LlmPlanner` plus retained `StaticPlanner` | Match Java |
| Plan repair | malformed model output may be retried | absent | one bounded repair by default with exact validation feedback | Improve reliability |
| DAG representation | Java has separate Plan and Team graph models | one small static graph | one `ExecutionPlan`, `PlanValidator`, and `DagScheduler` for future modes | Improve architecture |
| DAG validation | `/plan` detects cycles; `/team` is weaker | unique/unknown/self/cycle checks | same validation contract for every future caller | Deliberate improvement |
| Failed independent branch | mode-dependent | executor returned immediately on first failure | blocked descendants skip; independent ready work continues | Deliberate improvement |
| Replan state transfer | completed work mainly enters prompt text | replacement lost result map | matching completed tasks may be inherited into replacement | Deliberate improvement |

## Explicitly out of scope after Phase 5

The following remain separate phases and must not be advertised as completed:

- `/plan` and `/team` CLI modes
- Plan tasks executed by the shared LLM `AgentLoopEngine`
- real Worker/Reviewer sub-agents
- reviewer verdict, retry, and failure propagation
- worker-level parallel execution and workspace isolation
- default CLI HITL assembly
- automatic turn snapshots, checkpoints, and evaluation harness
- production-grade memory conflict resolution and stale-memory invalidation

## Compatibility rules

1. `Agent.run()` continues returning a string on successful completion.
2. Existing callers that expect `AgentLoopError` on a safety stop keep working.
3. `Agent.run_outcome()` is the new structured API for future orchestration.
4. Existing `ChatResponse(content, tool_calls)` construction remains valid;
   usage fields default to zero.
5. Budgets are recreated for every run; token and stagnation state never leak
   from one user turn to the next.

## Phase 0–5 known boundaries

- Token enforcement uses provider-reported `usage`. If a compatible endpoint
  omits usage, the loop still has stagnation and iteration protection but
  cannot infer exact billed tokens in this phase.
- `NonEmptyCompletionPolicy` only prevents empty success. Repository tests,
  diff checks, and reviewer verdicts remain later completion policies.
- `AgentOutcome.changed_files` records structured file-resource claims from
  successful tools. Shell and undeclared MCP side effects are intentionally not guessed.
- The built-in validator enforces the common JSON Schema subset documented in
  `paicli/tool_validation.py`; remote `$ref` resolution and every draft keyword
  are not implemented.
- A Python thread cannot be safely killed. The scheduler reports a timeout for
  overdue thread-based tools, while process tools must also enforce their own
  subprocess/network timeout to stop the underlying operation.
- History-summary model calls are cached and have a deterministic fallback, but
  their token usage is not yet folded into the parent `AgentOutcome`.
- Long-term memory remains append-only keyword retrieval; conflict resolution,
  update/delete, provenance, and staleness are later work.
- `LlmPlanner` is a library capability in Phase 5. The default CLI remains ReAct
  until `/plan` is wired in the next phase.
