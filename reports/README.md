# PaiCLI 1.0 release evidence

This directory contains reproducible, credential-free result artifacts from the
fixed suite in `eval/suites/coding-smoke.json`. The suite was executed with the
DashScope `qwen-plus` model alias. API keys, Authorization headers, temporary
workspaces, SQLite traces, and snapshot contents are not committed.

## Evidence set

| File | Purpose |
|---|---|
| `phase5-dashscope-baseline.json` | Real execution of historical commit `2107eab` through that revision's own CLI. |
| `dashscope-current.json` | Canonical successful single run of candidate commit `9dc8d14`. |
| `dashscope-1.0-run-02.json` | Second independent candidate run. |
| `dashscope-1.0-run-03.json` | Preserved failed Team run; this is intentionally not hidden. |
| `dashscope-1.0-run-04.json` | Fourth independent candidate run. |
| `dashscope-1.0-run-05.json` | Fifth independent candidate run. |
| `dashscope-1.0-stability.json` | Aggregate over all five candidate runs. |
| `phase5-vs-1.0.json` | Correctness-first comparison of Phase 5 and the canonical candidate run. |

## Results

Historical Phase 5 (`2107eab`):

```text
Tasks succeeded:       1 / 3
Assertions passed:     3 / 7
Task success rate:     33.3%
Assertion pass rate:   42.9%
```

Phase 5 could complete the ReAct marker task. Its public CLI did not expose
Plan or Team mode, so those two cases are explicit capability failures. The
historical evaluator uses `git archive`; current code does not impersonate a
missing old feature.

PaiCLI 1.0 candidate (`9dc8d14`), five independent runs:

```text
Fully successful runs: 4 / 5   (80.0%)
Tasks succeeded:       14 / 15  (93.3%)
Assertions passed:     34 / 35  (97.1%)
ReAct task:              5 / 5  (100%)
Plan task:               5 / 5  (100%)
Team task:               4 / 5  (80%)
Model errors:                 0
Model calls:                282
Tool calls:                 185
Tool errors:                 21
Input Tokens:            511331
Output Tokens:            35984
Estimated cost:      CNY 0.3563608
```

The comparison verdict is `improved`: Plan and Team changed from unsupported
failures in Phase 5 to successful cases in the canonical 1.0 run. Historical
Token/cost fields remain zero because the old CLI had no Trace collector; they
must not be interpreted as free or more efficient execution.

## Preserved bad case

`dashscope-1.0-run-03.json` records a real Team failure. Two independent write
Tasks chose inconsistent error-message contracts:

```text
implementation: ValueError("division by zero")
test:           expected "Cannot divide by zero"
```

Each local artifact looked plausible to its task-scoped Reviewer, but the final
deterministic verification failed. PaiCLI correctly returned `partial` and did
not claim the suite passed. This exposes a remaining reliability boundary:
cross-task acceptance contracts need stronger shared state or a repair path from
final Verification back to the responsible write Tasks.

The failure is retained because a five-run sample is intended to show model and
orchestration variance, not only a successful demonstration.

## Interpreting raw answers

The `answer` fields are model-authored output and may contain inaccurate side
claims, such as unnecessary discussion of Python 2.7. Release decisions use the
machine-owned fields instead:

```text
run_status
Task status
changed_files
ToolResult
assertion results
command exit code
Trace metrics
```

A non-zero `tool_errors` count does not by itself mean the task failed. It
includes rejected, corrected, or otherwise recoverable Tool Calls. The final
Task and assertion statuses are the correctness gates.

## Reproduce

Current candidate:

```bash
paicli-eval run \
  --suite eval/suites/coding-smoke.json \
  --provider dashscope \
  --output reports/dashscope-current.json
```

Historical Phase 5:

```bash
paicli-eval run \
  --suite eval/suites/coding-smoke.json \
  --provider dashscope \
  --revision 2107eab \
  --repository . \
  --output reports/phase5-dashscope-baseline.json
```

Compare and aggregate:

```bash
paicli-eval compare \
  reports/phase5-dashscope-baseline.json \
  reports/dashscope-current.json \
  --output reports/phase5-vs-1.0.json

paicli-eval stability \
  reports/dashscope-current.json \
  reports/dashscope-1.0-run-02.json \
  reports/dashscope-1.0-run-03.json \
  reports/dashscope-1.0-run-04.json \
  reports/dashscope-1.0-run-05.json \
  --output reports/dashscope-1.0-stability.json
```

Real-model output is nondeterministic. A rerun is valid evidence only when the
suite version, provider/model, budgets, initial files, and deterministic
assertions remain unchanged.
