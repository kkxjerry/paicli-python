# PaiCLI release evidence

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

## PaiCLI 1.1.0 evidence

The 1.1 product code evaluated by the final reports is:

```text
7022eaa31171867ec3203a1d2c4dc2ebf739058f
```

Final-code reports:

| File | Purpose |
|---|---|
| `dashscope-1.1-run-02.json` | First complete final-code run assembled from three task fragments. |
| `dashscope-1.1-run-03.json` | Second complete final-code run. |
| `dashscope-1.1-run-04.json` | Third complete final-code run. |
| `dashscope-1.1-stability.json` | Aggregate over the three final-code runs. |
| `1.0-vs-1.1.json` | Same-suite comparison of a successful 1.0 report and the second final 1.1 run. |
| `fragments/` | Per-task source reports used by `paicli-eval merge`; all carry the same final Git SHA. |

A pre-fix report is also retained:

```text
reports/dashscope-1.1-run-01.json
Git commit: eb6f0f31e6fc11749f814ae4f23c046b83a03b69
Result: 2/3 Tasks, 5/7 assertions
```

Its Team case repeated unchanged reads until the stagnation guard stopped the
worker. The report was not included in final-code stability because its Git SHA
differs. It directly motivated the one-round-early stagnation warning added
before `7022eaa`.

Final-code stability:

```text
Fully successful runs: 3 / 3  (100%)
Tasks succeeded:        9 / 9  (100%)
Assertions passed:     21 / 21 (100%)
Model errors:                 0
Model calls:                183
Tool calls:                 140
Tool errors:                 17
Input Tokens:            515584
Output Tokens:            23080
Estimated cost:      CNY 0.1855264
```

The release evaluation used a 45-second provider request timeout. Long suites
were run as independently persisted task fragments:

```bash
paicli-eval run \
  --suite eval/suites/coding-smoke.json \
  --provider dashscope \
  --task-id plan-add-subtract \
  --output reports/fragments/run-plan.json

paicli-eval merge \
  reports/fragments/run-react.json \
  reports/fragments/run-plan.json \
  reports/fragments/run-team.json \
  --suite eval/suites/coding-smoke.json \
  --output reports/dashscope-1.1-run.json
```

The merge operation rejects mixed suite versions, providers, models, Git
commits, duplicate cases, and incomplete task sets. Full interpretation and
release boundaries are in `docs/1.1.0-acceptance.md`.
