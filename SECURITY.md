# Security model

PaiCLI can read and modify files and, when explicitly enabled, execute shell
commands. Treat it as a local developer tool with powerful credentials, not as
a sandbox for untrusted users or repositories.

## Trust boundaries

Trusted by default:

- the local user launching PaiCLI;
- the configured project root;
- PaiCLI's deterministic policy, validation, scheduler, state, and snapshot
  code;
- explicitly installed local tools and MCP servers.

Untrusted by default:

- model output, including Tool Calls and plan JSON;
- repository text, comments, tests, and documents that may contain prompt
  injection;
- external web pages and MCP server responses;
- Tool arguments until runtime validation succeeds;
- model claims that a file changed or a test passed.

## Enforced controls

### File boundary

Built-in file tools resolve paths and reject anything outside
`--project-root`, including symlink traversal. Evaluation fixtures and assertion
paths use the same resolved-path rule.

### Tool validation

Every Tool Call is parsed and validated against an execution-time JSON Schema
before policy, approval, or the handler runs. Unknown extension tools default
to unknown risk, unknown side effects, and serial execution.

### Capability scopes

SubAgents receive only authorized Tool Schemas. Execution performs the same
scope check, so a hallucinated hidden tool cannot bypass the schema boundary.
Reviewers are read-only and the final Aggregator has no tools.

### Human approval

The production CLI places HITL in front of side effects:

- `write_file`: unified diff preview;
- `create_project`: destination and type preview;
- `execute_command`: working directory and exact command preview;
- unknown-risk MCP tools: argument preview.

Modes:

```text
ask   interactive confirmation; default
 deny fail closed without prompting
allow explicit automation in a disposable workspace
```

Hard command-policy matches are denied before HITL. The blocklist is a last
line of defense, not a complete shell sandbox.

### Snapshots and rollback

A coordinated run captures a BEFORE snapshot. Mutating tasks capture a
pre-attempt snapshot. Failed and partial runs restore the run snapshot by
default. Resume restores the last uncertain task boundary before retry.

### Secrets

- `.env` is ignored by Git.
- Do not echo API keys.
- Trace and audit fields redact common API-key, token, password, secret, and
  Authorization patterns.
- Full Authorization headers are never intentionally stored.
- Real-provider tests are opt-in and must not run on untrusted pull requests.

## Hard policy and persistent permissions

The base `ToolRegistry` enforces high-confidence destructive-command denial
before starting a shell process. This invariant does not depend on HITL and
cannot be overridden by an approval rule.

The production application remains fail-closed when embedded as a library:
side-effecting tools use HITL with deny behavior unless the caller explicitly
chooses an approval mode/handler or opts out in an already isolated environment.
Interactive approvals can persist exact or glob-shaped rules in
`.paicli/permissions.json`. Last matching rule wins, but hard policy always has
higher precedence.

## Optional extension boundary

Skill, MCP, browser, and web integrations are disabled until an explicit
`--extensions-file` is supplied. MCP stdio calls have a request timeout and
bounded stderr diagnostics. Browser tools are ordinary scoped MCP tools.

Web access requires an explicit host allow-list. The fetcher validates URL
scheme and credentials, resolves every host, rejects non-public addresses,
revalidates each redirect and the final response URL, and bounds response size.
This reduces SSRF exposure but does not make arbitrary Internet content trusted;
repository/web prompt injection is still handled through tool scope and policy.

## Residual risks

### Shell is not a sandbox

`cwd=project-root` does not prevent absolute-path access, network access,
credential discovery, child processes, or destructive behavior. HITL and
blocklists reduce risk but do not create OS isolation. Run untrusted tasks in a
separate account, VM, remote disposable host, or another real sandbox.

### Cloud-model data disclosure

The model provider receives prompts, selected source code, Tool Results, plan
state, and review evidence. Do not use a cloud provider for repositories whose
policy forbids that disclosure. A local vLLM endpoint is supported for such
cases.

### Prompt injection

Repository content can tell a model to ignore instructions or request unsafe
actions. Deterministic capability scopes, policy, validation, approval, and
completion gates must remain outside the model. Never grant a Reviewer write
access merely through Prompt instructions.

### Snapshot limitations

Snapshots restore files under the project root. They cannot undo external
network calls, package publication, database mutations, credential rotation,
or arbitrary shell side effects outside that tree.

### Concurrent mutation

PaiCLI intentionally serializes mutation-capable DAG tasks in a shared
workspace. It does not claim safe parallel code modification. A future design
would require per-worker Git worktrees, merge semantics, and post-merge tests.

### SQLite scope

The state and trace stores are safe for local threads and use WAL mode. They do
not implement distributed leases, multi-host ownership, or high availability.

## Recommended operating profile

For ordinary development:

```bash
python -m paicli \
  --provider dashscope \
  --project-root ./repo \
  --allow-shell \
  --approval-mode ask \
  --rollback-on-failure always
```

For CI/evaluation, use a disposable temporary workspace and explicit
`--approval-mode allow`. Never point autonomous evaluation at a valuable
working tree.

## Reporting a security issue

Do not include real keys, credentials, private source, or unredacted trace
databases in an issue. Report the minimal reproduction, affected commit,
platform, and expected policy boundary through the repository's private
security-reporting channel.
