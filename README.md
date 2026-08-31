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
- Function Calling with execution-time JSON Schema validation.
- LLM-generated plans with bounded JSON repair and deterministic DAG checks.
- Isolated Worker, Reviewer, and Aggregator histories and tool capabilities.
- Reviewer approval based on actual changed-file reads, not worker narration.
- Reviewer `changes_requested` retries only the current task, with a hard limit.
- HITL for writes and commands, unified diff previews, hard command policy, and
  redacted audit records.
- BEFORE/AFTER workspace snapshots, rollback policy, SQLite checkpoints, and
  resumable interrupted Plan/Team runs.
- Parent-child traces for model calls, tool calls, spans, Token usage, latency,
  failures, and configurable cost estimates.
- Persistent hybrid code retrieval: symbol + SQLite FTS5/BM25 + lexical cosine
  + optional embeddings, fused through RRF and returned with source line spans.
- Versioned long-term memory with IDs, provenance, verification, staleness, and
  soft deletion.
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
- Failed/partial runs roll back to their BEFORE snapshot by default.
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
If that snapshot is missing, it restores the complete run snapshot and restarts
the DAG rather than blindly replaying a possibly committed side effect.

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
is `~/.paicli/memory.db`; choose another path with `--memory-file`. Model-authored
writes enter as `unverified` until explicitly promoted. Explicit `.jsonl` paths
keep the original educational append-only implementation, and
`ManagedLongTermMemory` remains a 1.x compatibility adapter. SQLite memory adds
deduplication, provenance, confidence, verification, source hashes, stale and
superseded states, conflict resolution, and soft deletion. Disable memory with
`--no-memory`.

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
do not exist fail explicitly rather than being simulated by current code. See
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
- [docs/final-acceptance.md](docs/final-acceptance.md)
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
[x] a fresh clone can reproduce installation, tests, and live probes
```

The release checklist is marked complete only after normal tests, real
DashScope tests, the fixed suite, a clean `develop`, and fresh-install smoke
checks all pass.
