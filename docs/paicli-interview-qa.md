# PaiCLI 项目化面试问题与回答库

> 生成日期：2026-08-31；按 PaiCLI 1.1.0 于 2026-09-01 更新
> 题库来源：用户上传的《AI Agent / 大模型应用 / MCP 岗位面试问题全量题库》
> 项目依据：当前本地 `paicli-python` 的 `develop` 工作树、源码、测试、真实 DashScope 报告与已知边界
> 用途：面试理解、项目复盘、追问演练；不是需要逐字背诵的话术

## 1. 这份文档是怎么生成的

原题库共有 1165 道问题，其中 655 道被标为“高度相关”，248 道同时属于高度相关且较难/高难。题库有意保留不同公司的重复问法，因此本文件没有机械生成 655 份近义答案，而是把同一能力的不同问法合并成 **110 个 PaiCLI 项目主问题**。

转换规则：

1. 能直接落到 PaiCLI 代码的，改写成项目问题并给出当前实现。
2. 代码只完成一部分的，明确写“部分实现”及缺口。
3. 当前没有实现的，不用通用方案冒充项目能力。
4. 答案优先解释“为什么这样设计、具体怎么运行、哪里会失败”，再给代码落点。
5. 原题编号指上传题库表格中的“序号”，可回到原文件查看公司、岗位和轮次。

## 2. 回答时必须守住的事实边界

- 项目起点来自对 Java PaiCLI / 开源 Coding Agent 思路的学习，不说成从零原创。
- 当前定位是本地、单进程、真实 LLM 驱动的 Coding Agent Harness，不说成公司线上分布式平台。
- 已真实跑通 ReAct、Plan、Team 和 DashScope Tool Calling。
- 五轮固定任务中：整套成功 4/5，任务成功 14/15，确定性断言 34/35；Team 仍有真实失败样本。
- 默认各角色复用主模型，但 1.1 已支持可选角色级 Provider；仍没有 OS 级 Shell 沙箱、多人/多 Worker 安全并行写代码、Reviewer 人工校准集、Code RAG 检索黄金集和完整 MCP Streamable HTTP/SSE 生命周期。
- 普通 ReAct 的代码任务完成门禁弱于 Plan/Team，不能把后两者的能力套到 ReAct 上。

## 3. 30 秒项目介绍

> PaiCLI 是我参考 Java 版开源项目后，用 Python 做的一次结构化重写。它不是只封装模型 API，而是一个本地 Coding Agent Harness：支持 ReAct、Plan 和 Team 三种模式，复杂任务由 Planner 生成 DAG，Worker 真实读写代码，Reviewer 必须读取修改产物，失败可以局部返工。模型负责提出计划和工具调用，代码负责 Schema、权限、环检测、验证、Snapshot、Checkpoint、Trace 和评测。项目已经用 DashScope 真实模型跑过固定任务，但我也保留了 Team 的真实失败样本，所以我把它定位为完整的本地 Harness，而不是高可用企业平台。

## 4. 状态标记

- **已实现**：当前源码和测试中存在，且已接入实际主链。
- **部分实现**：已有主要机制，但存在明确边界或未接入完整闭环。
- **代码里没有**：当前项目未实现，答案只能说明理解和取舍。
- **真实数据/真实边界**：来自已保存的 DashScope 运行与稳定性报告。

---

# 一、项目定位、价值与总体架构

**题库映射示例**：原题 #3、#39、#75、#132、#153、#286、#374、#387、#428、#478、#483、#565、#674 等。

## Q001. 请介绍一下 PaiCLI，它主要解决什么问题？

**实现状态：已实现**

**建议回答：**

PaiCLI 是一个本地 Coding Agent Harness。它不是只把问题发给大模型，而是把模型放进一套可控执行系统里：模型可以读取代码、修改文件、运行命令；框架负责参数校验、权限审批、DAG 调度、审查、回滚、恢复、Trace 和评测。

我做它的直接原因是，真实 Coding Agent 的难点不在“能不能调用模型”，而在模型具有随机性、工具有副作用、长任务会失控。PaiCLI 的目标就是把这些不确定行为包进确定性的控制边界。

**代码落点：** `ARCHITECTURE.md`；`paicli/bootstrap.py`；`paicli/execution.py`

---

## Q002. 这是通用 Agent，还是垂直 Coding Agent？核心竞争力是什么？

**实现状态：已实现**

**建议回答：**

它面向代码仓库，是垂直 Coding Agent；但底层 Harness 抽象是通用的，比如统一 Agent Loop、ToolRuntime、Planner、Reviewer、Checkpoint 和 Trace 都可以复用。

它的核心不是“比通用模型更聪明”，而是三点：第一，模型提议、代码裁决；第二，真实副作用有权限、Diff、快照和恢复；第三，能用固定任务集和真实模型报告证明版本行为，而不是只展示一次成功 Demo。

**代码落点：** `ARCHITECTURE.md`；`README.md`；`reports/README.md`

---

## Q003. 这个项目是自研的，还是基于开源项目？你具体做了什么？

**实现状态：已实现**

**建议回答：**

最初参考了 Java 版 PaiCLI 的思路，我没有把它说成从零原创。真正的工作是把它重构成 Python 版，并且不是逐文件翻译，而是重新收敛行为：抽出统一 AgentLoopEngine，统一 Plan/Team 的 DAG，补运行时 Schema 校验、真实 Sub-Agent、Reviewer 证据门禁、HITL、Snapshot、SQLite 恢复、Trace、固定评测和持久化 Code RAG。

所以我会把贡献说成“基于开源思路做了结构化重写和工程补全”，而不是“发明了一套全新 Agent 理论”。

**代码落点：** `docs/java-parity.md`；`docs/phase-03-05-implementation.md`；`docs/phase-06-08-implementation.md`

---

## Q004. 为什么没有直接使用 LangGraph，而是自己实现 Harness？

**实现状态：已实现**

**建议回答：**

这个项目的目的之一就是把 Harness 的控制面拆开看清楚，所以我希望直接掌握消息协议、工具边界、DAG 状态、Reviewer 重试、Checkpoint 和恢复，而不是把状态流全部交给框架。

这不代表自研一定优于 LangGraph。若目标是快速做标准有状态工作流、分布式执行或接现成生态，我会优先评估 LangGraph；PaiCLI 的选择更适合学习底层机制、做行为可控的本地 Coding Agent，以及验证 Java/Python 两版差异。

**代码落点：** `ARCHITECTURE.md`；`docs/java-parity.md`

**追问边界：** 不要回答“LangGraph 不好”。重点是目标不同和控制粒度不同。

---

## Q005. 请从用户输入开始，讲完整执行链路。

**实现状态：已实现**

**建议回答：**

请求先进入 CLI，然后由 RunCoordinator 创建 run、BEFORE Snapshot、父级预算和根 Trace。接着根据模式进入 ReAct、Plan 或 Team。

ReAct 直接进入共享 AgentLoopEngine；Plan 先让 LlmPlanner 生成并校验 DAG，再按依赖调度 Worker；Team 在 Worker 后增加只读 Reviewer，必要时只重做当前 Task。所有工具都要经过 Schema 校验、权限范围、HITL 和资源调度。结束时持久化状态、生成 AFTER Snapshot，失败或 partial 时按策略回滚。

**代码落点：** `paicli/__main__.py`；`paicli/execution.py`；`paicli/orchestration.py`；`paicli/agents/loop.py`

---

## Q006. PaiCLI 的 Harness 是怎么分层的？

**实现状态：已实现**

**建议回答：**

我把它分成五层：入口层是 CLI 和评测器；编排层是 RunCoordinator、Plan 和 Team；执行内核是统一 AgentLoopEngine；能力层是 LlmClient 与 ToolRuntime；横切基础设施是 Context、Memory、RAG、HITL、Snapshot、State、Trace 和 Budget。

关键点是这些层不互相越权。比如 Planner 可以提出依赖，但不能直接把 Task 标成完成；模型可以生成 write_file 参数，但真正是否允许写由模型外代码决定。

**代码落点：** `ARCHITECTURE.md`；`paicli/bootstrap.py`

---

## Q007. 模型负责什么，确定性代码负责什么？

**实现状态：已实现**

**建议回答：**

模型负责提出候选决策：计划怎么拆、下一步调用哪个工具、Worker 怎么执行、Reviewer 给什么意见、最后怎么表达。

确定性代码负责不变量：JSON 和 Schema 校验、路径围栏、危险命令和人工审批、DAG 环检测与拓扑推进、写任务必须有验证、Reviewer 重试上限、Snapshot、Checkpoint、预算、Trace 和评测断言。我的原则是“模型可以建议状态迁移，但不能自己修改编排状态”。

**代码落点：** `ARCHITECTURE.md`；`paicli/tool_validation.py`；`paicli/planning.py`

---

## Q008. ReAct、Plan、Team 三种模式怎么选？

**实现状态：已实现**

**建议回答：**

简单、局部、路径不确定但步骤少的任务用 ReAct，例如读取一个文件并回答。步骤依赖清楚、需要多文件修改和最终验证时用 Plan。除了执行还需要独立审查、局部返工时用 Team。

