# 本地顺序 Supervisor：安全轮次自动接力

更新：2026-10-07。Horizon 现可在一次 `agent run` 或 `agent resume` 命令内，把长任务按完整
模型—工具轮次切成多个 Worker slice，并在每个持久化安全边界自动完成 Lease 释放、适配器重开
和新 epoch 接力。它解决的是“已有可恢复轮次仍需人工反复执行 resume”的操作缺口，不引入并行
调度、分布式队列或新的模型策略。

## 1. 使用方式

真实模型入口仍要求原有的明确付费确认，Supervisor 不增加任何预算或权限：

```powershell
uv run --locked --cache-dir .uv-cache horizon agent run `
  examples/agent-task.yaml examples/agent-plan.yaml `
  --image redis:7-alpine --confirm-paid --supervise
```

默认每个 Worker 执行一个模型 iteration。需要降低交接频率时可显式设置：

```powershell
uv run --locked --cache-dir .uv-cache horizon agent resume <run-id> `
  --image redis:7-alpine --confirm-paid --supervise --slice-iterations 2
```

没有 `--supervise` 时，既有行为保持不变：省略 `--slice-iterations` 会由当前 Worker 持续执行，
指定该参数则在一次安全切片后返回，由操作者决定何时 resume。

## 2. 接力协议

Supervisor 只在 `CodingAgentRunner` 正常返回以下状态时接力：

```text
RUNNING
+ 无未结算 reservation
+ AgentSession.next_iteration 严格前进
+ session Artifact / event cursor / workspace revision 已持久化
```

一次接力顺序固定为：

```text
epoch N Worker
  -> 执行有界 iteration
  -> 保存新 AgentSession
  -> 释放 epoch N Lease
  -> 重新打开 SQLite / ArtifactStore / Snapshot / RAG / Budget Ledger / Tool Gateway
  -> 获取 epoch N+1 Lease
  -> 从持久会话继续
```

Supervisor 不把内存对象当作恢复来源。新 slice 会从 SQLite EventLog 和内容寻址 Artifact 重建
当前 Run、活动 WorkItem、上下文、Memory、工具权限和 Campaign 账本。模型调用数、工具调用数、
费用、repair 次数和 wall-clock 截止时间仍属于同一个 Run，不会因 Worker 接力归零。

## 3. 停止条件

Supervisor 在以下任一条件成立时立即停止，不自动重试或 fallback：

| 状态 | 行为 |
|---|---|
| `SUCCEEDED` / `FAILED` / `CANCELLED` | 返回终态；终态投影清除 Lease |
| `WAITING_FOR_USER` | 返回持久化 HumanRequest；不越过人工决策 |
| Supervisor slice 上限 | 释放当前 Lease，返回可继续的 `RUNNING` |
| 已处理的 Horizon 控制器错误 | 仅在无未决副作用时尽力释放 Lease，然后原样报错 |
| 未预期异常或进程硬退出 | 不伪造安全交接；保留 Lease / intent，进入既有 reconcile 流程 |

最后一条是安全边界：Supervisor 只自动化“已证明静止”的交接。它不会因为捕获到异常就推断旧
Worker 没有产生副作用，也不会自动接受、回滚或重派 unknown 调用。

## 4. 输出与 Trace

`agent run` 和 `agent resume` 的 JSON 增加：

```json
{
  "supervision": {
    "enabled": true,
    "slice_iterations": 1,
    "worker_slices": 3,
    "worker_handoffs": 2,
    "slice_limit_reached": false
  }
}
```

每次接力仍使用既有 `LEASE_RELEASED` 和 `LEASE_ACQUIRED` 事件，因此 epoch、Worker 身份、会话
游标、预算和最终状态可由普通 JSONL Trace 重放，不增加旁路状态文件。多个 slice 的 Docker
清理失败会合并到现有 `check_cleanup_failures` 字段。

## 5. 已验证范围

离线集成测试使用 Scripted Model 完成 `read_file -> replace_text -> submit -> protected validation`：

- 前两个 slice 达到显式上限后，Run 保持 `RUNNING`、Lease 已释放、会话指向下一 iteration；
- 新 Supervisor invocation 获取 epoch 3 并完成任务；
- 三个 Worker 分别重建 Gateway、ArtifactStore、SnapshotManager、RAG 和 Campaign Ledger；
- Trace 中恰有 3 次 `LEASE_ACQUIRED`、2 次 `LEASE_RELEASED`，最终无活动 Lease；
- CLI 自动计划后以两个执行 slice、一次 handoff 完成，源工作区保持不变；
- CLI 先显式让出一个安全轮次、再由 `agent resume --supervise` 以两个 slice 接续成功，最终
  lease epoch 为 3；
- 全程不联网、不调用真实模型、不产生外部费用。

这些证据证明本地安全轮次的自动接力，不代表任意崩溃自动恢复、操作系统重启恢复、分布式
调度或模型任务成功率。异常退出后的 unknown 分类、精确副作用处置和人工确认继续由现有恢复
协议负责。
