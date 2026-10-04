# 首个受控 Coding Agent 闭环：实现说明

更新：2026-10-02。本文描述当前代码里已经运行过的实现，不是未来架构愿景。完整目标和
79 项需求仍分别以[详细开发设计](coding-agent-development-design.md)和
[需求追踪矩阵](requirements-traceability.md)为准。
上下文算法、Artifact 和恢复不变量另见[确定性上下文投影](context-projection.md)；顺序 DAG
调度与原子阶段交接见[多 WorkItem 执行](multi-work-item-execution.md)；执行前的 one-shot 模型
计划与 PLANNING 恢复见[一次性自动计划生成](automatic-plan-generation.md)；执行期 vN→vN+1
修订见[单次受限 Replan](execution-replanning.md)。

## 1. 这个增量解决什么

当前版本把此前分离的模型 adapter、Run 事件流、预算、工具、文件快照和 Docker 验证串成
一个最小可执行闭环：

```text
TaskSpec + 人工 Plan 或受控 one-shot 模型 Plan
        ↓
源目录内容寻址快照 → 唯一 staging 工作区
        ↓
完整 canonical transcript → 有界 ContextProjection Artifact
        ↓
模型提出 typed tool call
        ↓
若证据证明剩余结构失效：单次 revise_plan → 原子 Plan revision + 新 session
        ↓
Run 预算/权限门禁 → Tool Gateway → intent/receipt 事件
        ↓
读取 / 精确替换 / 受保护检查 / submit
        ↓
Harness 最终 Docker 验证 → checkpoint → validation evidence
        ↓
中间通过：原子切换下一 dependency-ready WorkItem
最终通过：最后 WorkItem PASS + Run SUCCEEDED
失败：有界 repair，或保留失败证据后终止
```

它证明了“模型只提议、Harness 掌握副作用和成功判定”的基本数据流。它不证明长程恢复、
真实 Issue 成功率、通用代码编辑能力或安全隔离已经完成。

## 2. 当前模块

| 模块 | 文件 | 责任 |
|---|---|---|
| 循环编排 | [`application/agent_loop.py`](../src/horizon/application/agent_loop.py) | 顺序 DAG 调度、模型迭代、单次受限 replan、工具 observation、submit、验证、repair、终态 |
| 自动计划 | [`application/planning.py`](../src/horizon/application/planning.py)、[`domain/planning.py`](../src/horizon/domain/planning.py) | 有界 PlanningContext、单次计划调用、DAG/权限/验收校验、response 复用 |
| 会话快照 | [`domain/agent.py`](../src/horizon/domain/agent.py) | 完整消息、下一 iteration、工作区 revision 和已覆盖事件序号 |
| 上下文投影 | [`application/context.py`](../src/horizon/application/context.py)、[`domain/context.py`](../src/horizon/domain/context.py) | 完整 transcript 的字符 + 完整请求保守 token 双门视图、工具对完整性和投影证据 |
| 悬空调用恢复 | [`application/recovery.py`](../src/horizon/application/recovery.py) | Run/Campaign 对账、未知副作用分类、恢复处置报告 |
| Run 服务 | [`application/services.py`](../src/horizon/application/services.py) | policy 绑定、模型/工具预留与结算、验证和 WorkItem 事件 |
| Run 投影 | [`domain/run.py`](../src/horizon/domain/run.py) | 从追加事件重建预算、调用、checkpoint、验证和终态 |
| 模型合同 | [`domain/model.py`](../src/horizon/domain/model.py) | 请求/响应、usage、policy、reservation 和 receipt Schema |
| 工具合同 | [`domain/tools.py`](../src/horizon/domain/tools.py) | 工具 intent/receipt、结果、验收结果 Schema |
| 工具网关 | [`tools/gateway.py`](../src/horizon/tools/gateway.py) | 允许工具、参数验证、路径权限、文件边界、Artifact |
| Docker 验证 | [`adapters/sandbox/validation.py`](../src/horizon/adapters/sandbox/validation.py) | 执行 TaskSpec 中控制器拥有的验收命令 |
| Provider | [`adapters/model/openai_compatible.py`](../src/horizon/adapters/model/openai_compatible.py) | SiliconFlow Chat Completions 请求和严格响应解析 |
| Campaign 账本 | [`adapters/persistence/campaign_budget.py`](../src/horizon/adapters/persistence/campaign_budget.py) | 跨 Run 的 CNY 累计费用上限 |
| 候选提升 | [`application/promotion.py`](../src/horizon/application/promotion.py)、[`adapters/workspace/promotion.py`](../src/horizon/adapters/workspace/promotion.py) | Git HEAD/源 revision 绑定、只读 diff、最多 8 个既有文件的显式 promotion 和部分 effect 恢复 |
| CLI 组装 | [`interfaces/cli/app.py`](../src/horizon/interfaces/cli/app.py) | 快照、staging、Run/Lease、Gateway、Agent、结果摘要 |

