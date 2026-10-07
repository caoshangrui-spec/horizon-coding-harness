# 真实模型 Pilot：离线预检、六轮负结果与付费边界

更新：2026-10-04。本 Pilot 的目标不是立即追求 benchmark 分数，而是在**完整真实仓库、
模型自主计划、模型自主检索和编辑**的条件下观察 Harness 的真实失败分布。已执行六个
付费 Agent Run：分别暴露计划结构/检索反馈、无界读取/上下文投影、Plan 路径假设/请求预留
过大、单边范围读取/单 Run 预留、路径存在与语义定位的区别，以及 Campaign 预留压力。六轮均没有修改代码或进入验收；结果可重放且
费用可对账，但不是 Issue 成功证据，也不足以推出模型或 Harness 的总体能力结论。

## 1. 为什么复用现有主循环

现有 `agent run --auto-plan` 已经具备一次性工作副本、one-shot 计划、Code RAG、受控编辑、
Docker 验收、Run/Campaign 双预算、恢复和 Trace。Pilot 不复制这些机制，只增加两个窄边界：

1. `horizon eval pilot-preflight` 在调用模型前固定真实来源、证明初始失败、检查 solution isolation，
   并生成内容寻址 TaskSpec 与预检报告；
2. `horizon agent run --pilot-preflight ...` 在付费调用前重新比对任务、源码快照、Git HEAD、
   Docker image digest、Provider policy、Harness Python 源码指纹、模型调用上限和费用上限。

实现位于：

- [`domain/pilot.py`](../src/horizon/domain/pilot.py)：Pilot manifest 与 preflight report 合同；
- [`application/pilot.py`](../src/horizon/application/pilot.py)：离线预检、内容寻址证据和启动绑定；
- [`interfaces/cli/app.py`](../src/horizon/interfaces/cli/app.py)：`pilot-preflight` 与
  `--pilot-preflight` 入口；
- [`real-model-pilot.yaml`](../benchmarks/run_ab/full/youtube-dl-3-unescape-html/real-model-pilot.yaml)：
  首个完整 checkout 任务；
- [`siliconflow-pilot.yaml`](../config/providers/siliconflow-pilot.yaml)：单 Run CNY 0.25 的专用策略。
- [`real-model-pilot-retry.yaml`](../benchmarks/run_ab/full/youtube-dl-3-unescape-html/real-model-pilot-retry.yaml)
  与 [`siliconflow-pilot-retry.yaml`](../config/providers/siliconflow-pilot-retry.yaml)：第一次显式
  授权复跑的 CNY 0.18 Campaign 合同；
- [`real-model-pilot-retry-continuation.yaml`](../benchmarks/run_ab/full/youtube-dl-3-unescape-html/real-model-pilot-retry-continuation.yaml)
  与 [`siliconflow-pilot-retry-continuation.yaml`](../config/providers/siliconflow-pilot-retry-continuation.yaml)：
  保持同一 Campaign、适配剩余额度的 CNY 0.14 continuation 合同，已执行并在下一次模型派发前
  被 Campaign 硬门限拒绝；
- [`real-model-pilot-budgeted-continuation.yaml`](../benchmarks/run_ab/full/youtube-dl-3-unescape-html/real-model-pilot-budgeted-continuation.yaml)
  与 [`siliconflow-pilot-budgeted-continuation.yaml`](../config/providers/siliconflow-pilot-budgeted-continuation.yaml)：
  针对第三轮证据收窄检索与上下文合同，单 Run cap 为 CNY 0.11；已完成零费用预检和一次明确
  授权的付费执行，并被单 Run 费用硬门限阻断。

## 2. 数据流与阻断点

```text
controller-side pilot manifest
  ├─ private source/fix metadata ───────┐
  ├─ private forbidden solution marker  │ 不进入模型上下文
  └─ model-visible TaskSpec template ───┼─> offline preflight
                                       │    ├─ clean Git HEAD gate
buggy full checkout ────────────────────┘    ├─ immutable snapshot
                                            ├─ no-network Docker initial check
                                            ├─ solution-isolation scan
                                            └─ prepared TaskSpec + report (CAS)
                                                           │
                                                           v
                                         agent run --pilot-preflight
                                            ├─ recheck every binding
                                            ├─ recheck campaign headroom
                                            └─ only then allow paid planning
```

预检命令不读取 `.env`，不加载 API Key，不构造模型 Gateway，也不联网。初始验收在从不可变
snapshot 恢复的副本中运行，源 checkout 前后重新检查 Git cleanliness 和内容 revision。

## 3. Solution isolation 的准确边界

本案例是 **solution-blind、acceptance-visible**，不是隐藏测试：

- 模型会看到任务目标、允许路径和验收行为，因为它需要知道完成标准；
- 模型不会看到 fixed commit、fix URL 或 manifest 中登记的精确修复 marker；
- `.git` 不进入 Agent workspace，Gateway 也不提供任意 shell 或网络工具；
- controller-side preflight report 虽包含来源审计字段，但不会加入 planning 或 execution prompt；
- buggy checkout 本身及正常代码线索仍可被模型检索，这是完成软件工程任务所需的合法证据，
  不是 gold patch 泄漏。

预检会扫描原始和生成后的 TaskSpec；一旦其中出现 fixed commit、fix URL 或私有 solution marker，
manifest/预检直接拒绝。该检查证明的是已声明私有信息没有进入 TaskSpec，不是对任意自然语言
旁路泄漏的完整安全证明。

