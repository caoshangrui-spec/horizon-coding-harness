# 开发进度与验证记录

更新：2026-10-04。已发布版本为 0.1.0，主干继续积累 Unreleased 改动；技术设计基线继续为
0.2.0。2026-09-30 的
可靠性内核证据保留，本次在其上增加 Provider 垂直切片和首个受控 Coding Agent 闭环。
本文记录实现事实与自检，不是全项目验收或独立安全审核。测试报告时间戳来自执行机器；
确定性测试中的时钟是注入值，不用它推断实际运行日期或耗时。

## 当前交付范围

已从可靠性内核推进到一个可执行的顺序多 WorkItem Agent Loop。现有 79 项要求全部保留，
未完成项没有被删除或变成可选项。不自动运行完整三角色，也不补齐旧 meta；普通开发
由当前 Agent 执行并自检，关键风险与重要结论仍须按需独立复核。

| 已实现部分 | 实际文件 | 本轮验证边界 |
|---|---|---|
| TaskSpec、权限模式、严格预算字段、不可变合同 | [task.py](../src/horizon/domain/task.py) | Schema/授权上限/序列化；未验证真实仓库来源 |
| 工作项 DAG、required 验收覆盖、候选调度 | [plan.py](../src/horizon/domain/plan.py) | 人工、one-shot 模型计划和单次执行期 revision 都经过领域层验收/工具权限复核；新计划还要求每个 acceptance ID 只有一个 owning WorkItem，历史 Trace 投影保留旧合同兼容；无自动触发或多次 replan |
| 一次性自动计划与恢复 | [planning.py](../src/horizon/application/planning.py)、[planning.py](../src/horizon/domain/planning.py) | 内容寻址 PlanningContext、1～8 项 DAG、权限/验收唯一归属校验、费用记账、settled response 复用；首个真实计划暴露不可执行分解并已转成准入回归，仍无计划成功率评测 |
| 计划阶段持久人工 fallback | [human.py](../src/horizon/domain/human.py)、[services.py](../src/horizon/application/services.py) | 非法模型 Plan 绑定原 response/Task/version 后进入 WAITING 并释放 Lease；本机人工计划原子记录决定并回到 READY；仅此窄场景，不是通用 HITL |
| NoProgress 人工指导恢复 | [human.py](../src/horizon/application/human.py)、[human.py](../src/horizon/domain/human.py) | 相同行为第 4 次或精确 A/B 循环第 6 步，将 pattern、工具证据、Task/Plan/WorkItem/workspace/session 绑定后进入 WAITING；本机指导写入新 session，新 Worker 续跑；不是通用审批或自动 replan |
| Run 状态投影、终态和成功前置检查 | [run.py](../src/horizon/domain/run.py) | 状态规则；顺序 WorkItem DAG、单次 vN→vN+1 revision 和最终 required checks 全量回归已接入；无并行或多次 replan |
| 追加事件、事务、幂等回执、历史合同、重放 | [sqlite.py](../src/horizon/adapters/persistence/sqlite.py) | SQLite 重启、投影损坏/删除、并发写、真实进程退出 |
| Lease/epoch、取消、合同修订、预算账本 | [services.py](../src/horizon/application/services.py) | 旧 Worker 拒绝写入；Run 内模型/工具 intent、receipt、unknown 与硬预算事件化 |
| 不可变内容寻址文件产物与新目录恢复 | [artifacts.py](../src/horizon/adapters/persistence/artifacts.py)、[snapshot.py](../src/horizon/adapters/workspace/snapshot.py) | 文本/二进制文件；同进程已完整验证且元数据未变化的 CAS 去重命中不重复读 blob，变化或重启后重新验 hash；不恢复 Git 对象、进程或网络状态 |
| 检查点产物引用与事件原子提交 | [checkpoints.py](../src/horizon/application/checkpoints.py) | 产物校验、游标 CAS、拒绝未结算动作；调度静止由未来 supervisor 保证 |
| 临时副本 Docker 执行与受保护验收 | [docker.py](../src/horizon/adapters/sandbox/docker.py)、[validation.py](../src/horizon/adapters/sandbox/validation.py) | 非 root、禁网、只读根、超时、输出有界；模型只能选择控制器冻结的 check ID |
| Typed Tool Gateway | [gateway.py](../src/horizon/tools/gateway.py) | 搜索、读取、单文件精确替换、最多 8 文件的结构化精确 patch、受保护检查和 submit；路径/链接/大小受限，每次 intent/receipt 持久化；exact search 饱和时显式标记截断并引导 ranked retrieval；`retrieve_code` 默认只返回 rank 1，需要时才显式扩大；大文件整读拒绝并要求最多 400 行、32 Ki 字符的范围读取 |
| 顺序多 WorkItem Agent Loop | [agent_loop.py](../src/horizon/application/agent_loop.py) | dependency-ready 调度、逐项会话/权限/验收、原子交接、最终全量回归、有限 repair 与单次受限 replan；不支持并行或自动/多次 replan |
| 执行证据驱动的受限 Replan | [plan.py](../src/horizon/domain/plan.py)、[services.py](../src/horizon/application/services.py)、[agent_loop.py](../src/horizon/application/agent_loop.py) | 模型显式 `revise_plan`，最多 1 次成功；完成项逐字段不可变，工具 receipt、Plan vN+1、新 session 同事务；NoProgress 后成功、跨项保留、非法提案回退和 Trace 重放已测；未做真实模型效果评测 |
| 确定性 ContextProjection + MandatoryFactLedger | [context.py](../src/horizon/application/context.py)、[context.py](../src/horizon/domain/context.py) | 完整 transcript 留存；候选投影同时满足字符上限与覆盖工具 Schema 的完整请求保守 token 上界，近期完整单元在任一硬上限需要时只折叠最少数量，incomplete 单元绝不折叠；Projection/Reservation 绑定版本化 estimator、请求字节、上界与配置 cap；Task/Plan/权限/验收/预算/策略/工具 Schema/workspace 另做内容寻址绑定；不是精确 tokenizer、语义压缩或 Project Memory |
| 证据驱动的 Run Memory | [memory.py](../src/horizon/application/memory.py)、[memory.py](../src/horizon/domain/memory.py) | 从工具事件和内容寻址输出派生；保留失败/unknown，按 workspace revision 失效并绑定模型请求恢复边界；仅 run scope，不是 Project Memory |
| 精确模式无进展保护 | [agent_loop.py](../src/horizon/application/agent_loop.py) | 同一 revision 下，相同精确动作第 3 次、A/B 精确循环第 5 步软阻断；继续模式分别在第 4/6 步进入可恢复人工等待；不是语义或任意周期检测 |
| Revision-aware 词法 Code RAG | [retrieval.py](../src/horizon/domain/retrieval.py)、[sqlite_fts.py](../src/horizon/adapters/retrieval/sqlite_fts.py)、[retrieval_eval.py](../src/horizon/application/retrieval_eval.py) | SQLite FTS5/BM25、有界 EvidencePack、权限/revision/hash 回查、显式 scan fallback；camelCase/snake_case、同名定义路径语境和不同文件优先已测，内部 5+2+2 与外部 3 案例的成功/负结果均记录；dirty revision 可重建新排名并重放旧证据；外部盲测仍为 Hit@1 0/3，且无 AST/向量/symbol 图或真实模型收益结论 |
| 冻结控制器策略评测 | [reliability.py](../src/horizon/domain/reliability.py)、[reliability_eval.py](../src/horizon/application/reliability_eval.py) | 12 案例/41 判断覆盖精确 NoProgress 与单次 replan 接受/拒绝；生产路径共享判定函数、报告内容寻址、零外部调用；不是模型或真实 Issue 效果评测 |
| 完整 Run A/B 与来源绑定 Suite | [run_evaluation.py](../src/horizon/domain/run_evaluation.py)、[run_ab_eval.py](../src/horizon/application/run_ab_eval.py)、[scripted.py](../src/horizon/adapters/model/scripted.py) | 同一 Task/Plan/workspace 的 baseline 与单次 replan arm；先真实确认初始失败，再检查 EventLog/预算/Gateway/Trace；1 个内部、3 个 BugsInPy 依赖裁剪案例，以及 tqdm 82 files、youtube-dl 872 files 两个完整 checkout 经禁网 Docker 通过；完整案例还验证干净 Git HEAD 与 Code RAG，仍是脚本模型 |
| 完整 checkout 多阶段/重启 A/B | [multi-stage.yaml](../benchmarks/run_ab/full/youtube-dl-3-unescape-html/multi-stage.yaml) | youtube-dl 872 files 上 production → regression-test 两个依赖 WorkItem；两 arm 均在安全边界换为 epoch 2 Worker，跨 revision RAG、active/stale Run Memory、完成项保留 replan、最终两项 required checks 与 Trace replay 通过；CRLF 精确替换失败和 60 秒重启 Lease 到期均作为负结果保留 |
| 真实模型 Pilot | [pilot.py](../src/horizon/domain/pilot.py)、[pilot.py](../src/horizon/application/pilot.py)、[real-model-pilot.md](real-model-pilot.md) | 离线预检绑定完整干净 checkout、初始失败、source snapshot、Docker image、Provider policy、Harness 源码指纹、费用 cap 和首次规划保守预留。四个付费 Run 分别暴露计划/检索、无界读取/上下文、Plan 假设路径/保守预留，以及单边范围读取/单 Run 预留问题；四次均无编辑/验证，Trace 可重放、source 未变。对应窄修复、离线演示、tqdm 新候选预检及后续零费用检索诊断在当前主干已有 293 项回归；没有继续付费运行 |
| 安全轮次续跑 | [agent.py](../src/horizon/domain/agent.py)、[agent_loop.py](../src/horizon/application/agent_loop.py) | 消息 Artifact + event/revision 绑定；新 Worker 续跑；PLANNING/READY 也可恢复，任意崩溃窗口对账未完成 |
| 悬空调用恢复与对账 | [recovery.py](../src/horizon/application/recovery.py)、[model_recovery.py](../src/horizon/application/model_recovery.py)、[tool_recovery.py](../src/horizon/application/tool_recovery.py) | Campaign-only 预留释放；response Artifact 跨 Worker 续跑；只读重试；精确 `replace_text` / `apply_patch` accept/rollback；Docker `run_check` 用 call ID 标签 attempt，可查询/显式停止/删除后丢弃未知结果，missing 仍需人工确认；部分 patch 与无停止证明的验证副作用仍阻塞 |
| 受控候选提升 | [promotion.py](../src/horizon/application/promotion.py)、[promotion.py](../src/horizon/adapters/workspace/promotion.py)、[git.py](../src/horizon/adapters/vcs/git.py) | 只读 diff、源/候选 revision 与可选 Git HEAD 绑定、显式 1～8 个既有文件修改、完整/部分 effect 崩溃恢复；不创建 commit |
| 任务准备、计划、状态、取消、执行、导出与重放 CLI | [app.py](../src/horizon/interfaces/cli/app.py) | `run` 保持 prepare-only；`agent run` 接受 PLAN_PATH 或 `--auto-plan` 且只操作 staging；`agent resume` 支持 PLANNING/READY/RUNNING；执行前 HorizonError 仅在无非 unknown 在途 reservation 时释放租约；promotion 需显式确认 |
| SiliconFlow 严格配置与 OpenAI-compatible adapter | [config.py](../src/horizon/adapters/model/config.py)、[openai_compatible.py](../src/horizon/adapters/model/openai_compatible.py) | 真实 Tool Calling 探针和历史 fixture Agent Run 通过；完整 checkout Pilot 已真实调用并以受控终态失败，无隐式 retry/fallback |
| CNY Campaign 与 Run 模型费用账本 | [campaign_budget.py](../src/horizon/adapters/persistence/campaign_budget.py)、[run.py](../src/horizon/domain/run.py)、[budget-stop-semantics.md](budget-stop-semantics.md) | Campaign 跨重启硬上限；Run 绑定 CNY policy 并事件化；确定性派发前费用不足携带 reason/scope/required/available 原子进入 `FAILED` 并清除 Lease，unknown 用量仍保守对账；TaskSpec 旧 USD 字段尚未迁移 |
| 一键离线作品集 EvidencePack | [portfolio_demo.py](../src/horizon/application/portfolio_demo.py)、[portfolio_demo.py](../src/horizon/domain/portfolio_demo.py)、[portfolio-demo.md](portfolio-demo.md) | `horizon demo run` 复用真实事件/Lease/会话/RAG/Gateway/验证主链路，在结构化工具错误后由 epoch 2 Worker 续跑；Trace 重放、最终投影、报告和摘要由 SHA-256 清单自检。零网络/零真实模型/零外部费用；只是固定 fixture 的 durable handoff，不是硬崩溃、真实模型或官方 benchmark 证据 |

