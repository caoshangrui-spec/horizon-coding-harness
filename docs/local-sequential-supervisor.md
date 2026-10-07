# 本地顺序 Supervisor：安全轮次自动接力

更新：2026-10-07。Horizon 现可在一次 `agent run` 或 `agent resume` 命令内，把长任务按完整
模型—工具轮次切成多个 Worker slice，并在每个持久化安全边界自动完成 Lease 释放、适配器重开
和新 epoch 接力。应用层 Supervisor 还提供一个窄的“父进程已回收子 Worker”入口：只有 Lease
身份、持久化边界和恢复报告同时安全时才接管；任何未决调用都会留在原 Lease 上等待既有恢复
流程。它解决的是长任务的本地顺序接力，不引入并行调度、分布式队列或新的模型策略。

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

## 4. 父进程确认退出后的窄接管

本地父进程在启动子 Worker 前用 `ReapedWorkerBoundary.capture(run, token)` 固定 Run ID、Lease
ID、Worker ID、epoch、事件序号和当时的 AgentSession。只有父进程的 `wait` 或 `communicate`
已经返回后，才可把实际 PID 和退出码交给
`SequentialAgentSupervisor.continue_after_reaped_worker(...)`。这是可信本地控制器的调用合同，
不是让模型或远端客户端提交一个 `worker_stopped=true` 布尔值。

接管依次执行以下门禁：

1. 当前 Run 仍对应启动前的精确 Lease/epoch，且 AgentSession 没有倒退；
2. `reservations` 必须为空，包含已标 unknown 的 reservation 也不例外；
3. 使用现有 `RecoveryService` 对 Run、Campaign 账本、响应 Artifact 和事件尾部进行一致性检查；
4. 只有 `safe_to_resume=true` 才记录带 PID、退出码和边界序号的 `LEASE_RELEASED`，重开适配器、
   获取新 epoch，并进入普通顺序 Supervisor；
5. 有未决 model/tool/budget intent 时返回 `reconciliation_required`，保留原 Lease，不运行
   reconcile、不创建新 Worker、不重派模型；无 reservation 但边界仍不安全时，返回同一结果并
   附带 RecoveryReport。

若子 Worker 已提交终态或持久化人工等待并按原协议清除 Lease，则返回 `stopped`，不会重新启动。
当前入口要求父进程在原 Lease 仍有效时及时处理；过期 Lease 继续使用显式
`--confirm-old-worker-stopped` 的既有恢复路径。它是 Python application API，尚未把 CLI
Supervisor 改造成常驻进程管理器。

## 5. 输出与 Trace

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

确认回收后的释放也复用 `LEASE_RELEASED`，并额外记录 `release_reason=confirmed_reaped_worker`、
`process_id`、`exit_code`、`launch_event_seq` 和 `safe_event_seq`。这些字段是父进程观察的审计证据，
不是跨主机存活证明；重放仍以事件 hash 链和 Run 投影为准。

## 6. 已验证范围

离线集成测试使用 Scripted Model 完成 `read_file -> replace_text -> submit -> protected validation`：

- 前两个 slice 达到显式上限后，Run 保持 `RUNNING`、Lease 已释放、会话指向下一 iteration；
- 新 Supervisor invocation 获取 epoch 3 并完成任务；
- 三个 Worker 分别重建 Gateway、ArtifactStore、SnapshotManager、RAG 和 Campaign Ledger；
- Trace 中恰有 3 次 `LEASE_ACQUIRED`、2 次 `LEASE_RELEASED`，最终无活动 Lease；
- CLI 自动计划后以两个执行 slice、一次 handoff 完成，源工作区保持不变；
- CLI 先显式让出一个安全轮次、再由 `agent resume --supervise` 以两个 slice 接续成功，最终
  lease epoch 为 3；
- 真实子进程完成一次 `read_file` 轮次、持久化 AgentSession 后以固定非零码退出；父进程同步
  回收它，RecoveryService 判定安全，Supervisor 记录退出证据并从 epoch 1 接管到 epoch 2，
  随后再经一次正常 handoff 到 epoch 3 完成编辑、提交和保护验证；完整 Trace 可重放；
- 第二个真实子进程留下未决 `read_file` intent 后退出；结果严格为
  `reconciliation_required`，原 Lease/intent 保留，reconcile/runner/模型均未调用；
- 全程不联网、不调用真实模型、不产生外部费用。

这些证据证明本地安全轮次的自动接力，以及父进程确认退出后对一个完整静止轮次的保守接管；
不代表任意崩溃自动恢复、操作系统重启恢复、分布式调度或模型任务成功率。异常退出后的
unknown 分类、精确副作用处置和人工确认继续由现有恢复协议负责。