领域层只依赖 Port，不导入 HTTP、Docker 或 SQLite adapter。Fake Model 与真实 Provider 使用
相同 `CodingAgentRunner`，因此确定性循环测试不需要联网。

## 3. 输入合同和授权

执行入口接收 TaskSpec，以及二选一的计划来源：

- `TaskSpec`：目标、源仓库、允许/禁止路径、验收命令、预算和 model policy；
- `PLAN_PATH`：人工编写的一个或多个 DAG WorkItem；或
- `--auto-plan`：一次有预算的模型调用提出 1～8 个 WorkItem，再由控制器校验。

当前执行前必须满足：

1. repository 为本地目录；
2. TaskSpec 的 `model_policy_id` 与 Provider 配置完全一致；
3. 人工或模型 Plan 均须无环、依赖有效并覆盖 required acceptance；当前按声明顺序串行选择
   ready item；
4. CLI 显式提供 `--confirm-paid` 和已经存在的 Linux Docker image；
5. 执行权限来自 TaskSpec、WorkItem 和 Gateway 的交集，模型响应不能扩大权限。

CLI 先对源目录做内容寻址快照，再恢复到 `.horizon/staging/agent-<uuid>`。模型和容器都不
操作源目录；成功首先只表示 staging 候选通过验收。之后可用只读 `agent diff` 检查候选，
再用带 `--confirm-promote` 的独立命令提升。当前 promotion 接受 1～8 个既有 UTF-8 文件；若
进程只提升一部分，新进程仅在每个目标仍精确等于 before 或 after 时继续。仍不支持新增、
删除、重命名或自动 Git commit。

## 4. 模型调用协议

每次模型调用按以下顺序：

1. 从完整 canonical transcript 生成候选 `ContextProjection`；
2. 用候选消息和当前工具 Schema 生成完整 `ModelRequest`，同时检查字符与保守 token 上限；
3. 将最终投影、字符使用和 `InputTokenBudget` 写入内容寻址 Artifact，并校验回读；
4. 复用同一个 token 上界和冻结 PriceCard 估算 reservation；
5. 在 Campaign ledger 预留 CNY；
6. 在 Run event stream 绑定 request hash、投影 Artifact、源/投影消息数并预留调用；
7. 调用 Provider；
8. 把结构化 `ModelResponse` 写入内容寻址 Artifact并校验回读；
9. 根据 Provider usage 在 Run 与 Campaign 中结算实际 token 和估算费用；
10. 把 assistant tool calls 加入完整 canonical message sequence。

Provider 在“可能已经计费但没有可信回执”的错误上会触发 Run 和 Campaign `unknown`，
保留完整 reservation，并停止当前 Run。当前没有自动 retry，也不自动切换模型。

Run Trace 保存 request hash、provider/model、usage、费用、response ID、provider trace ID 和
finish reason。每个完整且无悬空操作的模型—工具轮次结束后，完整消息序列另存为内容寻址
Artifact，Run 事件只保存 Artifact hash、下一 iteration、工作区 revision 和覆盖的事件序号。
新 Worker 只有在 Artifact、TaskSpec、Plan、工作区 revision 和事件尾部全部一致时才继续。

Campaign 与 Run 使用两个 SQLite 数据库，当前不能跨库形成单一事务。`agent reconcile`
会用 Run-scoped call ID、request hash、reservation 金额、provider receipt 和 response Artifact
做关联校验：Campaign-only 且尚未越过 Run intent 门禁的预留按 0 释放；Run 已有可信 receipt
而 Campaign 仍 reserved 时确定性补交 settlement，并允许新 Worker 消费已存响应。若只有
Run intent、没有可信 receipt，则把 Run/Campaign 标为 `unknown`，绝不自动重派。

