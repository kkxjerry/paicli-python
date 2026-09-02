# PaiCLI Python

PaiCLI 是一个在本地运行的编程智能体框架，提供三种真正可用的执行模式：

```text
ReAct：模型 <-> 工具循环执行，直到完成条件验收通过
Plan： LLM 规划器 -> 经校验的 DAG -> 隔离的任务 Worker -> 最终答复
Team： 规划器 -> Worker -> 只读 Reviewer -> 局部修复 -> 最终答复
```

智能体运行时、文件修改、测试、记忆、快照、检查点、链路追踪和评测都在本机执行。只有模型推理请求会发送给配置的服务商。项目默认对接阿里云百炼（DashScope），使用其兼容 OpenAI 的 Chat Completions API。

## 已实现功能

- ReAct 与所有 SubAgent 共用一套 `AgentLoopEngine`。
- 支持 `react`、`plan`、`team` 三种真实 CLI 模式。
- 支持 DashScope、GLM、DeepSeek、StepFun、Kimi，以及本地或远程 vLLM。
- 支持 Function Calling、SSE 流式内容/推理/分片 Tool Call，并在执行时进行 JSON Schema 校验。
- 由 LLM 生成计划，支持有限次数的 JSON 修复和确定性的 DAG 校验。
- Worker、Reviewer、Aggregator 各自拥有隔离的上下文和工具权限。
- Reviewer 必须读取实际变更文件后才能批准，不能只依据 Worker 的描述。
- Reviewer 返回 `changes_requested` 时，只重试当前任务，并设有严格的重试上限。
- 提供 `replace_text`、`multi_edit`、`apply_patch`、`grep`、`glob`、分段读取和 SHA 乐观并发控制，避免只能整文件覆写。
- 文件写入和命令执行支持人工确认（HITL）、统一 diff 预览、基础层强制命令策略、可持久化精确/模式权限和脱敏审计记录。
- 支持工作区执行前/后快照、回滚策略、SQLite 检查点，以及中断后的 Plan/Team 任务恢复。
- 为模型调用、工具调用和执行阶段记录父子链路、Token 用量、延迟、失败信息和可配置的费用估算。
- 持久化混合代码检索：符号、SQLite FTS5/BM25、词法余弦相似度和可选向量嵌入，通过 RRF 融合，并返回源码行范围。
- 带版本的长期记忆，包含 ID、来源、验证状态、过期状态和软删除。
- `AGENTS.md` / `.paicli.md` 会进入所有角色 Prompt；可选真实 stdio LSP 诊断会回灌 Agent 并参与完成门禁。
- Planner、Worker、Reviewer、Aggregator 可选择不同模型；Skill、MCP、浏览器 MCP 和 Web 通过显式扩展配置接入，默认不启动。
- 提供固定编程评测集，以及基线与候选版本对比报告。

早期教学阶段的 `phase-xx` 标签仍然保留。当前 `develop` 才是集成后的产品主线，请勿根据旧标签推断当前行为。

## 环境要求

- Python 3.11 或 3.12
- Git
- 支持 OpenAI 兼容 `/chat/completions` 协议的模型服务
- 执行编程任务测试时，需要目标仓库自身的工具链

核心运行时不依赖第三方 Python 包。

## 安装

```bash
git clone <repository-url> paicli-python
cd paicli-python
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

无需连接模型即可验证安装：

```bash
python -m paicli --help
python -m unittest discover -s tests -q
```

常规测试使用确定性的 Fake Client 和本地 HTTP 服务。真实模型测试需要显式开启，从而保证 CI 快速、可复现且不会产生 API 费用。

## 配置 DashScope

复制环境变量模板：

```bash
cp .env.example .env
```

在 `.env` 中配置：

```dotenv
DASHSCOPE_API_KEY=your-real-key
DASHSCOPE_MODEL=qwen-plus
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
DASHSCOPE_CONTEXT_WINDOW=131072
```

`.env` 已被 Git 忽略。请勿粘贴、打印、追踪或提交真实密钥。

允许模型修改文件前，先检查对话和工具调用能力：

```bash
python -m paicli --provider dashscope --check-model chat
python -m paicli --provider dashscope --check-model tools
```

## 运行 ReAct 模式

只读示例：

```bash
python -m paicli \
  --provider dashscope \
  --mode react \
  --project-root ./demo \
  -p "读取 README.md，并总结公开 API。"