当前是用户通过 CLI 选择模式，不是再花一次模型调用做自动路由。这样行为更可预测，也方便公平评测。Planner 内部对特别简单的目标还能退化成单节点计划。

**代码落点：** `paicli/__main__.py`；`paicli/planning.py`；`paicli/orchestration.py`

---

## Q009. 这个项目达到生产级了吗？

**实现状态：部分实现**

**建议回答：**

在“本地、单进程、受信任开发环境中的 Coding Agent Harness”这个范围内，它有比较完整的生产化要素：权限、安全、恢复、Trace、预算和评测。

但我不会称它为企业级通用平台，因为它没有 OS 级 Shell 沙箱、多机租约与高可用、任意远程副作用 exactly-once、多 Worker 独立 worktree 合并，也没有用人工标注集校准 Reviewer。生产级不是一个布尔值，要先说明信任边界和部署范围。

**代码落点：** `SECURITY.md`；`docs/final-acceptance.md`

---

## Q010. 项目最大的技术难点和实际产出是什么？

**实现状态：已实现**

**建议回答：**

最大的难点不是写一个 Agent 类，而是把模型随机性和工具副作用收敛成可验证状态机。实际过程中最费力的是统一多个阶段留下的接口、让真实模型按协议调用工具，以及处理 Reviewer、恢复和评测之间的状态一致性。

可量化产出是：ReAct、Plan、Team 真实运行；Python 3.11/3.12 的确定性测试全通过；DashScope 真实 Tool Calling 跑通；固定任务五轮中任务成功 14/15、断言通过 34/35，并保留了失败样本而不是只挑成功结果。

**代码落点：** `reports/dashscope-1.0-stability.json`；`docs/final-acceptance.md`

---

# 二、ReAct、Agent Loop 与终止机制

**题库映射示例**：原题 #47、#63、#173、#183、#189、#310—#314、#365、#535、#826—#833、#990、#1046、#1082 等。

## Q011. PaiCLI 的 ReAct 循环具体怎么运行？

**实现状态：已实现**

**建议回答：**

AgentLoopEngine 先检查取消和预算，再组装 Context，调用模型。如果模型返回 Tool Call，就把 assistant 的 tool_calls 写入历史，执行工具，再用相同 call_id 写入 Tool Message，继续下一轮。

如果模型不再调用工具，不是立即结束，而是先经过 CompletionPolicy。通过才返回 AgentOutcome；不通过就把框架反馈作为 System Message 注入，再继续循环。

**代码落点：** `paicli/agents/loop.py`

---

## Q012. Function Call 得到 Tool Result 后，结果怎么重新进入模型上下文？

**实现状态：已实现**

**建议回答：**

模型响应中的每个 Tool Call 都有 id、name 和 arguments。框架执行后追加一条 role=tool 的消息，其中 tool_call_id 必须与原调用一致，同时写入 name 和结果内容。

下一轮模型拿到完整消息链，因此能观察工具真实返回。这个配对不能靠自然语言模拟，否则模型和 Provider 无法可靠知道结果属于哪个调用。

**代码落点：** `paicli/agents/loop.py`；`paicli/llm_client.py`

---

## Q013. Agent Loop 在什么条件下结束？

**实现状态：已实现**

**建议回答：**

有四类结束原因：CompletionPolicy 接受最终回答；用户取消；Token 预算或最大轮数耗尽；检测到连续无进展。

成功结束和被迫停止会进入不同的 RunStatus 与 FinishReason，调用方不会只拿到一个模糊字符串。Plan/Team 还会继续考虑 Task、Reviewer 和 DAG 的终态。

**代码落点：** `paicli/agents/models.py`；`paicli/agents/budget.py`；`paicli/agents/loop.py`

---

## Q014. 如何检测重复工具调用和无进展死循环？

**实现状态：已实现**

**建议回答：**

每一轮会对工具名、规范化后的 JSON 参数以及 Tool Result 的哈希组成签名。连续三轮签名完全相同，才认为没有可观察进展。

我没有只比较工具名，因为重复 read_file 但文件内容已变化不应被误判；也没有把 call_id 放进签名，因为模型每次会生成新 id。被 CompletionPolicy 连续拒绝的同一空回答也进入停滞检测。

**代码落点：** `paicli/agents/budget.py`

---

## Q015. 模型不调用工具并声称完成，系统会直接相信吗？

**实现状态：部分实现**

**建议回答：**

不能一概而论。Plan 和 Team 的 Task 有任务类型对应的完成门禁，例如 FILE_WRITE 必须有成功写入证据，VERIFICATION 必须有真实读取或命令结果；Team 还要过 Reviewer。

普通 ReAct 当前默认只使用非空回答门禁，因此对开放问答足够，但对代码修改仍偏弱。更稳的后续做法是根据请求类型要求 changed_files 和成功测试证据。

**代码落点：** `paicli/agents/models.py`；`paicli/subagents.py`；`paicli/review.py`

**追问边界：** 面试中必须主动说出 ReAct 的这项边界，不能把 Plan/Team 的门禁套到 ReAct 上。

---

## Q016. 为什么默认最大 50 轮、停滞窗口是 3？

**实现状态：已实现**

**建议回答：**

这两个值首先是安全阀，不是通过大规模线上实验得到的最优参数。50 轮用于阻止模型长期不结束，连续 3 次相同无进展既能过滤一次偶发重复，又能较快止损。

参数都可以通过 CLI 调整。真正更成熟的做法是按任务类型、模型和历史 Trace 分层配置，而不是全局固定一个“神奇数字”。

**代码落点：** `paicli/agents/budget.py`；`paicli/__main__.py`

---

## Q017. ReAct 与单轮聊天式调用有什么区别？

**实现状态：已实现**

**建议回答：**

单轮聊天只做一次“输入到输出”。ReAct 把模型输出视为下一步控制决策：它可以调用工具，观察结果，再决定继续、修正还是结束。

所以 ReAct 的核心价值是把外部环境反馈接进推理过程；代价是调用次数、Token、错误传播和死循环风险都会增加，需要 Harness 管理。

**代码落点：** `paicli/agents/loop.py`

---

## Q018. ReAct 与 Plan-and-Execute 分别适合什么任务？

**实现状态：已实现**

**建议回答：**

ReAct 适合短任务和路径不确定的探索，例如先搜代码再决定读哪个文件。Plan-and-Execute 适合目标明确、存在依赖、需要可见进度和失败传播的多步任务。

Plan 不是替代 ReAct：PaiCLI 的每个 Worker 内部仍复用 ReAct 式 Agent Loop。外层 DAG 管宏观步骤，内层 Loop 管单 Task 的工具交互。

**代码落点：** `paicli/orchestration.py`；`paicli/agents/loop.py`

---

## Q019. 是否所有任务都必须经过 Planner？

**实现状态：已实现**

**建议回答：**

不是。CLI 允许用户显式选择 react、plan、team。简单读取、解释或小范围探索可以直接 ReAct，避免为一个单步任务额外支付规划成本。

即使进入 Plan，LlmPlanner 也有简单目标检测，可能直接生成单节点计划而不调用模型规划。这样把模式选择和规划成本都做成显式控制。

**代码落点：** `paicli/__main__.py`；`paicli/planning.py`

---

## Q020. 为什么 ReAct、Worker、Reviewer、Aggregator 都共用一套 Agent Loop？

**实现状态：已实现**

**建议回答：**

因为取消、消息配对、预算、工具执行、上下文压缩和完成门禁是共同机制。如果每个角色各写一套循环，很容易出现某个角色忘记检查取消、某个角色不记录 Token、另一个角色破坏 Tool Message 协议。

PaiCLI 让角色只改变 Prompt、可见工具、TaskPacket 和 CompletionPolicy，执行内核只有 AgentLoopEngine 一套，这也是 Python 重构相对原版最重要的收敛。

**代码落点：** `paicli/agents/loop.py`；`paicli/subagents.py`；`ARCHITECTURE.md`

---

# 三、Planner、DAG 与失败传播

**题库映射示例**：原题 #294—#297、#309、#311、#312、#806、#858—#862、#1068 等。

## Q021. Planner 的输入和输出是什么？

**实现状态：已实现**

**建议回答：**

输入是用户目标；重规划时还会带失败 Task、失败原因和可复用的已完成结果。输出是 ExecutionPlan，包含 plan_id、summary，以及多个 Task。

每个 Task 有 id、description、type、dependencies、acceptance_criteria 和运行状态。Planner 只负责提出候选计划，执行前还必须经过 PlanValidator。

**代码落点：** `paicli/planning.py`

---

## Q022. DAG 是谁生成的？模型生成的 DAG 靠谱吗？

**实现状态：已实现**

**建议回答：**

复杂任务由 LlmPlanner 调真实模型生成 JSON DAG；简单任务可以由规则生成单节点计划。模型生成的结果不能直接执行，因为它可能有重复 ID、未知依赖、自依赖、环或者漏掉验证。

我的做法是把模型当候选计划生成器，把结构合法性和执行顺序交给代码。这样不是要求模型“永远靠谱”，而是要求错误可检测、可修复、不可越过门禁。

**代码落点：** `paicli/planning.py`

