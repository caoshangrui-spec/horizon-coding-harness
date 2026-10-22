# Docker `created` 执行子进程退出后续跑到终态

## 1. 目标

这条证据链补上原 `safe_to_resume` 演示之后最关键的一段：真实 Docker effect-executor
子进程在 `create` 已完成、`start` 尚未调用时硬退出；持有 Lease 的演示控制器仍然存活，观察
退出码并保守处置旧 attempt。之后系统不停在 `RUNNING` 边界，而是由新的 Worker epoch、使用
新的工具 call ID 继续 Agent Loop，运行一次新的受保护验收，最终进入 `SUCCEEDED`。

它回答的是一个有界问题：**工具执行子进程在 created-state 窗口退出、且存活控制器已证明旧
调用可安全处置后，能否在不重放旧调用的前提下真正续跑到终态？** 它不证明持有 Lease 的
Agent Worker 自身崩溃后的自动接管，也不是任意崩溃点或真实模型修复能力评测。

## 2. 完整执行链

```text
epoch 1 / surviving demo controller
  durable run_check intent
    -> launch Docker effect-executor subprocess
    -> docker create
    -> executor hard exit 34 before docker start
    -> controller observes exit, reconciles unknown, releases lease

epoch 2 / recovery CLI
  no receipt -> unknown
    -> exact labeled attempt is created
    -> remove inactive attempt
    -> workspace revision unchanged
    -> settle cancelled / discard_check
    -> persist completed Agent session

epoch 3 / new Worker
  load recovered session
    -> one new scripted submit turn
    -> fresh protected run_check with a new call ID
    -> command marker appears
    -> checkpoint + passing validation
    -> WORK_ITEM_PASSED -> SUCCEEDED
```

旧 `run_check` 永远不会重新派发。新的 protected check 拥有不同的持久化 call ID；旧 receipt 和
新 receipt 会同时保留在同一条追加式 Trace 中。

## 3. 一条命令生成证据

使用本机已有、包含 `/bin/sh` 的 Linux 镜像。Harness 使用 `--pull never`，不会自动下载镜像：

```powershell
uv run --locked --cache-dir .uv-cache horizon demo docker-created-resume `
  --image python:3.12-alpine `
  --output .horizon/demos/docker-created-resume-local
```

演示只把新建的一次性 staging workspace 设为可由容器固定 UID `65534` 写入；父目录和源仓库不
在该挂载范围内。这也保证 Docker Desktop 与原生 Linux bind mount 使用同一执行条件。

命令不读取 Provider API Key，不调用网络或付费模型。恢复后的模型轮次使用确定性
`ScriptedModelGateway`；正数离线价格卡只用于走通与生产一致的费用预留合同，外部费用仍为
`0 CNY`。其中 `paid_model_called=false` 与 `network_called=false` 是固定 Runner 构造及适配器配置
保证的路径属性，不是 EvidencePack 通过 OS 级外部审计独立测得的结论。

2026-10-22 的公开 Linux 验收保留了完整的失败—修复链：首次
[run 39802257676](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/39802257676)
在新终态合同处失败，因为原生 Linux bind mount 的一次性 workspace 为 `0755`，容器 UID
`65534` 无法写 marker；提交 `fc2c416` 将该 staging 子目录显式设为可写后，复跑
[run 39803245157](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/39803245157)
通过全部 10 个 Docker 合同、新 EvidencePack 导出、既有冻结 suites、artifact 上传和汇总。
GitHub API 返回的统一 artifact `source-bound-docker-evidence` digest 为
`sha256:ed66fa5d447f0ff2bf603cbf84df21a0bac88200f593c34d6d89efcfa28e411b`，当前保留期截至
2026-11-21；run 页面和 digest 仍保留可审计身份，但 artifact 到期后不可再下载。

成功输出必须包含：

- `boundary_status=RUNNING`、`terminal_status=SUCCEEDED`；
- `executor_exit_code=34`；
- `attempt_transition=created->missing`；
- `recovery_disposition=discard_check`；
- Worker epoch 严格增加；
- 恢复前后的 immutable Docker image ID 完全相同；
- 恢复 Worker ID 与 terminal Trace 中的新 Lease 事件一致；
- `crash_call_id != resumed_check_call_id`；
- `created_trace_is_terminal_prefix=true`；
- `command_marker_present_after_resume=true`；
- `protected_validation_passed=true`、`task_succeeded=true`；
- unknown/open effect 均为 false；
- `paid_model_called=false`、`network_called=false`、`external_cost_cny=0`；
- `simulated_model_cost_cny` 单独给出生产预算器在确定性价格卡下的内部账本值。

## 4. 两阶段 EvidencePack

根目录不是只保存一份终态 JSON，而是链接两份可独立验证的子证据：