## 4. 预算和重试合同

首个 Pilot 固定：

| 项 | 上限 |
|---|---:|
| Provider / Model | SiliconFlow / `deepseek-ai/DeepSeek-V4-Flash` |
| 单 Run 累计费用 | CNY 0.25 |
| 模型调用总数 | 6（含 1 次自动计划） |
| Provider 隐式重试 | 0；`max_attempts=1` |
| 自动 fallback | 关闭 |
| Agent steps / tool calls | 16 / 16 |
| 网络 | 仓库执行禁网；模型只访问固定 Provider endpoint |

任何后续重试都必须成为新的、可记账调用；不在 HTTP adapter 内静默重试。Run 上限约束本次
计划和所有执行轮次，Campaign 的 CNY 3.00 上限继续跨 Run 累积。用户对本轮实验授权的是
**累计 CNY 0.25**，因此首轮实际费用也必须从任何后续重试的可用额度中扣除；不能用新的
单 Run CNY 0.25 cap 绕开累计上限。TaskSpec 中
`max_cost_usd` 是尚未迁移的旧字段，不作为本 Pilot 的 CNY 费用证据。

预检不会授权费用。正式入口仍必须显式提供 `--confirm-paid`；配置、任务、Harness 源码、目标源码
或镜像变化后，preflight report 失效。没有 Harness source digest 的历史报告可继续读取和审计，
但不能再次启动付费 Run。启动时还会重新读取 Campaign 剩余额度，余额不足以覆盖相应 Run cap
时在调用模型前阻断。

## 5. 已执行的零费用预检

命令：

```powershell
uv run --locked --cache-dir .uv-cache horizon eval pilot-preflight `
  benchmarks/run_ab/full/youtube-dl-3-unescape-html/real-model-pilot.yaml `
  --image python:3.12-alpine `
  --config config/providers/siliconflow-pilot.yaml `
  --state-dir .horizon/real-model-pilot-v1
```

2026-10-03 的结果：

- `ready=true`；`paid_model_called=false`；`network_called=false`；
- 完整 checkout HEAD 为 `f5469da9e6e259c1690c7ef54f1da1c19f65036f`，工作树干净；
- snapshot revision 为
  `fb97a849f99bbdd13922ac4a4a471c36b4b463b313493bf03479c464fed94b10`；
- 本机禁网 Docker image digest 为
  `sha256:0687a6bc9716edc2a6ee0fbfb0f87e7ee358b262b67c9215de91bc9b2d38ba71`；
- `youtube-dl-unescape-html-behavior` 真实退出码为 1，符合冻结初始负例；
- source 前后 revision 相同，`.git` 未进入 snapshot；
- solution isolation 通过；当前 Campaign remaining 为 CNY 2.979609；
- prepared TaskSpec ref：
  `8d6371b6c66e734419be3cca1740846625e89a479c4427596ea329ebdcb74da1`；
- preflight report ref：
  `b02f28d3e61332b088530d87cbaaa0fd36776966a426e1f72ff7272c16d6380b`。

第一次在受限会话中连接 Docker daemon 得到 permission denied；没有据此误判镜像缺失。获得
Docker 进程权限后只读确认本机已有镜像，再完成预检；没有拉取镜像或更改已有服务。

## 6. 首个付费 Pilot：冻结负结果

使用上述 prepared TaskSpec、preflight report、Provider policy 和本机固定镜像启动了一次
`--auto-plan --confirm-paid` Run。终态证据如下：

- Run ID：`run_3e3dc35284c248d6b98afe47528d12ab`；终态 `FAILED`；
  `failure_reason=model_iteration_limit`；
- 6 次模型调用（1 次 planning、5 次 execution），10 次工具调用；19,028 input tokens、
  983 output tokens；本 Run 冻结 PriceCard 估算费用 `CNY 0.0652398`；
- Campaign occupied/settled 为 `CNY 0.0856308`，remaining 为 `CNY 2.9143692`，
  reserved/unknown 均为 0；
- workspace revision 始终为
  `fb97a849f99bbdd13922ac4a4a471c36b4b463b313493bf03479c464fed94b10`，
  source workspace unchanged；没有编辑、checkpoint、validation 或 passed WorkItem；
- Trace：`.horizon/real-model-pilot-v1/run_3e3dc35284c248d6b98afe47528d12ab.trace.jsonl`；
  离线 replay projection hash 为
  `7b553f09a69548277b3aa09a2e6a99774e6d79cab7843593a624b76d45f8d898`；
- 失败 Run 不满足 promotion 前置条件，`agent diff` 拒绝提升是预期行为。

失败不是“模型找不到修复点”。Planner 把唯一 protected acceptance 同时分给 `locate-unescape`、
`fix-unescape` 和 `verify-fix` 三项；第一项只有只读工具，却必须在 submit 时让最终行为验收通过，
所以计划从结构上不可完成。执行阶段又对 path-ordered、最多 30 条的 `search_repo` 反复使用
近似查询；第 5 个执行轮次才调用 `retrieve_code("unescapeHTML")`，其 rank 1 已返回
`youtube_dl/utils.py` 的目标定义，但随后到达模型调用上限。

该证据直接驱动了一个窄修复：

