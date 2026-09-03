# PaiCLI Python

PaiCLI is a local coding-agent harness with three real execution modes:

```text
ReAct: model <-> tools until a completion gate accepts the result
Plan:  LLM Planner -> validated DAG -> isolated task workers -> final answer
Team:  Planner -> workers -> read-only Reviewer -> local repair -> final answer
```

The Agent runtime, file changes, tests, memory, snapshots, checkpoints, traces,
and evaluation all run on the local machine. Only model inference is sent to the
configured provider. The default integration is Alibaba Cloud Model Studio
(DashScope) through its OpenAI-compatible chat-completions API.

## What is implemented

- One shared `AgentLoopEngine` for ReAct and every SubAgent.
- Real CLI modes: `react`, `plan`, and `team`.
- DashScope, GLM, DeepSeek, StepFun, Kimi, and local/remote vLLM providers.
- Function Calling with execution-time JSON Schema validation and SSE streaming
  for content, reasoning fields, fragmented Tool Calls, and provider usage.
- LLM-generated plans with bounded JSON repair and deterministic DAG checks.
- Isolated Worker, Reviewer, and Aggregator histories and tool capabilities.
- Reviewer approval based on actual changed-file reads, not worker narration.
- Reviewer `changes_requested` retries only the current task, with a hard limit.
- Precise `replace_text`, `multi_edit`, `apply_patch`, `grep`, and `glob` tools;
  ranged reads and optimistic SHA checks avoid unnecessary whole-file rewrites.
- HITL for writes and commands, unified diff previews, unconditional hard command
  policy, persistent exact/pattern permissions, and redacted audit records.
- BEFORE/AFTER workspace snapshots, rollback policy, SQLite checkpoints, and
  resumable interrupted Plan/Team runs.
- Parent-child traces for model calls, tool calls, spans, Token usage, latency,
  failures, and configurable cost estimates.
- Persistent hybrid code retrieval: symbol + SQLite FTS5/BM25 + lexical cosine
  + optional embeddings, fused through RRF and returned with source line spans.
- Versioned long-term memory with IDs, provenance, verification, staleness, and
  soft deletion.
- Project instruction loading for every role, optional real stdio LSP diagnostics,
  role-specific model providers, and explicitly configured Skill/MCP/Web extensions.
- A fixed coding benchmark and baseline/candidate comparison reports.

The original `phase-xx` educational tags remain available. Current `develop`
is the integrated product line; do not infer current behavior from an old tag.

## Requirements

- Python 3.11 or 3.12
- Git
- A model endpoint that supports the OpenAI-compatible
  `/chat/completions` protocol
- For coding tasks that execute tests: the target repository's own toolchain

No third-party Python package is required for the core runtime.

## Install

```bash
git clone <repository-url> paicli-python
cd paicli-python
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Verify the installation without contacting a model:

```bash
python -m paicli --help
python -m unittest discover -s tests -q
```

The normal test suite uses deterministic fake clients and local HTTP servers.
Real-provider tests are opt-in so CI remains fast, reproducible, and free of
API charges.

## Configure DashScope

Copy the template:

```bash
cp .env.example .env
```

Set these values in `.env`:

```dotenv
DASHSCOPE_API_KEY=your-real-key
DASHSCOPE_MODEL=qwen-plus
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
DASHSCOPE_CONTEXT_WINDOW=131072
```

`.env` is ignored by Git. Do not paste, print, trace, or commit the key.

Check chat and Tool Calling before allowing file writes:

```bash
python -m paicli --provider dashscope --check-model chat
python -m paicli --provider dashscope --check-model tools
```

## Run ReAct

Read-only example:

```bash
python -m paicli \
  --provider dashscope \
  --mode react \
  --project-root ./demo \
  -p "Read README.md and summarize the public API."
```

Coding example with explicit shell capability and interactive approvals:

```bash
python -m paicli \
  --provider dashscope \
  --mode react \
  --project-root ./demo \
  --allow-shell \
  --approval-mode ask \
  -p "Fix the failing unit test and run the complete suite."
