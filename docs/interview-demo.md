# Horizon：3 分钟面试演示与架构讲解

这是一份面向实习面试的讲解入口。目标不是在三分钟里罗列所有模块，而是用一个真实、零费用、
可复核的进程故障案例说明：Horizon 为什么是 Coding Agent Harness，而不只是一次模型调用脚本。
可直接复制到简历的中英文项目描述、完整架构导览、STAR 故事和扩展问答见
[实习求职作品集包](internship-portfolio-kit.md)。

## 一句话定位

> Horizon 是面向长程软件工程任务的可恢复 Coding Agent Harness：它把任务、模型调用、工具
> 副作用、Worker Lease、预算、人工决策和验收证据放进同一个可重放状态机，使进程中断后能够
> 先判断已经发生了什么，再安全续跑或明确阻塞。

## 3 分钟讲解稿

### 0:00～0:25：先讲问题

“普通 Coding Agent Demo 往往只展示一次成功对话，但长程任务会跨多个模型和工具轮次。真正
困难的是：Worker 中断后，系统不能确定写入或付费调用是否已经发生。如果直接 retry，可能重复
改代码、重复计费，甚至把部分成功误报成完成。Horizon 主要解决的是这个执行可靠性问题。”

### 0:25～1:00：再讲核心架构

“任务先变成有验收条件和权限边界的 TaskSpec，再拆成 WorkItem DAG。每个执行动作都经过
Harness：事件日志保存权威状态，Lease epoch 隔离旧 Worker，Context Projection、Run Memory
和 revision-bound Code RAG 为模型组装有来源的上下文，模型只提出 typed tool proposal，真正的
读写由 Tool Gateway 做权限、预算和前后态检查。最后必须通过 protected validation，Trace 和
EvidencePack 才能说明这次运行实际发生了什么。”

### 1:00～1:50：现场运行真实故障链

“我运行一条离线命令。第一任 Worker 先做 revision-bound 代码检索，再故意提交一次缺失
`end_line` 的非法读取，Typed Gateway 把结构化错误持久化。第二任 Worker 从新 Lease 和同一
AgentSession 继续，用成对范围读取后执行精确写入；文件副作用已经发生、tool receipt 还没提交时，
真实子进程以 `os._exit(86)` 硬退出。Supervisor 同步确认 PID 已退出，只把悬空 intent 标为
`unknown`，不会直接 retry。第三任 Worker 核对唯一精确后态，显式 `accept_replace`，再做受保护
验收和最终提交。”

运行下方命令，并指出摘要中的 Final lease epoch `3`、Durable worker handoffs `2`、
Hard-crash recovery verified `True`、Exact write backed by same-revision evidence `True` 和
External model cost `CNY 0`。关键状态流是：

```text
retrieve evidence -> structured tool error -> durable handoff
-> write intent -> file effect -> child exit 86 -> unknown
-> exact post-state check -> accept existing effect once
-> protected validation -> SUCCEEDED
```

### 1:50～2:30：展示脱库复核

“演示结束后，我删除三个 SQLite 数据库，再用独立命令检查交付文件大小和 SHA-256、重放 JSONL
Trace、复算最终 Run 投影，并重新验证检索 Artifact → 模型写入上下文 → 目标路径/旧文本/revision
的 lineage，以及硬退出 marker、悬空 call ID、unknown 分类和单一恢复 receipt。篡改摘要、Trace、
检索 Artifact 或 crash marker 都会被拒绝，所以它不是只看一个成功字符串。”

### 2:30～3:00：主动说清边界

“这个案例证明一个固定的 `replace_text` effect-before-receipt 窗口可以保守对账并续跑，不证明
任意崩溃、操作系统重启或跨主机 exactly-once。公开评测另外在 youtube-dl、Luigi、FastAPI 三个
完整 checkout 上通过三阶段、两次 Worker 重启的 3/3 Scripted Model 合同；来源绑定 hard-crash
v2 又在 Luigi `replace_text` 和 FastAPI `create_file` 两个案例上实现 2/2、零重派和零重复副作用。
真实模型九轮 Pilot 仍都是负结果；最新 TheFuck-2 还证明预算门能在第三次派发前安全停止，所以我把
Harness 可靠性证据与模型修复能力明确分开。”

## 现场演示命令

首次准备环境：

```powershell
uv sync --locked
```

运行主作品集演示并独立复核本次 EvidencePack：

```powershell
$demo = ".horizon/demos/interview-$((Get-Date).ToString('yyyyMMdd-HHmmss'))"
uv run --locked --cache-dir .uv-cache horizon demo run --human --output $demo
Remove-Item "$demo/control.sqlite3", "$demo/campaign.sqlite3", "$demo/retrieval.sqlite3"
uv run --locked --cache-dir .uv-cache horizon demo verify-portfolio `
  "$demo/evidence-pack.json"