1. 新 Plan 准入要求每个 acceptance ID 只属于一个 WorkItem，复现首轮结构的提案会在执行前拒绝；
2. Planner prompt 明确要求把定位、修改和验证合入拥有验收项的端到端 WorkItem；
3. `search_repo` 达到结果/字节上限时显式返回 `SEARCH_TRUNCATED`，并引导 symbol/concept 查询使用
   ranked `retrieve_code`；执行 prompt 同时禁止重复等价的饱和搜索；
4. 历史事件投影保留旧合同兼容性，因此修复后首轮失败 Trace 仍能得到同一 projection hash；
5. 新 preflight 对当前加载的 `src/horizon/**/*.py` 路径、大小和内容哈希生成统一源码指纹；启动前
   重新计算，不匹配即拒绝，旧的无指纹 preflight 只能读取、不能再次启动付费 Run。

核心失败修复的定向测试为 60 passed；加入源码指纹后相关测试为 65 passed。没有自动发起第二次
付费运行，也没有人工修改 Pilot workspace。

## 7. 第一次受限复跑：上下文硬门限负结果

复跑使用独立 policy `siliconflow-deepseek-v4-flash-pilot-retry-018` 和独立 Campaign
`siliconflow-pilot-retry-2026-10`，仍为同一模型、6-call 上限、`max_attempts=1` 且 fallback 关闭。
Run 与整个 retry Campaign 的费用硬上限都为 `CNY 0.18`；因此即使配置被误触发多次，所有复跑
合计也不能超过 0.18，首轮与复跑累计最多为 `0.0652398 + 0.18 = CNY 0.2452398`。

第一次复跑前的零费用 Docker preflight 已通过：

- prepared TaskSpec ref：
  `6b4c4a9576d8e38661ec0dd886db4784606e5ba97fa3401e27d3fbf81bf72a92`；
- preflight report ref：
  `fed1b7902c9f852d1b8b2d51574a0d8ae75e4c59e5e981b9b709419a4e414b8a`；
- Harness source digest：
  `f56b52ffa378aa523ac665356333fe5ab7db6b23ae62c403f91e350ed1d49b45`；
- `ready=true`、初始验收 exit code 1、source unchanged、solution isolation 通过；
  `paid_model_called=false`、`network_called=false`。
- 当时独立 retry Campaign occupied/settled/reserved/unknown 均为 0，remaining 为 `CNY 0.18`；创建
  不可变 Campaign 定义是本地零费用预检的一部分，不加载凭据或请求 Provider。

用户授权后启动了 Run `run_a548388fa62d4a34a49a7086db5610a6`。该次调用的冻结事实是：

- 3 次模型调用（1 planning、2 execution），2 次只读工具调用；9,940 input tokens、351 output
  tokens；冻结 PriceCard 费用 `CNY 0.032979`；
- 新 Planner 只生成一个拥有唯一 acceptance 的端到端 WorkItem，首个 execution 动作直接调用
  `retrieve_code("unescapeHTML")`，rank 1 正确定位 `youtube_dl/utils.py`；首轮 Plan/搜索修复有效；
- 第二个 execution 动作对 `youtube_dl/utils.py` 做无范围 `read_file`，输出 Artifact 为 122,665
  字节。两个近期完整工具单元超过 `max_context_chars=60000`，而旧投影算法把
  `preserve_recent_context_units=6` 当成绝对保留，下一次模型 reservation 前抛出 `Conflict`；
- 退出时无模型/工具 reservation 或 unknown，workspace/source revision 未变；无编辑、checkpoint、
  validation 或 passed WorkItem。Run 进程已停止，但事件状态仍为 `RUNNING`；wall-clock deadline
  到期后，零网络 reconcile 按“不退还停机时间”规则拒绝接管，没有伪造终态或重新派发；
- 部分 Trace：
  `.horizon/real-model-pilot-retry-v2/run_a548388fa62d4a34a49a7086db5610a6.context-overflow.partial.trace.jsonl`；
  离线 replay projection hash 为
  `7841d09eb3279437f23979c689d1b89061244a982605e5c3538455f0de1b6d5b`。

该负结果驱动三个窄修复：

1. `read_file` 对超过 32 Ki 字符的整文件读取返回小型策略错误，要求成对的 1-based
   `start_line/end_line`，最多 400 行；执行 prompt 要求复用 `retrieve_code` 返回的 path/range；
2. `preserve_recent_context_units` 改为软保留目标；硬上限需要时，只把最少数量的最老近期完整单元
   折叠为带 digest/hash/excerpt 的确定性投影。incomplete 工具单元仍绝不折叠，超限继续拒绝；
3. `agent run/resume` 遇到 pre-dispatch `HorizonError` 时，只有同一 lease 且不存在非 unknown
   在途 reservation 才释放租约；不放宽未知副作用规则。

事故同尺寸的 122,665 字符回归在 60,000 字符生产配置下通过；当时完整离线回归为
`268 passed, 5 skipped`。这只证明根因回归，不证明任务已经修好。

## 8. 第二次受限复跑：预算硬门限负结果

第一次复跑已占用同一 Campaign `CNY 0.032979`，remaining 为 `CNY 0.147021`，reserved/unknown
均为 0。历史 policy 保持不可变；新 policy
`siliconflow-deepseek-v4-flash-pilot-retry-continuation-014` 仍绑定同一最大 `CNY 0.18` Campaign，
但把单 Run cap 收窄为 `CNY 0.14`，为账本保留 `CNY 0.007021` 余量。首轮加整个 Campaign 的理论
累计上限仍是 `CNY 0.2452398`，没有扩大用户的 `CNY 0.25` 上限。