当前恢复范围是“安全轮次边界”：`--slice-iterations` 在完整轮次后持久化会话并释放 Lease，
`agent resume` 由新 Worker 重新打开 SQLite 和 Artifact 后继续。reconciliation 能关闭
Provider 派发前和 response receipt 后的两个双库提交窗口，并输出 `resume` 或
`manual_reconciliation`。会话后的单个 `search_repo`/`read_file`/`retrieve_code` 悬空 intent
只有在原响应、
参数 hash、事件尾部和 workspace revision 全部一致，且用户显式授权后，才能取消旧尝试并从
同一响应生成新 call。单个 `replace_text` 或结构化 `apply_patch` 的参数、派发前 manifest 和
当前 workspace 可共同推导 `pre_effect / expected_effect / diverged`；只有前两种能由可信 CLI
显式 accept/rollback，部分写入和外部漂移继续阻塞。`apply_patch` 限制为最多 8 个不同既有
文件，全部 edit 在第一处写入前完成精确前像校验。其他悬空事件、reservation 或 staging 漂移
均在调用模型前拒绝。
若崩溃
发生在 Provider 已可能执行、但 response Artifact/Run receipt 尚未提交的窗口，调用仍只能记为
unknown；工具写入是否完整也不能自动判定，因此仍不是任意崩溃点续跑。

## 5. 工具面

| 工具 | 模型可提供 | Harness 强制规则 |
|---|---|---|
| `search_repo` | 精确子串 | 只扫描允许的 UTF-8 文件；总字节和结果数有界 |
| `read_file` | 相对 POSIX 路径；可选成对的 1-based `start_line`/`end_line` | allow/deny、无 traversal/drive、无 symlink/junction；小文件可整读，大文件必须按最多 400 行读取，单次模型可见输出最多 32 Ki 字符 |
| `retrieve_code` | 查询、最多 8 个 chunk | manifest + path scope 绑定的 FTS5/BM25 EvidencePack；命中回查 Artifact，empty/degraded 分离 |
| `replace_text` | path、old、new、期望出现次数 | 仅已存在 UTF-8 文件；精确计数；字节级临时文件 + 原子替换；失败回滚 |
| `apply_patch` | 1～8 个不同 path 的精确 old/new edit | 全批预校验后才写；每文件原子替换；普通异常回滚，硬退出部分态保持 unknown |
| `run_check` | check ID | 实际 command、timeout 来自不可变 TaskSpec，不接受模型命令 |
| `submit` | 简短摘要 | 只请求最终验证，不能直接把 Run 标记成功 |

每次工具调用先写 `TOOL_CALL_RESERVED`，再派发，最后写 `TOOL_CALL_SETTLED`。receipt 包含
参数 hash、状态、输出 hash/Artifact、前后工作区 revision 和 workspace manifest。工具输出
以内容寻址 Artifact 保存，事件不内嵌无限日志。

当前不向模型开放任意 shell。验收命令虽然能运行 shell/测试程序，但它来自用户提供且已经
冻结的 TaskSpec，只在非 root、禁网、只读容器根目录且资源受限的 Docker 中运行，唯一可写
挂载是 staging 工作区。

工具 intent 和副作用还不是单一原子事务：若进程在文件替换成功后、receipt 提交前崩溃，
事件会显示悬空 intent，staging 中可能已有修改。reconciliation 先持久化
`TOOL_CALL_UNKNOWN`。`search_repo`/`read_file`/`retrieve_code` 可保守取消后重试；
`replace_text` / `apply_patch` 可在完整 manifest 和 live workspace 唯一证明前态或精确后态时
显式接纳/回滚，并把 observation 追加到下一 Agent session。多文件工具只写完一部分时属于
`diverged`，仍保持 unknown。`run_check` 绝不自动重派。Docker 检查把已持久化 tool call ID
确定性映射为容器名，并写入 owner/attempt/image 三个标签；恢复端可用同一镜像精确查询。
已停止 attempt 可由控制器验证并删除；运行中 attempt 只有显式 `--stop-check-sandbox` 才会被
终止。missing 不能证明“从未运行/已经停止”，因此仍要求操作者确认。只有停止证据成立且 live
workspace 与派发前 revision 完全一致，才可写入 `cancelled/discard_check`；下一会话只看到
“未推断 pass/fail”的处置证据。其他无法唯一证明的副作用继续阻塞。详见
[有界多文件精确 Patch](bounded-multi-file-patch.md)。

