# Horizon — Recoverable Coding Agent Harness

面向长程软件工程任务的可恢复 Coding Agent Harness。当前已经有一个可执行的、受预算
保护的顺序多 WorkItem Coding Agent 闭环：真实模型提出工具调用，Harness 在一次性工作副本中
执行受控读取、单文件或有界多文件精确编辑，并用 Linux Docker 做受保护验收。它仍不是完整的长程 Agent，
但已经补上精确写入崩溃处置、最多 8 个既有文件的受控 promotion，以及保留完整原始会话的确定性上下文
投影、控制器 MandatoryFactLedger 和有证据的 Run Memory。执行前现可用一次受预算模型调用
提出最多 8 项的 WorkItem DAG；控制器校验权限、DAG 和验收覆盖并保存来源 receipt，规划提交
窗口崩溃后可复用响应而不重复计费。无效自动计划不会把 Run 直接判死：Harness 会持久化
与模型响应证据绑定的人工替换请求，原子进入等待态并释放 Lease；本机 CLI 提交通过同一控制器
校验的 Plan 后可从 `READY` 继续，且不会自动再调用模型。执行模型还可基于当前 Run 证据显式
提交一次受限 `revise_plan`：已通过 WorkItem 必须原样保留，TaskSpec/验收/权限/预算不变，
  工具记账、Plan vN+1 和新 session 原子提交。它已有三个同一真实 Issue 的冻结失败 Run，但还没有
真实 Issue 成功或 benchmark 成绩，也尚未实现自然
语言 TaskSpec intake、自动或多次 replan、并行工作项、任意副作用恢复、语义压缩、Project
Memory、向量/符号检索、通用审批式 HITL 和自动模型 fallback。当前
`retrieve_code` 已提供 revision-aware 的本地 SQLite FTS5 词法 Code RAG，并对每个命中回查
immutable source Artifact；它还能有界展开 camelCase/snake_case 词项并优先同名 `def/class` 定义。
固定 5 案例本仓诊断、2 案例外部 checkout 命名变体诊断和 12 案例控制器策略诊断均可离线重复运行。
同一真实任务的三个 Pilot Run 都观察到 ranked retrieval 命中目标定义，但单一任务的失败轨迹仍不足以形成检索或
replan 效果结论。完整 Run A/B 现包含一个内部 fixture、
三个 BugsInPy 来源的依赖裁剪历史缺陷，以及 tqdm（82 files）与 youtube-dl（872 files）两个
干净、固定的完整 checkout：均先确认初始验收失败，再比较 Baseline 可恢复等待与单次 replan
成功，并保存禁网 Docker 验收与可重放 Trace。两个完整案例的 revision-bound Code RAG 均把目标
文件排在 rank 1，同时如实保留 9 和 2 个文件未索引的 degraded 结果。它们是 Harness 路径与
规模开销证据，不是真实模型或官方 benchmark 成绩。872 文件案例暴露的重复 CAS blob 校验已
修复：同进程同 revision 再快照从 19.931 秒降至 2.247 秒，完整双项目 suite 墙钟从 1124.65 秒
降至 340.00 秒；内容哈希、元数据变化失效、新进程复核和 Trace replay 仍保留。
在同一 youtube-dl 完整 checkout 上还完成了一个两阶段 production → regression-test 任务：
Baseline 在完成第一项后于第二项等待，Treatment 保留已完成项、只 replan 剩余项并通过最终两项
验收；两个 arm 都在 WorkItem 安全边界释放第一任 Worker、重新打开持久化适配器，再由
`lease_epoch=2` 的新 Worker 续跑。跨 revision RAG、active/stale Run Memory 和 86/116-event
Trace 均有内容寻址证据。第一次真实重启运行还保留了 60 秒租约在大仓校验期间到期的负结果；
修正为显式 600 秒后，fencing、Trace replay、最终 revision 与验收均通过。
同一 youtube-dl 完整 checkout 现有四个真实模型 Pilot Run：离线预检先绑定干净 buggy
commit、初始失败、源码 snapshot、Docker image、Provider policy 和费用 cap，再生成 solution-blind、
acceptance-visible 的内容寻址 TaskSpec/report。四轮分别暴露：不可执行的 Plan 和重复
exact search；无范围整文件读取与近期上下文硬保留；以及 Plan 猜测路径压过 rank 1 仓库证据、
下一请求预留超过 Campaign 余额；以及单边范围读取与单 Run 预留不足。四轮都没有编辑或验证，
source 未变、费用已结算、Trace 可重放。对应窄修复已完成离线回归，但仍没有真实 Issue 成功或
benchmark 成绩；第四轮的一次性付费授权已经用完。
后续零费用实现已把未来同类 Run/Campaign 派发前费用不足变成带 required/available 金额的
可重放 `FAILED`，并用 Scripted Model 证明单边范围错误能够收到结构化反馈、改为双边界读取后
完成编辑和保护性验收；历史第四轮 Trace 保持原样。
现在还提供一条命令的离线作品集演示：它在结构化读取错误后完成 epoch 1 → 2 Worker handoff，
从持久化会话继续修复和验证，并导出可自检的 Trace、最终投影、报告与 EvidencePack。该演示
不调用真实模型、不联网、外部费用为 0，也不宣称真实模型质量或官方 benchmark 成绩。