```

允许执行 Shell 命令，并对副作用进行交互式确认：

```bash
python -m paicli \
  --provider dashscope \
  --mode react \
  --project-root ./demo \
  --allow-shell \
  --approval-mode ask \
  -p "修复失败的单元测试，并运行完整测试套件。"
```

`--allow-shell` 会开放 `execute_command` 工具；设置 `--approval-mode ask` 后，每条命令和每次文件修改仍需人工确认。确认前会展示命令预览或统一 diff。基础 `ToolRegistry` 始终执行危险命令硬拒绝，即使库调用方关闭 HITL 也不能绕过。

精确代码操作不依赖 Shell：

```text
read_file      分段读取和可选 SHA-256
replace_text   精确替换并校验旧文本、次数和文件 Hash
multi_edit     全部修改先验证，任意失败则一个文件都不写
apply_patch    校验多文件 unified diff 后再应用
grep / glob    项目根内有界检索
```

交互审批可只允许本次，也可持久化精确参数或 glob 模式到 `.paicli/permissions.json`。硬安全策略优先级始终高于持久化放行规则。

项目根中的 `AGENTS.md`（优先）或 `.paicli.md` 会注入 ReAct、Planner、Worker、Reviewer 和 Aggregator。可选真实语言服务器：

```bash
python -m paicli \
  --python-lsp-command "pyright-langserver --stdio" \
  --lsp-timeout-seconds 8 \
  ...
```

语言服务器不可用时自动回退到 `ast.parse`；ERROR 诊断会进入 Agent History 并阻止模型直接宣布完成。

## 运行 Plan 模式

计划通过确定性校验后，以非交互方式执行：

```bash
python -m paicli \
  --provider dashscope \
  --mode plan \
  --project-root ./demo \
  --allow-shell \
  --approval-mode ask \
  -p "检查实现，新增 subtract()，补充测试并运行全部测试。"
```

交互模式支持计划审阅和有限次数的修改：

```bash
python -m paicli --provider dashscope --project-root ./demo --allow-shell

> /plan 检查实现，新增 subtract()，补充测试并运行全部测试。
```

规划器会生成完整的 JSON DAG。程序将校验任务 ID、依赖关系、环、拓扑顺序，并确保每个 `FILE_WRITE` 任务之后都存在下游 `VERIFICATION` 任务。不合法的计划只允许进行一次有限修复。交互式计划审阅支持执行、取消，或补充要求后重新生成完整计划。

## 运行 Team 模式

```bash
python -m paicli \
  --provider dashscope \
  --mode team \
  --project-root ./demo \
  --allow-shell \
  --approval-mode ask \
  -p "修复除零处理，补充测试，运行测试，并审查实际变更。"
```

Team 模式执行流程：

```text
规划器
  -> 确定性 DAG 校验
  -> 任务级 Worker
  -> 只读 Reviewer
       approved            -> 任务完成
       changes_requested   -> 同一任务和 Worker 进行局部重试
       rejected/error      -> 任务失败
  -> 跳过失败任务的下游任务，独立分支继续执行
  -> 不使用工具的最终 Aggregator
```

只读任务可以并发执行。`FILE_WRITE`、`COMMAND` 和 `VERIFICATION` 任务会在共享工作区中串行执行。在实现每个 Worker 独立 worktree 及合并语义之前，PaiCLI 不会宣称支持安全的并发写入。

不同角色可以显式选择 Provider：

```bash
python -m paicli \
  --provider dashscope \
  --planner-provider dashscope \
  --worker-provider deepseek \
  --reviewer-provider glm \
  --aggregator-provider dashscope \
  ...
```

可选 Skill、MCP、浏览器 MCP 与 Web 能力通过 `--extensions-file` 接入。默认运行不会因为模块存在就连接外部服务；Web 必须配置 host allow-list，MCP stdio 具备请求超时、有界 stderr 和通知/响应 ID 处理。示例见 `extensions.example.json`。

## 安全模型

模型不会直接执行代码。每次操作都必须通过以下链路：

```text
工具 Schema -> 运行时校验 -> 强制策略 -> 人工确认 -> 资源调度器
            -> 处理器 -> 类型化 ToolResult -> 链路追踪/检查点