### 5.1 上下文投影边界

完整 Agent transcript 始终保存在会话 Artifact 中，是恢复和审计的权威记录。模型可见视图
同时受 `max_context_chars` 和 `max_input_tokens` 两个硬上限约束：前者测量投影消息规范 JSON
字符数；后者对包含工具 Schema 和参数的完整 `ModelRequest` 使用
`2 * utf8_bytes + 1024` 保守上界。它可离线重算并用于派发门禁/费用预留，但不是精确
tokenizer 计数或模型窗口探测。初始 system/user 合同总是保留；assistant tool calls 与其全部
tool results 组成不可拆分单元；最近 N 个单元和任何未完成单元保留；更旧的完整单元折叠为
确定性事实清单，保存原单元 digest、工具参数 hash、结果 hash 和最多 240 字符片段。孤立、
重复或错配的 tool result 会直接拒绝。

每次投影记录算法 Schema、字符上限/实际字符数、token 估算算法、完整请求字节数、token
上界/配置上限、近期单元数、完整 transcript digest、源/投影消息数和实际消息。对应模型
reservation 绑定同一个 `InputTokenBudget`。跨 Worker 消费已结算响应时会重新生成并逐字段比较；配置漂移、Artifact 损坏或
消息变化都会阻止继续。MandatoryFactLedger 另行绑定任务、计划、权限、验收、预算、模型
策略、工具 Schema 和 workspace。证据驱动的 Run Memory 从工具事件/Artifact 派生：失败保留
失败，旧 revision 标为 stale，unknown 保持 unresolved，模型 `submit` 声明不能升级为事实；
恢复按原请求事件边界重建。当前实现是 conservative-token-ceiling-aware，但仍不是精确
tokenizer 或模型生成的语义摘要，也不是跨 Run 的 Project Memory。详见
[上下文投影](context-projection.md)与[Run Memory](run-memory.md)。
另行接入的词法 Code RAG 只提供带来源片段，不能宣称语义等价或真实任务召回率已验证。

## 6. 验证和 repair

模型可以主动调用 `run_check` 获取反馈，但 `submit` 后 Harness 仍会再次执行所有 required
checks。成功条件同时要求：

- 所有 required checks 返回结构化结果且通过；
- 当前 workspace revision 与验证证据绑定；
- checkpoint manifest 可重新校验；
- 当前 WorkItem 已标记通过；
- 没有未结算预算或调用。

验证失败时，Harness 记录证据并进入 `VALIDATING → REPAIRING → RUNNING`，增加一次 repair
预算，把有界失败信息回送模型。超过 `max_repair_cycles` 或模型迭代上限会失败终止，最后
候选和证据仍保留。模型无法修改 TaskSpec 中的 acceptance，也无法用自然语言绕过验证。

## 7. 预算

当前同时检查：

- TaskSpec：模型调用数、工具调用数、steps、input/output token、repair、墙钟和旧 USD 字段；
- Run CNY policy：本 Run 的模型费用占用；
- Campaign CNY ledger：所有已授权联调 Run 的累计占用；
- Provider config：单请求输出上限、模型 ID、thinking/fallback 设置。
- Context policy：字符硬上限、完整请求保守 input-token 硬上限，以及近期工具单元的软保留数。

`reserved + settled + unknown` 都占硬上限。真实回执超过预留时先记录真实用量，再让 Run
失败；不能为了“预算通过”丢弃实际费用。TaskSpec 的 `max_cost_usd` 尚未迁移为通用多币种
合同，当前 CNY Run policy 是显式 overlay。

## 8. 执行命令

先做完全离线检查：

```powershell
uv run --locked --cache-dir .uv-cache horizon model check
uv run --locked --cache-dir .uv-cache horizon model budget
uv run --locked --cache-dir .uv-cache horizon task validate examples/agent-task.yaml
```

确认源目录、任务、预算和本机镜像后，真实执行：

```powershell
uv run --locked --cache-dir .uv-cache horizon agent run `
  examples/agent-task.yaml examples/agent-plan.yaml `
  --image redis:7-alpine --confirm-paid
```

若不提供人工 Plan，可额外消费一次受同一预算约束的规划调用：

```powershell
uv run --locked --cache-dir .uv-cache horizon agent run `
  examples/agent-task.yaml --auto-plan `
  --image redis:7-alpine --confirm-paid
```

