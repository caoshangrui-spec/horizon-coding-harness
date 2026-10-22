# Lease-owning Agent Worker 在 Docker `create -> start` 窗口崩溃后的恢复

## 1. 这条证据补了什么

旧的 `docker-created-recovery` 证明的是 Docker effect-executor 子进程退出，持有 Lease 的
Agent 控制器仍然存活。这里把故障提升到真正的 Agent Worker 进程：子进程运行生产
`CodingAgentRunner`，持有 Run 的精确 Lease token，在 `run_check` intent 已持久化且 Docker
容器已进入 `created` 后直接 `os._exit(34)`。它没有机会写 tool receipt、释放 Lease 或保存下一轮
AgentSession。

可信父控制器同步 `wait` 回收该直接子进程。原 Lease 到期后，现有
`SequentialAgentSupervisor.continue_after_reaped_worker` 使用 PID、退出码、旧 Lease ID、Worker
ID、epoch 和 launch event sequence 做原子接管。悬空 `run_check` 仍被保留，接管结果只能是
`reconciliation_required`，不能自动重派。

完整主线如下：

```text
parent persists crash-launch.json + fsync
  -> parent releases start gate
  -> epoch 1 child CodingAgentRunner calls scripted model
  -> durable model receipt
  -> durable run_check intent
  -> docker create + exact created-state check
  -> child writes crash-observation.json + fsync
  -> child os._exit(34)
  -> parent communicate()/wait() reaps exact PID
  -> old Lease expires
  -> epoch 2 confirmed_reaped_worker_expired takeover
  -> pending intent remains; no model/tool redispatch
  -> RecoveryService marks exact call unknown
  -> controller verifies created attempt + immutable image ID
  -> removes exact inactive container
  -> rechecks unchanged workspace revision
  -> persists cancelled / discard_check receipt + AgentSession iteration 2
  -> releases recovery Lease
  -> epoch 3 fresh child Agent Worker resumes
  -> submit
  -> fresh protected run_check with a new call ID
  -> SUCCEEDED + lease-free terminal Trace
```

## 2. 运行

前提是 Docker 已运行，且指定镜像已经存在于本机；命令不会拉取镜像、访问模型 API 或产生费用。

```powershell
uv run --locked --cache-dir .uv-cache horizon demo docker-worker-crash-resume `
  --image python:3.12-alpine `
  --output .horizon/demos/docker-worker-crash-local
```

关键 JSON 输出应包括：

```json
{
  "verified": true,
  "crashed_worker_exit_code": 34,
  "worker_epoch_chain": "1->2->3",
  "lease_transition": "expired_takeover",
  "attempt_transition": "created->missing",
  "launch_record_persisted_before_dispatch": true,
  "takeover_bound_to_reaped_pid": true,
  "original_call_not_redispatched": true,
  "protected_validation_passed": true,
  "terminal_status": "SUCCEEDED",
  "unknown_effects": false,
  "open_effects": false,
  "paid_model_called": false,
  "network_called": false
}
```

离线验真不需要 Docker、原数据库或工作区：

```powershell
uv run --locked --cache-dir .uv-cache horizon demo verify-docker-worker-crash-resume `
  .horizon/demos/docker-worker-crash-local/evidence-pack.json
