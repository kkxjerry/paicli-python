# P0–P2 runtime hardening and product wiring

This document records the post-1.0 hardening work. It distinguishes code that
exists from capability reachable through the assembled CLI/runtime.

## P0 — default-path correctness and safety

### Hard command policy is unconditional

`CommandGuard` is enforced by the base `ToolRegistry` before a shell process is
started. HITL can add approval but cannot bypass hard-deny rules. This also
protects library callers that explicitly enable shell without configuring an
interactive UI.

`build_application_runtime()` is fail-closed by default: side effects are
wrapped in HITL with `deny` behavior unless the caller explicitly supplies an
approval mode/handler or opts out with `enable_hitl=False` in an already isolated
environment.

### Snapshot behavior

Tree snapshots no longer prevent a run from starting merely because a generated
or binary file exceeds the configured per-file/total/file-count limit. Skipped
paths are recorded in snapshot metadata and preserved during restore.

A ReAct run stopped by iteration, stagnation, or per-Agent Token limits is stored
as `STOPPED`, not `FAILED`. It remains resumable and does not trigger automatic
whole-tree rollback. CLI failure rollback now defaults to an explicit user
choice. Snapshot retention removes unreferenced old snapshots while preserving
all snapshots required by resumable runs.

### Reachability evidence

`ApplicationRuntime.capability_matrix()` reports modes, model roles, actual tool
names, Memory, RAG, Trace, and optional extension tools from the assembled
runtime. Tests assert the default matrix so a README claim cannot silently drift
away from `bootstrap.py` wiring.

### Licensing

The Python project now includes an MIT `LICENSE` and a provenance `NOTICE.md`.
The license covers contributions owned by the repository owner; third-party code
or ideas remain subject to their original terms.

## P1 — coding primitives, project context, and diagnostic closure

### Precise local tools

In addition to whole-file `write_file`, the product runtime exposes:

```text
replace_text   exact replacement with expected count and optional file SHA
multi_edit     validate all replacements before one atomic file write
apply_patch    validate all file hunks before applying a unified diff
grep           bounded project-root regex/literal search without shell
glob           bounded project-root path matching without shell
```

`read_file` supports line ranges and SHA output. Mutations use temporary files
and `os.replace`; optimistic SHA checks prevent stale writes. HITL previews use
the same prepared mutation as the handler, avoiding preview/execution drift.

### Project instructions

The real runtime uses `PromptAssembler` for ReAct, Planner, Worker, Reviewer,
and Aggregator. Project instructions are loaded from `AGENTS.md` first, then
`.paicli.md`, bounded to a configured maximum. Skill/resource/runtime indexes
occupy separate prompt layers.

### Diagnostics close the loop

Post-edit diagnostics are appended to Agent history and observed by a completion
policy. Syntax errors therefore block completion instead of appearing only in
the terminal UI.

The default Python check remains dependency-free (`ast.parse`). An optional real
stdio LSP client can be configured, for example:

```bash
python -m paicli \
  --python-lsp-command "pyright-langserver --stdio" \
  --lsp-timeout-seconds 8 \
  ...
```

The client performs LSP initialize/didOpen/publishDiagnostics. If the configured
server is missing or crashes, PaiCLI records the condition internally and falls
back to deterministic syntax checking rather than losing the entire run.

### HITL and parallel reads

Approval prompts remain serialized, but approved calls are submitted together
to the base ToolRegistry. The resource scheduler can therefore execute safe
read/read calls concurrently while still serializing read/write, write/write,
process, and unknown-side-effect calls.

## P2 — interaction, permissions, model roles, and optional extensions

### Streaming

OpenAI-compatible clients support SSE streaming for content, reasoning fields,
fragmented Function Calling arguments, and usage. ReAct/SubAgent loops emit
`reasoning_delta` and `content_delta` events and mark streamed outcomes so the
CLI does not print the final content twice. Blocking clients continue to work
through the same `LlmClient` contract.

### Persistent permission rules

Approvals may be stored in `.paicli/permissions.json` as exact or glob rules over
Tool name and selected arguments. Rules are evaluated last-match-wins and may
explicitly allow, deny, or ask. The file is atomically replaced and restricted
to owner read/write permissions where supported. Hard policy denial always wins.

### Role-specific models

The CLI accepts independent provider choices for Planner, Worker, Reviewer, and
Aggregator while retaining a default ReAct provider:

```text
--planner-provider
--worker-provider
--reviewer-provider
--aggregator-provider
```

Each client is independently wrapped with retry, Trace, usage, and pricing. This
permits a coding-focused Worker, an independent Reviewer family, and a cheaper
Aggregator without duplicating orchestration logic.

### Explicit extension wiring

Optional modules are not started merely because their source files exist. Use an
explicit JSON file:

```bash
python -m paicli --extensions-file ./extensions.json ...
```

See `extensions.example.json`. Supported wiring:

- Skill roots: discover `SKILL.md`, add a bounded index to all role prompts, and
  register `load_skill`.
- MCP stdio/HTTP servers: initialize and register namespaced tools. Stdio
  requests have a hard timeout and bounded stderr diagnostics.
- Browser: configure a Chrome DevTools or other browser MCP server; its tools
  become ordinary scoped `mcp__server__tool` calls.
- Web: disabled by default. When enabled, every fetch requires an explicit host
  allow-list, validates DNS addresses and every redirect hop, rejects credentials
  and non-public addresses, bounds response bytes, and strips active HTML.

Extension startup failure is explicit and closes already-started transports;
the default runtime remains free of external-service startup dependencies.

## Explicit boundaries

This work does not claim:

- OS-level process/network/filesystem isolation for approved shell commands;
- safe concurrent mutation by multiple Workers without separate worktrees;
- exactly-once semantics for arbitrary external MCP/web operations;
- a persistent long-lived IDE-quality language-server session;
- complete MCP Streamable HTTP/SSE/OAuth/sampling lifecycle support;
- Reviewer accuracy without a human-labelled calibration set.

Those boundaries require separate product work rather than additional Prompt
instructions.