新的零费用 Docker preflight 已通过：

- prepared TaskSpec ref：
  `3a6cc80724afbb7c3db0ebc72421209768fbb293bf889bdef32ae3d17034c778`；
- preflight report ref：
  `4529efb5430611a316a641df07d89cff2b0b9bcabc30f4ee40bf8ad4987b7776`；
- Harness source digest：
  `8518ffc156887f63c3884053064db56f75045989ca32688512a53c0ff806fd51`；
- `ready=true`、初始验收 exit code 1、source unchanged、solution isolation 通过；
  `paid_model_called=false`、`network_called=false`。

用户授权后启动了 Run `run_f42d1e4bf3d34203b247d56448215f41`。该次调用的冻结事实是：

- 3 次模型调用（1 planning、2 execution）和 3 次只读工具调用；10,108 input tokens、
  412 output tokens；冻结 PriceCard 费用 `CNY 0.0285024`；
- Planner 在任务未指定路径时猜测 `youtube_dl/extractor/common.py`。第一个
  `retrieve_code("unescapeHTML")` 已将 `youtube_dl/utils.py` 目标定义排在 rank 1，但执行模型仍跟随
  Plan 的假设路径；`search_repo("def unescapeHTML")` 返回 `NO_MATCHES`，第二次
  `retrieve_code` 仍把 `utils.py` 排在 rank 1；
- 准备第四次模型调用时，当前请求的保守 input ceiling 为 51,328 tokens，费用预留
  `CNY 0.158592`，高于当时 Campaign 余额；持久化账本在 Provider 派发前拒绝，
  因此这一次未计费，也没有 unknown 费用；
- 退出时 workspace/source revision 未变，没有编辑、checkpoint、validation 或 passed
  WorkItem。事件投影保留非终态 `RUNNING`，但静止租约已在 pre-dispatch
  `HorizonError` 路径上释放，不存在运行中 Worker 或开放 reservation；
- 部分 Trace：
  `.horizon/real-model-pilot-retry-v3/run_f42d1e4bf3d34203b247d56448215f41.campaign-gate.partial.trace.jsonl`；
  离线 replay projection hash 为
  `503bba58685de1caaa0a79514d7116a85eb102f0f68a5f17b282d9e5313349f6`。

该轮结束后，retry Campaign occupied/settled 为 `CNY 0.0614814`，remaining 为
`CNY 0.1185186`，reserved/unknown 均为 0。连同首轮独立 Campaign 的 `CNY 0.0652398`，
项目此系列付费 Run 已实际累计 `CNY 0.1267212`，仍低于用户累计上限 `CNY 0.25`。

该负结果驱动三个窄修复：

1. Planner 明确将 repository inventory 仅视为路径存在证据；除非不可变任务指定路径，不得在
   objective/expected artifacts 中猜测精确实现路径；
2. execution prompt 明确 Plan 只是指导而非仓库证据；当 revision-bound retrieval 与猜测路径
   冲突时，必须跟随可验证证据；
3. `retrieve_code` 默认只返回 rank 1 片段；只有 rank 1 模糊或不足时才显式增加
   `max_chunks`，避免让已排名的低相关文本持续膨胀模型请求。

## 9. 第四轮：收窄 continuation 与单边范围读取

历史 policy 均保持不变。新 policy
`siliconflow-deepseek-v4-flash-pilot-budgeted-continuation-011` 继续绑定同一个最大
`CNY 0.18` Campaign，但将单 Run cap 收窄为 `CNY 0.11`、`max_context_chars` 收窄为
7,000，且只软保留最近 2 个完整上下文单元。`max_attempts=1`、fallback 关闭和 solution
isolation 边界都未放宽。

执行前按 7,000 上下文字符、2,555 字节完整工具定义、2,048 字节结构余量和当时的保守
token 上界公式离线计算，一次 execution 请求的预留为 `CNY 0.077298`。该值只能证明首个
请求可被当时的 Campaign 余额覆盖；每次调用前仍会按实际投影重新检查 Run/Campaign 剩余额度。

新的零费用 Docker preflight 已通过：

- prepared TaskSpec ref：
  `43e8209aa020a3fdf9c38a622f3e8401ba6733f96693e8e14a96fa432f9976c4`；
- preflight report ref：
  `094b48c78dc070a0fb45813831ca8af51b3d2c1aa24b77782c666737a8cdbfb0`；
- Harness source digest：
  `78d26ccce347f62b7b26393e41884f0c6dcad0141e5343b2006c71fc403672ee`；
- `ready=true`、初始验收 exit code 1、source unchanged、solution isolation 通过；
  `paid_model_called=false`、`network_called=false`。

用户随后明确授权：仅使用 `deepseek-ai/DeepSeek-V4-Flash` 执行一次，单次最多
`CNY 0.11`，系列累计仍不超过 `CNY 0.25`，不重试、不 fallback。Run
`run_a12c4d0dbdb54e8bb3c49cb143a69392` 的结果为：

- 5 次模型调用（1 planning、4 execution）、8 次工具调用；14,441 input tokens、781 output
  tokens；冻结 PriceCard 实际结算 `CNY 0.050352`；
- Planner 生成一个端到端 WorkItem，不再猜测 `common.py`；每轮 revision-bound RAG 都把
  `youtube_dl/utils.py` 及 561～600 行附近目标定义排在 rank 1；
