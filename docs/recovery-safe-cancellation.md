# 恢复安全取消

## 1. 解决的问题

取消不是简单地把 Run 状态改成 `CANCELLED`。当模型请求、文件工具或 Docker 检查已经派发时，
外部 effect 可能仍在发生；若控制器先进入终态并清除 Lease，迟到 receipt 和 unknown effect 将
无法再结算，Trace 也会错误地表现为“取消后没有副作用”。

Horizon 因此把取消拆成两个可重放阶段：

```text
无在途 effect
  cancel -> CANCEL_REQUESTED -> CANCELLED

有在途 effect
  cancel -> CANCEL_PENDING + cancel_requested=true
         -> receipt / unknown classification / explicit tool recovery
         -> CANCEL_REQUESTED(recovery_safe=true) -> CANCELLED

有受 Supervisor 管理的直接子 Worker
  cancel -> CANCEL_WORKER_STOP_PENDING（先落盘）
         -> terminate -> bounded wait -> optional kill -> wait
         -> CANCEL_WORKER_STOPPED + exact Lease fence
         -> 无 effect 则 CANCELLED；有 effect 则进入原 recovery
```

`CANCEL_PENDING` 不是新的 RunStatus。Run 保留原阶段，便于现有 recovery API 使用精确 Lease 和
原 reservation 完成收尾；`cancel_requested=true` 是独立的持久派发栅栏。

## 2. 不变量

1. `cancel_requested=true` 后，active gate 拒绝 reserve、transition、模型调用、工具调用、Lease
   续期和其他新工作，错误类型为 `RunCancellationRequested(run_id)`。
2. recovery gate 只允许既有 effect 的 receipt、unknown 分类、显式工具处置及 Lease 收尾；它
   不绕过派发栅栏。
3. 未分类 reservation 会阻止终态。已分类为 unknown 的普通/模型 reservation 可按保守占用进入
   `CANCELLED`；tool reservation 即使已标 unknown，仍须保留到副作用被精确处置。
4. 最后一个安全 blocker 被结算后，控制器自动追加
   `CANCEL_REQUESTED(recovery_safe=true)`；终态清除 Lease，但不抹去预算、unknown 或工具证据。
5. 多工具模型响应每次派发后重新读取 Run；首个工具触发取消时，剩余工具不会派发。protected
   validation 同样在每个 check 后重查。
6. 历史 Trace 兼容：旧 `CANCEL_REQUESTED` 仍可重放；新增投影字段只在新取消协议实际使用时
   序列化，因此旧 Run 的 projection hash 不变。
7. 一个持久化的 `cancel_worker_stop` 本身会阻止终态和新 Lease。同步 `wait` receipt 或旧 Lease
   过期后的显式操作者停止确认清除它；TTL 到期本身仍不等于进程已经退出。

## 3. receipt 与恢复路径

| 取消时的在途对象 | 收尾动作 | 允许进入 `CANCELLED` 的条件 |
|---|---|---|
| 模型调用返回可信响应 | 持久化 response Artifact、Run receipt、实际 usage，并结算 Campaign | model reservation 已移除 |
| 模型调用结果不可信 | Campaign 先标 unknown，再把 Run model/budget intent 标 unknown | 普通 reservation 已分类，保守占用保留 |
| 普通预算操作 | 写入 settlement 或 unknown 分类 | 不再存在未分类 reservation |
| 只读/写入工具 | 保留 tool intent，使用既有 `agent resolve-tool` 精确处置 | tool receipt 已持久化 |
| `run_check` | 接纳自然退出结果，或确认/控制器停止后 discard | 标签容器已闭合且 tool receipt 已持久化 |
| 直接子 Worker | 先绑定 PID/Lease/epoch，再 terminate/wait，超时才 kill/wait | Worker stop receipt 已持久化；若仍有 tool intent，继续等待恢复 |

Campaign unknown 必须先于 Run unknown 写入。这样即使两次持久化之间崩溃，Run 仍保持非终态，
下一个 recovery Worker 可以完成对账；不会留下一个 Campaign 尚未分类、Run 却已经取消的死角。

## 4. 精确停止在途 Docker check

仅当 Run 恰有一个持久化的 `run_check` intent 时，可执行：

```powershell
uv run --locked --cache-dir .uv-cache horizon cancel <run-id> `
  --image <existing-local-image> `
  --stop-check-sandbox
```

顺序固定为：

1. 校验目标 Run 和唯一 `run_check` call ID；
2. 先提交 `CANCEL_PENDING`，从此禁止新派发；
3. 用 call ID 派生的 owner/name 和本机镜像 digest 重新核验容器；
4. 只停止匹配 owner、attempt、image 的运行中容器，并再次确认其状态为 stopped；
5. 追加 `CANCEL_SANDBOX_STOPPED`，记录 call ID、容器名和 image ID；
6. 活 Worker 若仍在，会接收退出结果、提交 tool receipt 并触发最终取消；Worker 已崩溃时，先
   `agent reconcile`，再用 `agent resolve-tool --discard-check --image ...` 删除停止容器并结算。

