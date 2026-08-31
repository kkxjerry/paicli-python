# Snapshot, rollback, checkpoint, and resume semantics

## Why state persistence alone is insufficient

A Coding Agent can crash after a side effect succeeds but before its Task state
is committed. Replaying the same Tool Call may duplicate or corrupt work. PaiCLI
therefore persists both orchestration state and a workspace boundary.

## Run lifecycle

```text
RunCoordinator.execute
  -> create BEFORE tree snapshot
  -> create trace run and durable run row
  -> start global RunBudget
  -> run ReAct / Plan / Team
  -> append plan/task/review checkpoints
  -> success: keep workspace
  -> failed/partial: apply rollback policy
  -> create AFTER snapshot
  -> finalize run and trace rows
```

Production state is stored under the selected project root:

```text
.paicli/runs.db
.paicli/traces.db
.paicli/snapshots/
```

These paths are excluded from the workspace snapshot itself.

## Checkpoint boundaries

Plan and Team execution persist checkpoints at:

```text
plan_ready
before_task:<task>:attempt_<n>
review:<task>:<review_attempt>
after_task:<task>:<status>
```

Before a `FILE_WRITE`, `COMMAND`, or `VERIFICATION` Task starts, the observer
captures an additional task-level snapshot and stores its ID in the same atomic
checkpoint transaction.

A checkpoint records:

- complete validated `ExecutionPlan`;
- every Task status, result, error, attempts, and timestamps;
- Worker `AgentOutcome` and typed `ToolResult` records;
- Reviewer verdicts and model outcomes;
- current Task and its pre-attempt snapshot ID.

## Detecting interruption

When a new `RunCoordinator` opens the local state database, any row still marked
`running` is changed to `interrupted`. This is a local single-owner rule. It is
not a distributed lease protocol.

List runs:

```bash
python -m paicli --project-root ./repo --list-runs
```

Resume:

```bash
python -m paicli \
  --provider dashscope \
  --project-root ./repo \
  --allow-shell \
  --resume run_<id>
```

## Resume decision table

| Previous state | Workspace action | DAG action |
|---|---|---|
| Interrupted mutating Task with pre-task snapshot | Restore task snapshot | Reset only RUNNING task, retain verified completed tasks |
| Interrupted mutating Task without safe task snapshot | Restore whole BEFORE snapshot | Reset all tasks and restart full DAG |
| Failed/partial run already rolled back | Restore whole BEFORE snapshot | Reset all tasks and restart full DAG |
| Failed/partial run retained workspace | Keep workspace | Reset failed/skipped branch; retain completed tasks |
| ReAct interrupted | Restore whole BEFORE snapshot | Restart prompt with a fresh ReAct history |
| Succeeded/cancelled run | Not resumable | Reject resume request |

This policy prefers duplicated model inference over duplicated side effects.

## Rollback policy

```text
always  restore BEFORE on failed/partial run; production default
ask     call an interactive decision handler
never   retain the workspace; useful only for inspection/evaluation
```

CLI:

```bash
--rollback-on-failure always|ask|never
```

Snapshots may be disabled explicitly with `--no-snapshot`, but a run interrupted
inside an uncertain side-effect Task may then be impossible to resume safely.
PaiCLI reports that condition instead of blindly replaying the Task.

## Idempotency boundary

Built-in file writes are recoverable through snapshots. The state store records
Tool Call IDs and results, but arbitrary shell, MCP, web, or external service
operations are not automatically idempotent. A future external tool should
provide its own idempotency key or status-query operation.

Examples that a file snapshot cannot undo:

- publishing a package;
- sending a message;
- modifying a remote database;
- triggering a deployment;
- changing credentials;
- writing outside the project root through an approved shell command.

These actions must remain HITL-gated and should expose explicit idempotency or
compensation semantics.

## Inspecting state

The SQLite database can be inspected read-only:

```bash
sqlite3 .paicli/runs.db \
  'select run_id, mode, status, current_task_id, updated_at from runs;'

sqlite3 .paicli/runs.db \
  'select run_id, sequence, phase, created_at from checkpoints order by id;'
```

Do not edit rows manually. Task/result JSON and snapshots are versioned as
internal implementation details; use the CLI resume path so validation and
workspace restoration occur together.