- 执行模型三次提交同一类非法参数：
  `{"path":"youtube_dl/utils.py","start_line":560}`，只有 `start_line`、没有 `end_line`。
  `read_file` 均在 Gateway 参数校验阶段拒绝，没有读取大文件或产生写副作用；
- 三次非法读取之间穿插了不同检索词，因此现有“连续相同动作/精确 A-B 循环”保护没有把它
  判为同一停滞模式。这是当前精确 NoProgress 策略的已知边界，不据此扩展为语义检测；
- 准备下一次 execution 请求时需预留 `CNY 0.067530`，但单 Run 只剩 `CNY 0.059648`。
  Run 硬门限在 Provider 派发前拒绝；该次结算为 0，reserved/unknown 均未遗留；
- workspace/source revision 未变，没有编辑、checkpoint、validation 或 passed WorkItem。
  事件投影仍为非终态 `RUNNING`，但 Lease 已释放且没有活跃 Worker；
- 部分 Trace：
  `.horizon/real-model-pilot-retry-v4/run_a12c4d0dbdb54e8bb3c49cb143a69392.run-budget.partial.trace.jsonl`；
  离线 replay projection hash 为
  `ad88aada8bd5342c41ee02fb570ae37cfc24ec8e59347dd1ec615f1e7163a8a9`。

第四轮逐调用的保守预留与结算对照如下。各 reservation 是顺序创建、逐次结算，合计列只用于
诊断上界松紧度，不代表 `CNY 0.334050` 曾被同时占用：

| 阶段 | 预留 CNY | 实际 CNY | actual / reserved |
|---|---:|---:|---:|
| planning | 0.076968 | 0.012948 | 16.82% |
| execution 1 | 0.054456 | 0.007623 | 14.00% |
| execution 2 | 0.067566 | 0.009951 | 14.73% |
| execution 3 | 0.067530 | 0.009936 | 14.71% |
| execution 4 | 0.067530 | 0.009894 | 14.65% |
| 诊断合计 | 0.334050 | 0.050352 | 15.07% |

该单样本说明当前上界较保守，不能单独证明应降低安全系数。后续应在更多离线/真实回执上按
planning 与 execution 分层统计，再决定是否修改 token ceiling 或输出预留；费用硬门限不因本表
自动放宽。

本轮后 retry Campaign occupied/settled 为 `CNY 0.1118334`，remaining 为 `CNY 0.0681666`，
reserved/unknown 均为 0。连同首轮独立 Campaign 的 `CNY 0.0652398`，本系列实际累计
`CNY 0.1770732`，距离用户累计上限还剩 `CNY 0.0729268`；当前真正更紧的是 retry Campaign
余额 `CNY 0.0681666`。本次“一次”授权已经用完，未自动发起重试。

该负结果只驱动两个窄的离线修复：

1. `read_file` Schema 用 `dependentRequired` 强制 `start_line`/`end_line` 成对出现；工具描述、
   执行 prompt 和错误回执同时给出可直接复制的双边界示例；
2. Planner 已被禁止依据 inventory 猜测相关路径，因此默认 inventory 从 200 项收窄到 50 项，
   仍保留总数和截断标记。同一任务的离线 planning 费用预留由 `CNY 0.076968` 降到
   `CNY 0.044010`；这是输入上界对照，不是实际 Provider 账单或成功率提升证据。

上述 Harness Python 源码变化已经使本节旧 preflight report 失效。没有创建新付费合同，也没有
继续消耗剩余预算；下一次若要运行，必须先基于新源码重新做零费用 preflight，并取得新的明确
费用与外发授权。

第四轮历史 Trace 不追溯改写，因此仍保留 `RUNNING` 且 Lease 为空的原始投影。后续实现已增加
[确定性费用停止语义](budget-stop-semantics.md)：未来同类 Run/Campaign 派发前费用不足会原子写成
带 `required_cost`/`available_cost` 的 `FAILED`，同时清除 Lease；unknown 或待对账用量不会被
误终止。离线脚本回归还证明 Agent 能从同类单边范围错误收到结构化回执、改用双边界读取并完成
编辑、保护性验收和 Trace 重放，但这不是第四轮真实模型成功。

## 10. tqdm 新任务的零费用候选预检

在不复跑 youtube-dl、也不增加 Campaign 的前提下，新增了一个来源绑定的 tqdm-1 候选：

- Pilot manifest：
  [`full/tqdm-1-tenumerate-start/real-model-pilot.yaml`](../benchmarks/run_ab/full/tqdm-1-tenumerate-start/real-model-pilot.yaml)；
- Provider policy：
  [`siliconflow-tqdm-pilot.yaml`](../config/providers/siliconflow-tqdm-pilot.yaml)；
- 干净完整 checkout：82 files，Git HEAD
  `8cc777fe8401a05d07f2c97e65d15e4460feab88`；
- 模型可见范围只允许 `tqdm/contrib/**`，验收可见，但 fixed commit、fix URL 和精确修复字面量
  只存在于控制器 manifest；
- `max_attempts=1`、`fallback_enabled=false`，Run cap 为 `CNY 0.06`。以现有历史结算计，
  最坏情形下 retry Campaign 占用为 `0.1118334 + 0.06 = CNY 0.1718334`，系列累计为
  `0.1770732 + 0.06 = CNY 0.2370732`，分别低于 Campaign `CNY 0.18` 和用户累计
  `CNY 0.25` 上限。