## 开发环境

需要 Python 3.12 或 3.13 与 uv；依赖安装在项目自己的 `.venv`。

```powershell
uv sync --locked
uv run --locked pytest
uv run --locked ruff check .
uv run --locked horizon --help
```

受限环境可以加 `--cache-dir .uv-cache`，避免写入全局缓存。
容器测试只使用明确指定且本机已有的镜像；不会自动拉取镜像或运行付费模型。

## 当前可以运行什么

最短的完整演示命令：

```powershell
uv run --locked --cache-dir .uv-cache horizon demo run
```

它会在 `.horizon/demos/` 下创建唯一证据目录且不覆盖旧结果。输出合同、完整性链和声明边界见
[一键作品集演示与 EvidencePack](docs/portfolio-demo.md)。

v0.1.0 提供不可变任务合同、人工或 one-shot 模型计划的 DAG/验收覆盖校验、版本修订、SQLite
追加式事件、幂等操作、Worker Lease/epoch、预算预留和结算、文件快照原语、取消、
Trace 导出及离线重放，以及 `read → edit → test → submit → protected validation` 循环。
人工给定的 Plan DAG 可按 dependency-ready/声明顺序执行多个 WorkItem；每项隔离会话和工具
权限，中间交接与最终成功均原子提交，最后一项重跑全部 required checks。模型和工具的
intent/receipt、用量、费用、工作区版本、检查点和验证证据
都会进入 Run 事件流；离线 reconciliation 能分类悬空调用、修复有可信 Run 回执的 Campaign
提交窗口。新模型响应先进入不可变 Artifact；若只差 Campaign settlement，新 Worker 可直接
继续该响应而不重复计费。每次模型调用还有一份内容寻址的 ContextProjection：达到字符预算
时只折叠旧的完整工具轮次，保留初始合同、最近轮次和未完成工具对。每次调用另把
Task/Plan/权限/验收/预算/模型策略/工具 Schema/workspace 绑定为内容寻址 MandatoryFactLedger，
恢复时逐项重建并校验。控制器还会把已结算的读取、检索、编辑和验证结果派生为有来源的
Run Memory；失败结果保留为失败，未知副作用保持 unresolved，旧 workspace revision 的观察
标为 stale 且不向模型暴露旧片段。每个模型请求绑定对应 Memory Artifact 和事件边界，恢复时
按历史边界重建，模型 `submit` 声明不能直接写入事实。
同一 revision 下完全相同的读取/检索/编辑重复两次，或精确动作形成 `A→B→A→B` 时，
NoProgressPolicy 会在下一步拒绝实际执行并回送证据；模型收到反馈后仍继续同一模式，则持久化
完整轮次，进入 `WAITING_FOR_USER` 并原子释放 Lease。
在软阻断后，模型也可选择单次 `revise_plan` 调整尚未通过的工作结构；无效提案只生成 error
observation，不能改写完成项或扩大合同。详细合同见
[执行证据驱动的单次受限 Replan](docs/execution-replanning.md)。
本机 `agent guide` 可把有界人工建议加入新的内容寻址会话，再由新 Worker 续跑；指导不会扩大
合同权限，也不消耗模型调用。详细合同见
[执行停滞后的可恢复人工指导](docs/operator-guidance.md)。
WorkItem 还可授权 `retrieve_code`，从当前 immutable manifest 的允许路径建立 revision-bound
FTS5/BM25 索引，返回带 path/range/hash 的 EvidencePack；自然语言与 camelCase/snake_case
标识符可互相生成有界词项，最多检查 256 个组合词候选以优先同名定义；无 FTS 或跳过文件时
显式标 degraded。
无法证明是否执行过的模型/工具调用仍保守标为 `unknown`；当前可显式处理单个只读调用，或
在工作区精确等于预期前态/后态时接纳、回滚一个 `replace_text` 或 `apply_patch`。后者一次
预校验并修改最多 8 个不同的既有 UTF-8 文件；部分写入保持 unknown，不会被误判为成功。
每个 Docker `run_check` 现用已持久化 tool call ID 派生唯一容器名和 owner/attempt/image 标签。
悬空检查可用原镜像精确查询；已停止容器由控制器验证并删除，仍在运行时只有显式
`--stop-check-sandbox` 才会终止。查不到标签容器不算停止证明，仍需操作者确认。处置只丢弃
未知结果，不推断 pass/fail，也不重放原模型调用，并要求工作区仍等于派发前 revision。
若自动 Plan 未通过 Schema、DAG、权限或验收覆盖校验，Run 会保持为
`WAITING_FOR_USER`；使用输出中的 `horizon plan set <run-id> <plan.yaml>` 提交人工计划后，
再用 `horizon agent resume` 继续。这个入口只处理计划阶段的人工替换，不代表通用审批系统。

