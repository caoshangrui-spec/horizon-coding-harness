# Horizon v0.2.0 候选说明

> 状态：`0.2.0rc1` release candidate，尚未创建 `v0.2.0` tag，也未发布 GitHub Release。
> v0.1.0 仍是当前已发布版本。

v0.2.0 候选聚焦一件事：把 v0.1.0 的“可恢复状态内核”推进为可以用真实本地子进程运行、在
有限故障窗口内保守恢复、并导出可离线复核证据的 Coding Agent Harness。它仍优先证明执行
可靠性和声明边界，不把 Scripted Model 结果包装成模型能力。

## 从 v0.1.0 到候选版的主要变化

### 1. 从可恢复状态变成可管理的真实 Worker

- `agent run/resume --worker-process` 让每个有界 slice 在真实直接子进程中执行；父 CLI 持有
  进程句柄，同步 reap 后才按精确 Run、Lease、epoch、AgentSession 和 event cursor 接力。
- 正常轮次可由本地顺序 Supervisor 换 Worker/epoch 继续；pending model/tool effect 保守阻塞，
  不因子进程退出自动重派。
- `Ctrl+C` 与 `--worker-timeout-seconds` 共用 fence-before-stop 合同：先持久化精确取消目标，
  再 terminate/wait，必要时 kill/wait，最后才释放 Lease 或结束取消。

### 2. 把关键恢复窗口做到终态证据

- Docker `run_check` 的 `create → start --attach` 边界有稳定 attempt ID、容器标签和状态核验。
  `created` 且未执行的 attempt 可以精确删除并写入 `discard_check`；missing、漂移或不确定 effect
  不会盲目继续。
- 已覆盖两种 exit 34 路径：effect-executor 退出，以及真正持有 Lease 的 Agent Worker 退出。
  后者由可信父进程同步回收，再经历 epoch `1→2→3` 完成处置和新 call 验收。
- 来源绑定的 Luigi 多阶段案例覆盖写入 effect 已发生、receipt 未提交时的硬退出；恢复只接纳
  唯一既有后态，不重复写入，然后继续后续 WorkItem 到终态。

### 3. 长程执行控制更完整

- 完整 checkout 多阶段场景包含 3 个依赖 WorkItem、2 次持久边界接力、跨 revision RAG、
  active/stale Run Memory、只修改未完成项的单次 replan 和最终 required checks。
- 上下文投影同时受字符、保守 input-token 与 Run/Campaign/单调用 CNY 余量约束；只折叠闭合的
  旧工具单元，Mandatory Facts、Run Memory binding、未完成调用和完整 transcript 来源仍可复核。
- TaskSpec 的 `allowed_tools` 与 execution mode、WorkItem 权限取交集；有限 `create_file`、最多
  8 文件的结构化 patch 和 promotion 写入均有前后态/恢复合同。
- NoProgress 识别从同动作/二周期扩展到最长四周期的精确循环；只有有证据的停滞才可请求一次
  持久化 operator guidance。通用审批、多用户 HITL 和自动多次 replan 仍未实现。

### 4. 证据从单个成功 Demo 扩展为失败也可复核

- 通用终态 EvidencePack 支持 `SUCCEEDED`、`FAILED` 和 `CANCELLED`，保留 BudgetStop、unknown
  与 open effect；独立 verifier 可在没有 SQLite 的情况下校验文件、重放 Trace 并复算投影。
- terminal suite 对已有终态 Run 做确定性聚合，不执行任务，也不冒充 batch scheduler。
- `horizon demo process-timeout` 用真实直接子进程演示 timeout、取消栅栏和同步 reap。标准 CI
  运行演示与脱库 verifier，并发布保留 14 天的 `horizon-process-timeout-evidence` artifact。
- 一键 portfolio demo 升到 schema v3，绑定真实硬退出 marker、recovery disposition 与可从
  Trace 复算的 retrieval → write lineage。

## 候选证据快照