```

本机真实 Docker 合同为 `11 passed`。公开 Linux
[Source-bound Docker evidence run 39809773689](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/39809773689)
也完成了 11 项合同、演示命令、离线 verifier、既有四组 suite、证据上传和 summary；统一
artifact digest 为
`sha256:7d7aea85aa1ea8a4a33f1f3395bc847cf27ad41d862e9417f428b3be1eaf3120`。

## 3. Worker 身份与启动门

父进程先获取 epoch 1 Lease 并捕获 `ReapedWorkerBoundary`。启动子进程后，父进程立即得到真实
直接子进程 PID，写入并 `fsync` `crash-launch.json`，其中绑定：

- Run ID；
- PID；
- Lease ID、Worker ID 和 epoch；
- launch event sequence；
- 当前 AgentSession Artifact ref 和 `next_iteration`；
- Docker immutable image ID。

随后父进程写入一个只包含 launch record SHA-256 的启动门。子 Worker 在调用模型前必须读取并
校验该门、自己的 `os.getpid()`、Lease 和当前 Run 投影。子 Worker 到达 Docker created 边界时，
再次把 launch record SHA-256 写进 `crash-observation.json`。因此离线 verifier 能证明 child-written
观察与 pre-dispatch parent record 是同一条身份链，而不是事后只修改根包 JSON。

## 4. 为什么必须先阻塞，再恢复

父进程回收 PID 只证明旧 Worker 不能继续执行，不证明它已派发的工具没有副作用。因此接管事件
只围栏旧 Lease，并保留 `run_check` reservation。Supervisor 返回
`reconciliation_required`；runner factory 不会被构造，模型调用不会发生。

恢复控制器随后执行生产恢复协议：

1. `RecoveryService` 把无 receipt 的 tool intent 标记为 `tool_effect_unknown`；
2. 根据持久 call ID 查询标签容器，要求状态严格为 `created`、owner/attempt/image 标签全部匹配；
3. 仅删除这个未启动容器，并再次查询为 `missing`；
4. 删除前后分别捕获 workspace manifest，要求 revision 完全一致；
5. `ToolRecoveryService.discard_check_result` 写入单一 `cancelled / discard_check` receipt，并原子保存
   完成该 tool turn 的 AgentSession；
6. 再次 reconcile，只有 `safe_to_resume=true` 才允许 epoch 3 Worker 启动。

旧 call ID 永远不会重派。epoch 3 的 protected validation 使用新的 call ID；二者同时保留在终态
Trace 中，以便区分恢复 receipt 与真实重新验证。

## 5. EvidencePack

根包固定包含九个角色：

| role | 含义 |
|---|---|
| `terminal_pack` | 通用终态包；包含完整 JSONL Trace、最终 Run 投影和摘要 |
| `crash_launch` | 父进程在 start gate 前持久化的 PID/Lease/session/image 边界 |
| `crash_observation` | epoch 1 子 Worker 在 Docker created 边界写入的观察 |
| `recovery_observation` | Supervisor disposition、epoch 2 takeover 和精确处置结果 |
| `resume_launch` | epoch 3 子 Worker 的 PID/Lease/session/image 启动边界 |
| `final_workspace_manifest` | 新 protected check 绑定的内容寻址 manifest |
| `resumed_check_artifact` | 完整 passing check receipt Artifact |
| `command_marker` | 容器实际执行后写入的合成 marker，固定为 `executed` |
| `summary` | 由结构化 evidence 确定性生成的可读摘要 |

离线 verifier 会重新计算所有文件哈希，递归验证 terminal pack，重放 Trace，并重新推导根 evidence。
特别检查：

- launch sequence 对应的历史投影确实由 epoch 1 Lease 和指定 AgentSession 持有；
- child observation 的 PID、Lease、session 和 launch-record hash 与父记录一致；
- Trace 恰有一个 `confirmed_reaped_worker_expired` takeover，且 payload 精确绑定同一 PID、退出码、
  旧 Lease、epoch 和 launch sequence；
- epoch 严格为 `1 -> 2 -> 3`，epoch 3 Worker ID 与 resume launch 一致；
- 原 call 恰有一个 `discard_check` receipt，新 call 恰有一个普通 success receipt；
- final manifest、check Artifact、marker、ValidationReport 和终态投影互相一致；
- 最终不存在 reservation、unknown effect 或活动 Lease。

即使攻击者改写 observation 后重新计算文件哈希，PID、Lease、epoch、call ID、image 或 Artifact 与
Trace 不一致仍会拒绝。

## 6. 已证明与未证明

这条证据证明：一个本地直接子 Agent Worker 在特定 Docker `create -> start` 窗口退出后，可信父
控制器可在同步回收和 Lease 到期的前提下，按持久身份接管；未决副作用不会自动重派；精确处置
后可由新进程、新 epoch 和新 call ID 到达 `SUCCEEDED`。

它不证明：

- 任意 Agent 崩溃点都能自动恢复；
- 父控制器自身崩溃或操作系统重启后的 PID 事实可自动重建；
- 任意后代进程、进程组、跨主机 Worker 或分布式队列已受控；
- Docker daemon/kernel 崩溃、`missing` attempt 或超时/信号/OOM 结果可自动判定；
- 外部副作用 exactly-once；
- Scripted Model 的成功代表真实模型修复能力或官方 benchmark 成绩；
- 自洽哈希等于外部来源认证。

该场景使用本地 Scripted Model 与合成 marker，只评估 Harness 的恢复控制面；外部模型调用、模型
网络访问和实际费用均为零。
