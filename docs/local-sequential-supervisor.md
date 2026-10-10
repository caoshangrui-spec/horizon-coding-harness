# 本地顺序 Supervisor：安全轮次自动接力

更新：2026-10-10。Horizon 现可在一次 `agent run` 或 `agent resume` 命令内，把长任务按完整
模型—工具轮次切成多个 Worker slice，并在每个持久化安全边界自动完成 Lease 释放、适配器重开
和新 epoch 接力。应用层 Supervisor 还提供一个窄的“父进程已回收子 Worker”入口：只有 Lease
身份、持久化边界和恢复报告同时安全时才接管；任何未决调用都会留在原 Lease 上等待既有恢复
流程。现在同一可信父进程还可执行恢复安全取消：先持久化精确 Worker 停止目标，再终止并同步
回收该子进程，最后释放 Lease 或转入原有副作用恢复。它解决的是长任务的本地顺序接力和一个
直接子进程边界，不引入并行调度、分布式队列或新的模型策略。

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
2. 若原 Lease 仍有效且存在 reservation，保持原 Lease/intent 原样并返回
   `reconciliation_required`；
3. 若原 Lease 已过期，只有精确旧 Lease 身份和父进程回收证据都匹配，才以一个
   `LEASE_ACQUIRED` 原子提升 epoch；这只证明旧 Worker 已被围栏，不证明悬空副作用安全；
4. 过期接管后如存在 reservation，保留全部 intent 并返回带新恢复 Lease 的
   `reconciliation_required`，不运行 reconcile、runner、模型重派或 fallback；
5. 没有 reservation 时，使用现有 `RecoveryService` 对 Run、Campaign 账本、响应 Artifact 和
   事件尾部进行一致性检查；只有 `safe_to_resume=true` 才进入普通顺序 Supervisor；
6. 原 Lease 仍有效的安全路径记录带进程证据的 `LEASE_RELEASED` 后获取新 epoch；原 Lease 已
   过期的安全路径直接复用步骤 3 的新 epoch。边界不安全则返回
   `reconciliation_required` 和 RecoveryReport，不调用模型。

若子 Worker 已提交终态或持久化人工等待并按原协议清除 Lease，则返回 `stopped`，不会重新启动。
这个入口只接受同一个可信父进程同步回收的子进程；父进程自身重启后无法从数据库重建该操作
系统事实，仍使用显式旧 Worker 停止确认的既有恢复路径。若 Run 保存了取消目标，该路径还要
回填 Trace 中的精确 PID。它是 Python application API，尚未把 CLI Supervisor 改造成常驻进程
管理器。

## 5. 取消一个仍在运行的直接子 Worker

可信父进程必须在启动子进程前捕获 `ReapedWorkerBoundary`，并持有直接子进程句柄；需要取消时
调用：

```python
result = supervisor.cancel_and_reap_worker(
    boundary,
    service,
    process,
    key="operator-cancel-...",
)
```

这里的 `process.pid` 必须就是实际 Worker，而不是会再派生解释器的 launcher PID。调用方若使用
包装启动器，必须先解决直接句柄或做 PID 握手；Horizon 不会根据进程名猜测目标，也不会扫描并
终止其他 Python 进程。

固定顺序如下：

1. 复核 Run、Lease ID、Worker ID、epoch、启动事件游标和 AgentSession 没有倒退；不匹配时在
   触碰进程前拒绝；
2. 追加 `CANCEL_WORKER_STOP_PENDING`，把精确 PID/Lease/epoch/启动游标写入 Run；该目标本身是
   终态 blocker，也阻止 active 或 recovery Worker 获取新 Lease；
3. 若进程仍活着，先 `terminate` 并有界 `wait`；超时才 `kill`，随后必须再次 `wait`。已经退出
   的进程也必须调用 `wait` 完成同步回收；
4. 只有 `wait` 返回后才追加 `CANCEL_WORKER_STOPPED`，保存退出码、`already_exited` / `terminate`
   / `kill` 和 Lease 处置方式；仍由父进程持有的精确 Lease 在同一命令中释放；
5. 无未决 effect 时，普通取消门禁自动追加最终 `CANCEL_REQUESTED` 并进入 `CANCELLED`；仍有
   reservation 时保持 `RUNNING + cancel_requested=true`，保留原 intent、释放旧 Lease，只允许
   recovery Worker 分类或结算，绝不重派；
6. Worker 若在栅栏后自行观察到取消并先释放 Lease，父进程仍可写入同步回收 receipt；停止
   receipt 前任何新 Lease 都被拒绝，避免“旧进程尚活、新 epoch 已启动”的竞态。