```

`--allow-shell` exposes `execute_command`; `--approval-mode ask` still requires
confirmation before each command and file mutation. A command preview or
unified diff is displayed before approval.

The local coding toolset also includes:

```text
read_file      line ranges + optional SHA-256
replace_text   exact optimistic replacement
multi_edit     validate all replacements before one atomic file write
apply_patch    validate a multi-file unified diff before applying it
grep           bounded literal/regex search without shell
glob           bounded project-root path matching without shell
```

Hard command denial is enforced by the base ToolRegistry even when PaiCLI is
embedded as a library without interactive HITL. Persist a literal exact call
with `a`, or deliberately enter a glob with `p`; literal wildcard characters in
an exact call are escaped before storage. Approval handlers cannot rewrite
already validated arguments. Rules are stored in `.paicli/permissions.json`;
choose another file with `--permission-file`.

## Project instructions and diagnostics

`AGENTS.md` (preferred) or `.paicli.md` is loaded into the system prompt for
ReAct, Planner, Worker, Reviewer, and Aggregator. The content is bounded and kept
in a separate prompt layer rather than concatenated into user messages.

Python edits always receive an `ast.parse` syntax check. Configure a real stdio
language server when available:

```bash
python -m paicli \
  --python-lsp-command "pyright-langserver --stdio" \
  --lsp-timeout-seconds 8 \
  ...
```

Published diagnostics are returned to the Agent and block completion while an
error remains. If the optional server cannot start, the runtime falls back to
the deterministic syntax checker.

## Role-specific models

The default `--provider` remains the ReAct/fallback provider. Plan and Team can
select independent providers without changing orchestration code:

```bash
python -m paicli \
  --provider dashscope \
  --planner-provider dashscope \
  --worker-provider deepseek \
  --reviewer-provider glm \
  --aggregator-provider dashscope \
  ...
```

Each role client retains its own retry, Trace, Token, and pricing records.

## Optional Skill, MCP, browser, and web extensions

Optional external services never start merely because their modules exist. Copy
`extensions.example.json`, remove unused entries, and enable it explicitly:

```bash
python -m paicli --extensions-file ./extensions.json ...
```

- Skill roots add a bounded index to every role prompt and register `load_skill`.
- MCP stdio/HTTP servers register namespaced `mcp__server__tool` tools. Stdio
  requests have a hard timeout and bounded stderr capture.
- Browser automation is supplied by an explicitly configured browser MCP server,
  such as Chrome DevTools MCP.
- Web access is disabled by default. Enabled fetch/search requires a host
  allow-list, public DNS addresses, redirect-hop validation, and bounded bodies.
  Malformed HTML that ends inside an ignored active-content element fails rather
  than returning a successful but silently truncated document.

See `extensions.example.json`, [docs/p0-p2-hardening.md](docs/p0-p2-hardening.md),
and [docs/security-hardening-2026-09-02.md](docs/security-hardening-2026-09-02.md).

## Run Plan mode

Non-interactive execution after deterministic plan validation:

```bash
python -m paicli \
  --provider dashscope \
  --mode plan \
  --project-root ./demo \
  --allow-shell \
  --approval-mode ask \
  -p "Inspect the implementation, add subtract(), add tests, and run all tests."
```

Interactive mode adds plan review and bounded revision:

```bash
python -m paicli --provider dashscope --project-root ./demo --allow-shell

> /plan Inspect the implementation, add subtract(), add tests, and run all tests.
```

The planner proposes a complete JSON DAG. Code validates IDs, dependencies,
cycles, topological order, and the rule that every `FILE_WRITE` task has a
downstream `VERIFICATION` task. Invalid plans receive one bounded repair
request. Interactive plan review supports execute, cancel, or supplemental
requirements followed by a complete replan.

## Run Team mode

```bash
python -m paicli \
  --provider dashscope \
  --mode team \
  --project-root ./demo \
  --allow-shell \
  --approval-mode ask \
  -p "Fix division-by-zero handling, add tests, run them, and review the actual changes."
```

Team flow:

```text
Planner
  -> deterministic DAG validation
  -> task-scoped Worker
  -> read-only Reviewer
       approved            -> task completed
       changes_requested   -> same task/worker retries locally
       rejected/error      -> task failed
  -> failed descendants skipped; independent branches continue
  -> no-tool final Aggregator
```

Read-only tasks may run concurrently. `FILE_WRITE`, `COMMAND`, and
`VERIFICATION` tasks run serially in the shared workspace. PaiCLI does not claim
safe concurrent mutation until per-worker worktrees and merge semantics exist.

## Safety model

The model never executes code directly. Every operation passes through:

```text
Tool Schema -> runtime validation -> hard policy -> HITL -> resource scheduler
            -> handler -> typed ToolResult -> trace/checkpoint