| 路径/角色 | 作用 |
|---|---|
| `created-boundary/evidence/evidence-pack.json` | 原有 7 文件 created-state EvidencePack；终点是无 open/unknown effect 的 `RUNNING` 安全边界 |
| `terminal/evidence-pack.json` | 通用终态 EvidencePack；终点必须为 `SUCCEEDED` |
| `resume/runtime-observation.json` | 恢复时实际解析的 image ID、Worker ID、epoch 与新 check call ID |
| `resume/final-workspace-manifest.json` | 新 protected check 结束后的内容寻址工作区清单 |
| `resume/resumed-check-artifact.txt` | 新 check 的结构化结果，包括 ID、exit code、timeout 和 bounded output |
| `resume/never-started.txt` | 合成命令真实启动后写入的 `executed` marker |
| `SUMMARY.md` | 由上述机器事实确定性生成的人读摘要 |
| `evidence-pack.json` | 根清单，绑定全部角色、hash、因果字段和声明边界 |

根清单只引用子 EvidencePack 的入口文件；验证器会递归进入每个子清单，继续校验其所有 Trace、
最终投影和摘要，因此删除或修改子文件仍会失败。

## 5. 无 Docker 离线复核

生成完成后可以关闭 Docker，只保留输出目录：

```powershell
uv run --locked --cache-dir .uv-cache horizon demo verify-docker-created-resume `
  .horizon/demos/docker-created-resume-local/evidence-pack.json
```

验证器不会读取运行时 SQLite、Docker daemon 或模型 Provider。它会重新推导以下不变量：

1. created-boundary 与 terminal 子包分别通过自己的离线验证器；
2. 两个子包属于同一 Run、同一 TaskSpec；
3. created-boundary JSONL 必须是 terminal JSONL 的**逐字节严格前缀**；
4. 恢复观察中的 boundary/resumed image ID 必须相同，并等于 created-boundary 固定的 immutable
   image ID；Runner 在新 dispatch 前也会拒绝 tag 漂移；
5. 恢复观察中的 Worker ID 与 epoch 必须命中 terminal Trace 中边界之后唯一的新 Lease 事件；
6. 旧 call ID 只有一个 `cancelled / discard_check` receipt；
7. terminal Trace 恰有一个不同 call ID 的新 `run_check` 成功 receipt；
8. 新 receipt 的 Artifact 和 workspace manifest hash 与 Trace 完全一致；
9. manifest 必须包含 `never-started.txt`，其长度和 SHA-256 必须对应精确内容 `executed`；
10. check Artifact 必须与该合成命令的五行成功结果逐字节一致；
11. final validation 包含该 check，全部 WorkItem 已通过，Run 为 `SUCCEEDED`；
12. 终态没有 reservation、unknown model/tool call 或 pending promotion；
13. Worker epoch、事件数、模型调用数和工具调用数都必须严格向前推进。

测试还会分别伪造恢复 image observation 和 marker、重算根文件 hash，再确认语义验证仍拒绝结果。

## 6. 代码入口

- 运行与递归验真：`src/horizon/application/docker_created_recovery.py`
- EvidencePack 合同：`src/horizon/domain/docker_created_recovery.py`
- create-before-start 硬退出 effect-executor：`src/horizon/application/_docker_created_crash_worker.py`
- CLI：`src/horizon/interfaces/cli/app.py`
- Docker 合同测试：`tests/contract/test_docker.py`

生产主链路复用 `CodingAgentRunner`、`WorkspaceToolGateway`、`DockerAcceptanceExecutor`、
`RecoveryService`、`ToolRecoveryService`、Lease/epoch 和通用终态 EvidencePack；没有创建一套平行的
“演示状态机”。

## 7. 严格声明边界

本切片可以声称：在精确的 Docker create-to-start 崩溃窗口中，Docker effect-executor 子进程
退出、Lease-owning 控制器存活并完成保守处置；旧 attempt 被标记、核验并删除后，新 Worker
epoch 能从持久 Agent session 继续。恢复 dispatch 使用与边界相同的 immutable image ID；旧工具
调用不重派，新 protected check 使用新 call ID，最终验证通过并到达可离线重放的 `SUCCEEDED`。

不能声称：

- 任意 Docker、宿主机、内核、网络或分布式故障都可恢复；
- 已证明持有 Lease 的 Agent Worker 自身崩溃或无人值守自动接管；
- 具备跨 SQLite、Docker daemon 和文件系统的通用 exactly-once 事务；
- 合成 marker 等同于真实 Issue 修复或 SWE-bench 成绩；
- Scripted Model 证明任何真实 LLM 的规划、编辑或修复能力；
- 自洽 SHA-256 等同于第三方签名、可信时间戳或来源认证；
- `paid_model_called=false` 或 `network_called=false` 来自 OS 级外部审计；
- 当前容器限制已经通过独立恶意代码安全认证。

若恢复后的新 check 在 receipt 落盘前发生故障，其 attempt 可能被有意保留为待对账副作用；该
路径必须回到既有 reconciliation，不会为清理方便而自动删除未知效果。