---

## Q023. Planner 输出非法 JSON 或非法 DAG 时怎么办？

**实现状态：已实现**

**建议回答：**

框架先提取 JSON 对象并解析，再运行 PlanValidator。如果失败，会把具体错误，例如未知依赖或环路径，连同原始回答发回模型，要求返回一份完整修正版，而不是局部补丁。

默认只有一次修复机会，也就是最多两次模型输出。仍然非法就抛 PlanGenerationError，不进入执行阶段。

**代码落点：** `paicli/planning.py`

---

## Q024. 执行前对 DAG 做哪些确定性校验？

**实现状态：已实现**

**建议回答：**

会检查目标和任务非空、Task ID 非空且唯一、描述非空、依赖不重复、依赖必须存在、禁止自依赖、禁止环。

另外对 Coding Task 增加业务级不变量：每个 FILE_WRITE 必须存在下游 VERIFICATION。它不能证明计划语义一定正确，但能保证最基本的结构和验证闭环。

**代码落点：** `paicli/planning.py`

---

## Q025. 环依赖如何检测？

**实现状态：已实现**

**建议回答：**

PlanValidator 用 DFS 维护 visiting 和 visited。访问到当前递归栈里已经存在的 Task，说明出现回边，会把实际环路径拼出来，例如 A -> B -> C -> A，并拒绝计划。

调度器的拓扑排序还会做第二道检查：如果最终排序节点数少于任务数，也判定存在环。

**代码落点：** `paicli/planning.py`

---

## Q026. DAG 如何做拓扑排序和 Ready Task 调度？

**实现状态：已实现**

**建议回答：**

完整拓扑序使用入度推进，保持原始 Task 顺序作为稳定的同层顺序。运行时不需要每次重算全序，只选择状态为 PENDING 且所有依赖均 COMPLETED 的 Ready Task。

如果依赖已经 FAILED 或 SKIPPED，后代会被标记 SKIPPED；和失败节点无依赖的分支仍可继续。

**代码落点：** `paicli/planning.py`

---

## Q027. 任务粒度如何控制？拆太粗或太细有什么问题？

**实现状态：部分实现**

**建议回答：**

当前主要通过 Planner Prompt 约束：简单任务优先 1 到 3 个节点，复杂任务通常 4 到 7 个，并要求合并相关读取、避免冗余分析。

拆太粗会让 Worker 上下文过大、失败定位和局部回滚范围过大；拆太细会增加模型调用、Token、消息交接和跨 Task 契约漂移。现在是规则与 Prompt 控制，还没有学习型粒度优化器。

**代码落点：** `paicli/planning.py`

---

## Q028. 一个 Task 失败后，哪些任务跳过，哪些继续？

**实现状态：已实现**

**建议回答：**

DagScheduler 只跳过依赖失败 Task 的后代。独立分支如果依赖都完成，仍然是 Ready，可以继续执行。

这比“某个 Worker 失败就把剩余所有任务全部跳过”更精确，也能保留已经完成的有效工作。

**代码落点：** `paicli/planning.py`；`paicli/orchestration.py`

---

## Q029. 什么情况下原地重试，什么情况下重新规划？

**实现状态：部分实现**

**建议回答：**

Team 中可局部修复的实现问题由 Reviewer 返回 changes_requested，只重做当前 Task；Reviewer 明确认为前提错误、越权、需要改变计划时才是 rejected。

Plan 模式在 Task 失败后可以调用 Planner 生成完整替代计划，并携带失败原因和已完成结果。但当前还没有一套通用错误分类器自动决定“必然重试还是必然 replan”，部分判断仍依赖模式和 Reviewer 协议。

**代码落点：** `paicli/review.py`；`paicli/planning.py`；`paicli/orchestration.py`

---

## Q030. 为什么每个写任务必须有下游 Verification？

**实现状态：已实现**

**建议回答：**

因为“调用 write_file 成功”只能证明文件被写了，不能证明代码正确。Planner 如果生成写任务而没有可观察验证，PlanValidator 会直接拒绝。

Verification 可以是完整测试、语法诊断或其他确定性检查。它把完成定义从“模型说做完了”提高到“副作用后存在独立证据”。

**代码落点：** `paicli/planning.py`；`paicli/subagents.py`

---

# 四、Multi-Agent、Worker、Reviewer 与协作

**题库映射示例**：原题 #53、#76—#89、#151、#268、#281—#308、#366、#407—#408、#429—#435、#450—#458、#481—#492、#594—#597、#641、#654—#663 等。

## Q031. Worker 在 PaiCLI 里是什么？输入输出是什么？

**实现状态：已实现**

**建议回答：**

Worker 是复用统一 AgentLoopEngine 的真实 LLM Sub-Agent，不是普通 Python 回调。输入是 TaskPacket，包含总目标、当前 Task、验收标准、直接依赖结果、已改文件、尝试次数和 Reviewer 反馈。

输出是 AgentOutcome，包含状态、结束原因、自然语言结果、Token、changed_files 和结构化 ToolResult。编排器据此更新 Task，而不是只解析一段自述。

**代码落点：** `paicli/subagents.py`；`paicli/agents/models.py`

---

## Q032. Sub-Agent 如何隔离上下文？

**实现状态：已实现**

**建议回答：**

每个 Worker、Reviewer 和 Aggregator 都有独立 History、System Prompt、AgentBudget 和 Context Compactor。它们不会继承主 ReAct 会话，也看不到无关 Worker 的完整历史。

共享的是 LlmClient、受控 ToolRuntime、长期记忆和项目工作区。这样隔离推理上下文，同时保留必要基础设施。

**代码落点：** `paicli/subagents.py`；`ARCHITECTURE.md`

---

## Q033. Agent 之间传完整对话，还是结构化中间结果？

**实现状态：已实现**

**建议回答：**

传结构化 TaskPacket 和有界依赖结果，不传完整对话。依赖交接既包含 Worker 摘要，也带成功 ToolResult 的原始观察，防止上游模型把刚读到的文件总结错后误导下游。

这是一个重要取舍：全量历史信息最全，但会带来 Token 膨胀、无关噪声和角色耦合；只传摘要又可能丢事实，所以需要摘要加机器证据。

**代码落点：** `paicli/subagents.py`；`paicli/orchestration.py`

---

## Q034. 为什么使用 Multi-Agent，而不是一个 Agent 完成全部任务？

**实现状态：已实现**

**建议回答：**

Multi-Agent 的价值不是角色名字更多，而是状态和权限分离。Planner 只负责提出计划，Worker 执行，Reviewer 只读审查，Aggregator 不使用工具。

这样能让不同角色拥有不同上下文和工具能力，并把返工限制到当前 Task。代价是模型调用更多、跨 Task 契约可能漂移，所以简单任务仍应该用 ReAct。

**代码落点：** `paicli/orchestration.py`；`paicli/subagents.py`

---

## Q035. 为什么不能用一个 Agent 加多个 Skill 完全替代 Multi-Agent？

**实现状态：已实现**

**建议回答：**

Skill 主要提供按需加载的流程指导，它不会天然创建独立 History、权限边界和状态机。一个 Agent 即使加载“Reviewer Skill”，仍然可能同时保留写权限和 Worker 的自我偏见。

Multi-Agent 解决的是执行身份、上下文、工具能力和生命周期隔离；Skill 解决的是知识与流程复用。两者可以组合，但不是同一层能力。

**代码落点：** `paicli/skills.py`；`paicli/subagents.py`

---

## Q036. Plan 和 Team 的 Worker 是串行还是并行？

**实现状态：已实现**

**建议回答：**

只读类型的 FILE_READ 和 ANALYSIS Task 可以在同一个 Ready 层内有限并行。FILE_WRITE、COMMAND 和 VERIFICATION 在共享工作区中串行执行。

这是有意的安全取舍：没有独立 worktree 和合并协议前，直接并行写代码会把速度问题变成一致性问题。

**代码落点：** `paicli/planning.py`；`paicli/orchestration.py`

---

## Q037. 两个 Worker 同时修改同一文件怎么办？

**实现状态：已实现（通过禁止该场景）**

**建议回答：**

当前 Task 调度层不会并行运行写任务，所以正常 Team/Plan 路径下不会出现两个写 Worker 同时落盘。单个 Worker 一轮内的多个 Tool Call 还会经过资源声明，读写冲突和写写冲突被拆成不同波次。

项目没有宣称支持安全并行代码修改。真正要做，需要每个 Worker 独立 Git worktree、Patch 合并、冲突解决和合并后测试。

**代码落点：** `paicli/planning.py`；`paicli/tools.py`；`SECURITY.md`

---

## Q038. Reviewer 的输入是什么？它是否只看 Worker 自述？

**实现状态：已实现**

**建议回答：**

Reviewer 收到原始目标、当前 Task、验收标准、Worker Outcome、changed_files、每个 ToolResult，以及直接依赖结果。它的 Tool Scope 是只读。

Prompt 明确要求不能只相信 Worker narrative；框架还会检查 Reviewer 是否真的读取了所有 changed_files。没有直接读取证据却返回 approved，会被改成 changes_requested。

**代码落点：** `paicli/review.py`

