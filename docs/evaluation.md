# Fixed-task evaluation

PaiCLI separates two kinds of evidence:

1. deterministic unit/integration tests prove Harness state transitions;
2. opt-in real-model tasks prove a provider can cooperate with that Harness.

A successful demo is not a benchmark. The evaluation runner uses fixed initial
files, prompts, assertions, limits, and reports so a baseline and candidate can
be compared.

## Included smoke suite

`eval/suites/coding-smoke.json` contains:

- ReAct: read a marker that cannot be guessed without `read_file`;
- Plan: add `subtract()`, add a unittest, and run the complete suite;
- Team: repair division-by-zero behavior, add tests, and require Reviewer reads
  of actual changed artifacts.

Each case runs in its own disposable temporary project. The model cannot modify
the PaiCLI source checkout while the benchmark runs.

## Run

```bash
paicli-eval run \
  --suite eval/suites/coding-smoke.json \
  --provider dashscope \
  --output reports/dashscope-current.json
```

Or without installing the console script:

```bash
python -m paicli.evaluation run \
  --suite eval/suites/coding-smoke.json \
  --provider dashscope \
  --output reports/dashscope-current.json
```

The command exits zero only when every Task and deterministic assertion passes.
Real model variance means a single run is not a statistical estimate. Use
multiple reports for reliability analysis.

## Slice and resume a long suite

Long real-model suites should not depend on one process surviving every Case.
Run individual task IDs into durable report fragments:

```bash
paicli-eval run \
  --suite eval/suites/coding-smoke.json \
  --provider dashscope \
  --task-id react-read-marker \
  --output reports/fragments/run-react.json

paicli-eval run \
  --suite eval/suites/coding-smoke.json \
  --provider dashscope \
  --task-id plan-add-subtract \
  --output reports/fragments/run-plan.json
```

Then merge all canonical Tasks:

```bash
paicli-eval merge \
  reports/fragments/run-react.json \
  reports/fragments/run-plan.json \
  reports/fragments/run-team.json \
  --suite eval/suites/coding-smoke.json \
  --output reports/dashscope-current.json
```

The merger restores Suite order and recalculates aggregate metrics. It rejects
mixed Suite versions, providers, models, Git commits, duplicate task results,
unexpected tasks, and missing tasks. This makes evaluation resumable without
silently mixing code revisions or selecting only successful fragments.

## Run a historical Git revision

Use the same suite against an exported revision without creating another Git
worktree:

```bash
paicli-eval run \
  --suite eval/suites/coding-smoke.json \
  --provider dashscope \
  --revision 2107eab \
  --repository . \
  --output reports/phase5-dashscope-baseline.json
```

The evaluator uses `git archive`, starts that revision's own CLI in a subprocess,
and records its real capabilities. A revision that predates `--mode plan` or
`--mode team` receives explicit failed cases for those modes; current code is
never used to impersonate a missing historical feature. Historical revisions
may not expose Trace/Token metrics, so those report fields remain zero rather
than being guessed.

## Assertions

Supported deterministic assertions:

```text
answer_contains
file_exists
file_contains
file_not_contains
file_equals
command
```

`command` accepts an argv array and never invokes a shell. It records the actual
exit code and bounded output. This is the primary correctness gate for coding
tasks; LLM self-reports are not treated as test evidence.

Example:

```json
{
  "kind": "command",
  "command": ["python3", "-m", "unittest", "-q"],
  "exit_code": 0,
  "timeout_seconds": 30
}
```

## Report schema

A report contains:

```text
suite name/version
timestamp
Git commit
provider/model
per-case run ID/status/answer/error
changed files
per-assertion result
case duration
task success and assertion pass rate
model/tool call counts
model/tool errors
input/output/cache Tokens
configured estimated cost
unpriced model-call count
```

Cost is only calculated when verified pricing is configured for the exact
provider/model. Missing prices are visible as `unpriced_model_calls`.

## Compare

```bash
paicli-eval compare \
  reports/baseline.json \
  reports/candidate.json \
  --output reports/comparison.json
```

Comparison requires identical suite name and version. Verdict priority:

1. success-rate change;
2. assertion-pass-rate change;
3. cost or latency efficiency when correctness is unchanged.

Possible verdicts:

```text
improved
regressed
efficiency_improved
efficiency_regressed
unchanged_or_mixed
```

The comparison also identifies individual Tasks whose success changed.

## Aggregate repeated runs

After running the same suite several times, preserve every report and aggregate
them without hiding failed attempts:

```bash
paicli-eval stability \
  reports/dashscope-current.json \
  reports/dashscope-1.0-run-02.json \
  reports/dashscope-1.0-run-03.json \
  --output reports/dashscope-1.0-stability.json
```

The stability report contains full-run success rate, per-task success rate,
assertion pass rate, failed run IDs/errors, and distributions for duration,
Token usage, model calls, and tool calls. Reports must use the same suite
version, provider, model, and ordered task set.

## Interpreting results

A failure should be assigned to a layer before changing Prompts:

| Symptom | Likely layer |
|---|---|
| Planner JSON invalid after repair | Planner/model compatibility |
| DAG rejected | planning proposal, deterministic validator working |
| Hidden tool requested | role capability mismatch or model tool selection |
| Tool schema rejected | parameter generation or Tool definition |
| Tool result failed | repository/tool/runtime problem |
| Reviewer invalid JSON | Reviewer/model compatibility |
| Reviewer rejects correct output | review calibration |
| Agent says done, deterministic test fails | completion/review quality gap |
| Token/call budget exceeded | task decomposition, loops, context, or model |

Use `.paicli/traces.db` and the report `run_id` to inspect the exact model/tool
sequence. Do not judge a strategy solely by final-answer prose.

## Reviewer calibration

The included smoke suite proves the Reviewer reads artifacts and participates in
the state machine. It does not prove Reviewer accuracy against humans. A real
calibration set should contain human-labelled diffs with approve/change/reject
labels and compare precision, recall, false approvals, and false rejections.

False approval is the highest-cost Reviewer error because it allows faulty work
to reach downstream tasks. Deterministic tests and static diagnostics therefore
remain independent gates.

## Reproducibility rules

- Keep prompts, initial files, assertions, and suite version under Git.
- Record exact Git commit and model identifier.
- Use `--revision` for a historical baseline; do not synthesize old results from
  the current implementation.
- Do not silently edit an existing suite version; increment it.
- Keep provider retries and parent budgets fixed when comparing versions.
- Use disposable workspaces.
- Preserve failed reports and traces as bad-case regression fixtures after
  removing secrets and proprietary source.
- Do not run credentialed real-model evaluation on untrusted pull requests.
