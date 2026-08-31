# Phase 9 — DashScope real-model validation

Date: 2026-08-30
Base commit: `d773d50`
Provider: Alibaba Cloud Model Studio / DashScope OpenAI-compatible API
Default live-test model: `qwen-plus`

## Security handling

The API key is read from `DASHSCOPE_API_KEY`. Tests and diagnostics only check
whether it is present; they never print the value or the Authorization header.
All live coding tests run against temporary toy projects, not the PaiCLI source
tree.

## Native provider configuration

```dotenv
DASHSCOPE_API_KEY=...
DASHSCOPE_MODEL=qwen-plus
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
DASHSCOPE_CONTEXT_WINDOW=131072
DASHSCOPE_TIMEOUT_SECONDS=120
```

```bash
python3 -m paicli --provider dashscope --check-model chat
python3 -m paicli --provider dashscope --check-model tools
```

The provider factory validates timeout and context-window overrides before any
network request.

## Real executions completed

The following were executed with the actual `DASHSCOPE_API_KEY` from the login
shell; no FakeClient was involved:

1. Chat probe returned `PAICLI_OK`.
2. Tool probe emitted `probe_echo` with the expected structured argument.
3. ReAct called `read_file`, received the marker through the matching tool-call
   ID, and used it in the final answer.
4. Plan mode used a real LLM Planner, generated and validated a DAG, read a
   temporary `calculator.py`, added `subtract`, created unittest coverage, ran
   `python3 -m unittest -q`, and completed successfully.
5. Team mode used a test-only registry that deliberately corrupted the first
   file write. A real Reviewer inspected the changed file with `read_file`,
   rejected the local defect as retryable, the same Task alone was rerun, and a
   later review approved the corrected multiplication implementation.

## Commands

Live tests are opt-in because they spend API quota:

```bash
PAICLI_RUN_DASHSCOPE_LIVE_TEST=1 \
  python3.11 -m unittest tests.test_dashscope_live -v
```

Ordinary CI keeps these cases skipped while all control-flow behavior remains
covered by deterministic FakeClient tests.

## Reliability lessons from the live run

The first Team attempt exposed a real model/harness boundary: the Reviewer
correctly found the wrong implementation but labeled a locally repairable error
as `rejected`. The review contract now contains an explicit `retryable` boolean.
The model supplies quality evidence; deterministic orchestration owns whether a
local retry is allowed. This avoids making state transitions depend on subtle
word choice between `rejected` and `changes_requested`.

Review approval also requires actual read evidence for every changed artifact.
A Reviewer that only repeats the Worker's narrative receives a bounded repair
request and cannot silently approve.