如果父 Supervisor 在步骤 2 后崩溃，数据库只保存“应停止哪个 PID”，不会伪造进程已退出。
旧 Lease 过期且操作者从操作系统确认旧 Worker 已停止后，现有
`agent reconcile --confirm-old-worker-stopped --confirm-old-worker-pid <pid>` 只在 `<pid>` 等于
`cancel_worker_stop.process_id` 时写入 `operator_confirmed` receipt 并提升恢复 epoch；没有未决
effect 时会直接安全取消，有未决 effect 时继续原 recovery。缺失或错误 PID 始终拒绝接管且不
改变 Trace。

当前边界只覆盖一个直接子进程，不覆盖其任意后代进程或跨主机 Worker；没有进程组/Job Object、
后台 watcher、Provider 主动取消或 daemon。父进程在强制 `kill` 后仍收不到退出结果时，不写
停止 receipt、不释放 Lease，保留持久取消目标供人工排查。

## 6. 输出与 Trace

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

过期 Lease 不先伪造释放事件，而以新的 `LEASE_ACQUIRED` 覆盖旧 epoch，并记录
`takeover_reason=confirmed_reaped_worker_expired`、旧 Lease/Worker/epoch、PID、退出码、启动游标和
接管时事件游标。返回结果的 `lease_transition` 区分 `live_release`、`expired_takeover` 和 `none`。

`CANCEL_WORKER_STOP_PENDING` 与 `CANCEL_WORKER_STOPPED` 保存精确 Lease/PID/启动游标；后者另存
退出码、停止方式和 `parent`、`already_released` 或 `takeover`。因此 Trace 能区分同步回收、
Worker 协作释放以及父进程崩溃后的显式人工确认，不能把三者混写成同一种证据。

## 7. 已验证范围

离线集成测试使用 Scripted Model 完成 `read_file -> replace_text -> submit -> protected validation`：

- 前两个 slice 达到显式上限后，Run 保持 `RUNNING`、Lease 已释放、会话指向下一 iteration；
- 新 Supervisor invocation 获取 epoch 3 并完成任务；
- 三个 Worker 分别重建 Gateway、ArtifactStore、SnapshotManager、RAG 和 Campaign Ledger；
- Trace 中恰有 3 次 `LEASE_ACQUIRED`、2 次 `LEASE_RELEASED`，最终无活动 Lease；
- CLI 自动计划后以两个执行 slice、一次 handoff 完成，源工作区保持不变；
- CLI 先显式让出一个安全轮次、再由 `agent resume --supervise` 以两个 slice 接续成功，最终
  lease epoch 为 3；
- 真实子进程完成一次 `read_file` 轮次、持久化 AgentSession 后以固定非零码退出；父进程同步
  回收它；原 Lease 仍有效和通过注入时钟变为过期两种情况下，Supervisor 都记录对应退出证据，
  从 epoch 1 接管到 epoch 2，再经一次正常 handoff 到 epoch 3 完成编辑、提交和保护验证；两条
  完整 Trace 均可重放；
- 第二个真实子进程留下未决 `read_file` intent 后退出；结果严格为
  `reconciliation_required`：Lease 仍有效时保持原 Lease；Lease 过期时仅提升到恢复 epoch，
  两者都保留 intent，且 reconcile/runner/模型均未调用；
- 第三个真实子进程在首个 AgentSession 形成前退出且没有 reservation；活/过期 Lease 两种路径
  都由 RecoveryService 判为 `unsafe_agent_boundary`，证明“没有悬空调用”本身不足以自动续跑；
- 第四个真实直接子进程持久在线等待；父进程在调用终止前观测到
  `CANCEL_WORKER_STOP_PENDING` 已提交，再完成 terminate、wait、退出证据、Lease 释放和最终
  `CANCELLED`，完整 Trace 可重放；
- 第五个真实子进程先保存 `read_file` intent 再等待；取消只回收进程并释放 Lease，Run 保持
  `reconciliation_required`，普通 Worker 获取 Lease 被拒绝，显式 recovery settlement 后才取消；
- 确定性故障测试覆盖 terminate 超时后 kill、调用前 Lease 身份不匹配零进程操作、进程已提前
  退出、Worker 在栅栏后协作释放 Lease，以及父 Supervisor 崩溃后必须等 Lease 过期并显式确认
  旧 Worker 已停止；
- 领域服务负例证明仍有效的 Lease 和不匹配的旧 Lease 身份都不能使用过期接管入口；成功接管后
  旧 token 被 epoch 围栏，事件 Trace 可重放；
- 全程不联网、不调用真实模型、不产生外部费用。

这些证据证明本地安全轮次的自动接力、父进程确认退出后的保守接管，以及单个直接子 Worker 的
栅栏优先取消；不代表任意进程树、任意崩溃自动恢复、操作系统重启恢复、分布式调度或模型任务
成功率。异常退出后的 unknown 分类、精确副作用处置和人工确认继续由现有恢复协议负责。