```

Important defaults:

- File access is restricted to `--project-root`, including symlink resolution.
- Shell is hidden unless `--allow-shell` is set.
- Side effects require HITL in the production CLI.
- `approval-mode=ask` is interactive; `deny` fails closed; `allow` is an
  explicit automation choice and should be used only in disposable workspaces.
- Obvious destructive command patterns are denied before human approval.
- Failed/partial runs ask before restoring the BEFORE snapshot by default;
  stopped runs caused by iteration, stagnation, or Token limits keep their workspace.
- API keys and common secret fields are redacted from traces and audit logs.

See [SECURITY.md](SECURITY.md) for the trust boundary and residual risks.

## Snapshots, rollback, and recovery

Each coordinated run records:

```text
.paicli/runs.db       durable run/DAG/review/checkpoint state
.paicli/traces.db     spans, model calls, tool calls, events, and metrics
.paicli/snapshots/    compressed workspace snapshots
.paicli/audit/        redacted HITL decisions
```

List recent runs without connecting to a model:

```bash
python -m paicli --project-root ./demo --list-runs
```

Resume an interrupted or failed run:

```bash
python -m paicli \
  --provider dashscope \
  --project-root ./demo \
  --allow-shell \
  --resume run_<id>
```

Before a mutating task starts, PaiCLI stores a task-level snapshot. After a
process interruption it restores the uncertain task boundary before retrying.
Local runs record their owner PID, so opening a second PaiCLI process does not
silently mark a still-live run as interrupted. If a task snapshot is missing,
PaiCLI restores the complete run snapshot and restarts the DAG rather than
blindly replaying a possibly committed side effect.
Oversized, unreadable, or over-budget files are recorded as skipped and are
preserved during restore instead of preventing the Agent from starting. Old
unreferenced snapshots are pruned according to `--snapshot-retention`; snapshots
needed by resumable runs are retained.

Detailed semantics: [docs/recovery.md](docs/recovery.md).

## Budgets, tracing, and cost

Per-Agent protections:

```text
--max-steps 50
--stagnation-window 3
--token-budget <per-agent-token-limit>
```

Parent orchestration limits shared by Planner, Workers, Reviewers, retries, and
Aggregator:

```text
--max-run-tokens 100000
--max-run-cost-cny 10
--max-run-seconds 900
--max-model-calls 50
--max-tool-calls 100
```

Token usage comes from the provider response. PaiCLI includes one versioned
baseline for DashScope `qwen-plus` in Beijing with requests up to 128K input
Tokens: CNY 0.8/M input and CNY 2.0/M non-thinking output. The model currently
does not use context-cache accounting in PaiCLI. Configure overrides whenever
the model, region, input tier, thinking mode, or provider price differs:

```dotenv
PAICLI_PRICE_DASHSCOPE_QWEN_PLUS_INPUT_CNY_PER_MILLION=
PAICLI_PRICE_DASHSCOPE_QWEN_PLUS_OUTPUT_CNY_PER_MILLION=
PAICLI_PRICE_DASHSCOPE_QWEN_PLUS_CACHED_CNY_PER_MILLION=0
```

Models without an exact configured or versioned price remain visible as
`unpriced_model_calls` instead of being falsely reported as zero cost.

## Code retrieval and memory

The production CLI builds `.paicli/code-index.db` through the canonical
`paicli.rag.CodeIndex` and exposes `search_code`. Results contain the file,
exact line range, symbol, retrieval channels, and source text. Successful file
mutations trigger an incremental index refresh. Disable it with `--no-rag` or
choose a path with `--rag-path`. `paicli.hybrid_rag.HybridCodeIndex` remains a
1.x compatibility API but is not the product assembly path.

The canonical SQLite API is `ManagedMemoryStore`. The default long-term memory
is `~/.paicli/memory.db`; choose another path with `--memory-file`. In the full
application runtime, `save_memory` requires policy/HITL approval. Model-authored
writes enter as `unverified` and are excluded from normal future-context
retrieval until explicitly promoted; diagnostic code may opt in to inspecting
unverified candidates. Explicit `.jsonl` paths keep the original educational
append-only implementation for 1.x compatibility but emit a deprecation
warning. `ManagedLongTermMemory` remains a compatibility adapter. SQLite memory
adds deduplication, provenance, confidence, verification, source hashes, stale
and superseded states, conflict resolution, and soft deletion. Disable memory
with `--no-memory`.

## Fixed-task evaluation

Run the repository's real-model smoke benchmark:

```bash
paicli-eval run \
  --suite eval/suites/coding-smoke.json \
  --provider dashscope \
  --output reports/dashscope-current.json
