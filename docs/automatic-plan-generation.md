# 一次性自动计划生成与恢复合同

更新：2026-10-04。本文描述已经实现的自动计划入口；它把结构化 `TaskSpec` 转成受控的
WorkItem DAG，不把自然语言意图识别、通用动态重规划或真实长程任务效果冒充为已完成。执行期
另有一次受限 revision，见[执行证据驱动的单次受限 Replan](execution-replanning.md)。

## 1. 目标与边界

执行前不再强制要求人工编写 `plan.yaml`。用户可在已有 TaskSpec 上选择：

- `PLAN_PATH`：继续使用人工计划，行为与旧入口兼容；
- `--auto-plan`：允许一次有预算的模型请求提出 Plan，由控制器验证后才进入执行。

自动计划只负责分解已授权任务，不得扩大路径、工具、验收或预算。首版刻意保持 one-shot：
无效响应会作为负结果留下模型 receipt，并创建与该响应 Artifact、TaskSpec hash 和 Plan version
绑定的人工替换请求；Run 进入 `WAITING_FOR_USER`，而不是直接终止。不自动修提示词、不重试
模型、不切换 Provider。它本身不负责执行期修订；执行模型可在后续主循环中显式提交一次
`revise_plan`，但那是独立的有界事件合同，不是重试初始 Planner。

## 2. 数据流

```text
TaskSpec + immutable source snapshot
        │
        ▼
PlanningContext v1（内容寻址 Artifact）
        │  task hash / path scope / acceptance IDs / bounded path inventory
        ▼
预算预留 → MODEL_CALL_RESERVED(purpose=planning)
        │
        ▼
模型必须调用 propose_plan 一次
        │
        ▼
response Artifact → Run/Campaign 结算
        │
        ▼
Plan Schema + DAG + acceptance coverage + tool policy 校验
        │
        ▼
PLAN_CREATED(source_model_call_id=...) → READY → RUNNING

校验失败 → HUMAN_REQUEST_CREATED → WAITING_FOR_USER → 本机 plan set
                                                │
                                                └→ HUMAN_DECISION_RECORDED
                                                    → PLAN_CREATED → READY
```

规划请求若在 Provider 派发前确定超过单 Run 或 Campaign CNY 硬上限，会写入类型化
`BudgetStop` 并进入 `FAILED`；Campaign-only reservation（若已创建）先以 0 结算。unknown
用量仍进入对账边界，不会被当作确定性费用耗尽。详见[确定性费用停止语义](budget-stop-semantics.md)。

控制器先把源目录恢复到独立 staging workspace。规划模型看到的是 TaskSpec 的标题、目标、任务
类型、执行模式、任务级工具 allowlist、允许/禁止路径、非命令式 requirements、验收
ID/required 标记，以及最多
50 个经过 allow/deny 过滤并排序的文件路径。路径总数和截断标记仍保留；该清单只证明路径存在，
不作为实现位置证据。验收命令正文不进入规划提示，禁止路径和范围外
路径不进入清单。文件内容仍由后续执行阶段通过 `read_file`/`retrieve_code` 获取。

## 3. PlanningContext v1

领域合同位于 [`domain/planning.py`](../src/horizon/domain/planning.py)，核心字段为：

| 字段 | 约束 |
|---|---|
| `task_spec_hash` | 绑定完整 TaskSpec；恢复时必须一致 |
| `workspace_revision` | 绑定初始 immutable snapshot |
| `source_manifest_ref` | 指向初始内容寻址 manifest |
| `allowed_paths` / `denied_paths` | 必须与当前 TaskSpec 完全一致 |
| `acceptance[]` | 只暴露 ID 和 required，不暴露命令正文 |
| `repository_paths[]` | scope 过滤、排序、去重，默认最多 50 条 |
| `repository_path_count` | 过滤后的总数，不能小于实际列表长度 |
| `repository_paths_truncated` | 总数大于可见列表时必须为 true |
| `max_work_items` | 当前固定为 8 |
| `permitted_tools` | execution mode 与可选 TaskSpec `constraints.allowed_tools` 的有序交集，模型不能自报权限 |

完整 Context 使用 canonical JSON 计算 SHA-256。Artifact 引用、Context hash 和
`ModelCallReservation` 三者必须相同；执行型模型调用不能携带 planning context，规划调用也
不能伪装成执行阶段的 ContextProjection/RunMemory 请求。

