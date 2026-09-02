# P0–P2 hardening acceptance

Date: 2026-09-01
Branch under test: `codex/p0-p2-hardening`
Base commit: `9dc8d1438941f2865655bde99d3c500f80fbb558`

## Decision

The P0–P2 hardening change set is ready to merge into `develop`. This document
records the pre-merge evidence; it does not claim that a release tag or remote
CI already contains the changes.

## Implemented scope

### P0 — reachable-path safety and recovery

- destructive command policy enforced by the base `ToolRegistry`;
- library runtime fails closed unless HITL is explicitly disabled;
- oversized/unreadable snapshot files are recorded as skipped rather than
  preventing run startup;
- skipped paths are preserved during restore;
- iteration, stagnation, and per-Agent Token exits use resumable `STOPPED`
  status and do not trigger automatic rollback;
- snapshot retention protects snapshots referenced by resumable runs;
- runtime capability matrix reports only assembled capabilities;
- MIT `LICENSE` and provenance `NOTICE.md` added.

### P1 — coding primitives, instructions, and diagnostics

- `replace_text`, `multi_edit`, `apply_patch`, `grep`, and `glob` tools;
- ranged `read_file` plus optional SHA-256 and optimistic edit checks;
- all-or-nothing validation for multi-file edits;
- HITL collects decisions first, then delegates the approved batch to the
  conflict-aware scheduler;
- `AGENTS.md` / `.paicli.md` prompt layer wired to ReAct, Planner, Worker,
  Reviewer, and Aggregator;
- syntax/Language Server diagnostics are returned to the Agent and block
  completion while errors remain;
- optional real stdio Language Server with deterministic AST fallback.

### P2 — streaming, permissions, role models, and extensions

- OpenAI-compatible SSE streaming for content, reasoning variants, fragmented
  Tool Calls, and Usage;
- persistent exact/pattern permission rules, with hard policy taking priority;
- role-specific Planner/Worker/Reviewer/Aggregator clients;
- explicit Skill, MCP, browser-via-MCP, and allow-listed Web extension config;
- MCP stdio timeout, bounded stderr, response/notification handling, and clean
  process/PIPE shutdown;
- no optional external service starts without explicit configuration.

## Deterministic gates

Executed in the feature worktree on macOS arm64 without local Docker:

```text
Python 3.11: 232 tests passed, 5 opt-in external tests skipped
Python 3.12: 232 tests passed, 5 opt-in external tests skipped
Python 3.11 compileall: passed
Python 3.12 compileall: passed
git diff --check: passed
LSP ResourceWarning gate: passed
MCP PIPE ResourceWarning gate: passed
```

The five skips are four opt-in DashScope tests and one opt-in vLLM test.

## Real DashScope gate

With `PAICLI_RUN_DASHSCOPE_LIVE_TEST=1` and credentials supplied only through
ignored environment configuration:

```text
DashScope Live tests: 4 / 4 passed in one complete run
```

The live run covered chat/Function Calling, ReAct tool feedback, Planner and
Worker code modification plus verification, and Reviewer changed-file evidence
with same-Task retry after deliberate artifact corruption.

The deliberate-corruption fixture observes every successful repository
mutation, so the gate remains valid whether the model selects `write_file`,
`replace_text`, `multi_edit`, or `apply_patch`.

## Corrected integration defects

The pre-acceptance full run exposed and this change set fixed:

1. missing `ApplicationRuntime.extensions` state;
2. tests relying on implicit write approval after library runtime became
   fail-closed;
3. `STOPPED` and Plan-resume tests being masked by denied test writes;
4. inconsistent streaming event names (`reasoning` versus
   `reasoning_delta`);
5. unclosed Language Server process pipes;
6. unclosed MCP stdio process pipes;
7. the Reviewer live-test fixture intercepting only the legacy whole-file
   write primitive.

## Explicit boundaries

This acceptance does not claim:

- an OS-level shell sandbox;
- safe parallel code mutation without isolated worktrees;
- arbitrary remote side-effect exactly-once semantics;
- a long-lived IDE-grade Language Server workspace;
- full MCP Streamable HTTP/SSE/OAuth/Sampling support;
- statistically calibrated Reviewer accuracy.

Final release acceptance, versioning, remote CI, and tags are performed only
after merge into a clean `develop` checkout.