按安全轮次切片并在新进程继续：

```powershell
$partial = uv run --locked --cache-dir .uv-cache horizon agent run `
  examples/agent-task.yaml examples/agent-plan.yaml `
  --image redis:7-alpine --slice-iterations 1 --confirm-paid | ConvertFrom-Json
uv run --locked --cache-dir .uv-cache horizon agent resume $partial.run_id `
  --image redis:7-alpine --confirm-paid
```

正常切片会主动释放 Lease。若旧进程异常退出，必须等 Lease 过期并确认旧进程确实停止，才可
额外提供 `--confirm-old-worker-stopped`；该参数不会绕过仍存活的 Lease 或悬空操作检查。

异常退出后的离线对账不需要 API Key、Docker 或付费确认：

```powershell
uv run --locked --cache-dir .uv-cache horizon agent reconcile <run-id> `
  --confirm-old-worker-stopped
```

输出同时给出每个调用的 classification/action、`safe_to_resume`、`next_action` 和 Campaign
摘要。只有可信 Run receipt 能自动补齐 Campaign；未知调用保持预算占用并要求人工对账。

若报告中唯一悬空调用是只读 `search_repo`/`read_file`/`retrieve_code`，检查 staging 后可以
提交显式决定：

```powershell
uv run --locked --cache-dir .uv-cache horizon agent resolve-tool `
  <run-id> <tool-call-id> --retry-readonly
```

该命令不读取 API Key、不联网、不调用模型。它拒绝写工具、验证工具、多个 tool call、参数不匹配
和 workspace drift；成功后仍需另行执行带付费确认的 `agent resume`。

若唯一悬空调用是当前 WorkItem 授权的 `run_check`，优先用执行时的本机镜像查询该 tool call ID
对应的标签容器：

```powershell
uv run --locked --cache-dir .uv-cache horizon agent resolve-tool `
  <run-id> <tool-call-id> --discard-check --image redis:7-alpine
```

若容器仍运行，命令拒绝；只有操作者追加 `--stop-check-sandbox` 才终止并删除这个名称、owner、
attempt 和 image 全部匹配的容器。若容器 missing，缺失本身不是停止证明，人工核实旧进程后改用
`--confirm-check-sandbox-stopped`。控制器还会在任何 Docker 操作前复核原模型响应、check ID、
当前 WorkItem acceptance scope、事件尾部和 workspace revision，并在提交处置时再次复核；任一
不符继续保持 unknown。成功处置会原子写入 cancelled receipt 与下一 AgentSession。

精确写入可选择一种处置：

```powershell
uv run --locked --cache-dir .uv-cache horizon agent resolve-tool `
  <run-id> <tool-call-id> --accept-replace
# 或 --rollback-replace
```

成功 Run 的候选提升分成只读计划和显式执行：

```powershell
uv run --locked --cache-dir .uv-cache horizon agent diff `
  <run-id> --source <原始仓库目录>
uv run --locked --cache-dir .uv-cache horizon agent promote `
  <run-id> --source <原始仓库目录> --confirm-promote
```

promotion intent 在源文件写入前持久化；如果进程在完整效果之后、receipt 之前退出，重跑会
识别 source 已等于候选而只补 receipt。若最多 8 个目标只完成一部分，只有每个文件仍精确等于
计划 before/after 且无额外漂移时才继续剩余写入。源 revision、候选 revision、TaskSpec 路径
权限和（源路径本身为 Git 根时的）HEAD 必须全部匹配。命令不创建 commit。

命令输出包含 Run ID、状态、staging 路径、源目录是否未变、usage、费用、调用数、验证、
checkpoint、未决人工请求/下一动作和 Campaign 摘要。失败或 WAITING 退出码为 3；配置/合同错误
为 2。NoProgress WAITING 可先执行 `horizon agent guide <run-id> <guidance.txt>`，再 resume；详见
[人工指导恢复](operator-guidance.md)。

导出和离线重放：

```powershell
uv run --locked --cache-dir .uv-cache horizon trace export <run-id> `
  --output .horizon/traces/<run-id>.jsonl
uv run --locked --cache-dir .uv-cache horizon trace replay `
  .horizon/traces/<run-id>.jsonl