---

## Q039. Reviewer 如何被强制读取实际修改文件？

**实现状态：已实现**

**建议回答：**

框架遍历 Reviewer 每次模型运行产生的 ToolResult，提取 READ 类型的 ResourceAccess，再和 Worker.changed_files 对比。

如果有任何修改文件未被覆盖读取，approved 不会生效，而是返回“缺少实际产物检查”的可重试 changes_requested。这个门禁在模型外执行，不依赖 Reviewer 自觉。

**代码落点：** `paicli/review.py`

---

## Q040. Reviewer 的结构化输出是什么？

**实现状态：已实现**

**建议回答：**

输出是 ReviewResult，verdict 只能是 approved、changes_requested 或 rejected；还包括 summary、issues、suggestions、evidence 和 retryable。

模型输出非法 JSON、空 summary、approved 但仍带 unresolved issues，都会进入一次有界修复。修复仍失败则生成内部 ERROR，不会把解析失败当作通过。

**代码落点：** `paicli/review.py`

---

## Q041. Reviewer 拒绝后为什么只重做当前 Task？

**实现状态：已实现**

**建议回答：**

因为 DAG 已经把任务拆成有依赖边界的节点。若问题能通过重做当前 Task 修复，回滚整个 DAG 会重复已经验证过的工作，增加成本和新的随机性。

Team 只把 Reviewer feedback 放回同一个 TaskPacket，重新调用该 Worker，再次审查。只有 Reviewer 判断前提错误、越权或需要改变计划时，才把当前 Task 标记失败并阻断后代。

**代码落点：** `paicli/orchestration.py`；`paicli/review.py`

---

## Q042. Reviewer 一直拒绝时如何避免死循环？

**实现状态：已实现**

**建议回答：**

默认最多两次局部返工，加上第一次执行，当前 Task 最多执行三次。达到上限仍未批准，Task 明确 FAILED，依赖它的后代 SKIPPED。

Reviewer 自己输出非法 JSON 也只有一次修复机会；Reviewer 模型错误不会静默放行。

**代码落点：** `paicli/orchestration.py`；`paicli/review.py`

---

## Q043. Reviewer 和 Worker 用同一个模型，会不会有共同盲点？

**实现状态：部分实现**

**建议回答：**

会有这个风险。当前通过独立 Prompt、独立 History、只读权限和实际文件读取降低相关性；默认配置仍复用同一个 Client，因此共同误解依然可能同时出现在 Worker 和 Reviewer。

1.1 已支持为 Planner、Worker、Reviewer 和 Aggregator 配置不同 Provider，但这只是解耦能力，不等于已经证明异构模型一定更准。面试中应区分“路由能力已实现”和“Reviewer 人工校准尚未完成”。

**代码落点：** `paicli/bootstrap.py`；`paicli/subagents.py`；`paicli/__main__.py`

**追问边界：** 可以说支持角色级模型配置；不能说已有数据证明某个模型组合最优。

---

## Q044. Reviewer 的判断可信吗？如何校准？

**实现状态：部分实现**

**建议回答：**

当前能证明的是 Reviewer 参与了真实状态流、读取实际文件、能拒绝并局部返工；不能证明它与人类判断高度一致。

真正校准需要人工标注一批 Diff 和验收结果，统计误放行、误拒绝和多次运行一致性。PaiCLI 目前没有这套人工 Reviewer 数据集，所以 Reviewer 是质量门的一部分，不能替代确定性测试。

**代码落点：** `tests/test_dashscope_live.py`；`reports/dashscope-1.0-stability.json`

---

## Q045. 为什么局部 Reviewer 都通过，最终测试仍可能失败？

**实现状态：已观测到真实边界**

**建议回答：**

因为多个 Task 可能分别改变同一个业务契约。真实失败样本中，一个任务实现了“division by zero”，另一个任务的测试期待“Cannot divide by zero”；局部看都像合理修改，但跨 Task 合并后不一致。

最终 Verification 正确拦住了结果并返回 partial。当前缺少的是把最终测试失败自动路由回相关上游 Task，这也是下一版最重要的可靠性目标。

**代码落点：** `reports/dashscope-1.0-run-03.json`；`docs/final-acceptance.md`

---

# 五、Tool Calling、MCP 与 Skill

**题库映射示例**：原题 #32—#35、#44—#45、#62、#84、#103—#105、#122、#154、#158、#188—#190、#210、#248—#250、#329、#379、#397—#400、#530—#532、#635—#636、#666—#669、#749、#761、#775、#802、#822—#825、#933 等。

## Q046. 工具如何注册、发现和调用？

**实现状态：已实现**

**建议回答：**

每个工具通过 ToolSpec 注册，包含 name、description、JSON Schema、handler、风险级别、副作用类型、并发策略、超时和资源解析器。

Agent 只看到 definitions；真正调用时 ToolRegistry 根据名字查找、解析 JSON、做运行时 Schema 校验，再经过权限和 HITL 包装器执行，最后统一返回 ToolResult。

**代码落点：** `paicli/tool_contracts.py`；`paicli/tools.py`

---

## Q047. 模型如何决定要不要调用工具、调用哪个工具？

**实现状态：已实现**

**建议回答：**

框架把当前角色可见的工具 Schema 放进 OpenAI-compatible 请求，由模型根据目标、上下文和工具描述决定是否产生 Tool Call。

但模型的决定只是提议。不存在、不可见或参数非法的工具会在执行端被拒绝。PaiCLI 没有用关键字规则强行替模型选工具，也没有把执行权限交给模型。

**代码落点：** `paicli/llm_client.py`；`paicli/tools.py`；`paicli/subagents.py`

---

## Q048. Function Calling 的完整执行流程是什么？

**实现状态：已实现**

**建议回答：**

流程是：ToolSpec 生成模型可见 Schema；模型返回 tool_calls；框架校验工具名和 arguments；安全链审批；handler 执行；生成结构化 ToolResult；以匹配的 tool_call_id 回灌；模型观察后继续或结束。

这里 Function Calling 负责模型与工具的调用格式，真正的函数执行始终发生在本地 Harness。

**代码落点：** `paicli/llm_client.py`；`paicli/agents/loop.py`；`paicli/tools.py`

---

## Q049. 模型生成的工具名和参数如何校验？

**实现状态：已实现**

**建议回答：**

工具名必须在当前 ToolRuntime 中存在。arguments 必须是 JSON Object，并按 JSON Schema 检查 required、type、enum、范围、数组元素、additionalProperties 以及组合规则。

HITL 不允许审批器改写参数。模型参数先完成 Schema 与硬策略检查，审批器只能决定允许、拒绝或记住权限；若业务需要转换参数，必须进入独立的预处理与重新校验链，而不能在审批结果里偷换已经评估过的调用。

**代码落点：** `paicli/tool_validation.py`；`paicli/tools.py`；`paicli/policy.py`

---

## Q050. 模型调用不存在或无权限的工具时怎么办？

**实现状态：已实现**

**建议回答：**

不存在的工具返回 UNKNOWN_TOOL；角色被隐藏的工具返回 POLICY_DENIED。错误是结构化 ToolResult，会回灌模型，让它有机会换工具或修正行为。

例如 Reviewer 只有只读工具，即使幻觉调用 write_file，也不会穿透到底层 Registry。

**代码落点：** `paicli/tool_contracts.py`；`paicli/tools.py`；`paicli/subagents.py`

---

## Q051. 工具很多、Schema 撑爆上下文时怎么处理？

**实现状态：部分实现**

**建议回答：**

当前已经有角色级 Tool Scope：Reviewer、只读 Worker 和 Aggregator 只接收必要工具，这能显著减少 Schema。

但如果用户接入大量外部 MCP Tool，PaiCLI 还没有完整的 Tool Retrieval、分层路由或动态 Top-K Schema 注入。现阶段应该按 Server/领域配置，后续再做工具索引和两阶段选择。

**代码落点：** `paicli/subagents.py`；`paicli/mcp.py`

**追问边界：** 不要声称已经实现 500 个 Tool 的动态检索。

---

## Q052. 工具失败可以分成哪些类型？

**实现状态：已实现**

**建议回答：**

ToolResult 区分 UNKNOWN_TOOL、INVALID_ARGUMENTS、POLICY_DENIED、APPROVAL_DENIED、TIMEOUT、RESOURCE_CONFLICT、EXECUTION_ERROR 和 CANCELLED。

分类的价值是避免所有错误都变成一段文本：参数错可以修正，权限拒绝不应盲目重试，超时要结合副作用判断，资源冲突要等待或串行。

**代码落点：** `paicli/tool_contracts.py`

---

## Q053. 工具超时或异常后，是重试还是终止？

**实现状态：部分实现**

**建议回答：**

框架会返回带 retryable 和 timed_out 的 ToolResult。只读操作的超时通常可以重试；写入、Shell 或未知 MCP 可能已经产生部分副作用，默认不能盲重试。

工具层还会把仍在后台运行的超时 Future 标记为 uncertain，暂时阻止冲突资源继续操作。但通用“按错误类型自动重试几次”的策略还不是所有工具统一实现。