2026-10-04 的零费用增量把原先仅用于费用预留的完整请求保守上界接入实际派发门禁。
`ContextProjection` 升为 schema v2；`InputTokenEstimate` 固定记录算法 ID、规范请求 UTF-8
字节数和上界，`InputTokenBudget` 再绑定配置 cap。执行投影会对每个候选重新构造包含工具
Schema 的请求，并与字符门共同决定是否继续折叠；自动规划和 probe 在联网前拒绝超限请求，
pilot preflight 的 `ready` 也包含同一门槛。费用 reservation 与上下文门复用同一个 estimate，
但 Provider 实际 usage 仍单独结算，两者没有混写。该增量没有调用真实模型、网络或产生费用。

`domain/` 不导入基础设施、执行后端或上游 SDK，已有自动结构检查。
`EventStorePort` 隔离存储实现；没有为了通过测试而在真实执行路径回退到宿主 Shell。

## 实际环境和依赖

- Windows 主机：Python 3.12.4；项目独立 `.venv`；uv 0.11.18。
- SQLite 3.45.3；本机 FTS5 探针成功，已用于 revision-bound 词法 Code RAG 派生索引。
- Docker Client/Server 29.8.0，Linux 容器；受限会话需要额外进程权限才能连接 daemon。
- 容器合同使用已有 `redis:7-alpine` 的 shell，不启动 Redis 服务；完整 Python A/B 使用固定
  `python:3.12-alpine`。评测路径自身均保持 `--pull never`。
- 实际镜像 ID：Redis 为 `sha256:520775a41a63e77e06c73e35d2fd9cc15921a609516818796b4ecbb813078bc7`；
  Python 为 `sha256:0687a6bc9716edc2a6ee0fbfb0f87e7ee358b262b67c9215de91bc9b2d38ba71`。