```powershell
uv run --locked horizon doctor
uv run --locked horizon task validate examples/task.yaml
$run = uv run --locked horizon run examples/task.yaml --prepare-only --key example-001 | ConvertFrom-Json
uv run --locked horizon plan set $run.run_id examples/plan.yaml
uv run --locked horizon status $run.run_id
uv run --locked horizon cancel $run.run_id
uv run --locked horizon trace export $run.run_id --output .horizon/trace-example.jsonl
uv run --locked horizon trace replay .horizon/trace-example.jsonl

# Run 因确定性重复动作暂停后；guide 本身不调用模型。
uv run --locked horizon agent guide <run-id> .\guidance.txt

# 离线固定检索诊断；不加载模型、不联网、不执行仓库代码。
uv run --locked --cache-dir .uv-cache horizon eval retrieval `
  benchmarks/retrieval/horizon-lexical-v1.yaml --source .

# 固定 youtube-dl checkout 上的两类标识符命名变体；需先按完整 checkout 文档准备被 Git 忽略
# 的本地 fixture，同样零模型、零网络且不执行源码。
uv run --locked --cache-dir .uv-cache horizon eval retrieval `
  benchmarks/retrieval/youtube-dl-identifier-variants-v1.yaml `
  --source benchmarks/run_ab/full/youtube-dl-3-unescape-html/fixture

# 冻结控制器策略 Trace；不加载模型、不联网、不打开或执行 fixture 仓库。
uv run --locked --cache-dir .uv-cache horizon eval reliability `
  benchmarks/reliability/horizon-controller-v1.yaml

# 两条完整可重放 Agent Run；脚本模型零费用，验收使用本机已有禁网 Docker 镜像。
uv run --locked --cache-dir .uv-cache horizon eval run-ab `
  benchmarks/run_ab/stalled-reader-replan-v1.yaml --image redis:7-alpine

# 三个来源绑定的 BugsInPy 依赖裁剪案例；统一执行初始失败门和完整 A/B。
uv run --locked --cache-dir .uv-cache horizon eval run-ab-suite `
  benchmarks/run_ab/bugsinpy-reduced-v1.yaml --image python:3.12-alpine

# 两个完整 checkout 需先按 docs/full-checkout-pilot.md 固定源码。
uv run --locked --cache-dir .uv-cache horizon eval run-ab-suite `
  benchmarks/run_ab/bugsinpy-full-checkout-pilot-v1.yaml --image python:3.12-alpine