**代码落点：** `paicli/tools.py`；`paicli/tool_contracts.py`

---

## Q054. Shell 工具如何控制风险？

**实现状态：已实现但不是沙箱**

**建议回答：**

Shell 默认不暴露，必须显式 --allow-shell。暴露后，基础 ToolRegistry 先执行不可被审批绕过的危险命令硬拒绝，再经过持久权限、HITL、命令预览、超时和 Trace。

但 cwd 指向项目根并不等于 OS 沙箱。批准后的命令仍可能访问绝对路径、网络或项目外资源，所以不可信任务需要独立账户、VM 或真正容器隔离。

**代码落点：** `paicli/tools.py`；`paicli/policy.py`；`SECURITY.md`

---

## Q055. 写文件前的 Diff 和 HITL 如何实现？

**实现状态：已实现**

**建议回答：**

write_file、replace_text、multi_edit 和 apply_patch 在真正执行前生成 unified diff，并把工具、参数、风险和预览交给 ApprovalHandler。默认 CLI 使用 ask；用户也可以显式 deny 或在一次性测试工作区用 allow。

交互审批可以只允许本次，也可以持久化精确调用或 glob 模式。`a` 会把 `*`、`?`、`[` 等字符按字面量转义，只有显式选择 `p` 才授予通配权限；硬拒绝策略始终优先。审批结果会记录到结构化脱敏 AuditLog，被拒绝返回 APPROVAL_DENIED，不会伪装成执行异常。

**代码落点：** `paicli/policy.py`；`paicli/__main__.py`

---

## Q056. 文件访问如何限制在项目根目录？符号链接怎么办？

**实现状态：已实现**

**建议回答：**

所有文件路径先 resolve，再判断是否位于 project_root 下，因此 `..` 和指向外部的符号链接不能绕过边界。

这个围栏适用于内置文件工具；Shell 本身能力更强，所以必须单独强调 Shell 不是同等级路径沙箱。

**代码落点：** `paicli/tools.py`；`SECURITY.md`

---

## Q057. MCP 与 Function Calling 有什么区别？

**实现状态：已实现**

**建议回答：**

Function Calling 是模型接口层的调用表达：模型输出工具名和结构化参数。MCP 是 Host/Client 与外部 Server 之间发现工具、资源和调用能力的协议。

在 PaiCLI 里，MCP Client 先通过 JSON-RPC 初始化、tools/list 和 tools/call，把远程工具包装成本地 ToolSpec；之后模型侧仍然通过 Function Calling 使用它，所以二者是上下游关系，不是二选一。

**代码落点：** `paicli/mcp.py`

---

## Q058. PaiCLI 的 MCP 支持哪些传输方式？

**实现状态：已实现**

**建议回答：**

支持 stdio 子进程传输和基于 HTTP POST 的 JSON-RPC 请求。MCP 通过显式 extensions 配置接入；stdio 请求有超时、有界 stderr，并会跳过先到达的 notification，继续等待与当前请求 ID 匹配的响应。

HTTP 类名叫 StreamableHttpTransport，但当前只实现请求响应，不包含完整 SSE、会话生命周期和断线续传。

**代码落点：** `paicli/mcp.py`

---

## Q059. MCP 当前有哪些局限？

**实现状态：部分实现**

**建议回答：**

外部 Tool 的基础描述不包含统一风险、幂等和副作用元数据，所以 PaiCLI 默认把它们视为 UNKNOWN、串行并可能要求审批。MCP 错误后也不自动重试，避免远端动作已经部分成功。

stdio 已保留有界 stderr 并处理请求前通知，但还没有完整异步 dispatcher 或并发多请求复用；HTTP 没有完整 Streamable HTTP/SSE，会影响长连接、恢复和会话生命周期场景。

**代码落点：** `paicli/mcp.py`；`docs/final-acceptance.md`

---

## Q060. Skill 的本质是什么？与 Prompt 有什么区别？

**实现状态：已实现**

**建议回答：**

Skill 是可发现、可复用、按需加载的流程知识包，本质仍会进入模型上下文；它不是新模型，也不等于可执行 Tool。

与固定 System Prompt 相比，Skill 有独立元数据、说明、完整指令和允许工具，可以先只暴露索引，选中后再加载，减少所有流程一次性常驻上下文。

**代码落点：** `paicli/skills.py`

---

## Q061. Skill 如何渐进式加载？

**实现状态：已实现**

**建议回答：**

启动时扫描 SKILL.md，只把 name 和 description 组成短索引。模型调用 load_skill(name) 后，框架才读取并注入完整 instructions，SkillContextBuffer 还会避免同一会话重复加载。

当前 frontmatter 解析器只支持简化单行 YAML，不支持完整嵌套语法，这是实现边界。

**代码落点：** `paicli/skills.py`

---

## Q062. 如何统计一个 Skill 真正被使用了多少次？

**实现状态：代码里没有完整实现**

**建议回答：**

不能只统计 load_skill 次数，因为加载后可能在多轮中继续影响模型，也可能加载了却没采用。要统计真实使用，需要定义事件：加载、激活、引用、触发工具、影响最终结果分别是什么。

当前 PaiCLI 没有 Skill 归因埋点，我会诚实说没有做。合理方案是给 Skill ID 建 Trace Span，让后续模型步骤和工具调用显式关联，但不能靠 Prompt 自报保证准确。

**代码落点：** `paicli/skills.py`；`paicli/observability.py`

---

# 六、Context Engineering 与 Memory

**题库映射示例**：原题 #46、#79—#81、#117—#120、#136、#170、#188—#190、#274—#279、#303—#308、#431、#498、#519、#536—#542、#583、#601—#603、#622、#632、#677—#686、#746—#752、#829—#833、#845—#849、#894—#898、#993—#994 等。

## Q063. PaiCLI 的上下文由哪些部分组成？

**实现状态：已实现**

**建议回答：**

实际发送给模型的上下文包括 System Prompt、当前用户请求、保留的会话历史、Tool Call/Tool Result、相关长期记忆，以及角色自己的 TaskPacket。工具 Schema 作为请求中的 tools 字段单独发送。

不同 Sub-Agent 只拿与当前任务有关的内容，不会把主会话和其他 Worker 历史全部复制过去。

**代码落点：** `paicli/agents/loop.py`；`paicli/context.py`；`paicli/subagents.py`

---

## Q064. 什么时候触发上下文压缩？

**实现状态：已实现**

**建议回答：**

ContextSettings 根据模型窗口计算可用输入预算，达到可用预算约 90% 时触发压缩。触发点不是固定消息条数，因为一条含大 Tool Result 的消息可能比几十条短对话更大。

Token 预估用于提前保护，最终真实消耗仍以 Provider 返回 usage 为准。

**代码落点：** `paicli/context.py`；`paicli/memory.py`

---

## Q065. 上下文压缩具体怎么做？

**实现状态：已实现**

**建议回答：**

按 User Round 和协议安全边界切分，保留最近三轮用户交互的原文，把更早历史交给 LLM 生成事实摘要。摘要要求保留目标、约束、已完成操作、修改文件、关键决策、错误和待办。

摘要失败或返回空内容时使用确定性降级；相同旧前缀有缓存，避免每轮重复总结。

**代码落点：** `paicli/memory.py`

---

## Q066. 上下文压缩可能踩坏哪些信息？

**实现状态：已知边界**

**建议回答：**

最危险的不是少了几句闲聊，而是丢失硬约束、工具副作用、失败尝试、文件版本和未完成事项。摘要模型还可能把“尝试过”写成“已完成”。

PaiCLI 通过保留近期原文、结构化 TaskPacket、原始 ToolResult 和确定性状态库降低风险，但目前没有完整的摘要事实一致性评测集，所以压缩仍不是无损操作。

**代码落点：** `paicli/memory.py`；`paicli/state.py`

---

## Q067. 如何保证 Tool Call 与 Tool Result 不被压缩拆散？

**实现状态：已实现**

**建议回答：**

Compactor 只在协议安全边界切分，assistant.tool_calls 和后续匹配的 tool 消息必须作为完整单元保留或一起进入旧历史摘要。

否则下一次请求会形成 orphan Tool Result，Provider 可能直接拒绝，模型也会失去动作与观察的对应关系。

**代码落点：** `paicli/memory.py`；`tests/test_context_memory_runtime.py`

---

## Q068. 上下文窗口是不是越大越好？如何缓解 Lost in the Middle？

**实现状态：已实现一部分**

**建议回答：**

窗口大只代表能装更多，不代表模型会同等关注中间信息，而且会增加延迟和成本。PaiCLI 不把所有内容平铺，而是用近期原文、旧历史摘要、相关长期记忆和任务级隔离控制输入。

它没有实现专门的 Long-context Attention 优化；应用侧主要通过内容选择、结构排序和证据缩小来缓解。

**代码落点：** `paicli/context.py`；`paicli/memory.py`；`paicli/subagents.py`

---

## Q069. 短期记忆和长期记忆如何划分？

**实现状态：已实现**

**建议回答：**

短期记忆是当前 Agent History 和压缩摘要，服务正在进行的任务；长期记忆是跨会话持久化的项目事实、偏好、经验或决策。