- 完整 Python 依赖及哈希见 [uv.lock](../uv.lock)，`uv lock --check` 成功。
- mini-SWE-agent/SWE-ReX 尚未安装、固定或接入；本轮没有宣称底座 Spike 通过。
- 已按用户授权固定 SiliconFlow、`deepseek-ai/DeepSeek-V4-Flash`、Campaign CNY 3 元；历史
  fixture 使用单 Run CNY 1 元，首个完整 checkout Pilot 使用 CNY 0.25 cap，后续复跑共用
  CNY 0.18 不可变 Campaign。已执行 Tool Calling 探针、受控 fixture Agent Run 和四个真实任务
  Pilot Run；四个 Pilot 都未修好 Issue，不构成 benchmark 成绩。

Docker 限制参数按[官方运行文档](https://docs.docker.com/reference/cli/docker/container/run/)
核对。mini-SWE-agent 的[原生工具与消息边界](https://mini-swe-agent.com/latest/advanced/v2_migration/)
仍是后续适配约束；不能以该文档代替本项目的实际联调。

## 验证结果

### 2026-10-04 第二批独立外部定位 holdout

- v7 完成后，Luigi 1、Sanic 1、Tornado 1 的错误/修复 commit、公开 failing-test 名和查询先冻结
  在 commit `83ca2c1f0098720ada888fbfb7186f269fd3172f`，之后才以 name-status-only 方式揭示
  production gold path；没有读取补丁正文、执行仓库代码或调用模型。
- 三个真实修复文件均为 **rank 1**，因此 Hit@1/Hit@5/Micro Recall/MRR 都是 1.0。内容寻址
  report ref、完整 top 5 与 source HEAD 见
  [`result-v7.yaml`](../benchmarks/retrieval/external-holdout-v2/result-v7.yaml)。
- 291 个 scoped 文件中索引 252、跳过 39；Luigi 的二进制/单行压缩静态资源使其 degraded，
  Tornado 的 3 个二进制测试 fixture 也使其 degraded，Sanic 为 ok。目标 production 文件均未
  被跳过。该结果是 3 个作者选择案例的定位证据，不是官方 BugsInPy 分数或端到端修复成功率。
- 新 holdout 没有复现首批 0/3 Hit@1 缺口，因此当前不增加 symbol、embedding 或 vector
  组件；下一阶段转向 Agent 的“检索证据是否被正确消费并产生有效修改”。

### 2026-10-04 外部定位盲测与路径多样化

- 在未查看补丁和 gold path 前，把 Cookiecutter 2、HTTPie 2、The Fuck 1 的上游标题查询冻结在
  commit `74ddaaec2cd1ebdf79056777e5369102b59fa9c3`；PySnooper 1 因公开 Issue 正文直接给出目标
  文件和行而被排除。随后只用 `git diff --name-status` 揭示三个修复提交的 production path，
  没有执行外部仓库代码或调用模型。
- v6 盲测基线为 **Hit@1 0/3、Hit@5 3/3、Micro path recall 3/4=0.75、MRR
  0.416667**；Cookiecutter 目标 rank 4，HTTPie 只召回 2 个 gold path 中的 1 个，三个 report ref
  与完整返回路径见
  [`baseline-v6.yaml`](../benchmarks/retrieval/external-blind-v1/baseline-v6.yaml)。
- v7 只做通用路径多样化：保留原 rank 顺序，但先给不同文件分配结果槽，再用同文件后续 chunk
  补足。未增加依赖、服务或 gold 特例。处理后 **Hit@1 仍为 0/3**，Hit@5 保持 3/3，Micro
  path recall 提升到 **4/4=1.0**，MRR 为 0.444444；结果见
  [`treatment-v7.yaml`](../benchmarks/retrieval/external-blind-v1/treatment-v7.yaml)。该 treatment 已使用
  基线失败信息，不是新的独立 holdout。
- 内部 5 案例维持 5/5、MRR 0.9；同名符号维持 Hit@1 2/2、MRR 1、leakage 0；youtube-dl
  两例维持 rank 1。针对性检索回归 24 项通过；全量离线回归 **293 passed，6 skipped**。
  Docker 路径未受本增量影响，本批未重复运行 6 项 Docker 合同。

### 2026-10-04 同名符号与 dirty-revision 检索诊断

- 冻结 2 个同名 `render_invoice` Hit@1 案例。v5 基线为 **0/2 命中、MRR 0、leakage 2**，
  report ref `7ef81a374121e54b1e6e6cde6ed4fe6ca3b345f218fff275ba79a3399a4287a9`。
- v6 将 token 内的复合标识符与整句组合候选分开，并只在确实命中同名定义时比较模块路径
  token；处理后为 **2/2 命中、MRR 1.0、leakage 0**，report ref
  `022383e9996623c69335c9b1d8e4ca6cec8426cadccd89c06adfae59eed10465`。
- 首次广义路径加权使原 5 案例临时退化为 **4/5**；该负结果没有被当作成功。规则收窄后原
  清单恢复 **5/5、MRR 0.9**，report ref
  `4fb244757043f2a211fac19b7fc622c7bd98b34e98e250ec4de72771ca335103`；youtube-dl 2 案例仍
  全部 rank 1，report ref `84448f19b56575084e257323a3f29ab429f05aa262f2dc7b8a09560739897560`。
- 新增同一 workspace 前后 snapshot 测试：目标符号移动后 revision/index key 改变，新索引指向
  新模块，旧 manifest 仍重放原 EvidencePack；FTS5 与 scan fallback 使用同一消歧规则。
- 该批次离线全量回归：**291 passed，6 skipped**；真实 Docker 合同另行补跑，不把 skip 算通过。
  本增量不加载 Provider、不调用模型、不联网、不执行被检索夹具代码，外部费用为 0。

### 2026-10-04 完整请求 input-token 门禁

- 离线全量回归：**287 passed，6 skipped**；skip 均为未指定 Docker image 的显式分支。
- 随后指定本机已有 `redis:7-alpine` 复跑真实 Docker 契约：**6 passed**。
- `ruff check src tests`、`ruff format --check src tests` 与
  `uv --cache-dir .uv-cache lock --check --offline` 通过。
- 新增测试覆盖多字节内容在字符预算内但 token 上界超限时的确定性折叠、不可压缩单元的
  派发前拒绝、Projection/Reservation 完整请求估算一致、自动规划零派发拒绝，以及 pilot
  preflight 的 input-budget readiness gate。全部使用 Fake Model/本地文件；没有网络或费用。

### 2026-10-03 Provider、完整 Checkout 与快照优化

- 2026-10-03 当前离线全量回归为 **282 passed，6 skipped**；6 项跳过项指定本机已有
  `python:3.12-alpine` 单独复跑，得到 **6 passed** 的真实 Docker 契约结果。
- Pilot 合同测试覆盖 checked-in CNY 0.25 首轮、CNY 0.18 复跑 Campaign 和已执行的 CNY 0.11
  收窄 continuation policy、累计最坏费用
  `CNY 0.2452398`、私有修复信息进入 TaskSpec 时拒绝、初始失败证据、prepared Task/report 内容
  寻址，以及任务/Provider/source/image/Harness Python 源码在启动前漂移时拒绝。
- `ruff format --check src tests`、`ruff check src tests`、`uv --cache-dir .uv-cache lock --check` 和
  `uv --cache-dir .uv-cache build` 通过；源码分发包与 wheel 成功生成。公开 Markdown
  文档中的本地链接已检查到现有、未被 Git 忽略的目标。
- 新增安全轮次续跑测试会释放第一任 Worker Lease，重新打开 SQLite 和 ArtifactStore，由
  第二任 Worker 从下一 iteration 完成任务；workspace drift 或会话后的额外操作事件均在
  新模型调用前拒绝。
- 新增悬空调用对账测试覆盖：Run 已结算/Campaign 未结算的确定性修复、两本账同时 reserved、
  Campaign 已 unknown、Campaign 已结算但模型响应缺失、工具 intent 无 receipt，以及跨账本
  request hash 不一致时零修改拒绝。所有不确定副作用均保留占用且不会自动重派。
- 两项新增故障测试实际让子进程在 Run receipt 提交后、Campaign settlement 前，以及工具
  副作用写入后、receipt 前硬退出；新 Worker 接管后分别确定性补账和保守阻塞，未重放工具。
- 模型响应现在在 Run settlement 前写入不可变 Artifact。恢复 E2E 会在 Campaign settlement
  被中断后补账，由第二任 Worker 消费原响应、执行其工具调用，再仅发起下一轮模型调用；
  另测 Campaign-only 预留在 Provider 派发前按 0 释放，原模型调用次数保持 0。
- 新增第三项真实子进程硬退出：Run response Artifact/receipt 已提交而 Campaign 尚未结算时
  直接 `os._exit`，父进程仅依赖 SQLite/Artifact 恢复；恢复后原模型响应没有重派或重复计费。
- 新增最小人工处置：`agent resolve-tool --retry-readonly` 只接受单一 `search_repo`/`read_file`/
  `retrieve_code`
  intent，绑定原响应、参数 hash、事件尾部和 workspace revision；旧尝试按一次调用保守计数并
  留下 `cancelled` receipt。workspace drift 保持 unknown、拒绝续跑。
- 新增悬空检查处置：单一 `run_check` 只有在可信 CLI 收到
  `--discard-check --confirm-check-sandbox-stopped`、原响应/check ID/WorkItem scope/事件尾部匹配且
  workspace revision 未漂移时，才原子写入 `cancelled/discard_check` receipt 与下一 session。
  未知输出不会进入会话或 Memory，不推断 pass/fail，也不重放原模型调用；缺少确认继续阻塞。
- Docker 检查现把 reservation 的 tool call ID 传到执行器，确定性派生容器名并写入
  `horizon.owner/attempt/image` 标签。`resolve-tool --discard-check --image ...` 在任何 Docker
  操作前先验证原响应、scope、事件尾部与 workspace；停止容器可直接核验并删除，运行中容器
  只有追加 `--stop-check-sandbox` 才会终止，owner/attempt/image 任一不符都拒绝。missing 不被
  当作停止证明，仍需 `--confirm-check-sandbox-stopped`。单元/CLI 测试覆盖 running/stopped/mismatch，
  真实 Docker 合同覆盖正常自动清理，并在独立 Python 验证 Worker 运行 60 秒检查时强制终止，
  再由新 Sandbox 查询、停止、删除遗留容器。新增真实子进程分别在容器创建前，以及命令完成、
  容器已清理但 receipt 未提交时硬退出；两者重启后都呈现 `missing`，同时后者保留仓库内 marker
  证明命令实际执行，因此 missing 继续只允许人工确认，不能被提升为“未执行”证明。
- `replace_text` reservation 现绑定派发前 workspace revision 与 manifest。若 receipt 前退出，
  恢复服务只接受 live workspace 精确等于前态或由原参数推导出的唯一后态；可信 CLI 可显式
  accept 或 rollback，部分写入/额外文件漂移继续阻塞。真实子进程在写入后 `os._exit(26)` 的
  测试通过，恢复后没有重放原模型调用。
- 新增 `apply_patch`：一次调用最多修改 8 个不同既有 UTF-8 文件。Gateway 在第一处写入前验证
  全部路径、精确 occurrence、编码、大小与 immutable 前像；普通中途异常回滚仍等于本次后像
  的已写文件。硬退出后只接受完整 pre/expected revision，部分文件 effect 保持 unknown。真实
  子进程覆盖完整两文件 effect 后退出、显式 accept、跨 Worker 续跑且不重放模型；详细合同见
  [有界多文件精确 Patch](bounded-multi-file-patch.md)。
- 成功 Run 现可用 `agent diff` 只读检查候选，再以 `agent promote --confirm-promote` 提升 1～8
  个既有 UTF-8 文件修改。promotion 绑定源 path hash、初始 revision/manifest、候选 revision、
  TaskSpec 权限和可选 Git HEAD；完整 effect 后退出可只补 receipt，部分 effect 只有在每个目标
  仍精确等于 before/after 时才继续。真实子进程已覆盖两种窗口。当前不支持新增/删除/重命名，
  也不创建 commit。
- 每次模型调用都会持久化 `ContextProjection` Artifact。预算内除控制器 system ledger binding
  外保持 canonical 消息结构；超限时保留
  初始合同、最近单元和未完成工具对，只把旧完整单元变为 digest/参数 hash/结果 hash/有界片段。
  完整 transcript 仍是权威会话；恢复会重算投影并逐字段比对。压缩后的 response settlement
  中断测试由新 Worker 续跑且未重复计费。
- 每次调用新增 `MandatoryFactLedger` Artifact，显式绑定 TaskSpec/objective、Plan/WorkItem、
  allowed/denied path、required acceptance、预算、模型策略、工具 Schema 和当前 workspace。
  ledger 的内容 hash 同时进入 system binding、ContextProjection 和 ModelCallReservation；
  待恢复 response 遇到工具 Schema 漂移时在模型派发前拒绝。它不是语义 Memory。
- 每次调用新增 `RunMemorySnapshot` Artifact，从 `search_repo/read_file/retrieve_code/replace_text/
  apply_patch/run_check` 的权威事件与输出派生。模型 `submit` 声明被排除；失败 outcome 原样保留；unknown
  无证据且保持 unresolved；workspace revision 改变后旧观察标为 stale，模型侧不注入旧片段。
  Memory ref/hash/event boundary/count 同时进入 ContextProjection 和 ModelCallReservation；恢复按
  原历史事件边界重建，因此后续只读取消/重试事件不会改变原请求或触发重复模型调用。
- 新增 Memory 后 6,000 字符压缩回归一度超限；没有放大预算。模型可见 Memory 收紧为最近 2 条、
  每条最多 80 字符，完整 Memory Artifact 仍保留默认 12 条/每条 240 字符。历史摘要在超限时
  确定性移除最旧细节并保留聚合 digest 与 retained/omitted count；压缩恢复测试重新通过。
- Plan DAG 现可顺序执行多个 WorkItem。调度器按计划声明顺序选择首个 dependency-ready item；
  每项切换新的 canonical session 和 Gateway 权限。中间项 pass、`VALIDATING -> RUNNING` 与下一
  session 发布在一个 SQLite 事务中；最后 item pass 与 `SUCCEEDED` 也原子提交，关闭了原单项
  实现的最终提交窗口。工作项边界释放 Lease、重新打开 SQLite/Artifact、由新 Worker 续跑已
  通过离线 E2E。
- 中间项只执行自身 acceptance；最后项额外重跑 TaskSpec 全部 required checks。测试实际制造
  第二项通过自身检查但回归第一项的候选，Harness 拒绝成功、进入 repair，并在恢复早期不变量
  后才完成。已通过项不会因后续 checkpoint revision 改变而丢失，但最终全量验证仍绑定最终
  workspace revision。
- `agent run --auto-plan` 现以一次预算化 Tool Calling 生成最多 8 项的 Plan。PlanningContext
  绑定 TaskSpec hash、初始 manifest/revision、路径权限、required 标记和最多 200 条 scoped
  文件路径；不向规划模型暴露验收命令正文。控制器再次校验 Schema、DAG、验收覆盖和工具权限，
  并把 `source_model_call_id` 写入 Plan 事件。非法计划保留已结算负结果且不自动重试。
- 规划响应 Artifact 已结算但 Plan 事件尚未提交时，`agent reconcile` 可确认安全边界，
  `agent resume` 重建相同请求并复用原响应。离线 CLI E2E 实际从 PLANNING 恢复并完成编辑/保护
  验收，规划模型调用仍为 1；这不构成真实模型计划质量证据。详细合同见
  [一次性自动计划生成](automatic-plan-generation.md)。
- 非法自动 Plan 现在保留原 response receipt，创建内容绑定的 `HumanPlanRequest`，并在同一事务
  进入 `WAITING_FOR_USER`、释放规划 Worker Lease；不会自动重试或追加费用。本机 `plan set`
  同一事务写入 `HUMAN_DECISION_RECORDED`、人工 Plan 与 READY 状态。重启重放、非法替换不消费
  请求、reconcile 明确返回 `provide_replacement_plan`，以及 CLI WAITING 输出均有离线测试。
- 扩展确定性 NoProgressPolicy：对单 tool-call 响应中的 read/search/retrieve/replace/patch，以
  `tool_name + arguments_hash + same revision` 识别完全相同行为及精确 `A,B,A,B` 循环。相同行为
  第 3 次、A/B 循环第 5 步产生有 Artifact/receipt 的 error observation；模型收到反馈后仍继续
  模式，则分别在第 4/6 步保存完整会话，持久化带 pattern 的 `HumanGuidanceRequest`，并原子进入
  WAITING/释放 Lease。领域校验同时绑定 receipt 形状、policy 文本 hash、Task/Plan/WorkItem/
  workspace/session。本机 `agent guide` 写入新 session，由新 Worker 从相同 iteration 续跑；
  不自动重试、不换模型、不退还工具预算。详见[人工指导恢复](operator-guidance.md)。
- 新增单次执行期受限 replan：模型可用当前 Run 证据单独调用 `revise_plan`，控制器构造连续
  Plan version，并要求全部已通过 WorkItem 逐字段不变、required acceptance 继续覆盖、工具不
  越权且至少有一个可运行剩余项。成功工具记账、`RUNNING→REPAIRING→RUNNING`、`PLAN_REVISED`
  和新 AgentSession 以一个事务提交；失败提案只产生 error receipt，原计划继续。Run Memory
  现按事件发生时的 Plan version 校验历史 session，被替换的未完成项证据仍可审计。详见
  [执行期受限 Replan](execution-replanning.md)。
- 审查 Provider `retryable` 状态后保留负结论：当前请求合同没有可证明的服务端幂等键，连接
  超时和 5xx 不能证明请求未执行，因此仍标 unknown 并阻止隐藏 retry/fallback；没有用 HTTP
  分类绕过双账本和费用证据。
- `retrieve_code` 现从 immutable workspace manifest 建立 SQLite FTS5/BM25 派生索引，索引
  key 同时绑定 revision 与 allowed/denied scope。EvidencePack 区分 ok/empty/degraded，返回
  path、行范围、内容 hash 和有界 snippet；每个命中在返回前重新对照源 Artifact。dirty
  revision、非 UTF-8 降级、FTS scan fallback、索引篡改拒绝、缓存裁剪重建、Agent tool 调用和
  unknown read-only retry 均有离线测试。`horizon eval retrieval` 对固定 5 个本仓案例得到
  Hit@case-K=1.0、micro path recall=1.0、MRR=0.9，leakage/empty/degraded 为 0；这是内部诊断。
  现已有 3 个外部项目的顺序冻结盲测，但仍无真实模型检索或 Agent 成功率增益证据。
- v6 源码变更后重新运行同一固定检索清单：report ref
  `4fb244757043f2a211fac19b7fc622c7bd98b34e98e250ec4de72771ca335103`，scoped revision
  `11b930dabb207447a8a6a80b716285a112437e446f923c83a964575f99709943`；指标保持 5/5、Recall
  1.0、MRR 0.9，且无 leakage/empty/degraded。
- 新增标识符变体检索：保留原词并拆分 camelCase/snake_case，生成有界组合词；组合词与普通词
  都最多查看 256 个候选，并优先规范化同名 `def/async def/class`。合成测试加入高频文档/调用
  干扰；youtube-dl 764 文件范围的两个固定案例最终均 rank 1，report ref
  `84448f19b56575084e257323a3f29ab429f05aa262f2dc7b8a09560739897560`。v5 历史成功 ref
  `d711215e24ebf7dd98610a1d96a6a2bc84c2cb3901267e0524d091f1239c08c4` 继续保留。仅加词项与仅加精确
  token 优先的两版仍 Hit@5=0，对应 ref `52922a0aaf100589f6a84faa547b3094966c24718bd79e3b6702c05d3ae242e6`
  和 `79676ed48a4f0a7ea01aad3b377198598e1793681044333b235b717e5a201f6d`，负结果保留。该清单由
  实现者选题，不是盲测，也没有真实模型或 Agent 成功率增益结论。
- 新增 `horizon eval reliability`：冻结 7 个 NoProgress Trace 和 5 个执行期 replan 合同，共
  41 个逐步策略判断；当前 12/12 案例、41/41 判断通过，false positive/negative 均为 0。
  生产 Agent Loop 与 evaluator 共用领域判定函数，故意标错的负例会保留 11/12 和一次 false
  negative。Manifest digest 为 `4d27b02f9254bfe792dbd8a782cb1ba67f4b5f83a420ab96f437870a81d4a66f`，
  report ref 为 `debc94da6a00cd26419d6f9b450e81b3f23a8c132e71687513a013c41893e5fe`。
- 构造 replan 扩权负例时发现写模式 Plan 只在模型 Tool Schema 层枚举工具的缺口；现已把工具
  白名单校验下沉到 `Plan.check_task()`，人工、自动计划和执行期 revision 均不能写入未知工具。
  该结果是本轮风险修复及自检，尚无独立安全复核。
- 新增 `horizon eval run-ab` 完整路径对照。冻结内部 shell fixture 先由 Docker 确认 required check
  初始失败；Baseline 为 4 model / 4
  tool calls 后进入 `WAITING_FOR_USER`；Treatment 为 6 model / 7 tool calls，Plan v2、一次 replan、
  Docker required check 通过并 `SUCCEEDED`。两条 Run 分别有 48/74 个事件、零 unknown/悬空预留，
  JSONL 独立重放一致，源 fixture 未修改；合成成本增量 CNY 0.00096。Report ref
  `e222e84f0bf246981d9281aea7597853c98b6d84182c5d373b509fcdde61fedf`。这是 Harness 路径证据，
  不是实际模型选择 replan 的效果结论，详见[完整 Run A/B](run-ab-evaluation.md)。
- 新增 `horizon eval run-ab-suite` 与来源合同。Cookiecutter、FastAPI、tqdm 三个 BugsInPy 历史
  缺陷均绑定 buggy/fixed commit、上游 test、修复 URL 和 SPDX license；依赖裁剪 fixture 的
  初始 check 3/3 真实失败，Baseline 3/3 等待，Treatment 3/3 成功，suite report ref 为
  `da8c8557b15aa0fc6955f44d987b41bdd57aac6eb34b2490392a11d533639790`。这是外部来源的裁剪
  复现，不是完整 checkout 或 BugsInPy 官方分数，详见[外部来源 A/B Suite](external-run-ab-suite.md)。
- 新增[双项目完整 checkout suite](full-checkout-pilot.md)：执行前强制 case/benchmark、buggy/fixed
  commit、fix/license URL、实际 Git HEAD 和清洁状态一致；tqdm 82 files 与 youtube-dl 872 files
  的初始断言均失败，Baseline 2/2 等待、Treatment 2/2 单次 replan 后成功。Code RAG 分别索引
  73/82 与 870/872，两个目标生产文件均 rank 1，EvidencePack 如实保持 `degraded`；suite report
  ref 为 `78127dc48acccf9d4768f4ec4c3a47551d385fdf379c2f421bd397283686830a`。首次 youtube-dl Trace
  暴露的重复 CAS blob 校验已用有界、元数据失效的进程内 verified cache 修复：872 文件同 revision
  再 capture 从 19.931 s 降至 2.247 s，双项目 suite 从 1124.65 s 降至 340.00 s，结果、事件数、
  manifest、EvidencePack、source unchanged 与 Trace replay 均保持。这仍是脚本模型和依赖无关
  回归命令，不是 BugsInPy 官方环境或真实模型成绩。
- 新增[完整 checkout 多阶段 pilot](multi-stage-full-checkout-pilot.md)：同一 youtube-dl fixture 先修
  `youtube_dl/utils.py`，再在新 WorkItem 中补 `test/test_utils.py` 回归断言。Baseline 通过第一项后
  等待；Treatment 的 Plan v2 保留已完成 production 项、只替换剩余项，最终 2/2 checks 通过，
  同进程 114-event Trace 可重放。两 arm 随后都在第一项通过后释放 epoch 1 Lease、重新打开
  SQLite/ArtifactStore/检索库/预算账本，再由 epoch 2 Worker 续跑；重启版 Baseline/Treatment 为
  86/116 events，suite/case report refs 为
  `d812ecb5a9dfe23a28cbf079820ec6ee4862a37d614e3c98a3a90e2abe583e02` /
  `760cea79f8e306be372dda139db4d084fb46d58f1b902dfa008a7d9b4bf07356`。历史 CRLF 精确替换失败
  与默认 60 秒重启 Lease 在冷缓存复核期间到期的 partial Trace 均保留为负结果；后者改为显式
  600 秒后复跑通过，没有放宽 fencing。
- `ruff check src tests` 通过；`ruff format --check src tests` 通过。
- 离线 `horizon model check` 通过，Key 来源仅显示为 `.env` 引用，没有输出秘密。
- 一次真实探针通过：`finish_reason=tool_calls`，308 input / 49 output tokens，估算
  `CNY 0.001365`，无 retry、无 fallback、unknown 为 0。
- 一次真实 Agent fixture Run 成功：Run `run_c6c8234c58f5441e80a4b9b72da481c4`，5 次
  模型调用、6 次工具调用、5,043 input / 433 output tokens、0 次 repair；模型估算费用
  `CNY 0.019026`。Docker 中的 `greeting_regression` 通过，源 fixture 保持原样，改动只在
  一次性 staging 副本。
- Run 产生 committed checkpoint、验证 Artifact 和 56 个事件；导出 JSONL 后离线重放与
  SQLite 投影的 hash 均为 `3ceff0329f6ba0571429407a2f347b6bd25136c0221e146c4c9a197f15466bd1`。
- 当前 Campaign 账本：occupied/settled `CNY 0.0856308`，remaining `CNY 2.9143692`，
  reserved/unknown 均为 0。金额是本地冻结 PriceCard 估算，不冒充供应商最终账单。
- `horizon eval pilot-preflight` 已对 youtube-dl-3 的 872-file 干净 checkout 执行：冻结行为检查
  exit code 1、source 前后 revision 相同、`.git` 未进 snapshot、solution isolation 通过，报告 ref
  `b02f28d3e61332b088530d87cbaaa0fd36776966a426e1f72ff7272c16d6380b`。使用本机既有
  `python:3.12-alpine` digest；`paid_model_called=false`、`network_called=false`。详细边界见
  [真实模型 Pilot](real-model-pilot.md)。
- 首个完整 checkout 付费 Run `run_3e3dc35284c248d6b98afe47528d12ab` 已执行，6 次模型调用、
  10 次工具调用、19,028 input / 983 output tokens，Run 费用 `CNY 0.0652398`；终态为
  `FAILED / model_iteration_limit`。自动 Plan 把同一最终验收重复分给只读定位、修改和验证三项，
  使第一项不可完成；执行又在 4 个轮次中重复近似 exact search，第 5 轮 `retrieve_code` 才把
  `youtube_dl/utils.py` 目标定义排到 rank 1，随后达到调用上限。没有编辑、验证、checkpoint 或
  passed item，source 保持不变。
- 失败 Trace 已导出并在修复前后离线重放；projection hash 均为
  `7b553f09a69548277b3aa09a2e6a99774e6d79cab7843593a624b76d45f8d898`。新计划准入现拒绝重复
  acceptance ownership，planner/agent prompt 和 `search_repo` 截断反馈也已按该证据收窄；历史
  Trace 使用旧合同兼容投影，避免为了修复未来行为而破坏既有负结果。
- 修复后复跑使用独立 `CNY 0.18` Campaign，同时把单 Run cap 设为 0.18；即使配置被误触发多次，
  与首轮费用合计也最多 `CNY 0.2452398`。新的零费用 preflight report ref 为
  `fed1b7902c9f852d1b8b2d51574a0d8ae75e4c59e5e981b9b709419a4e414b8a`，并绑定 Harness source
  digest `f56b52ffa378aa523ac665356333fe5ab7db6b23ae62c403f91e350ed1d49b45`。当时 retry Campaign
  occupied/settled/reserved/unknown 均为 0、remaining 为 0.18；旧无指纹报告仍可读取，但不再
  允许启动付费 Run。
- 第一次复跑 Run `run_a548388fa62d4a34a49a7086db5610a6` 已调用 3 次模型（1 planning、
  2 execution）和 2 次只读工具，9,940 input / 351 output tokens，费用 `CNY 0.032979`。
  新 Planner 只生成一个端到端 WorkItem；首次执行直接用 `retrieve_code("unescapeHTML")`，rank 1
  正确定位 `youtube_dl/utils.py`。随后 `read_file` 无范围返回 122,665 字节 Artifact，两个近期完整
  工具单元总投影超过 60,000 字符；旧算法因 `preserve_recent_context_units=6` 拒绝折叠，CLI 在
  新模型 reservation 前以 `Conflict` 退出。无编辑、checkpoint、validation、passed item、reserved
  或 unknown 费用；source revision 未变。部分 Trace 已导出到
  `.horizon/real-model-pilot-retry-v2/run_a548388fa62d4a34a49a7086db5610a6.context-overflow.partial.trace.jsonl`，
  replay hash 为 `7841d09eb3279437f23979c689d1b89061244a982605e5c3538455f0de1b6d5b`。
- 该非终态 Run 的进程已经退出且 wall-clock deadline 已过；零网络 `agent reconcile` 被“downtime
  is not refunded”硬规则拒绝，因此没有篡改为成功或重新派发。它保留为过期负证据。修复包含：
  大文件整读拒绝并要求范围、近期完整单元在硬上限下最少应急折叠、incomplete 单元保守拒绝，
  以及 pre-dispatch HorizonError 的静止租约释放；122,665 字符事故尺寸有专门回归。
- 单 Run cap `CNY 0.14` 的 continuation 零费用 preflight 通过后，用户授权执行了
  Run `run_f42d1e4bf3d34203b247d56448215f41`。它调用 3 次模型（1 planning、2 execution）、
  3 次只读工具，消耗 10,108 input / 412 output tokens，费用 `CNY 0.0285024`。Planner 在
  任务没有指定路径时猜测 `youtube_dl/extractor/common.py`；两次 revision-bound retrieval
  都把真实目标 `youtube_dl/utils.py` 排在 rank 1，但执行模型仍被 Plan 假设带偏。
- 下一次请求的保守 input ceiling 为 51,328 tokens，需预留 `CNY 0.158592`；硬门限
  在 Provider 派发前拒绝，所以无额外计费、reservation 或 unknown。静止 Lease 成功释放；
  workspace/source 未变，无编辑、checkpoint 或 validation。部分 Trace 路径为
  `.horizon/real-model-pilot-retry-v3/run_f42d1e4bf3d34203b247d56448215f41.campaign-gate.partial.trace.jsonl`，
  replay hash 为 `503bba58685de1caaa0a79514d7116a85eb102f0f68a5f17b282d9e5313349f6`。
- retry Campaign 现为 occupied/settled `CNY 0.0614814`、remaining `CNY 0.1185186`，
  reserved/unknown 为 0；连同首轮独立 Campaign，本系列实际累计付费 `CNY 0.1267212`。
  该证据已转化为“Planner 不猜路径、execution 优先仓库证据、retrieval 默认 rank 1”的窄修复。
- CNY 0.11 收窄 continuation 将 `max_context_chars` 设为 7,000、最近完整单元软保留设为
  2。执行前零费用 Docker preflight 通过：Task ref
  `43e8209aa020a3fdf9c38a622f3e8401ba6733f96693e8e14a96fa432f9976c4`，report ref
  `094b48c78dc070a0fb45813831ca8af51b3d2c1aa24b77782c666737a8cdbfb0`，Harness source digest
  `78d26ccce347f62b7b26393e41884f0c6dcad0141e5343b2006c71fc403672ee`；`paid_model_called=false`、
  `network_called=false`。用户随后明确授权只执行一次、不重试、不 fallback。
- 第四轮 Run `run_a12c4d0dbdb54e8bb3c49cb143a69392` 调用 5 次模型（1 planning、4 execution）
  和 8 次工具，消耗 14,441 input / 781 output tokens，实际结算 `CNY 0.050352`。Planner 不再
  猜测实现路径，RAG 持续把 `youtube_dl/utils.py` 561～600 行排在 rank 1；但模型三次只传
  `start_line`、未传 `end_line`，均在 `read_file` 参数校验处失败，没有写副作用。
- 下一次 execution 需预留 `CNY 0.067530`，高于单 Run 剩余 `CNY 0.059648`，因此在 Provider
  派发前被 Run 硬门限拒绝且未计费。workspace/source 未变，无编辑、checkpoint、validation
  或 passed item；Run 投影仍为 `RUNNING`，但 Lease 已释放且没有活跃 Worker。部分 Trace 位于
  `.horizon/real-model-pilot-retry-v4/run_a12c4d0dbdb54e8bb3c49cb143a69392.run-budget.partial.trace.jsonl`，
  replay hash 为 `ad88aada8bd5342c41ee02fb570ae37cfc24ec8e59347dd1ec615f1e7163a8a9`。
- retry Campaign 当前 occupied/settled 为 `CNY 0.1118334`、remaining 为 `CNY 0.0681666`，
  reserved/unknown 为 0；本系列累计实际费用为 `CNY 0.1770732`，距用户累计上限还剩
  `CNY 0.0729268`。本次一次性授权已消费，没有自动重试。
- 该负结果增加了 `read_file` 双边界 Schema 约束、prompt 与可操作错误提示；Planner inventory
  默认由 200 收窄到 50，保留总量/截断标记。同一 planning 输入的离线费用预留由
  `CNY 0.076968` 降到 `CNY 0.044010`。源码变化使旧 preflight 失效，未创建或执行新合同。
- 新增 Trace-derived 离线闭环会先复现单边 `read_file` 参数错误，再消费结构化错误回执、改用
  双边界读取、精确编辑、运行 protected check 并成功提交；JSONL 重放投影一致，全程不联网。
- 新增类型化 `BudgetStop`：规划/执行的单 Run 上限、Campaign 单调用上限和 Campaign 总上限均
  给出 scope/currency/required/available。确定性派发前不足在无 Run reservation 边界原子进入
  `FAILED`，Campaign-only reservation 先以 0 结算；CLI 返回结构化原因。unknown 用量不走此
  终态捷径，第四轮历史 Trace 也未被追溯改写。
- 新增 `horizon demo run` 一键离线演示并实际产出 73-event Trace：第一任 Worker 完成检索并留下
  内容寻址的单边范围错误，第二任 Worker 以 `lease_epoch=2` 重开适配器、消费持久化错误，随后
  双边读取、精确修改、保护检查和最终 required validation 全部通过。EvidencePack 的 Trace、
  最终投影、报告和摘要均经大小/SHA-256 复核，Trace 再重放一致；真实模型、网络、仓库代码执行
  和外部费用均为 0，硬崩溃/真实模型/官方 benchmark/不可信代码沙箱结论明确排除。

详细配置、公式、供应商 Trace ID 和限制见
[SiliconFlow Provider 接入](siliconflow-provider-integration.md)。金额来自冻结 PriceCard 的
保守估算，不冒充供应商最终账单。

### 2026-09-30 可靠性内核基线

最终完整测试：**92 passed，0 failed，0 skipped**，包含 88 项本地规则/集成/故障测试与
4 项真实 Docker 契约测试，报告耗时 9.28 秒。语句覆盖 1,191 条中的 1,052 条，约 88.33%；
覆盖率只描述当前代码，不代表 79 项设计需求的完成比例。

- 历史完整 JUnit：`artifacts/tests/all-final.xml`（本地生成，不随源码分发）。
- 历史覆盖率数据：`artifacts/tests/coverage-final.json`（本地生成，不随源码分发）。
- 历史无 Docker 回归：`artifacts/tests/core-final.xml`（本地生成，不随源码分发）：
  88 passed，4 skipped（显式未选择镜像）。
- `ruff check .` 与 `ruff format --check src tests` 均通过。
- `uv build` 成功生成 wheel 与源码分发包；没有发布到外部仓库。

最终证据 SHA-256：

| 文件 | SHA-256 |
|---|---|
| `all-final.xml` | `9CE0959E0F909C53F4889A7099ABC74A2246A35BFB0872148743102C5999FC77` |
| `coverage-final.json` | `84ACA721382A07ABEE52163A985061BB360EEA8C83F5D76236E2B4B6523DEDE6` |

安装包内容检查确认包含 CLI 与 Docker adapter，且没有打入 `.venv`、`.horizon`、
`.uv-cache` 或运行证据目录。本地 Markdown 链接检查未发现断链。

以下测试实际执行了相应操作，并非只检查函数存在：

1. 八个并发调用争抢同一 Run 的 Lease，仅一个成功；epoch 与序号不重复。
2. Lease 到期但未确认旧进程停止时拒绝接管；确认后旧 token 被拒绝。
3. 删除/损坏缓存投影后，从事件恢复相同规范化状态；JSONL 可在无模型/无命令执行下重放。
4. 子进程在事件插入后、事务提交前 `os._exit`，重启后未提交事件和回执均不出现。
5. 子进程在提交后 `os._exit`，重启后保留事件，重复请求不会产生第二条事件。
6. 预算预留和 unknown 用量重启后不丢失；实际用量超限先记账再失败，不把费用改成零。
7. 文件产物损坏使恢复失败；恢复只写全新目录，保留原目录与已有脏修改。
8. 真实容器无法写根目录，可写授权临时副本；超时终止容器及后台子进程；100,000 字节
   输出完整排空，但仅保留配置的 128 字节。

## 本轮暴露并处理的问题

- 初次测试受全局 TEMP 权限限制：40 passed、23 setup errors。改为每次独立、唯一的
  项目内测试目录；没有修改全局 TEMP 或删除用户目录。
- 两项故障测试最初混用了冻结时钟和真实子进程时钟，导致时钟倒退拒绝；统一注入后通过。
  没有关闭生产代码的时序检查来迁就测试。
- 幂等 Lease 重试必须返回原始历史回执，不能返回其他新 Worker 的最新 Lease；已修正并加测试。
- 首次 Docker 回归有一个 pytest cache 权限 warning；最终运行显式停用非必需缓存，无该 warning。
- 镜像声明的匿名卷在初次清理时遗留 3 个：通过唯一容器标签与 daemon 事件逐个对应后，
  确认未被使用并删除；执行器改为连同自身匿名卷一起清理。最终没有本次测试容器或相关卷残留。
  删除的仅为本次测试临时资源，非用户既有数据；未动已有服务。
- PowerShell 请求 PyPI 元数据出现 TLS 认证错误；没有关闭 TLS 验证。uv 的依赖解析与安装正常。
- 新增 Docker fixture 合同时，测试代码第一次用 Windows 文本写入把 LF 转成 CRLF，导致
  Linux shell 验证失败。测试改为字节级替换，并新增 `replace_text` 保留 LF 的回归测试；
  生产 Gateway 原本就是 UTF-8 字节原子写入，没有通过关闭验证来规避问题。
- 本批第一次全量回归出现 1 个 Lease TTL 断言失败：`expires_at` 与事件 `created_at` 分别读取
  系统时钟，600 秒证据少约 1 毫秒。没有放宽断言；`NewEvent.occurred_at` 现在只作为存储层消费
  的非持久元数据，让 Lease 决策与事件时间使用同一采样，Trace Schema 不增加字段。目标测试
  及完整回归随后通过。

## 仍未实现、未验证和下一步

当前既不是 M0 整体通过，也不是 M1～M5 完成。以下工作继续保留：

1. **上游与通用主循环**：当前闭环是自研、确定性顺序 WorkItem DAG 适配，不是完整
   mini-SWE-agent/SWE-ReX Spike；已有受控 one-shot 自动分解，但还要支持基于执行证据的有限
   自动 replan 触发、第二次/失败升级策略、任意 diff/edit、period-3+／语义无进展检测；当前不
   并行执行工作项。完全相同动作和精确 period-2 循环已有窄保护，执行模型有一次受限 Plan
   revision，但不能扩展宣称为通用自治规划或无进展检测。
2. **完整恢复**：安全轮次和已持久化 response receipt 边界现可续跑，模型/工具悬空 intent
   和 Run/Campaign 提交窗口可分类；单一只读工具支持显式重试，精确 `replace_text` 与有界
   `apply_patch` 支持基于 manifest 的 accept/rollback；`run_check` 有可查询的 Docker attempt，
   可核验/显式停止后丢弃未知结果，missing 时仍需人工确认。仍需 Provider 返回到 response
   Artifact 提交之间的处理、容器创建前持久启动证明/结果恢复、任意 diff、新增/删除写入、旧
   容器隔离和 Execution Fork。
3. **安全边界**：首个 Gateway 已阻止任意 shell，并把模型工具 intent/receipt 与 Run 预算
   事件化；但模型派发、文件写入和容器副作用还不是跨 SQLite/文件系统的单一原子事务，
   也没有独立安全复核或通用审批系统；当前 promotion 仅覆盖最多 8 个既有文件修改。
4. **Agent 能力**：自然语言意图与 admission、通用审批/澄清式持久化 HITL、
   Project Memory、symbol/vector Code RAG、六层精确 tokenizer/semantic Context、统一
   retry/breaker/fallback、更完整的 repair/replan。现有确定性字符 + 完整请求保守 token 上界
   双门投影、run-scope 证据 Memory 和
   词法 FTS 是部分能力，不是上述完整闭环。
5. **预算与隐私补全**：CNY Campaign 与 Run 模型账本现已同时工作，但 TaskSpec 的旧
   `max_cost_usd` 尚未做版本化多币种迁移；也没有取消/截止后的迟到回执主动对账和生产级
   全链路脱敏。当前 Trace 包含 TaskSpec；不应在其中嵌入凭据。
6. **真实效果与验收**：四个真实模型 Pilot Run 分别因迭代上限、上下文硬门限、Campaign
   费用硬门限和单 Run 费用硬门限停止；它们证明了费用/Trace/source isolation 路径，也分别暴露
   并修复了 Plan/搜索反馈、大文件读取/上下文/租约、Plan 假设路径/请求膨胀，以及范围参数协议
   缺口，但仍没有完成目标修复或 protected validation。仍缺固定 20～30 个真实任务、长程/检索/记忆诊断、
   A/B/C/D 与消融、关键安全独立复核。单个负样本不能替代这些证据。

四轮负证据驱动修复、后续作品集演示、input-token 门禁、同名符号诊断和外部定位盲测后，当前主干已完成 293 项离线回归；
该 preflight 证明首次规划保守预留 `CNY 0.031572` 可由 `CNY 0.06` Run cap 覆盖，但没有启动
第五个真实模型 Run。同一 CNY 0.18 retry Campaign 尚余 `CNY 0.0681666`，系列累计实际费用为
`CNY 0.1770732`。第四轮旧 preflight 已因 Harness
源码修复失效，剩余 Campaign 或用户总额度都不能自动扩权；任何新付费 Run 仍须重新预检并取得
明确授权。当前优先继续零费用可靠性验证，现有证据不支持立即引入 symbol/vector
索引、模型摘要或自动 replan。Run Memory 已形成最小垂直切片，
精确 NoProgress/replan 的冻结策略 Trace 已建立 12 案例基线；内部 A/B、三个来源绑定的依赖裁剪
案例和两个不同项目的完整 checkout 均已保存初始负例、Trace、验收、调用数和合成费用。完整
suite 还给出两个 Code RAG rank 1，以及 9/2 个文件跳过的显式降级证据；重复 CAS blob 校验的
规模开销已完成前后对照优化。完整 checkout 的两阶段 production → regression-test 任务及其
WorkItem 边界 epoch 1 → 2 Worker 恢复也已通过。`run_check` 已有不采信结果、不自动重派的窄
丢弃合同和可查询标签 attempt，真实子进程硬退出已覆盖 running、pre-create 与 post-cleanup
窗口，并保留 missing 不足以证明未执行的负证据。首轮失败已经提供一个真实样本，但尚无分布
或收益证据；在形成多样本证据前不增加
自动触发、第二次修订或语义循环检测。
计划失败和确定性停滞的两条窄持久 HITL 已落地，通用审批暂不扩张。
Project Memory 等
文件级失效与显式 promotion/revoke 合同具备后再做。Campaign 的持久硬上限仍为 CNY 3 元；
它不是自动追加实验授权，本轮 Pilot 的用户累计授权上限仍单独按 CNY 0.25 计算。密钥只在
本地 `.env` 或环境变量中读取，不写入 Git、TaskSpec、
SQLite 账本或文档。