# 同一 youtube-dl fixture 上的两阶段 production + regression-test A/B；两 arm 均在阶段边界换 Worker。
uv run --locked --cache-dir .uv-cache horizon eval run-ab-suite `
  benchmarks/run_ab/bugsinpy-multi-stage-pilot-v1.yaml --image python:3.12-alpine

# 第四轮历史零费用预检命令；不读取 API Key、不调用模型，并绑定当时的源码指纹。
# 当前源码已改变，因此旧报告只能审计、不能再次启动付费 Run。
uv run --locked --cache-dir .uv-cache horizon eval pilot-preflight `
  benchmarks/run_ab/full/youtube-dl-3-unescape-html/real-model-pilot-budgeted-continuation.yaml `
  --image python:3.12-alpine `
  --config config/providers/siliconflow-pilot-budgeted-continuation.yaml `
  --state-dir .horizon/real-model-pilot-retry-v4
```

示例 TaskSpec 的仓库路径和 SHA 是占位值，仅验证控制面，不会执行示例测试命令。
`--prepare-only` 只创建运行记录；省略它会明确报错，不会假装完成任务。运行及证据默认
位于 `.horizon/`，不同进程使用同一 `--db` 即可查询已提交状态。相同幂等键返回原始
操作的历史回执；查看最新状态请用 `status`。

Trace 当前包含完整 TaskSpec，以及模型/工具调用的元数据、哈希和 Artifact 引用：**不要在
任务合同、计划或验收命令里嵌入密钥**。密钥不会写入 Run Trace，但当前仍未做生产级
全链路脱敏或独立安全复核。

## SiliconFlow 受控联调

非秘密配置已固定在 `config/providers/siliconflow.yaml`。本地复制 `.env.example` 为
`.env`，只填写 `SILICONFLOW_API_KEY`；该文件已被 Git 忽略，不要把真实 Key 写入其他
配置、TaskSpec、命令参数或 Trace。

```powershell
# 不联网、不计费；只验证严格配置、预算和 Key 是否可用。
uv run --locked --cache-dir .uv-cache horizon model check

# 不联网；查看 CNY 3 元 Campaign 的持久账本。
uv run --locked --cache-dir .uv-cache horizon model budget

# 会发起一次有硬预算保护的真实 Tool Calling；只有明确需要时才运行。
uv run --locked --cache-dir .uv-cache horizon model probe --confirm-paid

# 会在一次性 staging 副本中运行首个受控 Coding Agent，并在本机 Docker 中验收。
# 必须使用已经存在的镜像；不会自动拉取，也不会把改动提升回源目录。
uv run --locked --cache-dir .uv-cache horizon agent run `
  examples/agent-task.yaml examples/agent-plan.yaml `
  --image redis:7-alpine --confirm-paid

# 也可省略人工 Plan，额外使用一次受同一 Run/Campaign 预算约束的规划调用。
uv run --locked --cache-dir .uv-cache horizon agent run `
  examples/agent-task.yaml --auto-plan `
  --image redis:7-alpine --confirm-paid

# 不要复用第四轮的旧报告或 CNY 0.11 cap：retry Campaign 当前余额只有 CNY 0.0681666。
# 任意后续付费 continuation 都须先生成绑定当前源码的新 preflight，使用不超过剩余额度的新合同，
# 并取得新的明确外发和费用授权。

# 长任务可在完整模型—工具轮次后安全让出，再由新进程继续。
$partial = uv run --locked --cache-dir .uv-cache horizon agent run `
  examples/agent-task.yaml examples/agent-plan.yaml `
  --image redis:7-alpine --slice-iterations 1 --confirm-paid | ConvertFrom-Json
uv run --locked --cache-dir .uv-cache horizon agent resume $partial.run_id `
  --image redis:7-alpine --confirm-paid

# 异常退出后先对账；不调用模型、不联网、不产生新费用。
# 旧 Lease 曾存在时，必须等它过期并确认旧进程已停止。
uv run --locked --cache-dir .uv-cache horizon agent reconcile $partial.run_id `
  --confirm-old-worker-stopped

# reconcile 返回某个单一只读 tool call 为 unknown 时，可显式授权重新读取；仍不联网、不付费。
uv run --locked --cache-dir .uv-cache horizon agent resolve-tool `
  $partial.run_id <tool-call-id> --retry-readonly