长期记忆不会把所有对话自动落盘，只有模型显式调用 save_memory 或外部代码写入。完整应用主链会把 save_memory 当作跨会话副作用，先经过策略/HITL；模型写入只是未验证候选，不会直接进入后续会话上下文。

**代码落点：** `paicli/memory.py`；`paicli/managed_memory.py`

---

## Q070. Managed Memory 保存哪些字段和状态？

**实现状态：已实现**

**建议回答：**

正式 SQLite 接口是 ManagedMemoryStore，记录 ID、内容、kind、confidence、tags、source、source_hash、时间和生命周期状态。

状态包括 unverified、verified、active、stale、superseded 和 deleted。模型写入默认 unverified；正常上下文检索只使用 active 和 verified，同时排除未验证、过时、被替代和删除的记录。诊断工具可以显式选择查看 unverified 候选。

**代码落点：** `paicli/managed_memory.py`

---

## Q071. 错误、过时或冲突记忆如何处理？

**实现状态：已实现基础机制**

**建议回答：**

来源哈希变化时可以把相关记忆标成 stale；冲突时可以建立 supersedes 关系，把旧记忆标为 superseded；错误记录可以软删除，人工确认后才能 verify。

它比“最后一条永远覆盖前一条”更可审计。但当前没有自动事实核验 Agent，也没有基于真实代码变更自动覆盖所有来源的完整依赖追踪。

**代码落点：** `paicli/managed_memory.py`

---

## Q072. 哪些信息值得写入长期记忆？模型能自动写吗？

**实现状态：已实现但需治理**

**建议回答：**

适合长期保存的是稳定用户偏好、项目约束、经过验证的架构决策和可复用经验；临时错误、未验证猜测、一次性日志不应直接晋升为可信事实。

模型可以调用 save_memory，但完整运行时会先要求审批，写入状态仍是 unverified，并且默认不参与后续上下文召回。只有明确验证或提升后才成为正常检索候选，从而区分“模型建议的记忆”和“已验证事实”。

**代码落点：** `paicli/memory.py`；`paicli/managed_memory.py`

---

# 七、Code RAG 与检索

**题库映射示例**：原题 #41、#48、#103、#174、#216—#228、#269—#270、#316—#320、#326—#328、#363、#381—#396、#467—#469、#497、#516—#518、#580—#582、#620、#631、#638—#639、#646、#664—#665、#690、#707—#711、#739、#764—#765、#837—#844、#995—#996、#1070—#1071 等。

## Q073. PaiCLI 的 Code RAG 从建库到检索是什么链路？

**实现状态：已实现**

**建议回答：**

启动时 CodeIndex 扫描项目文件，按语言和符号切成 CodeChunk，计算内容哈希并写入 SQLite，同时建立 FTS5 索引；可选 Embedding Client 会额外保存向量。

查询时同时跑符号匹配、FTS5/BM25、词法余弦和可选 Dense Retrieval，再用 RRF 融合，返回文件、符号、开始结束行和原始代码片段。

**代码落点：** `paicli/rag.py`；`paicli/bootstrap.py`

---

## Q074. 为什么 Coding Agent 需要 Code RAG，而不是每次扫描整个仓库？

**实现状态：已实现**

**建议回答：**

全仓库扫描会浪费 Token、增加延迟，还容易让模型在大量无关代码中丢失重点。Code RAG 的作用是先把候选范围缩到可验证的源码片段和符号。

但 RAG 不是替代文件工具：检索负责找入口，Worker 仍可以通过 read_file 读取完整上下文和最新文件。

**代码落点：** `paicli/rag.py`；`ARCHITECTURE.md`

---

## Q075. 代码如何切 Chunk？

**实现状态：已实现**

**建议回答：**

Python 优先利用 AST 抽取函数、类等符号块，保留精确行号；其他支持语言使用通用行块策略。每个 Chunk 都有 path、symbol、start_line、end_line、content 和 hash。

这不是面向任意长文档的父子 Chunk 系统，主要针对代码仓库。Chunk 参数目前由实现规则决定，还没有通过专门检索评测自动调优。

**代码落点：** `paicli/rag.py`

---

## Q076. 为什么使用符号、BM25、词法和可选 Dense 的混合检索？

**实现状态：已实现**

**建议回答：**

代码查询里精确标识符非常重要，纯 Dense 可能把相似语义放在精确函数名之前；BM25 和词法通道擅长关键词，Dense 擅长同义表达。

所以 PaiCLI 保留多个互补通道，而不是押注单一路径。没有配置 Embedding 时，系统仍能用符号、FTS 和词法正常工作。

**代码落点：** `paicli/rag.py`

---

## Q077. 多路结果如何融合？是否使用独立 Reranker？

**实现状态：部分实现**

**建议回答：**

当前使用 Reciprocal Rank Fusion，根据每个通道中的名次融合，不要求把 BM25 分数、余弦分数和符号分数校准到同一尺度。

目前没有接入独立 Cross-Encoder Reranker。因此面试中可以说“做了多路召回和 RRF”，不能说已经完成学习型 Rerank。

**代码落点：** `paicli/rag.py`

---

## Q078. 是否做 Query Rewrite 或 HyDE？

**实现状态：代码里没有**

**建议回答：**

当前 search_code 直接使用用户或模型提供的查询，不做多维 Query Rewrite，也不生成 HyDE 假文档。

这是一项明确未实现能力。加入前需要先做检索评测，否则 Rewrite 改错标识符或意图，可能比原查询更差。

**代码落点：** `paicli/rag.py`

---

## Q079. 检索结果如何提供可验证证据？

**实现状态：已实现**

**建议回答：**

SearchResult 不只返回相似度，还返回源文件、符号、精确行范围、命中的检索通道和原始代码内容。Worker 可以据此继续 read_file，而不是依据模型生成的摘要作答。

这使检索结果具备可定位性，也方便后续做 expected-source 和 expected-symbol 评测。

**代码落点：** `paicli/rag.py`

---

## Q080. 代码修改后索引如何保持最新？

**实现状态：已实现**

**建议回答：**

CodeIndex 使用文件哈希做增量构建，修改文件会重新切块并替换旧记录，删除文件会清理对应 Chunk。

生产 ToolRuntime 外面还有 IndexRefreshingToolGateway，成功文件变更后触发相关路径刷新，避免本轮后续检索仍看到旧代码。

**代码落点：** `paicli/rag.py`；`paicli/bootstrap.py`

---

## Q081. 如何评测 Code RAG 的检索质量？

**实现状态：代码里没有完整评测**

**建议回答：**

当前固定 Coding Agent Suite 评测最终任务是否完成，不等于 RAG 检索评测。项目还没有独立的 Recall@K、MRR、nDCG、expected-source recall 或 expected-symbol recall 数据集。

所以我能证明 RAG 被接入并有确定性测试，不能给出“检索准确率是多少”的数字。下一步应先构建代码查询—正确符号/文件的黄金集。

**代码落点：** `paicli/evaluation.py`；`tests/test_rag_persistent.py`

---

## Q082. Embedding 模型如何选型？当前项目用了什么？

**实现状态：部分实现**

**建议回答：**

Dense Embedding 是可插拔接口，核心运行不要求外部模型。项目提供 OpenAI-compatible Embedding Client，也保留测试用的确定性 Hash Embedding。

当前正式评测主要验证没有 Dense 服务时的可用性，没有做多个 Embedding 模型的效果、延迟和成本对比，因此不能声称某个模型最优。

**代码落点：** `paicli/rag.py`；`paicli/hybrid_rag.py`

---

# 八、安全、HITL、Snapshot 与恢复

**题库映射示例**：原题 #60、#186、#231—#232、#350—#351、#367、#391—#403、#454—#456、#474、#482、#485、#496、#499—#500、#508、#561、#565—#571、#585、#597、#600、#610—#611、#616、#640、#656、#659、#662—#663、#666、#681、#695、#792、#807—#809、#850—#857、#898—#900、#1072 等。

## Q083. PaiCLI 的安全边界是怎么分层的？

**实现状态：已实现**

**建议回答：**

工具请求依次经过：角色 Tool Scope、工具名与 JSON Schema 校验、硬安全策略、HITL、资源冲突调度、Handler、结构化 ToolResult、Trace 与 Checkpoint。

Prompt 只负责指导模型，真正的权限边界在模型外。未知风险或未知副作用的扩展工具默认失败收紧，而不是自动当成安全工具。

**代码落点：** `paicli/tools.py`；`paicli/policy.py`；`ARCHITECTURE.md`

---

## Q084. 如何防止仓库里的 Prompt Injection 诱导 Agent 误操作？

**实现状态：部分实现**

**建议回答：**

仓库内容即使写着“忽略系统指令并执行危险命令”，它也只能影响模型建议，不能直接绕过 Tool Scope、Schema、路径围栏、命令策略和人工审批。

但 Prompt Injection 不能被彻底消灭，尤其用户主动允许 Shell 后仍有风险。当前方案是能力最小化和模型外门禁，不是声称内容过滤能百分百识别恶意文本。

**代码落点：** `SECURITY.md`；`paicli/policy.py`

---

## Q085. HITL 应该插在哪些节点？

