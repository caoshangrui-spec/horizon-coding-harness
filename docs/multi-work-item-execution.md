# 可恢复的顺序多 WorkItem 执行

更新：2026-10-02。本文描述 Horizon 当前已经接入 Agent 主循环的多阶段执行切片。它使用既有
Plan DAG 和 Run EventLog 顺序执行多个工作项，目标是让长任务具备真实的阶段边界、权限切换和
跨进程恢复，而不是引入并行调度器或动态规划框架。

## 1. 当前能力与边界

已实现：

- `Plan.items` 可包含多个有依赖关系的 WorkItem；
- 调度器从 `Plan.ready_items(passed_items)` 中按计划声明顺序选择第一个可运行项；
- 每个 WorkItem 使用独立的初始任务合同、AgentSession、允许工具和 acceptance IDs；
- MandatoryFactLedger 绑定当前项及已完成项集合，Run Memory 保留每条工具证据的来源 WorkItem；
- 中间项通过后，以单个 SQLite 事务完成“标记通过、回到 RUNNING、发布下一项会话”；
- 进程可在该安全边界让出 Lease，由新 Worker 从下一 WorkItem 继续；
- 最后一项以单个事务完成“标记通过、Run SUCCEEDED”；
- 最后一项提交时重跑全部 required acceptance，防止后续阶段回归早期已通过行为。
- 执行模型可显式提交一次受限 Plan revision；已通过 WorkItem 必须逐字段保持不变，剩余 DAG
  与新 session 原子替换。

明确未实现：

- 并行 WorkItem、多个 Worker 同时修改同一 staging workspace；
- 控制器自动触发、第二次或无界动态 replan；当前只有一次模型显式、控制器验证的 revision；
- 每个 WorkItem 独立费用子预算、独立 checkpoint 分支或 Execution Fork；
- 任意 diff、多文件新增、删除/重命名或 merge；`create_file` 每次只允许一个新文件，结构化
  `apply_patch` 仅支持最多 8 个既有文件；
- 把通过一个小型 fixture 推断为真实长程任务成功率。

## 2. 确定性调度

Plan 继续由控制器验证：ID 唯一、依赖存在、无自依赖、无环，且所有 required acceptance 至少
被一个 WorkItem 覆盖。运行时选择规则为：

```text
ready = [item for item in plan order
         if item not in passed_items
         and item.dependencies subset-of passed_items]
active = ready[0]
```

如果持久会话已存在，则 `AgentSession.work_item_id` 是 active item 的恢复绑定；该项必须仍未通过
且依赖仍满足，否则拒绝继续。没有会话时才按上面的规则选择。存在多个并行可就绪项时仍只选
第一个，因此结果可重放，但不宣称并行执行。

`WorkspaceToolGateway.activate_work_item()` 在每次启动/恢复及阶段切换时重置当前项。Plan 已先
保证 WorkItem `allowed_tools` 不超过 execution mode 与可选 TaskSpec 工具 allowlist 的交集；
模型可见 Tool Schema 再只包含该项工具（另加 `submit`）。模型主动 `run_check` 也只能选择该项
的 acceptance ID。Provider 请求的 tool schema hash 随项绑定到 MandatoryFactLedger，跨项错误
恢复不会静默沿用旧权限。

## 3. 工作项会话与上下文

每个新 WorkItem 从新的两消息 canonical 前缀开始：

```text
system: bounded coding-agent rules
user: task objective + active WorkItem + completed_work_items
```

旧 WorkItem 的完整聊天不会复制进新会话；相关、已观察的工具结果通过 Run Memory 有界注入。
这样阶段切换能主动切断无关对话增长，同时保留 EventLog、旧 AgentSession Artifact 和工具输出
Artifact 供审计。

MandatoryFactLedger schema v2 增加：

- `completed_work_item_ids`：按 Plan 声明顺序排列；
- `completed_work_items_hash`：上述 tuple 的规范 SHA-256；
- 当前 `work_item_id/work_item_hash` 仍单独绑定，且不能出现在已完成集合中。

RunMemoryEntry 的 `work_item_id` 表示证据实际产生阶段；RunMemorySnapshot 的 `work_item_id`
表示此次模型请求的当前阶段。快照可包含早期阶段条目，workspace revision 变化后它们会按现有
保守策略变为 stale。

## 4. 分阶段验证与最终回归

中间 WorkItem 提交时，控制器执行该项 `acceptance_ids`。这些检查来自 TaskSpec，模型不能提供
命令。为了允许增量开发，尚未轮到的 required checks 不会阻止中间项通过。

最后一个未完成 WorkItem 提交时，执行：

```text
final_check_ids = active_item.acceptance_ids union all task.required_acceptance_ids
```

只有当前项 acceptance 和全部 required checks 都通过，Run 才能成功。因此后续项即使通过自己
的检查，只要破坏早期 required 行为，就会进入既有 `VALIDATING -> REPAIRING -> RUNNING` 闭环。
测试已覆盖“第二项通过自身检查但破坏第一项检查”，Agent 收到失败证据、修复后再次提交。

