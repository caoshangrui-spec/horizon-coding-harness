# Docker `created` 状态硬崩溃恢复 EvidencePack

## 1. 目标

这条演示只覆盖一个可精确定义、可重复执行的提交窗口：Harness 已持久化
`run_check` intent，Docker 已完成 `create`，但 Worker 在调用 `docker start` 前以退出码
`34` 硬退出。此时命令尚未进入容器，Harness 必须：

1. 将没有 receipt 的工具调用保守标为 `unknown`；
2. 用持久化 call ID 找到唯一标签容器，并确认状态严格为 `created`；
3. 删除这个从未启动的非活动容器；
4. 再次快照工作区，要求 revision 未变化；
5. 将工具调用结算为 `cancelled / discard_check`，保存恢复 Artifact 和 Agent session；
6. 导出可脱离 Docker、SQLite 和模型 Provider 复核的 JSONL Trace 与 EvidencePack。

它复用生产 `DockerSandbox`、`DockerAcceptanceExecutor`、`RecoveryService`、
`ToolRecoveryService` 和 `agent resolve-tool` CLI，不另造测试专用恢复逻辑。

## 2. 一条命令运行

先准备一个本机已有、包含 `/bin/sh` 的 Linux 镜像。命令使用 `--pull never`，不会自动下载：

```powershell
docker pull python:3.12-alpine
uv run --locked --cache-dir .uv-cache horizon demo docker-created-recovery `
  --image python:3.12-alpine `
  --output .horizon/demos/docker-created-recovery-local
```

输出目录必须是新目录。执行过程不读取 API Key、不发模型或网络请求，也不运行用户仓库代码；
所谓“模型轮次”是本地构造并写入事件流的冻结 `run_check` tool call，用于验证真实恢复协议。

成功输出至少包含：

- `worker_exit_code=34`；
- `attempt_state_before=created`、`attempt_state_after=missing`；
- `command_marker_absent=true`；
- `recovery_disposition=discard_check`、`tool_status=cancelled`；
- `workspace_revision_unchanged=true`；
- `trace_replay_verified=true`、`safe_to_resume=true`；
- `paid_model_called=false`、`network_called=false`、`external_cost_cny=0`。

## 3. 离线复核

生成后可以关闭 Docker，并只用证据目录复核：

```powershell
uv run --locked --cache-dir .uv-cache horizon demo verify-docker-created-recovery `
  .horizon/demos/docker-created-recovery-local/evidence/evidence-pack.json
```

验证器不会读取 runtime 控制库。它会：

- 校验 EvidencePack 列出的 7 个文件的路径、字节数和 SHA-256；
- 重放 `trace.jsonl`，并要求结果逐字节等于 `final-run.json`；
- 要求同一 call ID 只有一个 `TOOL_CALL_RESERVED → TOOL_CALL_UNKNOWN →
  TOOL_CALL_SETTLED` 有序链；
- 要求最终 Run 保持 `RUNNING`，存在持久化 Agent session，且没有 open/unknown reservation；
- 要求工具 receipt 为 `cancelled / discard_check`，workspace revision 前后一致；
- 校验恢复 Artifact 的内容寻址 hash，并要求它明确记录
  `State.Status=created` 和“不推断通过或失败”；
- 校验 workspace manifest 的 revision，且其中不存在 `never-started.txt`；
- 校验 CLI receipt 记录了 `controller_observed_created_and_removed`、安全续跑和零网络/付费模型；
- 校验容器名由持久化 call ID 按生产规则确定性派生。

即使攻击者修改恢复叙述并重新计算文件 hash，语义复核仍会因 Trace receipt、Artifact 内容或
manifest 不一致而失败。

## 4. EvidencePack 文件

| 文件 | 作用 |
|---|---|
| `trace.jsonl` | 追加式事件真相源，可重放 Run |
| `final-run.json` | Trace 重放得到的精确最终投影 |
| `crash-observation.json` | 退出码、镜像 digest、确定性容器名、`created → missing` 与 marker 观察 |
| `recovery-receipt.json` | 生产 `agent resolve-tool` 输出的恢复回执 |
| `workspace-manifest.json` | 恢复后内容寻址工作区清单与 revision |
| `recovery-artifact.json` | `discard_check` 的持久化原因和效果边界 |
| `SUMMARY.md` | 从上述机器可验事实确定性生成的人读摘要 |

`runtime/` 保存本次控制数据库、Campaign ledger 和内容寻址 Artifact，便于本地深入审计；离线
验证 EvidencePack 不依赖它。

## 5. 严格声明边界

本演示可以声称：对于这个精确 Docker `create → start` 窗口，真实子进程硬退出后，控制器观察
到唯一 attempt 为 `created`，命令标记未出现，删除非活动容器，持久化取消 receipt，并恢复到
可继续的 Agent 边界；完整过程可由 JSONL Trace 离线重放。

不能声称：

- 任意指令点、宿主机、Docker daemon、内核或跨主机故障都可恢复；
- marker 缺失足以证明任意外部副作用都未发生；
- 已提供分布式 exactly-once 执行；
- 自洽 SHA-256 等同于签名、可信时间戳或第三方来源认证；
- 这次演示证明了模型能力、真实 Issue 成功率或恶意代码隔离安全性。