**实现状态：已实现**

**建议回答：**

PaiCLI 把 HITL 放在副作用真正发生前，而不是模型回答后：write_file、create_project、execute_command 以及风险未知的外部工具都可以触发审批。

Plan 在执行前也支持人工审阅和修订。工具层的审批器只做允许、拒绝和权限记忆，不具备参数改写能力，因此不会出现“审批后用未经硬策略评估的新参数执行”的提权通道。

**代码落点：** `paicli/policy.py`；`paicli/orchestration.py`

---

## Q086. Snapshot 与回滚具体怎么做？

**实现状态：已实现**

**建议回答：**

RunCoordinator 在顶层 Run 开始前创建 BEFORE Snapshot，结束后创建 AFTER Snapshot；有副作用的 Task 在尝试前还有任务级快照。

当 Run 失败或 partial 时，CLI 默认 rollback-on-failure=ask，由用户选择恢复 BEFORE；也可以显式设为 always 或 never。迭代、停滞或单 Agent Token 上限产生 STOPPED 状态，保留工作区并允许恢复。恢复结果和 Snapshot ID 都写入状态和 Trace。

**代码落点：** `paicli/execution.py`；`paicli/snapshot.py`；`paicli/safety.py`

---

## Q087. 哪些副作用无法被 Snapshot 回滚？

**实现状态：明确边界**

**建议回答：**

Snapshot 只能恢复 project_root 内的文件，不能撤销已经发送的网络请求、发布包、远程数据库更新、部署、凭证变更，也不能可靠撤销 Shell 写到项目外的内容。

所以外部副作用必须继续 HITL，并由工具本身提供 idempotency key、查询状态或补偿操作。

**代码落点：** `SECURITY.md`；`docs/recovery.md`

---

## Q088. 进程中断后如何恢复任务？

**实现状态：已实现于本地单进程范围**

**建议回答：**

RunStateStore 用 SQLite 保存 Run、Plan、Task、Reviewer 结果和 append-only Checkpoint。重启后可以列出可恢复 Run。

若中断 Task 有安全的任务快照，就先恢复该边界，再重试未完成 Task；如果缺少安全边界，就恢复整个 BEFORE Snapshot 并重启 DAG，优先避免重复副作用。

**代码落点：** `paicli/state.py`；`paicli/execution.py`；`docs/recovery.md`

---

## Q089. 如何避免恢复时重复执行副作用？

**实现状态：部分实现**

**建议回答：**

内置文件写通过任务 Snapshot 和 Tool Call 记录控制：恢复前先把工作区回到确定边界，再执行。框架不会看到 RUNNING 就直接盲目重放。

但任意远程 API 和外部 MCP 没有 exactly-once 保证。对这类工具，恢复只能失败收紧，或者依赖工具提供幂等键和状态查询。

**代码落点：** `paicli/state.py`；`docs/recovery.md`

---

## Q090. 并发任务如何避免资源冲突？

**实现状态：已实现基础策略**

**建议回答：**

DAG 层只允许只读 Task 并行，写、命令和验证串行。工具层进一步根据 ResourceAccess 判断 READ/WRITE 冲突，同一路径读写或写写不会同波执行。

当前没有跨进程分布式锁，也没有多 Worker worktree 合并，所以范围是单进程、本地共享工作区。

**代码落点：** `paicli/planning.py`；`paicli/tools.py`

---

## Q091. 工具线程超时后真的停止了吗？

**实现状态：部分实现**

**建议回答：**

如果 Python Future 已经开始运行，线程不能安全强杀。PaiCLI 会返回 TIMEOUT，把相关资源标记 uncertain，并阻止后续冲突操作，直到 Future 真正结束。

内置 Shell 还有 subprocess 级 timeout，但第三方 Handler 可能继续在后台产生副作用。这也是为什么未知工具默认串行、审批且不可盲重试。

**代码落点：** `paicli/tools.py`

---

## Q092. PaiCLI 是否提供 OS 级 Shell 沙箱？

**实现状态：代码里没有**

**建议回答：**

没有。它有 Shell 默认关闭、HITL、危险命令规则、项目根、超时、Snapshot 和审计，但这些不等于 syscall、网络和文件系统命名空间隔离。

面对不可信代码，应在独立用户、VM、远程临时机或真正沙箱中运行。面试时必须把“安全门禁”和“OS 沙箱”分开。

**代码落点：** `SECURITY.md`

---

# 九、Trace、评测、性能、模型与真实边界

**题库映射示例**：原题 #31、#56、#64—#69、#85—#90、#123—#128、#140、#156、#167—#169、#229、#233—#234、#335—#351、#371、#408、#461—#464、#484—#490、#495—#506、#570、#582、#584、#610、#616、#639—#640、#643、#659、#664—#665、#669、#691、#710、#760、#785、#795、#817、#850—#857 等。

## Q093. Trace 中记录哪些信息？

**实现状态：已实现**

**建议回答：**

TraceStore 用 SQLite 保存顶层 run、父子 span、task_id、agent_role、agent_name，以及每次模型尝试和工具调用。

模型侧记录 Provider、Model、输入/输出/缓存 Token、耗时、错误和价格；工具侧记录参数摘要、结果、错误类型、超时、changed_files 和耗时。敏感字段会脱敏，原始私有思维链不持久化。

**代码落点：** `paicli/observability.py`

---

## Q094. 出现 Badcase 时，如何定位是 Planner、Worker、Tool、Context 还是 Reviewer？

**实现状态：已实现基础闭环**

**建议回答：**

先从顶层 run 状态和失败 Task 开始，再沿 parent_span_id 看 Planner 输出、Worker ToolResult、Reviewer verdict、最终 Verification 和回滚记录。

如果计划本身非法看 Planner/PlanValidator；工具参数错看 INVALID_ARGUMENTS；模型多轮不动看停滞签名；Reviewer 误判看实际读取证据；最终测试失败但局部审查通过，则看跨 Task 契约。Trace 让问题可以按层归因，而不是只看最终回答。

**代码落点：** `paicli/observability.py`；`paicli/state.py`；`paicli/evaluation.py`

---

## Q095. Token、成本、耗时和错误率如何统计？

**实现状态：已实现**

**建议回答：**

LlmClient 解析 Provider usage，每次调用写入 Trace；Tool Gateway 记录工具耗时和错误。RunBudget 汇总整个 Planner、Worker、Reviewer 和 Aggregator 的调用。

评测报告聚合 input/output Token、模型/工具调用数、错误数、平均耗时和估算成本。价格可配置；未知价格会标记 unpriced，而不是假装成本为零。

**代码落点：** `paicli/observability.py`；`paicli/evaluation.py`

---

## Q096. 如何限制整个 Team 的全局预算？

**实现状态：已实现**

**建议回答：**

除了每个 Sub-Agent 自己的最大轮数和 Token，RunCoordinator 外层还有父级 RunLimits，可以限制总 Token、总成本、总时长、模型调用数和工具调用数。

所有角色共享同一个 RunBudget，因此某个 Worker 没超限不代表 Team 可以无限扩张；任何角色继续调用前都会消耗父级预算。

**代码落点：** `paicli/observability.py`；`paicli/execution.py`；`paicli/__main__.py`

---

## Q097. PaiCLI 的固定任务评测体系怎么设计？

**实现状态：已实现**

**建议回答：**

固定 Suite 为每个任务声明 mode、初始文件、Prompt、时间和确定性断言。执行时在临时工作区运行真实 Agent，再检查最终回答、文件内容和命令退出码。

报告保留 Git commit、模型、Run ID、changed_files、Token、耗时、错误和成本。普通 CI 用 Fake Client 验证控制流，真实 DashScope 评测显式启用，二者职责不同。

**代码落点：** `paicli/evaluation.py`；`eval/suites/coding-smoke.json`；`reports/README.md`

---

## Q098. 如何公平比较历史版本和当前版本？

**实现状态：已实现**

**建议回答：**

评测器通过 git archive 导出真实历史提交，在临时目录启动旧提交自己的 CLI，而不是用新版本代码模拟旧版本能力。相同 Suite、Provider 和断言分别生成报告，再比较任务成功变化和指标差值。

Phase 5 基线只有 ReAct，Plan/Team 不支持会明确记为失败；这能证明能力覆盖扩展，但不等于完全公平的同功能质量对比。

**代码落点：** `paicli/evaluation.py`；`reports/phase5-dashscope-baseline.json`；`reports/phase5-vs-1.0.json`

---

## Q099. 真实 DashScope 的稳定性结果怎么样？

**实现状态：有真实数据**

**建议回答：**

连续五轮固定任务中，整套 4/5 全成功，单任务 14/15 成功，确定性断言 34/35 通过。ReAct 和 Plan 都是 5/5，Team 是 4/5。

这说明主链真实可运行，但 Team 还不是高可靠生产水平。报告保留了失败轮次，没有只挑最好的一次。

**代码落点：** `reports/dashscope-1.0-stability.json`；`docs/final-acceptance.md`

---

## Q100. 为什么 Team 有一轮失败？

**实现状态：已定位真实机制**

**建议回答：**