```

重要默认规则：

- 文件访问限制在 `--project-root` 内，并包含符号链接解析后的路径检查。
- 未设置 `--allow-shell` 时，不会向模型暴露 Shell 工具。
- 生产 CLI 中，产生副作用的操作必须经过人工确认。
- `approval-mode=ask` 表示交互确认；`deny` 表示默认拒绝；`allow` 是显式自动放行，只应在可丢弃的工作区中使用。
- 明显危险的命令模式会在人工确认前直接拒绝。
- 失败或部分完成的任务默认询问是否回滚；`STOPPED`（迭代、停滞或单 Agent Token 上限）保留工作区并允许恢复。
- API 密钥和常见敏感字段会从链路追踪与审计日志中脱敏。

信任边界和剩余风险见 [SECURITY.md](SECURITY.md)。

## 快照、回滚与恢复

每次协调执行都会记录：

```text
.paicli/runs.db       持久化的运行、DAG、审阅和检查点状态
.paicli/traces.db     执行阶段、模型调用、工具调用、事件和指标
.paicli/snapshots/    压缩的工作区快照
.paicli/audit/        脱敏后的人工确认记录
```

无需连接模型即可查看最近的运行记录：

```bash
python -m paicli --project-root ./demo --list-runs
```

恢复中断或失败的运行：

```bash
python -m paicli \
  --provider dashscope \
  --project-root ./demo \
  --allow-shell \
  --resume run_<id>
```

修改型任务开始前，PaiCLI 会保存任务级快照。如果进程中断，重试前会恢复到状态不确定任务的边界。若该快照缺失，则恢复整个运行快照并重新执行 DAG，而不是盲目重放可能已经产生副作用的操作。超大、不可读或超过总预算的文件会记录为 skipped，并在恢复时保持原样，不再阻止 Agent 启动；未被可恢复 Run 引用的旧快照按 `--snapshot-retention` 清理。

详细语义见 [docs/recovery.md](docs/recovery.md)。

## 预算、链路追踪与费用

每个智能体的保护参数：

```text
--max-steps 50
--stagnation-window 3
--token-budget <单个智能体的 Token 上限>
```

规划器、Worker、Reviewer、重试任务和 Aggregator 共享的编排上限：

```text
--max-run-tokens 100000
--max-run-cost-cny 10
--max-run-seconds 900
--max-model-calls 50
--max-tool-calls 100
```

Token 用量来自模型服务商响应。PaiCLI 内置了一套带版本的北京地域 DashScope `qwen-plus` 基准价格，适用于输入不超过 128K Token 的请求：输入 0.8 元/百万 Token，非思考输出 2.0 元/百万 Token。PaiCLI 当前不计算上下文缓存费用。当模型、地域、输入档位、思考模式或服务商价格不同时，请配置覆盖值：

```dotenv
PAICLI_PRICE_DASHSCOPE_QWEN_PLUS_INPUT_CNY_PER_MILLION=
PAICLI_PRICE_DASHSCOPE_QWEN_PLUS_OUTPUT_CNY_PER_MILLION=
PAICLI_PRICE_DASHSCOPE_QWEN_PLUS_CACHED_CNY_PER_MILLION=0
```

如果模型没有精确匹配的配置价格或内置价格，其调用会计入 `unpriced_model_calls`，而不是被错误地记为零费用。

## 代码检索与记忆

生产 CLI 使用标准的 `paicli.rag.CodeIndex` 构建 `.paicli/code-index.db`，并向模型提供 `search_code`。检索结果包含文件、精确行范围、符号、命中渠道和源代码。文件修改成功后会触发增量索引刷新。可通过 `--no-rag` 禁用，或通过 `--rag-path` 指定路径。`paicli.hybrid_rag.HybridCodeIndex` 仍作为 1.x 兼容 API 保留，但并非产品装配路径。

标准的 SQLite 记忆 API 是 `ManagedMemoryStore`。长期记忆默认保存在 `~/.paicli/memory.db`，也可通过 `--memory-file` 指定。模型写入的记忆初始状态为 `unverified`，直到被明确提升为已验证。显式使用 `.jsonl` 路径时，会继续采用早期教学版本的只追加实现；`ManagedLongTermMemory` 作为 1.x 兼容适配器保留。SQLite 记忆支持去重、来源、置信度、验证、来源哈希、过期和被替代状态、冲突解决与软删除。可通过 `--no-memory` 禁用记忆。

## 固定任务评测

运行仓库中的真实模型冒烟评测：

```bash
paicli-eval run \
  --suite eval/suites/coding-smoke.json \
  --provider dashscope \
  --output reports/dashscope-current.json
