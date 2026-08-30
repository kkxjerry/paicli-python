# Java → Python behavior parity ledger

This file is the Phase 0 source of truth for porting PaiCLI behavior.  The goal
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

## Explicitly out of scope for Phase 0–2

The following remain separate phases and must not be advertised as completed by
this change:

- LLM Planner and unified DAG validation
- `/plan` and `/team` CLI modes
- real Worker/Reviewer sub-agents
- reviewer retry and failure propagation
- runtime JSON Schema validation
- conflict-aware tool or worker scheduling
- automatic memory/context wiring
- snapshots, checkpoints, and evaluation harness

## Compatibility rules

1. `Agent.run()` continues returning a string on successful completion.
2. Existing callers that expect `AgentLoopError` on a safety stop keep working.
3. `Agent.run_outcome()` is the new structured API for future orchestration.
4. Existing `ChatResponse(content, tool_calls)` construction remains valid;
   usage fields default to zero.
5. Budgets are recreated for every run; token and stagnation state never leak
   from one user turn to the next.

## Phase 0–2 known boundaries

- Token enforcement uses provider-reported `usage`. If a compatible endpoint
  omits usage, the loop still has stagnation and iteration protection but
  cannot infer exact billed tokens in this phase.
- `NonEmptyCompletionPolicy` only prevents empty success. Repository tests,
  diff checks, and reviewer verdicts remain later completion policies.
- `AgentOutcome.changed_files` currently records successful built-in
  `write_file` calls. Shell or MCP side effects are intentionally not guessed.