两个独立写 Task 分别修改实现和测试，却选择了不同错误消息契约；局部 Reviewer 都可能认为自己负责的文件合理，最终 unittest 才发现不一致。

系统的正确行为是返回 partial、执行回滚或保留现场，而不是声称成功。缺口是 Verification 失败后还不能自动定位并重开相关上游写 Task。

**代码落点：** `reports/dashscope-1.0-run-03.json`；`docs/final-acceptance.md`

---

## Q101. Prompt 调优出现“修好一类、坏了另一类”怎么办？

**实现状态：已具备评测方法，未自动优化**

**建议回答：**

不能靠再改一句 Prompt 然后看单个 Demo。应该固定 Suite 和模型配置，比较任务成功率、断言、Token、工具错误和失败类型，并保留 Badcase。

PaiCLI 已能做报告和版本比较，但不会自动搜索最优 Prompt。当前的正确做法是小步改动、历史对跑、失败样本分类和回归测试。

**代码落点：** `paicli/evaluation.py`；`reports/`

---

## Q102. 如何接入 DashScope 和其他模型？

**实现状态：已实现**

**建议回答：**

所有 Provider 统一成 LlmClient 的 OpenAI-compatible chat/completions 协议。CLI 支持 DashScope、GLM、DeepSeek、StepFun、Kimi 和 vLLM；差异封装在 ProviderConfig 和 Client Factory。

DashScope 使用独立环境变量，并有真实 Chat、Function Calling、ReAct、Plan 和 Team 测试。更换模型主要改配置，不改 Agent Loop。

**代码落点：** `paicli/llm_client.py`；`paicli/model_probe.py`；`tests/test_dashscope_live.py`

---

## Q103. 模型 API 遇到 429、5xx 或网络超时如何处理？是否有 Fallback？

**实现状态：部分实现**

**建议回答：**

RetryingLlmClient 对 408、409、425、429、500、502、503、504 和部分网络错误做有限次数重试，支持 Retry-After 和指数退避。

当前没有自动 Provider Fallback，也没有熔断器；连续失败最终返回明确模型错误并终止对应 Agent。跨 Provider 降级需要先定义模型能力兼容和评测门槛，不能只换一个 URL。

**代码落点：** `paicli/llm_client.py`

---

## Q104. 不同角色能否使用不同模型？

**实现状态：已实现配置能力，效果仍需评测**

**建议回答：**

默认情况下 Planner、Worker、Reviewer 和 Aggregator 共享主 LlmClient；1.1 也允许分别配置角色 Provider。每个角色 Client 都独立进入 Retry、Trace、Token、价格和 Context Window 链路，不需要修改编排状态机。

这个能力可用于强 Planner、Coding Worker、独立 Reviewer和低成本 Aggregator，但当前还没有对不同组合做足够的重复评测，所以不能声称异构模型一定优于共享模型。

**代码落点：** `paicli/bootstrap.py`；`paicli/subagents.py`；`paicli/__main__.py`

---

## Q105. 如何判断性能瓶颈在模型 API 还是 Harness？

**实现状态：已具备诊断数据**

**建议回答：**

先看 Trace 中模型总耗时、工具总耗时、队列和任务跨度。如果模型调用占主要时间，检查调用次数、上下文长度、Prompt、模型档位和缓存；如果工具耗时高，看命令、索引、文件 I/O 和外部 MCP。

当前小任务中 Plan 平均约 79 秒、Team 约 113 秒，模型调用数较多，说明主要优化空间在任务粒度、重复模型轮次和 Reviewer/Worker 交互，而不是本地 DAG 计算。

**代码落点：** `paicli/observability.py`；`reports/dashscope-1.0-stability.json`

---

## Q106. 当前还有哪些不稳定和未完成边界？

**实现状态：明确记录**

**建议回答：**

真实 Team 整套成功率只有 80%；普通 ReAct 代码任务完成门禁仍偏弱；跨 Task Verification 失败不会自动回流；Aggregator 可能在最终总结里加入证据外说明；工具错误分类还需要更细统计。

系统还没有 OS 沙箱、多 Worker 安全并行写、Reviewer 人工校准、RAG 检索黄金集和完整 MCP Streamable HTTP/SSE。角色级模型路由已经具备，但模型组合效果仍缺少稳定性评测。下一版应先补可靠性，不是继续堆角色。

**代码落点：** `docs/final-acceptance.md`；`SECURITY.md`；`reports/dashscope-1.0-stability.json`

---

## Q107. 这个项目最值得讲的工程亮点是什么？

**实现状态：已实现**

**建议回答：**

我会讲“模型提议、代码裁决”的完整闭环，而不是只讲 Multi-Agent。Planner 的 DAG、工具参数、Reviewer verdict 都来自模型，但真正的结构校验、权限、证据、重试上限、回滚、恢复和评测由确定性代码控制。

这个亮点能把 Agent Loop、DAG、Tool Gateway、Reviewer、Trace 和 Eval 串成一条主线，比单独罗列功能更有说服力。

**代码落点：** `ARCHITECTURE.md`

---

## Q108. 开发过程中最难调试的真实问题是什么？

**实现状态：有真实案例**

**建议回答：**

一个典型问题是 Worker 明明读到了真实文件，却在自然语言摘要里错误声称目标函数已经存在，导致下游 Worker 相信摘要而不再写文件。

修复不是继续调 Prompt，而是改变交接协议：依赖结果同时携带 Worker 摘要和成功 ToolResult 的原始观察，发生冲突时以机器证据为准。这体现了为什么 Agent 不能只传模型自述。

**代码落点：** `paicli/orchestration.py`；`docs/phase-10-15-implementation.md`

---

## Q109. 如果面试官问：为什么真实模型不是 100% 成功，你怎么回答？

**实现状态：有真实数据**

**建议回答：**

我不会回避。Harness 能让失败可检测、可定位、可回滚，但不能消除模型随机性。五轮里 Team 有一次跨 Task 契约不一致，最终测试把它拦住并返回 partial。

这说明系统目前的强项是 fail closed 和可观测，弱项是失败后的跨 Task 自动修复。相比声称 100% 稳定，我更愿意给出成功率、失败机制和下一步改进。

**代码落点：** `reports/dashscope-1.0-stability.json`；`reports/dashscope-1.0-run-03.json`

---

## Q110. 下一版最应该优先做什么？

**实现状态：规划项**

**建议回答：**

第一，增加 VerificationFailureRouter，把最终测试错误定位并回流到相关写 Task；第二，为 ReAct 代码任务增加 changed_files 与测试证据门禁；第三，让最终 Aggregator 只基于结构化事实输出。

之后再扩充 20 到 30 个任务的评测集、做工具错误分型、Reviewer 人工校准和 Code RAG 检索评测。优先提升可靠性和证据，不优先增加更多 Agent 角色。

**代码落点：** `docs/final-acceptance.md`；`reports/README.md`

---

# 十、哪些原题没有强行转成 PaiCLI 项目问题

以下类别仍然值得单独准备，但不应该借 PaiCLI 冒充项目经验：

1. SFT、DPO、PPO、GRPO、蒸馏、训练 Loss、GPU 通信等模型训练问题。
2. MySQL、Redis、Kafka、RocketMQ、JVM、Spring Boot 等纯后端八股；PaiCLI 本地状态主要使用 SQLite。
3. FastAPI、SSE、WebSocket、Kubernetes、多机部署等服务化问题；当前 PaiCLI 是 CLI 和本地单进程。
4. 车载、广告、会议转录、企业知识问答等行业方案题；可以迁移 Harness 思路，但不能说 PaiCLI 已在这些业务上线。
5. 算法与数据结构题，应独立刷题，不转换成项目回答。
6. RAG 中已被问到但项目未实现的 Query Rewrite、HyDE、Cross-Encoder Rerank 和检索黄金集，要按“理解但未做”回答。

# 十一、建议的复习顺序

先读下面六条真实调用链，再开始练问答：

1. `CLI -> RunCoordinator -> ReAct / Plan / Team`
2. `AgentLoopEngine -> LLM -> Tool Call -> Tool Result -> CompletionPolicy`
3. `LlmPlanner -> PlanValidator -> DagScheduler`
4. `TaskPacket -> Worker -> Tool Scope -> TaskCompletionPolicy`
5. `Reviewer -> changed_files read evidence -> verdict -> current Task retry`
6. `Run -> Trace / Checkpoint / Snapshot -> rollback / resume`

第二轮重点练最容易被追穿的十题：

- Q005 完整执行链路
- Q011 ReAct Loop
- Q024 DAG 校验
- Q038 Reviewer 是否看真实产物
- Q041 Reviewer 为什么只重做当前 Task
- Q049 Tool 参数校验
- Q065 上下文压缩
- Q073 Code RAG 链路
- Q094 Badcase 定位
- Q100 Team 真实失败

# 十二、维护规则

项目代码继续变化时，不要只改答案文字。每次更新应同时检查：

- 代码落点是否仍存在；
- 默认 CLI 是否真正接入；
- 单元测试和真实模型证据是否仍成立；
- “部分实现/代码里没有”是否需要改变；
- 稳定性报告是否来自同一模型、同一 Suite 和明确 Git commit。