preflight 同时补上了一个此前缺失的门：根据冻结 TaskSpec、允许路径 inventory、规划工具 Schema
和 Provider PriceCard，离线计算**首次自动规划请求**的保守预留，并要求它同时不超过 Run cap
和当前 Campaign 余额。该门只证明首个确定性请求可派发；后续动态上下文仍在每次派发前重新计算，
不会预先承诺整条 Run 一定能在预算内完成。

2026-10-04 在 Evidence→Write lineage 提交后的本地禁网 Docker preflight 结果：

- `ready=true`；初始保护性验收按预期 exit code 1，source 再快照不变；
- 首次规划 input ceiling 8,220 tokens、output ceiling 768 tokens，保守预留
  `CNY 0.031572`，同时通过 `CNY 0.06` Run cap 和 `CNY 0.0681666` Campaign 余额门；
- prepared TaskSpec ref：
  `a5ab50188c4ddf0941dfe1ef247218fbc8c38156722b7671a96934b6c2f5dc72`；
- preflight report ref：
  `07453273206fa1e7af4f84531345f0b7bf763846ddb2729837178de40c193cbb`；
- Harness source digest：
  `36ff2b9ea368a85eb26ea7866322ace1fc2f4ee7f03c18313df013c355986c5e`；
- Docker image digest：
  `sha256:0687a6bc9716edc2a6ee0fbfb0f87e7ee358b262b67c9215de91bc9b2d38ba71`；
- `credential_loaded=false`、`paid_model_called=false`、`network_called=false`。

在该 preflight 生成时，这还不是第五个真实模型 Run，也不是成功证据。生成的 launch command 被刻意停在
`--confirm-paid` 之前；现有旧授权均已用完，只有新的明确外发与费用授权才能启动。

## 11. tqdm 第五轮：规划成功、执行派发前预算停止

用户明确授权只执行一次 tqdm-1 运行：Run 上限 `CNY 0.06`，历史系列累计上限仍为
`CNY 0.25`，最多 6 次模型调用，每次请求不重试、不 fallback。Run
`run_fec07b6bf28d45d5bc428cda120959a3` 的实际结果为类型化失败，而不是 Issue 修复成功：

- 状态 `FAILED`，`failure_reason=run_model_cost_limit`；
- 唯一规划调用成功结算：1,117 input / 247 output tokens，实际费用 `CNY 0.005574`；
  response Artifact 为
  `250b49c66ac60dfe1a799f079d1731fac2d864176529517259a8f3531378e806`，Provider trace ID 为
  `ti_jdbd1xd8wvvro97i3p`；
- 第一条 execution 请求需保守预留 `CNY 0.054522`，但 Run 只余 `CNY 0.054426`，短缺
  `CNY 0.000096`。控制器在 Provider 派发前写入 `BudgetStop` 并清除 Lease；
- execution 模型调用、工具调用、步骤、编辑、checkpoint 和 protected validation 均为 0；
  source 与 staging workspace revision 都保持
  `0facaee0ad34b048338c656cdbfc61dfc443229991d7b6573ccf0bcc8e9effc2`；
- Campaign 没有 reservation/unknown，occupied/settled 为 `CNY 0.1174074`，remaining 为
  `CNY 0.0625926`。连同首轮独立 Campaign，本系列实际累计为 `CNY 0.1826472`。

Trace 位于
`.horizon/real-model-pilot-tqdm-v2/run_fec07b6bf28d45d5bc428cda120959a3.trace.jsonl`，共 14 个
事件，文件 SHA-256 为
`4f542b0cc5106b506430b866f7e34274b20351171b71e933b7a5d447950179d8`；离线 replay 得到投影 hash
`1cfa80c84ffb476767a39afbc6b02af135b86826d7e531d01929253ec3d4143c`，与终态一致。

该轮当时被误记为“模型写入不存在的 `tqdm/contrib/itertools.py`”；后续直接核对冻结 checkout
确认该文件实际存在，只是 `tenumerate` 不在其中。这个更正保留在文档中，不能把“路径存在”
偷换成“实现位置正确”。离线增量加入的窄规则只会在 inventory 完整时拒绝真正不存在的路径；
inventory 截断时保持未知。它没有、也不应声称解决已有文件上的语义定位问题。

第五轮授权消耗完后没有复跑；随后为第六轮准备了新的零费用合同：

- manifest：
  [`real-model-pilot-v2.yaml`](../benchmarks/run_ab/full/tqdm-1-tenumerate-start/real-model-pilot-v2.yaml)；
- policy：
  [`siliconflow-tqdm-pilot-v2.yaml`](../config/providers/siliconflow-tqdm-pilot-v2.yaml)；
- 新 Run cap 为 `CNY 0.062`。这是根据实际短缺和固定的 execution 512-token 输出上界设置的
  新独立 policy；最坏情况下 Campaign 为 `0.1174074 + 0.062 = CNY 0.1794074`，系列为
  `0.1826472 + 0.062 = CNY 0.2446472`，仍分别低于 `CNY 0.18` 和 `CNY 0.25`；