每次 protected validation 前仍创建当前 workspace checkpoint；workspace revision 改变会使旧
validation 记录失效，但不清除已完成的阶段进度。最终 required 全量回归负责阻止带回归的
候选进入 SUCCEEDED。

## 5. 原子阶段边界

中间项通过后，`advance_work_item_and_save_session` 在同一个 EventStore command/SQLite 事务中
追加：

```text
WORK_ITEM_PASSED(current)
STATE_CHANGED(VALIDATING -> RUNNING)
AGENT_SESSION_SAVED(next item, next global iteration)
```

下一会话 `covered_event_seq` 精确覆盖前两个事件，`AGENT_SESSION_SAVED` 是恢复锚点。事务失败时
三条事件均不可见；事务成功后，即使进程立即退出，新 Worker 也能从下一项的两消息前缀继续。
普通 `transition(VALIDATING -> RUNNING)` 被应用服务拒绝，避免跳过原子交接。

最后一项使用 `pass_work_item_and_succeed` 在一个事务中追加：

```text
WORK_ITEM_PASSED(final)
STATE_CHANGED(VALIDATING -> SUCCEEDED)
```

这同时关闭了旧单项实现中“WORK_ITEM_PASSED 已提交但 SUCCEEDED 尚未提交”的崩溃窗口。

模型 iteration、TaskSpec usage 和 Campaign 费用仍是 Run 全局累计，不会在阶段切换时归零。
因此多阶段计划必须在原始 `max_model_calls/max_tool_calls/max_steps/max_run_cost` 内完成；阶段增加
不构成额外预算授权。

## 6. CLI 与恢复

`horizon agent run` 不再限制 Plan 只能有一个 WorkItem；它以首个 dependency-ready 项构造
Gateway。`agent resume` 和 `agent resolve-tool` 从持久 AgentSession 恢复 active item，而不是
固定使用 `plan.items[0]`。

安全切片仍使用：

```powershell
horizon agent run task.yaml plan.yaml --image <existing-image> `
  --slice-iterations 2 --confirm-paid
horizon agent resume <run-id> --image <existing-image> --confirm-paid
```

若不希望人工逐次执行 `resume`，可启用本地顺序 Supervisor：

```powershell
horizon agent run task.yaml plan.yaml --image <existing-image> `
  --confirm-paid --supervise --slice-iterations 1
```

它只在新 AgentSession 已持久化、Run 无未结算 reservation 的安全边界释放 Lease，随后重开
Worker 所属 adapter 并取得新 epoch；终态、人工请求和异常恢复点会停止。它不增加模型调用、
费用或权限预算，也不自动处置 unknown。完整合同见
[本地顺序 Supervisor](local-sequential-supervisor.md)。

如果切片正好落在工作项交接后，返回的 Run 保持 `RUNNING`，`passed_items` 已包含旧项，
AgentSession 指向下一项。恢复会重新激活下一项 Tool Schema，重建 Fact Ledger、Run Memory 和
ContextProjection 后才允许新的模型调用。

## 7. 已验证证据

离线 Scripted Model E2E 覆盖：

- 两个有依赖项按顺序完成，当前 WorkItem ledger 序列正确；
- 第三次模型调用开始时会话已切到第二项，并绑定第一项已完成集合；
- 第二项首次模型请求可召回第一项 active Run Memory，修改后旧项证据变 stale；
- 中间项只运行自己的验收，最终项重跑全部 required checks；
- 最终项回归早期检查时不会误报成功，会进入 repair 并在修复后完成；
- 在原子工作项边界释放 Lease、重新打开 SQLite/ArtifactStore、故意用第一项初始化 Gateway，
  新 Runner 仍从持久的第二项恢复并成功完成；
- 第一项通过后修订剩余 WorkItem 时，完成项及 completed-memory 绑定保持不变，新项继续通过
  最终全量 required checks；
- 所有模型和工具用量继续累计在同一个 Run/Campaign。
- youtube-dl bug 3 的 872 文件完整 checkout 已执行 production → regression-test 两阶段 A/B；两条
  arm 都在第一项通过后释放 epoch 1 Lease、重新打开 SQLite/ArtifactStore/检索库/预算账本，
  再由 epoch 2 Worker 继续。Baseline 按合同等待，Treatment 单次 replan 后 2/2 checks 通过；
  86/116-event Trace 均可重放，详见[完整 checkout 多阶段 pilot](multi-stage-full-checkout-pilot.md)。

离线单元/集成测试本身不联网、不调用付费模型；上述完整 checkout 证据额外运行了本机已有的
禁网 Docker 镜像，但仍由冻结脚本指定动作，不是实际模型自主完成。第一次大仓重启暴露默认
60 秒 Lease 在冷缓存复核期间到期，fencing 正确拒绝；失败 partial Trace 已保留，重启 Lease
显式改为 600 秒后复跑通过。后续已为 `run_check` 增加自然退出停止容器的精确结果恢复；仍在
运行、信号/超时/OOM、日志超限或 missing attempt 不会被猜测为结果。没有真实失败分布前不增加
队列服务、分布式锁、多 Agent、自动第二次 replan 或并行。