```

不指定 `--output` 时也会在 `.horizon/demos/` 下创建唯一目录，不覆盖旧证据。`--human` 原样展示
哈希绑定的 `SUMMARY.md`，并附上即时离线复核结果和可复制命令；省略该参数仍返回适合脚本解析的
单行 JSON。上述命令不调用真实模型、网络、Docker 或外部仓库代码，外部费用为 0；演示会运行
冻结 Scripted Model、真实 Harness 工具链和一个故意硬退出的本地子进程。若现场时间很短，只运行
第一条演示命令；若要证明证据不依赖运行数据库，再删除三个 SQLite 文件并运行独立 verifier。

公开来源绑定 hard-crash v2 的 Linux 证据见
[`41027080498`](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/41027080498)。
它与本地一键作品集故事分开：前者覆盖 Luigi/FastAPI 两类写入工具的 2/2 公开复跑，后者提供最短、
无需 Docker 的面试入口。

## 一张图说明数据流

```mermaid
flowchart LR
    T[TaskSpec + WorkItem] --> R[Revision-bound retrieval]
    R --> E[(Events + Artifacts)]
    E --> W1[Worker 1: structured error]
    W1 --> W2[Worker 2: bounded read + exact write]
    W2 -->|effect before receipt| X[Child os._exit 86]
    X --> U[Unknown + old Lease fenced]
    U --> A[Exact post-state accept once]
    A --> W3[Worker 3: protected validation]
    W3 --> P[Trace + EvidencePack]
    P --> V[Offline hash + replay + semantic verifier]
```

这条演示执行了检索、上下文、工具错误、写入、进程故障、恢复、验收和证据链。完整系统的四个面为：

| 面 | 当前设计 | 为什么需要 |
|---|---|---|
| 编排与恢复 | TaskSpec、WorkItem DAG、追加式事件、Checkpoint、Lease epoch | 新 Worker 从持久事实继续，旧 Worker 不能继续提交 |
| 上下文与知识 | Mandatory Facts、确定性 Context Projection、Run Memory、revision-bound Code RAG | 压缩历史时不丢权限/验收，代码证据不会跨 revision 静默复用 |
| 动作与治理 | Model Gateway、typed Tool Calling、Tool Gateway、Campaign/Run 预算、有界 operator-guidance HITL | 模型提出动作，但不直接获得无边界执行权 |
| 验收与证据 | protected validation、Trace replay、EvidencePack、失败/unknown 原样保留 | “代码改了”与“任务通过”分开，失败也能审计 |

## CI EvidencePack

标准 [CI workflow](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/workflows/ci.yml)
在 Linux runner 上重新运行主作品集演示，删除三个 SQLite 数据库后调用独立 verifier，再上传
名为 `horizon-portfolio-evidence` 的 workflow artifact，保留 14 天。下载并解压后可再次运行：

```powershell
uv run --locked --cache-dir .uv-cache horizon demo verify-portfolio `
  <解压目录>/evidence-pack.json
```

同一个 workflow 还会发布更窄的 `horizon-process-timeout-evidence`，专门证明直接子进程 timeout
取消栅栏和同步 reap；它不是主作品集故事的替代品。

若被追问上下文窗口耗尽，可直接生成并脱库复核独立包：

```powershell
$result = uv run --locked --cache-dir .uv-cache horizon demo context-capacity-recovery |
  ConvertFrom-Json
uv run --locked --cache-dir .uv-cache horizon demo verify-context-capacity-recovery `
  "$($result.evidence_pack)"
```

标准 CI 同时发布 `horizon-context-capacity-evidence`。该包证明 2,000 字符门限先于模型派发持久
等待、下一 Lease epoch 与消息不变的新 session 恢复、最终 required check 通过；真实子进程回归
另证明 PID reap 与 decision/session 事务回滚。也可用安装后的能力清单快速定位实现 profile：

```powershell
uv run --locked --cache-dir .uv-cache horizon doctor |
  ConvertFrom-Json |
  Select-Object recoverable_context_capacity_stop, recoverable_context_capacity_stop_profile
