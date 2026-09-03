# PaiCLI 1.1.1 security and recovery hardening — 2026-09-02

Release line: `develop` / `v1.1.1`
Predecessor: `v1.1.0`

This record documents a focused audit response applied after the PaiCLI 1.1.0
release. The work changes reachable runtime behavior rather than adding another
Agent role or extending the feature surface.

## Decisions

| Finding | Decision | Runtime behavior |
|---|---|---|
| `a=always exact` stored raw values as glob patterns | Fixed | Exact decisions escape `*`, `?`, and `[` before persistence; only the explicit `p=pattern` path grants glob authority. |
| Approval handlers could return replacement arguments | Removed | `ApprovalResult` can approve/deny and optionally remember a rule, but cannot rewrite arguments after validation and hard-policy assessment. |
| Large `Retry-After` values could block the CLI for hours | Fixed | Provider waits up to 60 seconds are honored; larger values fail immediately with the requested wait and local limit in the error. They are not clamped into rapid retry loops. |
| Malformed HTML could end inside an ignored element | Fixed | Web extraction fails instead of returning a successful but silently partial document when input ends inside `script`, `style`, `noscript`, or `svg`. |
| Concurrent Team failures replaced task records | Fixed | A `TaskRunRecord` is registered before execution and retained when Worker, Reviewer, or checkpoint callbacks raise. Existing Tool Results, changed files, usage, and review evidence remain available for diagnosis and recovery. |
| Nested audit arguments were not structurally redacted | Fixed | Audit events are parsed and recursively redacted by key before being written as JSONL. |
| Tree-snapshot symlink entries were restored without complete validation | Fixed | The full manifest is validated before any deletion or overwrite; symlink path, parent, and resolved target must remain inside the project root. |
| Checkpoint sequence was read before acquiring the write transaction | Fixed | `BEGIN IMMEDIATE` now precedes sequence allocation and the checkpoint/run-state update. |
| Model-authored memory could affect later sessions immediately | Fixed | `save_memory` is approval-worthy in the production runtime. Managed model writes remain `unverified`, and default context retrieval includes only `active` and `verified` records. Explicit diagnostic inspection can request unverified records. |
| Legacy JSONL memory lacks trust lifecycle | Deprecated | Explicit `.jsonl` memory remains available for 1.x compatibility but emits a warning. It is scheduled for removal rather than receiving new trust features. |
| Starting another process interrupted every `RUNNING` row | Fixed for local use | Runs record an owner PID. Startup marks only rows whose owner is no longer alive as `interrupted`; a live local PaiCLI process is not silently displaced. |

## Security invariants

The resulting order for a side-effecting Tool Call is:

```text
model proposal
→ tool name and JSON Schema validation
→ unconditional hard policy
→ persistent exact/pattern permission resolution
→ optional HITL decision
→ original validated arguments only
→ resource scheduling and handler
→ typed ToolResult, audit, trace, and checkpoint
```

An approval cannot alter arguments, and a persistent rule cannot override the
hard command policy. Exact rules are literal; pattern rules require the user to
select the explicit pattern flow.

Long-term memory uses a separate trust boundary:

```text
model calls save_memory
→ HITL/policy
→ write as unverified
→ excluded from normal context retrieval
→ explicit verification/promotion
→ eligible for future context injection
```

This prevents a repository or web prompt injection from becoming trusted merely
because the model chose to persist it.

## Recovery boundary

PID ownership is a local-process safeguard, not a distributed lease. It does not
provide fencing tokens, protect against PID reuse across long periods, coordinate
two mutation-capable processes in the same working tree, or allow two processes
to resume the same Run safely. Multi-host ownership still requires a real
lease/heartbeat design.

Snapshots continue to cover only project-root filesystem state. They do not undo
network calls, package publication, remote database changes, or shell effects
outside the project tree.

## Compatibility

`ApprovalResult.arguments` was intentionally removed. A custom approval handler
must now return only a decision and optional remembered permission metadata. A
caller that needs transformed arguments must implement a separate, explicitly
validated tool or preprocessing layer before authorization rather than modifying
an already assessed request.

Legacy JSONL memory is retained during the 1.x line but is no longer the
recommended runtime. `ManagedMemoryStore` is the canonical implementation.

## Verification

The change is covered by deterministic tests for:

- literal permission values containing `*`, `?`, and `[`;
- nested audit credential redaction;
- excessive `Retry-After` fail-fast behavior;
- malformed HTML partial-extraction rejection;
- concurrent Team record retention after observer/checkpoint failure;
- pre-mutation validation of tampered symlink manifests;
- checkpoint transaction ordering;
- live/dead owner PID handling;
- approval of cross-session memory writes;
- exclusion and explicit inspection of unverified memory;
- legacy JSONL deprecation warning.

Release validation also runs the complete Python 3.11 and 3.12 suites,
`compileall`, warning-as-error checks, the opt-in DashScope live tests, Git diff
hygiene, and remote CI before any new tag is created.