```

Trace 文件拒绝覆盖已有文件。重放不会调用模型、工具或 Docker。

## 9. 当前验证证据

2026-10-04 当前环境：

- 离线全量回归：304 passed、6 skipped；
- 随后指定本机已有 `redis:7-alpine` 单独复跑跳过项：6 passed，均为真实 Docker 合同；
- Fake Model E2E：精确编辑后成功，以及首次验收失败后一次 repair 成功；
- 自动计划 Fake E2E：Plan provenance/预算、越权拒绝且不重试、Plan 事件前崩溃复用响应，
  以及 CLI 从 PLANNING 恢复后完成保护验收；
- 顺序双 WorkItem E2E：dependency-ready 调度、原子阶段交接、跨 Worker 续跑，以及后项回归
  前项 required check 时必须 repair；
- 执行期 replan Fake E2E：NoProgress 证据后成功修订、完成项不可变、非法提案回退、单次上限、
  新 session/工具记账原子发布，以及 JSONL Trace 等价重放；
- 子进程硬退出覆盖双账本提交窗口、response Artifact 续跑、单/多文件精确写副作用，以及完整
  和部分 promotion effect 窗口；
- 确定性上下文测试覆盖预算内恒等、旧完整单元折叠、孤立工具拒绝、近期溢出拒绝、完整
  transcript 保留，以及压缩后的 pending response 跨 Worker 恢复不重复计费；
- 词法 Code RAG 测试覆盖 scope/revision 隔离、FTS/scan 降级、EvidencePack、源 Artifact
  回查、派生缓存篡改拒绝与裁剪重建；Scripted Agent 可实际调用该工具；
- 来源门验证后的 tqdm 82 files 与 youtube-dl 872 files 完整 checkout A/B：两个初始断言均失败，
  Code RAG 对两个目标生产文件均返回 rank 1，Baseline 2/2 等待、Treatment 2/2 单次 replan 后
  成功；73/82 与 870/872 文件可索引，degraded 不隐藏；
- youtube-dl 872 files 两阶段 A/B 在 production 项通过后释放 epoch 1 Lease，以重新打开的
  SQLite/ArtifactStore/检索库/预算账本和 epoch 2 Worker 执行 regression 项；Baseline 86-event
  Trace 等待，Treatment 116-event Trace 成功，两个 Trace 均可重放且源 checkout 未变化；
- 确定性恢复 E2E 验证 response Artifact 被新 Worker 消费，原模型调用没有重派或重复计费；
- 真实 Run：5 次模型调用、6 次工具调用、5,043 input / 433 output tokens、0 repair；
- Run 模型估算 CNY 0.019026；累计 Campaign 估算 CNY 0.020391；unknown 0；
- Docker 验收通过，源 fixture revision 未变化；
- 真实 Docker 合同另验证 tool call ID 派生标签：正常检查结束后容器不存在；独立验证 Worker
  在 60 秒检查运行中被强制终止后，容器仍可由新 Sandbox 精确识别、停止和删除，owner 不匹配
  会拒绝；
- 导出 JSONL 与 SQLite 的 projection hash 均为
  `3ceff0329f6ba0571429407a2f347b6bd25136c0221e146c4c9a197f15466bd1`。

金额来自冻结 PriceCard 的本地估算，不是供应商最终账单。单 fixture 成功不能推断真实任务
成功率，也不能替代独立安全审核。

## 10. 下一开发顺序

真实子进程现已覆盖 Docker attempt 的 running、容器创建前，以及命令完成并清理后但 receipt
未提交三个窗口。后两个窗口在重启后都表现为 `missing`，证明 missing 不能区分“从未执行”与
“已执行并清理”；因此仍保持人工确认，暂不增加持久启动回执。接下来的顺序为：

1. 在现有确定性投影、MandatoryFactLedger 和 Run Memory 上增加 tokenizer-aware 预算；
2. 现有外部盲测仍为 Hit@1 0/3；先冻结新的 holdout，再按重复出现的负结果决定是否做 symbol/vector 增量；
3. 为现有 Run Memory 增加文件级依赖重检，再设计显式 Project Memory promotion/revoke；
4. 在现有 Plan replacement 与 NoProgress guidance 两条窄 HumanRequest/Decision 上，只有真实
   任务需要时再抽象通用审批；
5. 加入统一 retry/breaker 和仅预授权的 fallback；
6. 冻结真实任务集后再做长程、恢复、成本和消融评测。

每一步继续区分：代码存在、确定性测试、真实运行、独立复核和 benchmark 效果，不能互相
替代。