```

无需创建额外 worktree，即可在真实历史提交上运行同一套评测：

```bash
paicli-eval run \
  --suite eval/suites/coding-smoke.json \
  --provider dashscope \
  --revision 2107eab \
  --repository . \
  --output reports/phase5-dashscope-baseline.json
```

对比基线与候选版本，并聚合候选版本的多次运行结果：

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

报告包含任务与断言成功率、Git 提交、模型、模型/工具错误、Token 用量、延迟、配置费用和变更文件。历史版本中不存在的执行模式会明确失败，不会使用当前代码进行模拟。

历史 1.0 的五次 DashScope 样本中，完整评测成功 4/5 次，并保留了跨任务契约不一致的 Team 失败。最终代码版 1.1 连续三轮完整评测全部成功：9/9 个任务、21/21 条确定性断言通过；修复前的重复读取失败也单独保留。长评测可使用 `--task-id` 分片，再通过 `paicli-eval merge` 合并，并拒绝混入不同 Git SHA。详见 [docs/evaluation.md](docs/evaluation.md) 和 [reports/README.md](reports/README.md)。

## 真实模型测试

对话与工具调用探测通过后，可运行：

```bash
PAICLI_RUN_DASHSCOPE_LIVE_TEST=1 \
python -m unittest tests.test_dashscope_live -v
```

使用可选的 A40 vLLM 隧道时：

```bash
PAICLI_RUN_VLLM_LIVE_TEST=1 \
python -m unittest tests.test_vllm_live -v
```

不要在不可信的 Pull Request 中开启真实模型测试，因为模型控制的代码操作会使用凭据，并可能产生 API 费用。

## 架构与实现记录

- [ARCHITECTURE.md](ARCHITECTURE.md)
- [SECURITY.md](SECURITY.md)
- [docs/recovery.md](docs/recovery.md)
- [docs/evaluation.md](docs/evaluation.md)
- [docs/phase-09-dashscope-live.md](docs/phase-09-dashscope-live.md)
- [docs/phase-10-15-implementation.md](docs/phase-10-15-implementation.md)
- [docs/final-acceptance.md](docs/final-acceptance.md) — 1.0 历史验收
- [docs/1.1.0-acceptance.md](docs/1.1.0-acceptance.md) — P0–P2 正式验收
- [reports/README.md](reports/README.md)
- [docs/java-parity.md](docs/java-parity.md)
- [PHASES.md](PHASES.md)

## 验收状态

最终发布由以下检查项控制：

```text
[x] develop 主线干净且唯一
[x] ReAct / Plan / Team 均可通过 CLI 执行
[x] DashScope 真实对话、工具调用、ReAct、Plan 和 Team 测试通过
[x] 规划器能够生成并修复经过校验的 DAG
[x] Worker 能够读取、修改代码并进行确定性验证
[x] Reviewer 会读取实际变更产物
[x] Reviewer 只重试当前任务
[x] 人工确认、diff 和命令/文件权限控制已启用
[x] 失败快照与回滚可用
[x] 中断后的任务状态可安全恢复
[x] 模型与工具调用具备父子链路追踪
[x] Token、配置费用、延迟和错误均会记录
[x] 固定任务报告能够对比基线和候选版本行为
[x] 多次真实模型报告保留成功与失败的波动信息
[x] 全新 Python 3.11/3.12 环境可以复现安装和 CLI 入口
```

1.0 的历史证据保留在 [docs/final-acceptance.md](docs/final-acceptance.md)。P0–P2 的实现、修复前 Bad Case、最终三轮真实模型结果和范围边界记录在 [docs/1.1.0-acceptance.md](docs/1.1.0-acceptance.md)。只有最终干净的 `develop` 提交通过 GitHub Actions Python 3.11/3.12 矩阵后，才会创建 `v1.1.0` 标签。
