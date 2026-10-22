# 本地子进程 Supervisor

更新：2026-10-23。

`agent run` 和 `agent resume` 现在可用 `--worker-process` 把每个有界 Agent slice 放进一个
真正的本地子进程。CLI 父进程持有进程句柄、同步回收该直接子进程，再依据 SQLite Trace 中的
Run/Lease/AgentSession 证据决定是否启动下一 Worker。该开关隐含 `--supervise`；原
`--supervise` 仍保留同进程重建 adapter 的低开销模式。

这项能力解决的是“正式 CLI 仍在父进程里直接跑 Agent Loop”的缺口，不把 Horizon 扩成 daemon、
队列或分布式调度系统。

## 使用

```powershell
uv run --locked --cache-dir .uv-cache horizon agent run `
  examples/agent-task.yaml examples/agent-plan.yaml `
  --image redis:7-alpine --confirm-paid --worker-process

uv run --locked --cache-dir .uv-cache horizon agent resume <run-id> `
  --image redis:7-alpine --confirm-paid --worker-process
```

`--slice-iterations N` 仍控制每个子 Worker 最多执行多少个模型 iteration；省略时为 1。
`--confirm-paid`、TaskSpec/Run/Campaign 预算、Provider 的无重试/无 fallback 规则均不改变。
父进程不会把 API Key 写进子进程命令行；子进程从原配置和 `.env`/环境变量重新读取凭据。

## 运行链

```text
CLI parent
  │ capture exact Run + Lease(epoch N) + AgentSession + event seq
  │ spawn direct child (no shell, no credential in argv)
  ▼
Agent Worker child
  │ reopen SQLite / Artifact / Campaign / RAG / Docker adapters
  │ verify exact launch identity
  │ execute one bounded CodingAgentRunner slice
  │ persist intent, receipt and next AgentSession through normal services
  ▼
CLI parent
  │ communicate/wait -> exact PID + exit code
  │ reload durable Run; exit code alone is never a safe-boundary proof
  │ RecoveryService verifies no pending effect and a recoverable session
  │ persist LEASE_RELEASED with PID/exit/launch/safe event seq
  │ acquire epoch N+1 only when another child will actually run
  └─ terminal / HITL / slice limit / reconciliation_required
```

子进程启动时必须同时匹配：

- `run_id`；
- `lease_id / worker_id / epoch`；
- 父进程捕获的 `launch_event_seq`；
- AgentSession Artifact 引用与 `next_iteration`；
- 空 reservation 集；
- TaskSpec 中的 staging workspace；
- 父进程验证过的 Provider 配置内容 hash，以及该配置对应的 Campaign ledger。

任一字段漂移都会在模型或工具派发前拒绝执行。每个正常交接事件记录被回收的 PID、退出码、
launch cursor 与安全 cursor；下一子进程使用新的 Lease epoch。进程退出码只说明 OS 进程已经
结束，不能替代持久状态检查。

## CLI 输出

启用该模式后，原 `supervision` 字段增加：

```json
{
  "enabled": true,
  "mode": "local_process",
  "disposition": "stopped",
  "slice_iterations": 1,
  "worker_slices": 3,
  "worker_handoffs": 2,
  "slice_limit_reached": false,
  "processes": [
    {"process_id": 12001, "exit_code": 0},
    {"process_id": 12015, "exit_code": 0},
    {"process_id": 12028, "exit_code": 0}
  ],
  "pending_reservation_ids": []
}
```

`disposition` 有三种：

| 值 | 含义 | CLI 结果 |
|---|---|---|
| `stopped` | Run 已终态或进入人工等待 | 由 Run status 决定退出码 |
| `slice_limit_reached` | 本次进程数上限到达，Run 在无 Lease 的安全边界 | `RUNNING`，可再次 `agent resume` |
| `reconciliation_required` | 子进程结束后存在 pending effect，或 RecoveryService 无法证明安全 session | 保留原证据，CLI 退出 3，不启动下一 Worker |

stdout 中的子 Worker 小型诊断只用于汇总 `check_cleanup_failures`；是否续跑完全不信任该输出，
只看持久 Run 与同步 reap 证据。

## 故障语义

| 故障点 | 当前动作 | 明确不做 |
|---|---|---|
| `Popen` 前/过程中启动失败且 Run 仍静止 | 释放该精确 Lease并返回错误 | 不伪造 Worker 已运行 |
| Worker 在新 AgentSession 后以任意退出码结束 | 父进程验证 session 前进与 recovery-safe，记录进程证据后可换 epoch | 不把退出码 0 当作唯一证据 |
| Worker 留下模型或工具 intent | 返回 `reconciliation_required`，保留 intent 和原 Lease | 不重试模型、不重复工具、不 fallback |
| Worker 未产生新 session且无 pending intent | RecoveryService 返回 `unsafe_agent_boundary`，停止 | 不猜测“没有副作用所以可继续” |
| 最后允许的 slice 安全结束 | 记录 reaped release，保持无 Lease `RUNNING` | 不为一个不会启动的 Worker 多拿 Lease |
| Run 已 `SUCCEEDED/FAILED/CANCELLED/WAITING_FOR_USER` | 直接返回持久状态 | 不开启额外 Worker |

`reconciliation_required` 后先检查 `pending_reservation_ids` 和 Run status。普通恢复仍走
`agent reconcile`；具体工具 effect 再走窄化的 `agent resolve-tool` 决策。若旧 Lease 尚未过期，
现有恢复入口仍要求等待 TTL 并明确确认旧 Worker 已停止。父进程已经回收直接子进程这一事实
不会被扩大解释为外部 Provider、Docker 容器或其他后代进程已经停止。

## 已验证

离线真实子进程集成测试覆盖：

- 三个独立 PID 依次执行 `read_file -> replace_text -> submit`，epoch `1 -> 2 -> 3`，最终
  `SUCCEEDED`；父进程自身的模型对象零请求；两个交接事件均绑定实际 PID并可完整 Trace replay；
- 两个 slice 后达到上限，Run 保持 `RUNNING` 且无 Lease，可由后续命令继续；
- 子进程持久化 `read_file` intent 后以 38 退出，父进程返回
  `reconciliation_required`，不调用 RecoveryService、模型或下一 Worker，也不释放/覆盖 intent；
- `--worker-process` 在 `agent run` 中隐含 supervision，并输出 PID/退出码和 disposition；
- 生产 child command 绑定完整启动身份，命令行不包含 API Key。

这些是离线 Scripted Model 与本地进程协议证据，不是付费模型效果、Docker 隔离安全认证或跨主机
容灾验收。

## 严格边界

- 仅管理父 CLI 直接创建的一个子进程；没有进程树/job object 管理。
- 父控制器或操作系统崩溃后，内存中的 `Popen`/reap 事实不可恢复；仍走显式旧 Worker 停止确认。
- pending effect 不自动接纳、回滚、重试或丢弃。
- 没有后台 daemon、任务队列、并行 Worker、跨主机租约或自动重启策略。
- 没有新增任何付费调用授权；运行真实模型仍需用户当次明确 `--confirm-paid`。