# 写工具的结果必须与派发前 manifest 推导出的唯一前态/后态完全一致。
uv run --locked --cache-dir .uv-cache horizon agent resolve-tool `
  $partial.run_id <tool-call-id> --accept-write
# 或：--rollback-write；旧 --accept-replace/--rollback-replace 是兼容别名。

# 优先查询 tool call ID 对应的标签容器；若仍运行，必须显式授权停止这一精确 attempt。
uv run --locked --cache-dir .uv-cache horizon agent resolve-tool `
  $partial.run_id <tool-call-id> --discard-check --image redis:7-alpine
# 若返回仍在运行，追加 --stop-check-sandbox；若容器不存在，人工核实后改用
# --confirm-check-sandbox-stopped。任何路径都不采信未知检查结果。

# 成功 Run 可先只读查看候选差异，再显式提升最多 8 个既有 UTF-8 文件修改。
uv run --locked --cache-dir .uv-cache horizon agent diff `
  $partial.run_id --source <原始仓库目录>
uv run --locked --cache-dir .uv-cache horizon agent promote `
  $partial.run_id --source <原始仓库目录> --confirm-promote
```

当前 Provider 关闭 thinking、自动重试和 fallback；任何不确定失败均保留费用预留。
`agent reconcile` 只自动修复能由已提交证据唯一确定的状态：Provider 派发前的 Campaign-only
预留按 0 释放；完整响应 Artifact + Run receipt 可补齐 Campaign 并继续。单一只读工具只有在
原模型响应/参数 hash/事件尾部一致、工作区未漂移且用户提供 `--retry-readonly` 时才会把旧尝试
以 `cancelled` 结算并保守计数一次，然后生成新的 tool call。单一 `replace_text` 或最多 8 文件
的结构化 `apply_patch` 可在 live workspace 与预期 effect 或原 manifest 完全一致时显式
accept/rollback；部分写入或外部漂移继续阻塞。`run_check` 初始也返回
`manual_reconciliation`；其中单一调用可经上述显式确认取消未知结果并恢复到下一轮，
但绝不自动重试或把它记为验证事实。Agent 入口支持有界的顺序多 WorkItem DAG、一次性自动计划、一次
执行期受限 Plan revision、精确字符串替换、受控多文件 patch 和控制器定义的检查；它不支持
自动触发或多次 replan，也不并行执行，
也不支持任意 diff、文件新增/删除或模糊 patch。自动计划的 context、预算、拒绝和恢复合同见
[一次性自动计划生成](docs/automatic-plan-generation.md)。完整 Provider 数据流、
预算公式、真实联调证据和剩余边界见
[SiliconFlow Provider 接入](docs/siliconflow-provider-integration.md)。

## 验证

```powershell
# 无 Docker/无模型的测试；Docker 用例会明确跳过。
uv run --locked pytest -q -p no:cacheprovider