- 禁网 Docker preflight 为 `ready=true`，首次规划 input/output ceiling 为 8,622/768 tokens，
  预留 `CNY 0.032778`；prepared Task ref 为
  `ceb471da1e0af85e7567f6bae14ed84332b27495a9ec786ad38e2344c9dd5ce6`，report ref 为
  `72715671ce28cfe92f7ffb5ed6beb466cec628f9218fc5fc9d58b6a7a969f176`，Harness digest 为
  `8335b5419ca0ce80b3605d5374b11b1bbeb509ea11f9de92646c5f665d8ff8f7`；
- `credential_loaded=false`、`paid_model_called=false`、`network_called=false`，初始负例按预期
  失败且 source unchanged。

该 preflight 当时只是可启动证据，不保证动态后续请求都能放进预算。用户随后另行明确授权了
第六轮；旧授权与该 report 均不能再次用于新的付费运行。

## 12. tqdm 第六轮：RAG 纠正 Plan 假设，Campaign 派发前停止

用户明确授权只执行一次 v2 Run：允许向 SiliconFlow 发送公开 tqdm-1 上下文、允许路径内代码
片段、候选修改和工具回执；模型仍为 `deepseek-ai/DeepSeek-V4-Flash`，单 Run 上限
`CNY 0.062`、历史累计上限 `CNY 0.25`、最多 6 次调用、不重试、不 fallback。Run
`run_7fab106a71174c77bb0a82a8a054d3ed` 的实际结果仍为受控失败：

- 终态 `FAILED`，`failure_reason=campaign_cost_limit`，无开放 reservation 或 unknown；
- planning 调用 1,148 input / 247 output tokens，费用 `CNY 0.005667`；execution 调用
  2,254 input / 45 output tokens，费用 `CNY 0.007167`；本 Run 合计 `CNY 0.012834`；
- Planner 再次把 `tqdm/contrib/itertools.py` 写成实现位置。该文件确实存在，因此不存在路径
  准入不会拒绝它；但文件内容不包含 `tenumerate`，Plan 的语义定位仍没有证据；
- execution 没有直接读取或编辑该假设路径，而是调用
  `retrieve_code({"query":"tenumerate"})`。SQLite FTS5 在 3 个允许文件中把真实实现
  `tqdm/contrib/__init__.py` 1～40 行排为 rank 1，0 skipped、无 degraded reason；EvidencePack
  Artifact 为 `d6d9f8bea59c99c2b56c1df5de67b0b26bd14d921d6aa9086331ec8595503288`；
- 消费 retrieval 回执后的下一次模型请求需保守预留 `CNY 0.05832`，当时 Campaign 只余
  `CNY 0.0497586`。控制器在 Provider 派发前写入 scope=`campaign` 的 `BudgetStop`；
- 共 2 次模型调用、1 次只读工具、1 step；编辑、checkpoint、protected validation 和 passed
  WorkItem 均为 0。source 与 staging workspace revision 都保持
  `0facaee0ad34b048338c656cdbfc61dfc443229991d7b6573ccf0bcc8e9effc2`。

Trace 位于
`.horizon/real-model-pilot-tqdm-v3/run_7fab106a71174c77bb0a82a8a054d3ed.trace.jsonl`，共 23 个
事件，SHA-256 为
`804bd8644d8d02d1ea3e43421c010c31b5ae35b75a7ecbf9772a84f06bc6e921`；离线 replay 投影 hash 为
`a3a9ff4b0067a6b323c14cc5f954ca1197d20b13099a63c96d5be97f1d66c4bc`。Campaign 目前
occupied/settled 为 `CNY 0.1302414`、remaining 为 `CNY 0.0497586`，reserved/unknown 为 0；
连同首轮独立 Campaign，本系列实际累计为 `CNY 0.1954812`，距用户累计上限还剩
`CNY 0.0545188`。

该轮提供了两个不应混淆的结论：revision-bound RAG 在真实模型路径假设错误时给出了正确 rank-1
证据；但 Agent 尚未得到消费该证据并编辑的下一次模型调用，因此不能声称“模型已正确使用 RAG”
或“接近修复成功”。高额保守 reservation 再次成为实际停止原因。当前不降低费用安全系数、不建
新 Campaign，也不自动复跑；应先离线分析完整请求固定开销与历史 reserved/actual 比率，再决定
是否有足够证据修改预算估计或上下文合同。

上述离线分析现已落地为 `horizon trace reservation-report`。六轮 20 个已结算调用经 Trace replay
后得到：派发时预留费用累计 `CNY 1.341318`、本地 PriceCard 结算累计 `CNY 0.1954812`，聚合比
`6.861621`；input 上界/Provider input 的单调用中位数为 `7.100207`。这确认了系统性预留压力，
但 17 个较早调用没有 request-byte estimator 元数据，因此仍不授权直接替换硬门禁公式。口径和
限制见[模型预算预留诊断](reservation-diagnostics.md)。

2026-10-07 的进一步零费用拆分发现，Workspace Tool Gateway 本来就只暴露当前 WorkItem 允许的
工具，不能把压力误归因于“每轮发送全部工具”。真正可安全裁剪的是新会话中尚无结构性执行证据
时的动态 `revise_plan` Schema。以第六轮保存的 4 条会话消息和同一组 WorkItem 工具重建请求，
移除该 Schema 后 wire body 从 9,197 降为 7,962 bytes，减少 1,235 bytes；在未改变的生产公式
下，input ceiling 减少 2,470 tokens，单次保守预留减少 `CNY 0.007410`。把相同差额应用到当时
已投影的停止请求，预留会从 `CNY 0.058320` 降到 `CNY 0.050910`，仍高于旧 Campaign 余额
`CNY 0.0497586`，所以该优化本身不能把历史失败改写成可派发或成功。