```

Run the same suite against a real historical commit without creating another
worktree:

```bash
paicli-eval run \
  --suite eval/suites/coding-smoke.json \
  --provider dashscope \
  --revision 2107eab \
  --repository . \
  --output reports/phase5-dashscope-baseline.json
```

Compare baseline and candidate, then aggregate repeated candidate runs:

```bash
paicli-eval compare \
  reports/phase5-dashscope-baseline.json \
  reports/dashscope-current.json \
  --output reports/phase5-vs-1.0.json

paicli-eval stability \
  reports/dashscope-1.0-run-01.json \
  reports/dashscope-1.0-run-02.json \
  reports/dashscope-current.json \
  --output reports/dashscope-1.0-stability.json
```

A report includes task/assertion success, Git commit, model, model/tool errors,
Token usage, latency, configured cost, and changed files. Historical modes that
do not exist fail explicitly rather than being simulated by current code.

The historical 1.0 sample records 4/5 fully successful suites and retains its
cross-Task Team failure. The final-code 1.1 sample records 3/3 fully successful
suites, 9/9 successful Tasks, and 21/21 passing deterministic assertions; a
pre-fix repeated-read failure is also retained separately. Long suites can use
`--task-id` fragments plus `paicli-eval merge` without mixing commits. See
[docs/evaluation.md](docs/evaluation.md) and [reports/README.md](reports/README.md).

## Real-provider tests

After chat/tool probes pass:

```bash
PAICLI_RUN_DASHSCOPE_LIVE_TEST=1 \
python -m unittest tests.test_dashscope_live -v
```

For the optional A40 vLLM tunnel:

```bash
PAICLI_RUN_VLLM_LIVE_TEST=1 \
python -m unittest tests.test_vllm_live -v
```

Never enable live tests in untrusted pull requests, because model-controlled
code operations consume credentials and may create billable API calls.

## Architecture and implementation record

- [ARCHITECTURE.md](ARCHITECTURE.md)
- [SECURITY.md](SECURITY.md)
- [docs/recovery.md](docs/recovery.md)
- [docs/evaluation.md](docs/evaluation.md)
- [docs/phase-09-dashscope-live.md](docs/phase-09-dashscope-live.md)
- [docs/phase-10-15-implementation.md](docs/phase-10-15-implementation.md)
- [docs/final-acceptance.md](docs/final-acceptance.md) — historical 1.0 evidence
- [docs/1.1.0-acceptance.md](docs/1.1.0-acceptance.md) — P0–P2 release evidence
- [docs/security-hardening-2026-09-02.md](docs/security-hardening-2026-09-02.md) — permission, retry, memory, snapshot, and recovery audit response
- [reports/README.md](reports/README.md)
- [docs/java-parity.md](docs/java-parity.md)
- [PHASES.md](PHASES.md)

## Acceptance status

The final release is gated by these checks:

```text
[x] one clean develop mainline
[x] ReAct / Plan / Team execute from CLI
[x] DashScope real chat, Tool Calling, ReAct, Plan, and Team tests pass
[x] Planner generates and repairs a validated DAG
[x] Workers read, modify, and deterministically verify code
[x] Reviewer reads actual changed artifacts
[x] Reviewer retries only the current task
[x] HITL, diff, and command/file permission gates are active
[x] failure snapshots and rollback work
[x] interrupted task state can resume safely
[x] model/tool calls have parent-child traces
[x] Token, configured cost, latency, and errors are reported
[x] fixed task reports compare baseline and candidate behavior
[x] repeated real-model reports preserve success and failure variance
[x] fresh Python 3.11/3.12 environments reproduce installation and CLI entry points
```

Historical 1.0 evidence remains in
[docs/final-acceptance.md](docs/final-acceptance.md). P0–P2 implementation,
pre-fix and final-code real-model runs, exact metrics, and residual boundaries
are recorded in [docs/1.1.0-acceptance.md](docs/1.1.0-acceptance.md). Release tags
are created only after the final clean `develop` commit passes the remote
Python 3.11/3.12 CI matrix.