## 4. 模型输出与控制器验证

请求使用 `temperature=0`、`tool_choice=required`，且只提供一个 `propose_plan` 工具。其参数
Schema 要求 Plan version 1、1～8 个 WorkItem，以及每项的 ID、标题、目标、依赖、预期产物、
验收 ID 和允许工具。

即使 Provider 绕过 JSON Schema，控制器仍重复检查：

1. Pydantic Plan/WorkItem 严格字段合同；
2. WorkItem ID 唯一，依赖存在、无自依赖、无环；
3. 不得虚构验收 ID，所有 required acceptance 必须被覆盖；
4. 每项工具必须是 execution mode 与 TaskSpec 工具 allowlist 的允许子集；
5. Plan version 必须为 1，WorkItem 不超过 8 个；
6. inventory 完整时，标题、目标和 expected artifacts 中出现的路径必须逐字存在于该清单；
   唯一例外是同一 WorkItem 显式授权 `create_file`，且新路径仍落在 TaskSpec allow/deny 范围内；
7. 计划事件必须引用已结算且 `purpose=planning` 的模型调用。

第 6 项只在 `repository_paths_truncated=false` 时作否定判断：完整清单可以证明路径不存在；
截断清单则不能。对于 `create_file`，缺失路径本身是预期状态，因此控制器改为同时要求该
WorkItem 拥有创建权限，且路径通过 TaskSpec 范围检查；父目录存在、目标仍不存在和 UTF-8 字节
上限在实际派发时再次检查。其他缺失或越权路径仍只结算原规划调用并进入人工计划 fallback，
不会自动重试模型。

这条规则只证明“路径存在”，不证明“实现位于该路径”。Planner prompt 仍要求：除非不可变任务
明确写出路径，否则 Plan 应使用通用的 evidence-discovered artifact 描述。执行阶段必须以
revision-bound retrieval/read 回执为准，不能把 Plan 中的路径假设当作代码证据。第六轮 tqdm
真实模型负例证明了这个区别：`tqdm/contrib/itertools.py` 确实存在，但 `tenumerate` 实际位于
`tqdm/contrib/__init__.py`；后续 `retrieve_code("tenumerate")` 正确返回了后者 rank 1。

写任务在未进一步收窄时可分配 `search_repo`、`read_file`、`retrieve_code`、`replace_text`、
`apply_patch`、`create_file` 和 `run_check`；只读/仅计划任务只允许前三个读取工具。TaskSpec 可用
`constraints.allowed_tools` 从这些 mode 工具中声明非空、无重复的子集，Planner Schema 只枚举
该交集，WorkItem 还可继续收窄。字段省略时保持原有权限和历史 TaskSpec 序列化/hash。`submit`
由执行循环统一提供，不需要写入 allowlist 或 WorkItem；Shell、任意 diff、多文件新增、删除/重命名
和网络工具不会因模型提案而出现。

## 5. 预算与费用

规划调用与执行调用共享同一 Run 和 Campaign 上限。派发前仍使用：

```text
reserved_input = 2 * utf8_bytes(canonical_openai_payload) + 1024
reserved_cost  = price(reserved_input, max_output_tokens)
```

默认规划输出上限为 `min(1024, provider.request.max_output_tokens)`。TaskSpec 使用自动计划时
至少需要 2 次模型调用额度：1 次规划，加至少 1 次执行。规划已消费的调用会从执行循环的
最大 iteration 数中扣除；Run 的 token/call 上限和 CNY policy 仍在每次派发前检查。

规划无隐藏重试。Run intent 在派发前持久化 client Trace ID。Provider 派发结果不确定，或
Provider 已返回但 response Artifact/Run receipt 未能可信提交时，Run 与 Campaign 都保持
`unknown` 占用；在明确对账前不得再发第二次规划请求。

## 6. 崩溃恢复

模型响应先保存为不可变 Artifact，再提交 Run receipt，最后结算 Campaign 并写 Plan 事件。
恢复逻辑按证据处理提交窗口：

