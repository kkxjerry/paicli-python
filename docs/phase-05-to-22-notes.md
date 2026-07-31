# Phase 05-22 中文学习笔记

建议每期都按这个顺序看：**先读测试 -> Debug 主流程 -> 再读异常分支**。
下面的“边界”专门说明这一期还没有真正实现什么，避免被类名误导。

## Phase 05-10：协作、安全与能力扩展

| 阶段 | 主流程 | 实现边界 |
|---|---|---|
| 05 Multi-Agent | DAG 角色任务 -> Worker -> MessageBus -> 汇总 | Worker 是 Python 函数，不是多个真实 LLM Agent |
| 06 HITL | 硬策略 -> 风险评估 -> 人工审批 -> 审计日志 | 无图形审批 UI，审计为本地 JSONL |
| 07 并行工具 | tool_calls -> 线程池 -> 按原顺序回灌 | timeout 不能强制终止已运行线程 |
| 08 Provider | provider 名 -> 配置/环境变量 -> OpenAI-compatible Client | 只是多厂商配置工厂，不是自动路由或故障转移 |
| 09 Web | URL 策略 -> HTTP -> HTML 文本提取/SearxNG | SSRF 防护还缺 DNS rebinding 和每次重定向复检 |
| 10 MCP | JSON-RPC -> stdio/HTTP transport -> 工具发现和注册 | 是最小 MCP 客户端，不包含完整协议生命周期 |

## Phase 11-16：上下文、浏览器、Skill 与输出

| 阶段 | 主流程 | 实现边界 |
|---|---|---|
| 11 MCP Resources | list/read -> 缓存 -> @mention 注入；token 协作取消 | 取消不能杀死不合作的阻塞调用 |
| 12 Context | 模式预算 -> 80% 触发 -> 压缩/检索 -> 最终预算检查 | token 是估算，不是模型官方 tokenizer |
| 13 Browser | URL 策略 -> 审批 -> Chrome DevTools MCP 工具 | 配置只生成命令，不会自动启动 Chrome |
| 14 会话/记忆 | endpoint -> TTL 复用；save_memory -> JSONL | 会话管理的是元数据；记忆只在显式工具调用时保存 |
| 15 Skills | 扫描 SKILL.md -> 索引 -> load_skill 懒加载 | frontmatter 是简化解析，不是完整 YAML |
| 16 Renderer | Agent event -> Plain/Inline Renderer -> 终端 | Inline 只是轻量输出，没有真正的键盘交互 TUI |

## Phase 17-22：工程化与产品交互

| 阶段 | 主流程 | 实现边界 |
|---|---|---|
| 17 LSP | write_file -> 按后缀选 Provider -> diagnostics 事件 | Python 只用 ast.parse，不是真正 pyright/pylsp |
| 18 Snapshot | BEFORE/AFTER -> Base64 JSON -> restore | 只快照显式指定文件，不是整仓库备份 |
| 19 Prompt | base -> mode -> project -> skills -> resources -> runtime | 负责稳定组装，不负责 prompt 效果自动评估 |
| 20 Runtime | QUEUED -> RUNNING -> 终态 -> JSON 持久化 | localhost API 无鉴权/TLS，不能直接暴露公网 |
| 21 Images | @image -> 路径/签名/大小校验 -> data URL | 无缩放、压缩、OCR，JPEG/WebP 尺寸未解析 |
| 22 Interaction | REPL -> 命令或 prompt -> Agent -> Renderer | 是标准库 input/print REPL，还不是完整终端产品 |

## 学习标签

中文注释版使用 `phase-05-cn` 到 `phase-22-cn`。例如：

```bash
git switch --detach phase-05-cn
python3 -m unittest discover -s tests -v

git diff phase-05-cn..phase-06-cn
git switch --detach phase-06-cn
```

学习时不要一次读完整项目，只看当期测试和 diff。