```

这里的 `true` 仍只是 CLI 对当前实现范围的声明。面试时要把主写入崩溃包、上下文容量包和真实
子进程故障回归分开讲，不说主 Demo 同时执行了上下文超限，也不把 Scripted Model 成功外推为
真实模型任务成功率。

GitHub Actions artifact 通常需要登录 GitHub 后下载；它是 CI 发布的限时证据，不是匿名永久发布
资产。包内哈希提供自洽性与防意外修改能力，不是签名、可信时间戳或第三方来源认证。

## 高频追问与回答要点

### 1. 为什么不直接 retry？

因为进程退出只能证明没有 receipt，不能证明外部 effect 没发生。Horizon 先用持久化 intent、
调用 ID、工作区 revision 和前后态把结果分类为可确认、可回滚或 `unknown`；只有满足策略的
只读操作或已证明未生效的动作才允许重派。否则显式阻塞，避免重复写入和重复计费。

### 2. Lease epoch 和 PID 各解决什么？

PID 证明父进程实际等待和回收的是哪个直接子进程；Lease ID/epoch 是持久化提交权限。进程停止
后必须先留下 stop receipt，再释放该 Lease。即使旧 Worker 之后异常恢复，也不能用过期 epoch
继续提交状态。

### 3. Context、Memory、RAG、Trace 有什么区别？

- Context 是某次模型调用实际可见的有界视图；完整 transcript 不被投影覆盖。
- Run Memory 只保存控制器从工具结果中可重建、可失效的事实，不接受模型猜测冒充事实。
- Code RAG 从指定 workspace revision 检索带路径、范围和哈希的代码证据。
- Trace 是执行中真正发生过的事件，不是给模型看的知识库。

压缩只折叠已闭合的旧模型—工具单元；TaskSpec、权限、预算、required checks、未完成 tool call
和内容摘要 digest 不能静默丢弃。必保内容仍超限时会记录 typed HumanRequest、进入等待并释放
Lease，不通过删约束换取继续运行；只有本机配置同时达到持久化字符/token 最低值，才以消息完全
不变的新 session 边界继续。`--worker-process` 下父进程仍须先同步 reap 原 PID。

### 4. 这里怎样做“意图识别”？

首版不训练独立意图分类器。CLI 控制命令走确定性路由，软件任务通过结构化 TaskSpec 标记任务
类型、执行模式、路径权限和验收要求，再生成或读取 Plan。无法确定会改变权限、预算或验收的
当前实现不会让自由文本分类结果直接授权工具执行；通用澄清审批仍是设计边界，不冒充已完成的
HITL 功能。

### 5. Fallback 为什么默认不开？

静默换模型可能改变费用、数据流向、工具能力和上下文限制。当前 Provider 配置把
`fallback_enabled` 固定为 `false`，没有自动跨模型切换。设计上只有预先声明兼容 Provider/模型、
数据权限和独立预算后才应增加有界 fallback；这部分不能当作当前已实现能力。已有词法检索的
scan fallback 会显式标记 degraded，不能把 `evidence_missing` 包装成通过。

### 6. 为什么没有先上 Redis、Celery、Kubernetes 或多 Agent？

项目要先证明单机 Harness 的恢复不变量：状态权威、旧 Worker 围栏、副作用对账、预算和验收。
分布式调度会增加组件，却不会自动解决这些问题。当前边界明确是本地直接子进程；跨主机 Lease、
父控制器被硬 kill 后，持久 launch 阻止新 Lease、精确 PID 人工停止确认和原 session 续跑已有
离线故障注入证据；自动探活/重启、OS 灾难恢复与进程树回收仍属于后续生产化工作。

### 7. EvidencePack 到底证明了什么？

它证明包内文件的完整性、Trace 可重放、最终投影与指定语义关系一致。这个演示额外证明一次真实
子进程在精确写入 effect 后、receipt 前硬退出，旧调用没有重派，既有后态被精确接纳一次并由新
epoch 续跑成功。它不证明官方 SWE-bench 成绩、模型修复能力、恶意代码隔离认证、外部系统
exactly-once，也不证明任意崩溃窗口都已覆盖。

## 面试时不要说过头

- 不说“通过了 SWE-bench”；当前外部案例是来源绑定、作者选择且使用 Scripted Model 的评测。
- 不说“保证 exactly-once”；当前保证的是可审计 intent/receipt、保守 unknown 和有限窗口恢复。
- 不说“支持分布式容灾”；当前进程证据覆盖本地直接子 Worker。
- 不把九轮真实模型 Pilot 的可重放失败说成修复成功；它们是保留的负结果。
- 不把 Docker 限制参数说成安全沙箱认证。

更细的实现合同见[一键作品集演示](portfolio-demo.md)、
[直接子 Worker 超时取消演示](process-timeout-cancellation-demo.md)、
[三项目多阶段恢复评测](multi-stage-full-checkout-pilot.md)、
[能力设计](agent-capabilities-design.md)、[上下文投影](context-projection.md)和[Run Memory](run-memory.md)。