| 已提交状态 | 处理 |
|---|---|
| 只有 Campaign reservation、没有 Run intent | `agent reconcile` 按派发门之前的证据结算 0 |
| Run intent 无可信响应 | 保留 client Trace ID，标记 unknown，不自动重试 |
| Run response receipt 已有、Campaign 未结算 | 从 Run receipt 补齐 Campaign |
| Run/Campaign 已结算、Plan 事件未写 | `agent resume` 重建相同请求并复用 response Artifact |
| response 已结算但 Plan 非法 | 持久化人工替换请求并进入等待态，不产生第二次费用 |
| 人工请求提交前退出 | 请求、失败响应引用和 `resume_state=PLANNING` 可由事件流重建 |
| 人工 Plan 提交 | 决策、等待态回转、Plan 创建和进入 READY 在同一事务提交 |
| Plan 已写、尚未进入 RUNNING | `agent resume` 从 READY 进入执行 |

复用前会核对 request hash、planning context ref/hash、Provider、模型、预留金额、实际估算费用、
Provider trace ID 和 response Artifact；client Trace ID 保留在历史 intent 供对账。配置或上下文漂移会阻止复用，不会静默发起新请求。

## 7. CLI

人工计划保持兼容：

```powershell
uv run --locked horizon agent run `
  examples/agent-task.yaml examples/agent-plan.yaml `
  --image redis:7-alpine --confirm-paid
```

一次性自动计划：

```powershell
uv run --locked horizon agent run `
  examples/agent-task.yaml --auto-plan `
  --image redis:7-alpine --confirm-paid
```

`PLAN_PATH` 与 `--auto-plan` 必须且只能提供一个。规划或执行异常退出后，先离线对账，再继续：

```powershell
uv run --locked horizon agent reconcile <run-id> --confirm-old-worker-stopped
uv run --locked horizon agent resume <run-id> `
  --image redis:7-alpine --confirm-paid
```

`resume` 支持安全的 PLANNING、READY 和已有会话的 RUNNING 三种边界。旧 Lease 尚未过期时不会
仅凭调用方声明强行接管；必须等 TTL 到期且确认旧进程已停止。

若自动计划不合法，命令退出码为 3，但 Run 不是失败终态。JSON 输出包含
`human_request`、`planning_error` 和可复制的 `next_action`。请求创建、进入等待态和原 Worker
Lease 释放在同一 SQLite 事务完成。准备好符合当前 TaskSpec 的 Plan 后执行：

```powershell
uv run --locked horizon plan set <run-id> .\replacement-plan.yaml
uv run --locked horizon agent resume <run-id> `
  --image redis:7-alpine --confirm-paid
```

`plan set` 只能消费当前唯一的 replacement request；Plan 必须保持请求的 version，覆盖全部
required acceptance，并继续服从 TaskSpec 的 execution mode。决策记录绑定 request ID、Plan
hash 和 TaskSpec hash。人工 Plan 的 `plan_source_model_call_id` 为 null，避免把它伪装成模型产物。
等待期间没有模型调用；`agent reconcile` 会返回 `next_action=provide_replacement_plan`，而不是把
这个已知等待边界误报为未知副作用。

## 8. 当前验证与未完成项

离线 Fake Model 测试已经覆盖：

- 有效双 WorkItem DAG 的预算结算、Plan 事件和 model-call provenance；
- 越权工具提案被拒，重复调用生成器不产生第二次模型请求；
- response 已结算但 Plan 事件前崩溃，reconciliation 判定可恢复并复用原响应；
- 非法 response 创建可重放的人工请求、原子释放 Lease，非法人工计划不会消费请求；
- 本机 `plan set` 原子记录决定并进入 READY，规划模型调用总数仍为 1；
- `agent run --auto-plan` 完成“规划—编辑—提交—受保护验收”，源目录保持不变；
- `agent run --auto-plan` 的非法提案通过 CLI 返回 WAITING 和明确下一动作，而不是 FAILED；
- `agent resume` 从 PLANNING 恢复，规划调用总数仍为 1；
- 文件清单先做 path scope 过滤，再执行数量截断。

这些测试不调用 SiliconFlow、不产生费用，也不证明计划质量或真实 Issue 成功率。当前仍未实现：

- 自然语言意图识别与 TaskSpec 自动生成；
- 读取代码内容后再规划、symbol/vector RAG 规划；
- 自动 replan 触发、第二次修订和专用执行期 Planner；现有能力仅允许执行模型显式提交一次
  受控 vN→vN+1 revision；
- 低置信判断、自由文本澄清、通用工具审批、超时/过期和多用户 HITL；
- 规划质量 benchmark、真实长程 Issue 对照和独立安全复核。

因此准确表述是“可恢复的一次性结构化计划生成入口”，不是“完整自主规划器”。