实现现改为：新 WorkItem 先隐藏 `revise_plan`，在持久会话已有至少两轮执行证据，或已有通过
WorkItem 后再暴露；成功使用后仍永久移除。判定只依赖已保存会话元数据与通过项，避免模型响应/
只读工具恢复时因实时工具尾部变化而改变 request hash。集成回归覆盖证据前隐藏、证据后出现、
跨 WorkItem 立即可用和成功后移除；本次没有联网、没有模型调用、没有新 Campaign，也没有改变
保守 token 公式或历史 Trace。

## 13. tqdm v3：任务级最小工具权限与零费用预检

第六轮之后没有复用旧授权。2026-10-07 先完成两项不调用模型的通用收窄：

1. 新 WorkItem 不再无条件携带大型 `revise_plan` Schema；只有持久会话已有至少两轮执行证据，
   或前序 WorkItem 已通过时才暴露；
2. TaskSpec 新增可选 `constraints.allowed_tools`。它只能从 execution mode 的工具上界中收窄，
   Planner Schema、Plan 校验和执行 Gateway 依次取交集；`submit` 仍由控制器提供。省略该字段时
   历史 TaskSpec 序列化与 hash 不变。

用第六轮持久会话做同消息离线 A/B：原“全部 WorkItem 工具 + replan”请求为 9,197 bytes；只做
replan 证据门后为 7,962 bytes；再应用 v3 的 `read_file / retrieve_code / replace_text` 任务级
权限（加控制器 `submit`）后为 6,786 bytes。相对原请求减少 2,411 bytes，在未改动的生产上界
公式下减少 4,822 input ceiling、`CNY 0.014466` 单次保守预留。把同一差额用于第六轮已投影的
停止请求只得到 `CNY 0.043854` 的反事实估算；它是请求结构诊断，不是 Provider 回执，也不改写
历史失败。

新候选保持同一公开 tqdm-1 完整 checkout 和模型，但创建独立合同文件：

- manifest：
  [`real-model-pilot-v3.yaml`](../benchmarks/run_ab/full/tqdm-1-tenumerate-start/real-model-pilot-v3.yaml)；
- Provider policy：
  [`siliconflow-tqdm-pilot-v3.yaml`](../config/providers/siliconflow-tqdm-pilot-v3.yaml)；
- Task 工具上界只有 `read_file`、`retrieve_code`、`replace_text`；模型不需要主动 `run_check`，
  因为 `submit` 后的 required acceptance 始终由控制器执行；
- Run cap 为 `CNY 0.049`。若完整占用，该 Campaign 最多为
  `0.1302414 + 0.049 = CNY 0.1792414`，历史系列最多为
  `0.1954812 + 0.049 = CNY 0.2444812`，都不超过既有冻结上限。

本机已有 `python:3.12-alpine` 上的禁网 Docker preflight 结果为：

- `ready=true`；82-file full checkout/Git HEAD、初始 required check 失败、source unchanged、
  `.git` 排除和 solution isolation 全部通过；
- planning request 为 3,965 bytes，input/output ceiling 为 8,954/768，保守预留
  `CNY 0.033774`，同时低于 Run cap 与 Campaign remaining `CNY 0.0497586`；
- prepared Task ref：
  `ff59040de8a6357503c9c718753bd28acc8749cc04163d7f3141470876fa91c9`；
- preflight report ref：
  `8d493f7be6ee86aa8c2a37fbe5d690525f817ca0822ac7e7b44fdb14d164c683`；
- Harness source digest：
  `b5ee9da701392eaa4283620c5ef47ce78cf17520f3c7e31ac217e84d228fb477`；
- Docker image digest：
  `sha256:0687a6bc9716edc2a6ee0fbfb0f87e7ee358b262b67c9215de91bc9b2d38ba71`；
- `credential_loaded=false`、`network_called=false`、`paid_model_called=false`，没有 reservation 或
  unknown 用量。

`ready=true` 只说明首次规划请求和静态启动绑定可通过，不保证后续动态请求或任务成功，也不
构成付费授权。六次旧授权都已消费；启动 v3 仍需新的、逐字明确的外发范围、模型、单 Run
`CNY 0.049`、系列累计 `CNY 0.25`、不重试和不 fallback 授权。

## 14. 后续付费 Pilot 的完成条件

正式运行时只接受 preflight 输出的 TaskSpec/report、同一镜像和专用 Provider policy。结果无论
成功还是失败，都必须记录：

- 模型自主生成的 Plan、检索词、工具调用和最终 workspace revision；
- 初始负例与最终所有 protected checks；
- 规划/执行各调用的 usage、CNY cost、response Artifact 和 provider trace ID；
- 是否进入 NoProgress、repair、replan、WAITING、unknown 或恢复路径；
- source unchanged、Trace export/replay、开放 reservation/unknown 数量；
- 失败分类，而不是人工补丁替模型完成任务。

一次通过只能称为“真实模型 Pilot 个案”，不能称为 BugsInPy 分数、泛化能力或长程任务
成功率。第六轮仍是失败个案。任何下一次付费 continuation 都需要
新的零费用 preflight、与剩余额度一致的新 Run cap，以及新的明确授权；仍不增加向量库、多
Agent、Project Memory、自动 fallback 或第二次 replan。