# 如本机已有其他合适镜像，可显式替换；不会自动下载。
$env:HORIZON_TEST_DOCKER_IMAGE = 'redis:7-alpine'
uv run --locked pytest -q -p no:cacheprovider
uv run --locked ruff format --check src tests
uv build
```

当前离线全量回归为 **281 passed，5 skipped**；5 个跳过项指定本机已有
`python:3.12-alpine` 单独复跑，得到 **5 passed** 的真实 Docker 契约结果。另有一次真实
SiliconFlow + Docker 的受控 fixture Run 通过；这是历史联调证据，不是 benchmark 或真实
Issue 效果。新增真实模型 Pilot 单元测试覆盖私有答案拒绝、初始失败证据、内容寻址报告和
启动时任务/源码/镜像/Provider/Harness 源码漂移拒绝；真实 checkout 的离线 Docker preflight
已通过。四轮真实模型负结果均已导出并可重放，分别驱动 Plan/搜索反馈、有界行读取/
近期完整单元应急压缩/异常租约释放、“不猜路径、优先 revision-bound 证据、默认 rank 1”，
以及范围参数成对约束/规划输入收窄。retry Campaign 当前仍剩 `CNY 0.0681666`，本系列实际
累计付费 `CNY 0.1770732`；第四轮源码修复已使旧 preflight 失效，没有自动重试或 fallback。
未来确定性费用拒绝不再留下无 Lease 的 `RUNNING`：控制器会持久化类型化 `BudgetStop`、进入
`FAILED` 并在 CLI/status 中报告所需与可用金额；unknown 用量仍保守进入对账路径。
详情见下方文档。故障测试实际强制退出
子进程，覆盖 Run receipt、response Artifact、精确写入副作用和 promotion receipt 附近的提交
窗口；它们仍不等于设计里的全部故障矩阵。新增顺序双 WorkItem E2E 覆盖原子阶段交接、跨
Worker 续跑和最终 required checks 回归修复。自动计划、上下文、Run Memory、多 WorkItem
与词法检索测试使用 Fake Model 或纯本地
SQLite，不产生网络费用。

Docker 测试以非 root、禁网、只读根目录、资源限制运行，仅挂载本项目的临时副本。
默认不转发宿主环境变量，禁用容器日志落盘；只删除带本次唯一标签的容器及其匿名卷。
这些是有限契约验证，不是对任意恶意代码的隔离保证，也未做独立安全复核。

详细结果与未完成项见 [开发进度与验证记录](docs/development-progress.md)。

## 开源与贡献

Horizon 原创代码和文档使用 [MIT License](LICENSE)。依赖裁剪的外部 benchmark fixture 继续
遵循各自上游许可证，来源、固定 commit 与许可证见
[Third-party notices](THIRD_PARTY_NOTICES.md)。版本变化记录在 [Changelog](CHANGELOG.md)。

提交修改前请阅读 [Contributing guide](CONTRIBUTING.md)；安全问题和当前隔离边界见
[Security policy](SECURITY.md)。GitHub CI 只运行锁定依赖下的离线测试、静态检查、一键演示和
构建，不读取 Provider Key、不调用真实模型，也不自动拉取 Docker 镜像。

## 设计与实现边界

- [详细开发设计](docs/coding-agent-development-design.md)
- [Agent 能力设计](docs/agent-capabilities-design.md)
- [需求追踪矩阵](docs/requirements-traceability.md)
- [当前开发进度](docs/development-progress.md)
- [SiliconFlow Provider 接入](docs/siliconflow-provider-integration.md)
- [首个受控 Agent 闭环实现说明](docs/bounded-agent-loop.md)
- [一次性自动计划生成与恢复合同](docs/automatic-plan-generation.md)
- [确定性费用停止语义](docs/budget-stop-semantics.md)
- [可恢复的顺序多 WorkItem 执行](docs/multi-work-item-execution.md)
- [执行证据驱动的单次受限 Replan](docs/execution-replanning.md)
- [确定性上下文投影与恢复合同](docs/context-projection.md)
- [证据驱动的 Run Memory](docs/run-memory.md)
- [精确模式无进展保护](docs/no-progress-policy.md)
- [有界多文件精确 Patch 与崩溃恢复](docs/bounded-multi-file-patch.md)
- [Revision-aware 词法 Code RAG](docs/code-retrieval.md)
- [词法检索离线诊断](docs/retrieval-evaluation.md)
- [控制器可靠性策略离线评测](docs/reliability-evaluation.md)
- [完整 Run A/B：停滞等待与单次 Replan](docs/run-ab-evaluation.md)
- [外部来源 Run A/B Suite：BugsInPy 依赖裁剪复现](docs/external-run-ab-suite.md)
- [完整上游 Checkout Suite：BugsInPy tqdm-1 与 youtube-dl-3](docs/full-checkout-pilot.md)
- [完整 Checkout 多阶段 Pilot：youtube-dl-3](docs/multi-stage-full-checkout-pilot.md)
- [真实模型 Pilot：预检、首轮负结果与付费边界](docs/real-model-pilot.md)

既有 79 项需求继续保留。当前采用直接实现、自检和风险触发复核；历史工作流标签不构成
产品运行时依赖。测试、实现、真实任务效果、独立安全复核分别报告。