| 证据 | 当前结果 | 不能推出什么 |
|---|---|---|
| 离线回归 | `463 passed, 11 skipped`；包含新增的版本元数据一致性测试 | 跳过的 Docker 合同不能算本地通过 |
| 标准 CI | [公开 workflow](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/workflows/ci.yml)运行锁定依赖、Ruff、全量离线测试、两条 Demo、EvidencePack 上传与构建 | 不调用真实模型，不证明模型能力 |
| Docker 合同 | 本机与公开 Linux runner 的当前切片均为 11 项通过 | 是有限隔离/恢复合同，不是恶意代码安全认证 |
| 有界恢复矩阵 v3 | 21/21；自动恢复 11/11、安全阻塞 10/10，incorrect resume、恢复重派与重复副作用均为 0 | 其余多数是生成 fixture，不是所有进程/主机故障 |
| 完整 checkout A/B v2 | tqdm 82 files、youtube-dl 872 files、Luigi 382 files，本地可信和公开禁网 Docker 均为 3/3 | 使用 Scripted Model，不是官方 BugsInPy 成绩 |
| 多阶段恢复 A/B v3 | youtube-dl + Luigi 两例均完成 3 WorkItem、2 次重启和 final epoch 3 | 作者选择的两个任务，不代表总体成功率 |
| 真实模型 Pilot | 七轮费用与 Trace 可核对，source 未变；七轮均未修复成功 | 负结果，不能说成真实 Issue 成功 |

## 面试演示入口

最快展示真实进程控制和脱库验证：

```powershell
uv sync --locked
$result = uv run --locked --cache-dir .uv-cache horizon demo process-timeout |
  ConvertFrom-Json
uv run --locked --cache-dir .uv-cache horizon demo verify-process-timeout `
  "$($result.evidence_pack)"
```

完整三分钟讲稿、架构图与追问回答见[面试演示指南](interview-demo.md)。更长的 Agent 主链路
演示仍可运行：

```powershell
uv run --locked --cache-dir .uv-cache horizon demo run
```

两条演示都不需要 API Key、Docker 或网络，不产生外部模型费用。

## 兼容性与升级边界

- Python 支持范围仍为 3.12～3.13；依赖继续由 `uv.lock` 锁定。
- CLI、领域对象和 EvidencePack 都仍处于 1.0 之前；只有文档明确列出的旧 schema reader/参数
  别名具备兼容保证，不能推断所有内部 Python API 稳定。
- 当前 SQLite `user_version` 仍为 1，但项目没有从任意 v0.1.0 运行库到候选库的通用 migration
  验收。不要直接拿重要的旧 `.horizon/*.sqlite3` 做原地升级；保留原证据目录，并为候选运行使用
  新 state directory。实现 migration 前，这一点是发布边界，不靠版本号掩盖。
- `TaskSpec.max_cost_usd` 是尚未完成多币种迁移的历史字段；真实 Provider 路径的权威硬上限仍是
  CNY Campaign、Run policy 与单调用上限。
- 自动跨模型 fallback 仍关闭；`fallback_enabled` 固定为 `false`。检索 scan fallback 会显式标记
  degraded，不会静默增加预算或换 Provider。

## 严格声明边界

v0.2.0 候选不声称：

- 官方 SWE-bench/BugsInPy 分数或真实模型 Issue 成功率；
- 父控制器/操作系统重启、进程树、daemon、队列或跨主机容灾；
- 任意外部副作用 exactly-once；
- 生产级多租户权限、通用 HITL、Project Memory、向量/图 RAG；
- Docker 配置已经构成恶意代码安全认证；
- EvidencePack 哈希等于签名、可信时间戳或第三方来源认证。

## 发布门

候选整理完成后，正式发布仍需同时满足：

1. `pyproject.toml`、`horizon.__version__`、`uv.lock` 和构建产物版本一致；
2. 精确候选提交的标准 CI 通过，并实际生成 timeout EvidencePack artifact；
3. 最新相关 runtime 提交的公开 Docker evidence 通过；仅文档/版本元数据提交不重复冒充 Docker
   runtime 复验；
4. wheel 与 sdist 可离线构建，安装后的包能导入并运行 `horizon doctor`；
5. Changelog 继续保留全部负结果、unknown、预算和兼容性边界；
6. 用户明确批准后，才把 `0.2.0rc1` 收敛为 `0.2.0`、创建 `v0.2.0` tag 和 GitHub Release。

当前这份文件是候选说明，不是发布公告。
