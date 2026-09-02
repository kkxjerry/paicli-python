# PaiCLI Python 1.0 final acceptance

Date: 2026-08-31
Release line: `develop` / `v1.0.0`
Product code evaluated: `9dc8d1438941f2865655bde99d3c500f80fbb558`
Historical baseline: `2107eab87c6e4a7b498c0aab88b9b333c9ab7ef5`

## Decision

PaiCLI Python is accepted as a **local, single-process, real-LLM coding-agent
harness** with ReAct, Plan, and Team execution modes. The release is suitable
for learning, interview demonstration, local repository work, and further
engineering development within the trust boundary documented in
`SECURITY.md`.

Acceptance does not claim an OS-level shell sandbox, distributed scheduling,
exactly-once arbitrary remote side effects, safe parallel mutation without
isolated worktrees, or human-calibrated Reviewer accuracy.

## Original acceptance checklist

| Goal | Result | Evidence |
|---|---|---|
| `develop` has one trusted mainline | Pass | One checkout/worktree after cleanup; production CLI and evaluation both use `build_application_runtime()` and `RunCoordinator`. |
| ReAct / Plan / Team run from CLI | Pass | CLI parser, deterministic tests, DashScope Live tests, and fixed real-model suite. |
| DashScope performs Tool Calling | Pass | Opt-in chat/function probes and real `read_file` feedback test. |
| Planner generates and repairs DAGs | Pass | Bounded JSON repair plus code-owned ID, dependency, self-dependency, cycle, topology, and verification checks. |
| Worker reads, modifies, and verifies code | Pass | Real Plan Live test and five fixed-suite Plan cases; deterministic command assertions pass 5/5. |
| Reviewer reads actual artifacts | Pass | Live test corrupts the first write and asserts a Reviewer `read_file` observation before approval. |
| Reviewer retries only the current Task | Pass | Unit/integration state tests and corrupt-first-write DashScope Live test. |
| Writes/commands have HITL, diff, and permission gates | Pass | Production CLI enables HITL; runtime Schema validation, scoped tools, hard command policy, diff/command preview, redacted audit. |
| Failures have Snapshot and rollback | Pass | BEFORE/AFTER and task snapshots; rollback policies tested for success, non-zero result, and exception. |
| Interrupted state can resume | Pass within local scope | SQLite run/checkpoints plus task-boundary restore; no distributed lease or arbitrary remote exactly-once claim. |
| Model/tool calls have complete Trace | Pass | SQLite runs, nested spans, model attempts, Tool Results, task/role metadata, redaction, errors, and durations. |
| Token, cost, latency, and error rates are measurable | Pass | Provider Usage, versioned/overridable pricing, parent budgets, report aggregates; unpriced models are explicit. |
| Fixed tasks prove change between versions | Pass | Real `git archive` Phase 5 baseline, five 1.0 runs, stability report, and correctness-first comparison. |
| README supports independent reproduction | Pass locally | Python 3.11/3.12 fresh editable installs, both console scripts, environment template, architecture/security/recovery/evaluation docs. Remote CI is required before tagging. |

## Deterministic release gates

Executed on macOS arm64 without local Docker:

```text
Python 3.11: 209 tests passed, 5 opt-in external tests skipped
Python 3.12: 209 tests passed, 5 opt-in external tests skipped
Python 3.11 compileall: passed
Python 3.12 compileall: passed
git diff --check: passed
python -W error -m paicli.evaluation --help: passed
paicli --help: passed
paicli-eval --help: passed
```

The five ordinary-suite skips are four DashScope tests and one vLLM test. The
four DashScope tests were then explicitly enabled and all passed. The vLLM test
was not rerun for this release because an A40 tunnel was not part of the final
release environment.

## Real DashScope gates

Model alias: `qwen-plus`
Provider path: DashScope OpenAI-compatible API
Credential handling: `.env` ignored by Git; API key not printed or committed

```text
DashScope Live tests: 4 / 4 passed
```

The live gates cover:

1. real chat and Function Calling probes;
2. ReAct `read_file` plus `tool_call_id` feedback;
3. Planner, Worker file changes, tests, and final Plan result;
4. deliberately corrupted first write, independent Reviewer file read,
   `changes_requested`, same-Task retry, correction, and approval.

These tests prove provider/Harness compatibility, not statistical reliability.

## Fixed-suite evidence

Suite: `eval/suites/coding-smoke.json`
Provider/model: DashScope / `qwen-plus`

Historical Phase 5:

```text
Tasks:       1 / 3 passed (33.3%)
Assertions:  3 / 7 passed (42.9%)
```

The historical commit exposed ReAct but not Plan or Team through its own CLI.
Those modes are recorded as unsupported failures rather than simulated by the
current implementation.

PaiCLI 1.0, five independent runs:

```text
Fully successful runs:  4 / 5  (80.0%)
Task attempts passed:  14 / 15 (93.3%)
Assertions passed:     34 / 35 (97.1%)
ReAct:                   5 / 5 (100%)
Plan:                    5 / 5 (100%)
Team:                    4 / 5 (80%)
Model errors:                  0
Model calls:                 282
Tool calls:                  185
Tool errors:                  21
Input Tokens:             511331
Output Tokens:             35984
Estimated cost:       CNY 0.3563608
```

Comparison verdict: `improved`. Plan and Team changed from unsupported failures
at Phase 5 to successful cases in the canonical 1.0 run.

Evidence files are under `reports/`. The report set retains every run used by
the stability aggregate; it is not a best-run-only selection.

## Known real-model bad case

One of five Team runs returned `partial`. Separate write Tasks chose inconsistent
contracts:

```text
implementation raised: "division by zero"
test expected:          "Cannot divide by zero"
```

The final deterministic command assertion failed, so PaiCLI did not claim
success. This demonstrates both a strength and a remaining boundary:

- strength: deterministic verification caught a cross-Agent inconsistency;
- boundary: a failed final Verification does not yet automatically route a
  shared contract correction back to the responsible upstream write Tasks.

This issue is accepted for 1.0 because it is observable, fails closed, appears
in the committed stability report, and does not corrupt the release checkout.
It is the highest-value reliability target for a future 1.1 release.

## Pricing evidence

The bundled `qwen-plus` price entry is a versioned baseline for the Beijing
endpoint with requests up to 128K input Tokens: CNY 0.8/M input and CNY 2.0/M
non-thinking output, with no cache discount assumed by PaiCLI. Environment
variables override the entry when region, tier, thinking mode, model alias, or
provider pricing differs. Unknown model prices remain `unpriced_model_calls`.

Historical reports with no Trace collector contain zero Token/cost metrics; the
zeros mean “not observed,” not “free.” Correctness is evaluated before cost.

## Fresh installation gates

New `uv` virtual environments were created for Python 3.11 and Python 3.12.
Each environment successfully performed:

```text
editable install of paicli-python==1.0.0
paicli --help
paicli-eval --help
python -W error -m paicli.evaluation --help
package metadata == paicli.__version__ == 1.0.0
```

No third-party runtime dependency is required by the core package.

## Release record

The `v1.0.0` product-code tag points at the evaluated commit `9dc8d14`. This
acceptance record and the full report set were committed immediately afterward
rather than rewriting the historical code tag. GitHub Actions for Python 3.11
and 3.12 passed on the evaluated code commit.

## Explicit residual boundaries

The 1.0 release intentionally does not claim:

- process, network, filesystem-namespace, or syscall isolation for Shell;
- parallel code mutation without per-Worker Git worktrees and merge tests;
- multi-host leases, failover, or high availability;
- exactly-once semantics for arbitrary remote APIs;
- secrecy of repository content sent to a configured cloud model;
- Reviewer accuracy against a human-labelled calibration dataset;
- automatic knowledge of future provider prices;
- full MCP Streamable HTTP/SSE lifecycle support.

Within those boundaries, the release checklist is complete.