命令使用 `--pull never` 语义，不下载镜像。missing attempt 不是停止证明；owner、attempt 或
image 任一不符都拒绝。控制命令不会猜测或终止任意宿主进程，也不会把被信号杀死的 check
结果伪装成通过。

## 5. 精确停止 Supervisor 子 Worker

`SequentialAgentSupervisor.cancel_and_reap_worker(...)` 只接受可信父进程持有的直接子进程句柄。
它先写 `CANCEL_WORKER_STOP_PENDING`，再触碰进程；Lease/epoch/启动游标不匹配时不会调用
`poll`、`terminate`、`kill` 或 `wait`。`wait` 返回后才写 `CANCEL_WORKER_STOPPED` 和退出码，
并记录 `already_exited`、`terminate` 或 `kill`。若 Worker 在栅栏后协作释放 Lease，receipt 仍可
闭合；在 receipt 前，任何新 Lease 都被拒绝。

父 Supervisor 若在栅栏后崩溃，不能从数据库重建 `wait` 事实。旧 Lease 过期后，只有操作者从
操作系统确认旧 Worker 已停止，并同时使用 `agent reconcile --confirm-old-worker-stopped`
与 `--confirm-old-worker-pid <cancel_worker_stop.process_id>`，才会写入 `operator_confirmed`
receipt 并进入 recovery。缺少或不匹配的 PID 保持原栅栏不变；普通 `agent resume` 不能越过
取消栅栏。

该入口不按进程名扫描，不终止任意 Python 进程，也不覆盖进程树、Job Object、跨主机 Worker
或 Provider 侧取消。调用方必须保证句柄 PID 就是实际 Worker，而非会再派生解释器的 launcher。

## 6. 关键故障窗口

| 故障点 | 持久结果 | 恢复行为 |
|---|---|---|
| `CANCEL_PENDING` 前退出 | 原 Run 不变 | 可安全重试同一 cancel key |
| `CANCEL_PENDING` 后、容器停止前退出 | 取消栅栏已生效，tool intent 保留 | 重试 stop，或等待旧 Worker receipt |
| 容器已停止、stop event 前退出 | attempt 仍可按标签观察为 stopped | 重试命令并补写 stop receipt |
| stop event 后、tool receipt 前退出 | Trace 有停止身份，tool intent 仍阻塞终态 | reconcile + 精确 discard/result recovery |
| Worker stop fence 后、terminate 前父进程退出 | 精确 PID/Lease 目标保留，Run 与新 Lease 均被围栏 | 确认进程停止且 Lease 过期后显式 recovery takeover |
| Worker 已退出、stop receipt 前父进程退出 | 不把“可能已退出”当成证据 | 同上；Trace 标记为 `operator_confirmed` 而非同步 reap |
| Worker 已 reap、存在 tool intent | stop receipt 和 Lease 释放已落盘，tool intent 原样保留 | recovery Lease 只做分类/结算，不重派 |
| 模型 response 返回、Run receipt 前退出 | Run 保持 pending，reservation 可隔离为 unknown | Campaign 先分类，再安全取消 |
| tool/model receipt 后、final cancel 前退出 | blocker 已移除且 cancel flag 仍在 | 任一后续控制命令运行 `cancel_if_safe`，幂等终态化 |

SQLite 事件与 Docker/Provider 不是分布式事务，因此这里提供的是可检测、可恢复、不会误重派的
协议，不宣称外部副作用 exactly-once。

## 7. 当前边界

- 已实现：取消派发栅栏、迟到模型 receipt、模型 unknown、普通 reservation、工具恢复、顺序
  多工具截断、protected check 截断、精确 Docker check 停止、单个直接子 Worker 的栅栏优先
  terminate/wait/kill 与父进程崩溃后的显式确认、Trace replay。
- 未实现：通用宿主进程组/后代进程终止、Provider 侧主动取消/receipt 查询、后台定时取消 Worker、多个
  并发外部 effect、daemon/分布式队列。
- `status=RUNNING` 且 `cancel_requested=true` 表示“原阶段仍供 recovery 使用，但业务执行已被
  永久围栏”，不是 Agent 仍可继续工作。

## 8. 离线验收

当前回归覆盖：

- 普通迟到 receipt 先结算费用再取消；
- 模型迟到 response 持久化且工具派发为 0；
- 不可信模型调用先完成 Campaign/Run unknown 对账再取消；
- 两工具响应在首个工具触发取消后，实际派发数为 1；
- `run_check` stop receipt、unknown、显式 tool settlement 和 Trace replay；
- 真实直接子 Worker 的 fence-before-terminate、同步 wait、Lease 释放和 Trace replay；
- 带未决只读 intent 的 Worker 被回收后不重派，需 recovery settlement 才终态；
- terminate 超时后的 kill、已退出进程、协作释放 Lease、身份不匹配零进程操作，以及父进程崩溃
  后无显式停止确认不接管；
- CLI 精确停止路径不联网、不调用付费模型。

这些测试证明当前有界实现的状态闭合，不代表任意进程、任意 Provider 或分布式执行环境都已支持。
