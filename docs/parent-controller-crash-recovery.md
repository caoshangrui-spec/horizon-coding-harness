# 父控制器硬退出后的本地 Worker 恢复

更新：2026-10-23。

本切片解决一个窄但关键的问题：`agent run/resume --worker-process` 的父控制器被强制结束后，
重启的 Horizon 如何识别旧直接子 Worker，并在没有“旧 PID 已停止”的精确证据时拒绝派发。

它不是自动故障转移。Horizon 不扫描、探测或终止任意系统进程；操作者仍需在操作系统侧确认
Trace 中记录的那个 PID 已停止。实现只把这份确认绑定到原 Run、Lease、Worker、epoch、PID 和
launch cursor，防止把另一个进程的停止误用为恢复依据。

## 持久协议

每个本地子 Worker 使用一个随机 `launch_id`，并依次写入三类事件：

1. `PROCESS_WORKER_LAUNCHED`：绑定 `launch_id + PID + Run + Lease + Worker + epoch +
   launch_event_seq`；
2. `PROCESS_WORKER_START_AUTHORIZED`：父进程确认上述启动记录已经持久化，才写入执行授权；
3. `PROCESS_WORKER_STOPPED`：只接受父进程同步 `wait/communicate` 得到的退出码，或恢复时对同一
   PID 的显式 `operator_confirmed` 证据。

Run 投影同时保存一个活动 `process_worker_launch` 和历史
`process_worker_stop_receipts`。事件可从 JSONL Trace 独立重放，旧 Trace 因字段按需出现而保持
原 projection hash。

```text
父进程 Popen(stdin=PIPE)
        │
        ├─ PROCESS_WORKER_LAUNCHED
        ├─ PROCESS_WORKER_START_AUTHORIZED
        └─ stdin 写入精确 launch_id
                     │
                     ▼
             子进程才打开 SQLite、Provider、工具和 workspace
                     │
          正常退出 ─┴─ 父进程同步回收
                     │
             PROCESS_WORKER_STOPPED
                     │
          RecoveryService / Lease handoff
```

子进程在读取到正确的一次性启动闸门前，不打开控制数据库、模型、工具或 workspace。收到闸门后，
它还会从 SQLite 复核活动 launch 的 PID、Lease、epoch、launch cursor 和授权事件。因此：

- 父进程在 `Popen` 后、启动记录前退出：管道关闭，子进程在任何派发前退出；
- 父进程在启动记录或授权后退出：活动 launch 保留，任何新 Lease 都被阻止；
- 父进程放行后退出：旧子进程可能已经派发，既有 reservation/unknown recovery 规则继续生效，
  不因为父进程消失而自动重试；
- 父进程正常同步回收：先写 `PROCESS_WORKER_STOPPED`，再释放或接管 Lease。

## 操作恢复

先查看持久状态：

```powershell
uv run --locked --cache-dir .uv-cache horizon status <run-id>
```

若 `process_worker_launch` 非空，记录其中的：

- `process_id`；
- `launch_id`；
- `lease_id / worker_id / epoch`；
- `start_authorized`；
- `launch_event_seq`。

随后在操作系统侧确认**这个精确 PID** 已停止，并等待旧 Lease TTL 到期。优先先做零模型、零网络
的离线对账：

```powershell
uv run --locked --cache-dir .uv-cache horizon agent reconcile <run-id> `
  --confirm-old-worker-stopped `
  --confirm-old-worker-pid <process_worker_launch.process_id>
```

该命令会先写 `PROCESS_WORKER_STOPPED(stop_evidence=operator_confirmed)`，再取得 recovery Lease。
缺失 PID、PID 不匹配、旧 Lease 仍存活或仍有无法分类的 effect 时，不会启动新 Worker。若报告
`safe_to_resume=true`，后续按正常显式付费授权继续：

```powershell
uv run --locked --cache-dir .uv-cache horizon agent resume <run-id> `
  --image <existing-image> --confirm-paid --worker-process
```

也可在已完成系统侧核实后，用同一组确认参数直接执行 `agent resume`；这会同时进行 Lease takeover
和后续付费模型执行，因此只有确实要继续任务时才使用：

```powershell
uv run --locked --cache-dir .uv-cache horizon agent resume <run-id> `
  --image <existing-image> --confirm-paid --worker-process `
  --confirm-old-worker-stopped `
  --confirm-old-worker-pid <process_worker_launch.process_id>
```

## 失败关闭规则

| 状态 | 行为 |
|---|---|
| 活动 launch，未确认旧 Worker 已停止 | 拒绝新 Lease |
| 活动 launch，只给布尔确认而无 PID | 拒绝，不写事件 |
| 活动 launch，PID 不匹配 | 拒绝，不写事件 |
| PID 匹配但旧 Lease 尚未到期 | 拒绝，不提前围栏仍可能存活的 Worker |
| PID 匹配、TTL 已过、无 pending effect | 写停止证据并进入 recovery-safe session 检查 |
| PID 匹配、TTL 已过、存在 pending effect | 写停止证据但保留 intent，进入显式 reconciliation |
| Trace 无活动 launch | 保留旧的 Lease TTL + 人工停止确认兼容路径；不会伪造 PID receipt |

终态 EvidencePack 现在也把尚未清除的 `active_process_launch_id / active_process_id` 计为
`open_effects_present=true`，避免把“任务已终态”和“宿主 Worker 已确认停止”混为一件事。

## 已验证证据

离线故障注入执行以下真实进程序列：

1. 一个 Scripted Model 轮次先保存可恢复 AgentSession；
2. 独立父控制器启动闸门子进程并持久化 launch/authorization；
3. 测试对父控制器执行硬 `kill`，确认子进程仍存活且模型调用数未增加；
4. 精确停止旧子进程，推进到 Lease 过期；
5. 缺失 PID 和错误 PID 均被拒绝且事件数不变；
6. 正确 PID 写入 operator stop receipt，epoch `N → N+1`；
7. RecoveryService 接受原 AgentSession，随后完成剩余 `replace_text -> submit`，旧
   `read_file` 不重派，最终 `SUCCEEDED`；
8. 完整 Trace JSONL 重放与最终 Run 投影一致。

测试不调用真实模型、网络或 Docker，外部费用为 0。它证明的是一个父控制器进程被强杀后的
单个本地直接子进程恢复协议，不证明掉电/内核崩溃、PID reuse 的操作系统级自动识别、进程树停止、
跨主机接管、后台自动重启、任意外部 effect 的 exactly-once，或真实模型任务成功率。

