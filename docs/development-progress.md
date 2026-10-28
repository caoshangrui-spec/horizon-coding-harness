# 开发进度与验证记录

更新：2026-10-28。已发布版本和主干包元数据均为 0.3.0；版本范围与发布证据见
[v0.3.0 release notes](release-v0.3.0.md)。2026-09-30 的
可靠性内核证据保留，本次在其上增加 Provider 垂直切片和首个受控 Coding Agent 闭环。
本文记录实现事实与自检，不是全项目验收或独立安全审核。测试报告时间戳来自执行机器；
确定性测试中的时钟是注入值，不用它推断实际运行日期或耗时。

## 2026-10-28 Run A/B 成功质量与检索降级聚合

- 把终态 EvidencePack 原有的 required-check、unknown acceptance、unknown/open effect 和
  Trace-bound degradation 计算提取为 status-independent facts；Run A/B 的 `WAITING_FOR_USER`
  arm 与终态 arm 现在使用同一口径，不复制第二套判断逻辑。
- 每条新 arm report 增加可选 `reliability`；case/suite 分列 clean 与 quality-flagged success、
  验收未知、已知 required-check 失败、fallback/degradation arm、检索降级 arm、降级记录数和
  逐 flag arm 数。`terminal_success_count` 仍只由 `SUCCEEDED` 决定，成功不会抹掉质量标记。
- 新增 test-only 不可解码源码回归：Baseline 与 Treatment 各有两次真实 partial-index retrieval
  degradation；Treatment 的 protected check 仍通过并被报告为 quality-flagged success，后续
  NoProgress 策略回执不被误计为 degradation。旧 arm 可省略 reliability，旧 suite 质量字段按 0
  读取；冻结 manifest 与 digest 未修改。
- 本批未增加 scheduler、数据库事件、付费模型调用或公开任务数量。全量离线回归为
  **607 passed，11 skipped**，Ruff 格式与静态检查、离线构建通过。
- 首次公开复跑
  [`40673288132`](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40673288132)
  的四组 suite、Docker 合同、证据上传均成功，但最后的摘要门禁误把三阶段 Baseline 预期成
  3 条 known required-check failure，因而失败。实际 Trace-derived facts 为 3 条
  `acceptance_evidence_unknown`、0 条 known failure：前两项局部检查已通过，但当前 Task 的完整
  required acceptance 尚无最终 receipt。该失败保留为证据定义错误，没有改写 report 或伪报通过。
- 修正门禁后，公开复跑
  [`40682108470`](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40682108470)
  的 Docker 合同、四组 suite、artifact 上传和摘要发布全部成功；标准 CI
  [`40682108483`](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40682108483)
  同步通过。统一 artifact ID `12813023460`，201,900,712 bytes，digest
  `sha256:e1a37b2308e7992244d096cde9f629ceea5df3c2290d6cb1ea2e474bf6dd7735`，保留至
  2026-11-27。未调用付费模型，评测路径未发起模型网络请求。

| Suite | Cases passed | Unknown acceptance | Clean / flagged success | Known check failure | Retrieval-degraded arms | Degradation records |
|---|---:|---:|---:|---:|---:|---:|
| dependency-reduced v2 | 5/5 | 5 | 5 / 0 | 0 | 0 | 0 |
| full-checkout v8 | 15/15 | 15 | 1 / 14 | 0 | 28 | 56 |
| multi-stage v4 | 3/3 | 3 | 0 / 3 | 0 | 6 | 27 |
| hard-crash v1 | 1/1 | 1 | 0 / 1 | 0 | 2 | 9 |

## 2026-10-27 Terminal Suite 可靠性聚合

- `terminal-suite` 升为 schema v2：每个 case 只从已验证的子 EvidencePack 读取 required-check、
  unknown acceptance、quality flags、fallback 与 degradation，不从失败文本或摘要猜测。
- 原始 `task_success_rate` 保持只由 `SUCCEEDED` 决定；新增 clean 与 quality-flagged success 分列、
  验收证据未知、已知 required-check 失败、逐 flag Run 数、fallback/degradation 和
  `retrieval_degraded` Run 数。真实 Trace-bound 检索降级成功样本会显示为成功但非 clean。
- `verify-terminal-suite` 递归验证子包、重放 Trace 并复算新增字段。回归覆盖重算顶层 hash 后的
  虚假可靠性汇总拒绝，以及由 schema v1 suite 和 v1 子包组成的历史证据继续脱库验证。
- 本批没有新增调度器、数据库表或运行期事件；suite 仍只聚合既有终态 Run。全量离线回归为
  **606 passed，11 skipped**，Ruff 检查和格式检查通过；未联网、未调用付费模型。

## 2026-10-26 Provider 可靠性：持久熔断与保守 Retry/Fallback 合同

- 新增 `reliability/retry.py`：统一限制逻辑请求最多 3 次、累计等待最多 60 秒，并把
  transport 内层尝试固定为 1。401/403、配置和协议错误直接停止；429/5xx/连接失败虽标记为
  transient，但现有调用无法证明未派发或未计费，因此决策为
  `stop / unknown_dispatch_or_cost`，不以 HTTP 标签绕过双账本。
- 新增 `reliability/breaker.py` 与 `adapters/model/reliability.py`：健康 key 绑定
  endpoint/provider/model/operation/credential reference，SQLite 投影和追加事件与 Campaign
  ledger 共库但独立版本。连续 3 次 transient failure 后 OPEN，30 秒后只允许一个 half-open
  探针；重启保持状态，401 不增加计数。probe、自动 planning、同进程和直接子进程 execution
  均在费用 reservation 前检查 breaker。
- 新增 `domain/run_reliability.py` 与 `reliability/fallback.py`：只允许最多一个预授权且工具/Schema/context/API/数据边界/
  币种/Run 与 Campaign 余量均兼容的 candidate；可比评测、未知费用未保留或 Task/required
  acceptance hash 改变都拒绝。当前只做资格合同，不实际切换模型。
- `RunReliabilitySummary` 要求降级保留原错误、策略、quality flag 和限制；required checks
  未通过或证据未知时不能标 passed。该摘要已进入通用终态 EvidencePack schema v2：从重放
  Trace 复算终态、required-check、unknown acceptance 和 unknown/open effect，CLI 导出/验证
  返回同一结构；schema v1 历史包继续按旧规则验证。
- 生产 Agent Loop 会解析成功 `retrieve_code` 的 typed EvidencePack；明确 `degraded` 时，把原原因、
  lexical/partial-index 策略、call ID、Artifact ref、Task 与 required-acceptance hash 同原
  `TOOL_CALL_SETTLED` 原子提交。Run 投影仅在存在记录时增加字段，保持旧 Trace projection hash；
  TaskSpec 修订会清空当前合同的投影 history，但旧事件仍保留。终态摘要现能展示真实检索降级；
  model fallback、context-summary/model degradation 仍无生产事件，不从日志猜测。
- 本批 Provider 合同新增 29 个测试 case，连同 doctor 入口断言共 30 项；终态集成另新增已知
  check failure、schema v1 兼容、可靠性元数据篡改拒绝、真实 lexical degradation 端到端和
  伪绑定原子回滚回归。当前离线全量 **603 passed，11 skipped**；未联网、未调用付费模型。
- 详细数据流、状态转换和未实现边界见
  [Provider 重试、熔断与 Fallback 合同](provider-reliability.md)。

## 2026-10-26 保守意图入口：零模型分类与权限收窄

- 新增 `domain/intake.py` 与 `application/intake.py`：`IntentEnvelope` 分开记录
  `request_kind`、`task_kind`、请求模式、有效模式、目标 Run、缺失字段、原文证据区间、有效模型工具和
  admission 调用/费用。结构化 TaskSpec 优先于文本任务规则；显式控制命令优先于两者。
- `horizon intake route` 可独立复核 explain/review/plan/bugfix/refactor/tests、status/resume/cancel、
  唯一当前 Run、缺失/未知 Run、否定写入指令和结构化 TaskSpec。控制请求与确定性规则均为
  `admission_model_calls=0`、`admission_cost_cny=0`，且不创建数据库、Run 或工具副作用。
- 有效模式严格取“请求模式 ∩ admission authority”；只读/计划模式不暴露写工具，结构化 TaskSpec 的
  tool allowlist 再做一次交集。未知任务或同时出现 read/write 意图时保持 `read_only`、输出
  `admission_ready=false` 与待分类字段，不用高置信度猜测换取写权限。22 项聚焦测试覆盖上述正例和负例。
- 当前只是确定性 admission 垂直切片：没有调用分类模型、没有 `intake_events`/`INTAKE_ATTACHED`
  持久化，也没有把自然语言 envelope 自动派发到现有 status/cancel/resume 或 Run 创建入口；因此
  FR-701/702 只记为部分实现，不把 CLI 分类预览宣传成完整自然语言 Agent 入口。

## 2026-10-26 完整 Checkout v8：再增加三种独立失效模式

- v8 原样保留 v7 十二例，再加入预冻结 development 来源中的 Sanic-5 root logger namespace、
  TheFuck-2 platform PATH separator 与 Luigi-2 Beam BigQuery target path。当前为 15 案例、
  9 个项目、4,491 tracked files；suite digest
  `1985508c6a8c807d4231102d78fdfb34d599d8a81de04954b245614e6271a229`，三个新增 manifest
  digest 分别为 `28eb3c03...ed7f`、`142cfa00...0e32` 与 `82164654...3ff3`。
- 三个 buggy 保护检查分别以错误 logger key、Windows-style PATH 被冒号切碎，以及
  `BigQueryTarget` 顶层属性不存在而失败；对应 fixed commit 均通过。test-only 本地完整 Harness
  中三个 Baseline 都以 48 events 进入 `WAITING_FOR_USER`，三个 Treatment 都以 6/7/7
  model/tool/step、74 events 成功。report ref 分别为 `bfbd3487...1699`、`17322d14...2cbc`、
  `71e426d5...a6f0`；六条 Trace 均可重放且 source 未变，无网络或付费模型调用。
- 三条 revision-aware Code RAG 最终均把生产目标文件排 rank 1；Sanic/TheFuck/Luigi 分别索引
  154/378/327 个文件并显式跳过 2/2/55 个文件，状态保留为 `degraded`。对应 EvidencePack ref
  为 `0d09843b...e225`、`7089973e...17d4` 与 `e34ed925...dec3`。初始较泛的 Sanic 与 Luigi
  query 曾将目标排在 rank 3/4，随后使用 failing-test 已知的配置常量或类/方法标识符收窄到
  rank 1；这是 development 调整，不是独立 RAG 泛化证据。
- 本机 Docker daemon 未运行，因此三个新增案例先记为本地可信 Harness 3/3。随后公开 run
  [`40311947427`](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40311947427)
  重建全部 15 个精确 checkout，并通过完整 checkout 15/15、三阶段 3/3 与硬崩溃 1/1。统一 artifact
  ID `12645122879`，201,843,271 bytes，digest
  `sha256:764e8ee2af63a050bb8465542adf69e9fc521e24aebc502eff312bc91b457b84`；doctor 的来源案例数与
  公开 Docker verified count 现均为 15。

## 2026-10-26 完整 Checkout v7：三种新失效模式

- v7 原样保留 v6 九例，再加入 2026-10-24 在补丁揭示前冻结的 development 来源：
  Sanic-2 的 asyncio lifecycle proxy、HTTPie-4 的 Host header，以及 Luigi-3 的 flat tuple 反序列化。
  当前为 12 案例、9 个项目、3,573 tracked files；suite digest
  `825d359204ddf66081c1faf8d6f3b60d4869ae3e2a82b16e6f151039a7dc2047`，三个 case digest 分别为
  `a29294e3...9d971`、`c03af519...aa46` 与 `af4c7dce...06129`。
- 三个 buggy 保护检查分别以 lifecycle methods 缺失、重复 Host header 和 scalar `TypeError` 失败，fixed
  commit 均通过。本地完整 Harness 中三个 Baseline 都以 48 events 进入 `WAITING_FOR_USER`；
  Sanic/HTTPie Treatment 均以 6/7/7 model/tool/step、74 events 成功，Luigi 以 7/8/8、
  83 events 成功。report ref 分别为 `9002c6ed...8124e`、`14d018f0...8c81`、
  `27c5ca11...bc83`；六条 Trace 均可重放且 source 未变，无网络或付费模型调用。
- revision-aware Code RAG 在三例都把生产目标文件排 rank 1；Sanic/HTTPie/Luigi 分别索引
  186/57/320 个文件并显式跳过 2/2/55 个非文本文件，状态保留为 `degraded`。第一次 Luigi
  Treatment 因锚点 `except ValueError:` 在文件内出现三次，被精确写工具安全拒绝；失败 report ref
  `0dde8138...b8ba9`。改成两个唯一、逐行、语义等价的锚点后才通过。
- Windows 十二案例联合尝试没有伪报成功：旧 Tornado 多行 LF 锚点未命中 CRLF checkout，随后
  Cookiecutter 在长 state 路径触发 Win32 path-length 限制。首次公开 v7 run `40303452323` 随后
  确认该 Cookiecutter commit 在 Linux 含文档符号链接，安全门按设计以 `AUTHORS.md` 拒绝；失败
  artifact digest `sha256:9e159c8e...0b77a86`。未排除链接或放宽门禁，而是移除该候选并以预冻结的
  Sanic-2 替换；Sanic-2 本地完整 Harness 已通过。随后替换版公开 run
  [`40305739627`](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40305739627)
  通过完整 checkout 12/12、三阶段 3/3 与硬崩溃 1/1。统一 artifact ID `12642577416`，
  183,527,015 bytes，digest
  `sha256:dade5bfc81fd600cfd85e504e5415e8fa890e8de3794dfc1d8388953f7acaf63`；doctor 的
  source-bound case count 与 Docker verified count 现均为 12。

## 2026-10-25 完整 Checkout v6：PySnooper 两文件修复

- v6 原样保留 v5 八例，再加入预冻结 development 选择中的 PySnooper-2：精确 buggy commit
  `e21a31162f4c54be693d8ca8260e42393b39abd3`、25 tracked files、MIT。九项目合计 2,951 files；
  suite digest `52afa08518568e0c4e2ab19df6bf4cd267dcf260804152888b301e16f2940428`，case digest
  `b7dbab0c33caefd4eb833d2c5da1d57c29a79815907a3ebb4ea5eb9ab1f5153b`。
- buggy 版本的真实包检查以不支持 `custom_repr` 失败，fixed commit 通过。本地完整 Harness 中
  Baseline 48 events 后进入 `WAITING_FOR_USER`；Treatment 一次 replan、跨两个生产文件八次单行
  替换，以 13/14/14 model/tool/step、137 events 进入 `SUCCEEDED`。report ref
  `9ab791353fd1e6172cc5d974bcb897ee8210be60285f08bce47989c4208dd3fa`；两条 Trace 可重放且 source
  未变，无网络或付费模型调用。
- revision-aware Code RAG 索引 25/25 文件，无降级；冻结查询把 `pysnooper/utils.py` 与
  `pysnooper/tracer.py` 分别排第 1/3。第一次 Treatment 因 YAML block scalar 丢失补丁缩进而失败，
  report ref `78f888cf2444d27b0c7628d3c43fcdf4796990a8cb5314848a866542c37ef269`，负结果保留；改用显式
  换行后才通过。
- [公开 workflow 40206304099](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40206304099)
  已重新获取九个精确 commit 并通过 `case_count=passed=initial_failed=treatment_recovered=9` 硬门；
  九例的禁网 Docker 验收、Trace replay 与 source isolation 均通过。统一 artifact ID
  `12604040662`、167,434,042 bytes、digest
  `sha256:8c991b53d88d2934459e940ca7ba20fcba8dd13f8fd14faf4361cb01c331a224`；doctor 的来源案例与公开
  Docker verified count 现均为 9。

## 2026-10-25 完整 Checkout v5：扩到八项目

- v5 原样保留公开 Docker 6/6 的 v4，再加入此前已在检索规则开发前冻结的 TheFuck-1，以及独立
  retrieval holdout 中的 Sanic-1；新增的端到端 manifest 是本轮制作，不能重新称为 holdout。
  TheFuck/Sanic 分别为 401/189 tracked files，八项目合计 2,926 files；suite digest
  `2dca6b3bcffcb88456ae0f05e833f11fa78a3ee1e0ca6dd081c5d0087c7ab12b`。
- 两个 buggy checkout 的依赖无关 AST 保护检查均失败，对应 fixed commit 均通过。本地可信 A/B
  中两例 Baseline 都以 48 events 进入 `WAITING_FOR_USER`；TheFuck Treatment 用同文件两次精确替换，
  83 events 后 `SUCCEEDED`，report ref `3e25e0f...0189c89`；Sanic Treatment 用一次精确替换，
  74 events 后 `SUCCEEDED`，report ref `6b6bbce8...664331`。四条 Trace 可重放且 source 未变。
- [公开 workflow 40203191203](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40203191203)
  已通过 v5 的 `case_count=passed=initial_failed=treatment_recovered=8` 硬门；八个固定 commit 的
  HEAD/文件数/clean 状态、禁网 Docker 验收、Trace replay 与 source isolation 均通过。统一 artifact
  ID `12602543425`、166,866,560 bytes、digest
  `sha256:319ad06ae83d92fb1f921cf49dab71f019987d1bab75533306d334c89ef4c330`；doctor 的 source-bound
  case count 与公开 Docker verified count 现均为 8。

## 2026-10-25 完整 Checkout v4：新增 HTTPie

- `bugsinpy-full-checkout-pilot-v4.yaml` 原样保留 v3 五例，再加入 BugsInPy HTTPie-2 的精确
  `356e0436510fee70b4071fac58be81c0a0a7db59` checkout（70 tracked files）；suite digest 为
  `17d12ba657bde272251783d3364b4f809691fa71a5ae55c8d6dce98947b1d99c`，case digest 为
  `6f01c4403356930507d9a2188cf3c33c940bf6a9f0fe992d0a08fd5b80796920`。
- 保护检查从真实 `httpie/client.py` AST 提取并执行 `get_response`：buggy 版本没有把
  `args.max_redirects` 传到 Session，初始检查按预期失败。Baseline 进入 `WAITING_FOR_USER`；
  Treatment 一次 replan 后修改生产行并进入 `SUCCEEDED`。report ref
  `dbe16c62fcb95a9b730279961199b7271092dcd0edf2df84534c750f14ae19e9`；两条 Trace 可重放、源目录
  不变、无网络或付费模型调用。
- 首次 Windows 本地六案例联合执行为 4/6，未隐藏：新 clone 的 Tornado 与 HTTPie 使用 CRLF，
  两个 LF 多行 exact anchor 无法匹配。HTTPie 改为唯一单行 anchor，新增 LF/CRLF 合同并单案复跑
  通过；冻结 v3 Tornado manifest 未被改写，故本地不能宣称六案例统一 6/6。
- [公开 workflow 40200117233](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40200117233)
  已通过 v4 的 `case_count=passed=initial_failed=treatment_recovered=6` 硬门；六个固定 commit 的
  HEAD/文件数/clean 状态、禁网 Docker 验收、Trace replay 与 source isolation 均通过。统一 artifact
  ID `12602031276`、157,416,541 bytes、digest
  `sha256:a70612aab310c6d6c09c9ea0796242da75877fe025b10a9c72d76a525bd3c0e8`；doctor 的 source-bound
  case count 与公开 Docker verified count 现均为 6。

## 当前交付范围

已从可靠性内核推进到一个可执行的顺序多 WorkItem Agent Loop。现有 79 项要求全部保留，
未完成项没有被删除或变成可选项。不自动运行完整三角色，也不补齐旧 meta；普通开发
由当前 Agent 执行并自检，关键风险与重要结论仍须按需独立复核。

| 已实现部分 | 实际文件 | 本轮验证边界 |
|---|---|---|
| TaskSpec、权限模式、严格预算字段、不可变合同 | [task.py](../src/horizon/domain/task.py) | `execution_mode` 给出工具上界，可选 `constraints.allowed_tools` 再做任务级最小权限，WorkItem 只能继续收窄；字段省略时保持历史序列化/hash；未验证真实仓库来源 |
| 工作项 DAG、required 验收覆盖、候选调度 | [plan.py](../src/horizon/domain/plan.py) | 人工、one-shot 模型计划和单次执行期 revision 都经过领域层验收/工具权限复核；新计划还要求每个 acceptance ID 只有一个 owning WorkItem，历史 Trace 投影保留旧合同兼容；无自动触发或多次 replan |
| 一次性自动计划与恢复 | [planning.py](../src/horizon/application/planning.py)、[planning.py](../src/horizon/domain/planning.py) | 内容寻址 PlanningContext、1～8 项 DAG、任务级工具 allowlist、权限/验收唯一归属校验、费用记账、settled response 复用；完整 inventory 下拒绝模型猜测的不存在路径，只对同项已授权 `create_file` 且仍在 TaskSpec 范围内的新路径放行，截断 inventory 保持未知；仍无计划成功率评测 |
| 保守意图 admission | [intake.py](../src/horizon/domain/intake.py)、[intake.py](../src/horizon/application/intake.py) | 显式控制→结构化 TaskSpec→有限规则；输出请求/任务种类、请求/有效模式、目标 Run、缺失字段、原文 span、工具交集与零模型费用。权限只收窄，未知及 read/write 冲突 abstain；尚无模型分类、intake 持久事件或自动派发 |
| Provider retry/breaker/fallback 合同 | [retry.py](../src/horizon/reliability/retry.py)、[breaker.py](../src/horizon/reliability/breaker.py)、[run_reliability.py](../src/horizon/domain/run_reliability.py)、[fallback.py](../src/horizon/reliability/fallback.py)、[设计与边界](provider-reliability.md) | probe/planning/execution 在 reservation 前使用跨重启 SQLite breaker；每次 transport 只派发一次，unknown 费用不自动重试。fallback 仅做最多一个候选的预授权/能力/数据/预算/验收不变校验；typed summary 已并入终态 EvidencePack，生产 retrieval degradation 已与工具 receipt 原子入 Trace。仍无 Provider receipt 查询、安全自动 retry、实际换模型或 model fallback history |
| 计划阶段持久人工 fallback | [human.py](../src/horizon/domain/human.py)、[services.py](../src/horizon/application/services.py) | 非法模型 Plan 绑定原 response/Task/version 后进入 WAITING 并释放 Lease；本机人工计划原子记录决定并回到 READY；仅此窄场景，不是通用 HITL |
| NoProgress 人工指导恢复 | [human.py](../src/horizon/application/human.py)、[human.py](../src/horizon/domain/human.py) | 相同行为第 4 次、period-2 第 6 步或 period-3/4 第 3p 步，将 pattern、精确周期、工具证据、Task/Plan/WorkItem/workspace/session 绑定后进入 WAITING；本机指导写入新 session，新 Worker 续跑；不是通用审批或自动 replan |
| Run 状态投影、终态和成功前置检查 | [run.py](../src/horizon/domain/run.py) | 状态规则；顺序 WorkItem DAG、单次 vN→vN+1 revision 和最终 required checks 全量回归已接入；无并行或多次 replan |
| 追加事件、事务、幂等回执、历史合同、重放 | [sqlite.py](../src/horizon/adapters/persistence/sqlite.py) | SQLite 重启、投影损坏/删除、并发写、真实进程退出 |
| Lease/epoch、取消、合同修订、预算账本 | [services.py](../src/horizon/application/services.py)、[恢复安全取消](recovery-safe-cancellation.md) | 旧 Worker 拒绝写入；无在途 effect 时直接取消，在途 effect 先写持久派发栅栏，迟到 receipt/unknown/工具处置闭合后再终态；Run 内模型/工具 intent、receipt、unknown 与硬预算事件化 |
| wall-clock 到期与迟到回执 | [services.py](../src/horizon/application/services.py)、[agent_loop.py](../src/horizon/application/agent_loop.py)、[语义说明](budget-stop-semantics.md#21-wall-clock-到期边界) | 到期后禁止新派发并在安全边界记录 `FAILED / wall_clock_limit`；已派发调用可先结算/隔离，恢复 Lease 不扩展执行权限；待 HITL 的工具副作用保留非终态。当前无后台定时器，需由控制命令或 slice 边界观察到期 |
| 不可变内容寻址文件产物与新目录恢复 | [artifacts.py](../src/horizon/adapters/persistence/artifacts.py)、[snapshot.py](../src/horizon/adapters/workspace/snapshot.py) | 文本/二进制文件；同进程已完整验证且元数据未变化的 CAS 去重命中不重复读 blob，变化或重启后重新验 hash；不恢复 Git 对象、进程或网络状态 |
| 检查点产物引用与事件原子提交 | [checkpoints.py](../src/horizon/application/checkpoints.py) | 产物校验、游标 CAS、拒绝未结算动作；正常轮次的调度静止由本地顺序 Supervisor 校验，异常退出仍须 recovery 围栏 |
| 临时副本 Docker 执行与受保护验收 | [docker.py](../src/horizon/adapters/sandbox/docker.py)、[validation.py](../src/horizon/adapters/sandbox/validation.py) | 非 root、禁网、只读根、超时、输出有界；模型只能选择控制器冻结的 check ID |
| Typed Tool Gateway | [gateway.py](../src/horizon/tools/gateway.py) | 搜索、读取、单文件精确替换、最多 8 文件的结构化精确 patch、单个最多 64 KiB 的 UTF-8 新文件、受保护检查和 submit；路径/链接/大小受限，每次 intent/receipt 持久化；existing target 不覆盖、父目录不自动创建；exact search 饱和时显式标记截断并引导 ranked retrieval；`retrieve_code` 默认只返回 rank 1，需要时才显式扩大；大文件整读拒绝并要求最多 400 行、32 Ki 字符的范围读取 |
| 顺序多 WorkItem Agent Loop | [agent_loop.py](../src/horizon/application/agent_loop.py) | dependency-ready 调度、逐项会话/权限/验收、原子交接、最终全量回归、有限 repair 与单次受限 replan；不支持并行或自动/多次 replan |
| 本地顺序 Supervisor | [supervision.py](../src/horizon/application/supervision.py)、[设计与边界](local-sequential-supervisor.md) | `--supervise` 在新 AgentSession 已持久化后释放 Lease并重开完整 Worker 栈；可信父进程同步回收子进程后可绑定精确 Lease/epoch 接管。应用层还支持一个直接子 Worker 的 fence-before-stop；pending intent 原样进入 recovery。正式进程模式的父控制器硬退出由持久 launch + 精确 PID 人工停止确认安全接管。当前不是 CLI 常驻进程管理器、进程树控制、daemon 或分布式队列 |
| 本地子进程 Supervisor | [local_process_supervision.py](../src/horizon/application/local_process_supervision.py)、[_local_agent_worker.py](../src/horizon/application/_local_agent_worker.py)、[设计与边界](local-process-supervisor.md)、[父崩溃恢复](parent-controller-crash-recovery.md) | `agent run/resume --worker-process` 让每个 slice 由真实直接子进程执行；父进程先持久化 launch ID/PID 与启动授权再放行，正常同步 reap 后按精确 Run/Lease/epoch/session/event cursor 通过 RecoveryService 门禁。父进程 hard kill 后活动 launch 持续阻止接管，TTL 后仍须匹配 PID 停止确认；`Ctrl+C` 与 slice timeout 复用取消栅栏。pending intent 原样阻塞、无 retry/fallback；不是 daemon、自动探活/重启、进程树、OS 灾难恢复或跨主机恢复 |
| 直接子 Worker timeout 一键 EvidencePack | [process_timeout_demo.py](../src/horizon/application/process_timeout_demo.py)、[_process_timeout_demo_worker.py](../src/horizon/application/_process_timeout_demo_worker.py)、[领域合同](../src/horizon/domain/process_timeout_demo.py)、[演示合同](process-timeout-cancellation-demo.md)、[面试讲解](interview-demo.md) | `demo process-timeout` 启动一个真实直接子进程，ready marker 绑定 launch ID/PID/Lease/epoch/launch event hash；根包验证 process launch→start authorization→cancel fence→synchronous reap→terminal cancel，并递归重放通用终态子包。标准 CI 发布保留 14 天的 artifact。无模型/工具/网络/Docker/仓库代码调用；只覆盖一个父侧 timeout，不等同于父/OS 自动恢复、进程树、跨主机或 exactly-once 验收 |
| 执行证据驱动的受限 Replan | [plan.py](../src/horizon/domain/plan.py)、[services.py](../src/horizon/application/services.py)、[agent_loop.py](../src/horizon/application/agent_loop.py) | 模型显式 `revise_plan`，最多 1 次成功；大型动态 Schema 只在当前 WorkItem 已积累至少两轮执行证据或已有通过项后暴露，并用持久会话元数据保持崩溃恢复请求稳定；完成项逐字段不可变，工具 receipt、Plan vN+1、新 session 同事务；未做真实模型效果评测 |
| 确定性 ContextProjection + MandatoryFactLedger | [context.py](../src/horizon/application/context.py)、[context.py](../src/horizon/domain/context.py) | 完整 transcript 留存；候选投影同时满足字符上限与覆盖工具 Schema 的完整请求保守 token 上界，执行期再按 Run/Campaign/单调用最小 CNY 余量收窄 effective cap，近期完整单元在任一硬上限需要时只折叠最少数量，incomplete 单元绝不折叠；新 Projection/Reservation 使用 Adapter 实际 OpenAI-compatible body 字节，绑定版本化 estimator、payload hash/字段分量、上界与 effective cap，并按 estimator ID 恢复 v1；Task/Plan/权限/验收/预算/策略/工具 Schema/workspace 另做内容寻址绑定；不是精确 tokenizer、语义压缩或 Project Memory |
| 证据驱动的 Run Memory | [memory.py](../src/horizon/application/memory.py)、[memory.py](../src/horizon/domain/memory.py) | 从工具事件和内容寻址输出派生；保留失败/unknown，按 workspace revision 失效并绑定模型请求恢复边界；仅 run scope，不是 Project Memory |
| 精确模式无进展保护 | [agent_loop.py](../src/horizon/application/agent_loop.py) | 同一 revision 下，相同精确动作第 3 次、period-2 第 5 步、period-3/4 第 3p-1 步软阻断；继续模式分别在第 4/6/3p 步进入可恢复人工等待；最小周期校验防止短周期重复冒充长周期；不是语义或任意周期检测 |
| Revision-aware 词法 Code RAG | [retrieval.py](../src/horizon/domain/retrieval.py)、[sqlite_fts.py](../src/horizon/adapters/retrieval/sqlite_fts.py)、[retrieval_eval.py](../src/horizon/application/retrieval_eval.py)、[retrieval_sources.py](../src/horizon/application/retrieval_sources.py) | SQLite FTS5/BM25、有界 EvidencePack、权限/revision/hash 回查、显式 scan fallback；内部/命名变体/同名符号与两批外部小样本均保留成功和负结果。新增 7 项目 24 案例冻结集：16 个 development 的 Hit@1=11/16、Hit@5=16/16、Recall@5=18/20、MRR=0.817708，3 个 degraded，另 8 个 holdout 仍密封；development 源码现可从固定公开 URL/commit/package 一键重建并核对文件数、字节数、内容树 digest，冻结排名可排除机器耗时后逐项验真。已知困难子集的 16 个冻结查询扰动显示英文改写 Recall@8=8/8、中文描述仅 1/8，保留为多语言负结果。七轮真实模型 Trace 的 10 次 `retrieve_code` 中，6 次可由 canonical 参数哈希匹配到 3 种公开英文 query，保守候选 6/6 原样旁路；另外 4 次原文和语言保持 `UNKNOWN`。无 AST/向量/symbol 图、真实查询分布或模型修复收益结论 |
| 冻结控制器策略评测 | [reliability.py](../src/horizon/domain/reliability.py)、[reliability_eval.py](../src/horizon/application/reliability_eval.py) | 14 案例/62 判断覆盖精确 period-1/2/3/4 NoProgress 与单次 replan 接受/拒绝；生产路径共享判定函数、报告内容寻址、零外部调用；不是模型或真实 Issue 效果评测 |
| 有界恢复矩阵 v3 | [recovery_evaluation.py](../src/horizon/domain/recovery_evaluation.py)、[recovery_eval.py](../src/horizon/application/recovery_eval.py)、[horizon-recovery-matrix-v3.yaml](../benchmarks/recovery/horizon-recovery-matrix-v3.yaml)、[recovery-matrix-evaluation.md](recovery-matrix-evaluation.md) | 保留 v1/v2 digest；新增 promotion effect→receipt 前真实硬退出，与既有写入/模型响应硬退出及 18 个三工具状态/决策组合合计 21/21。自动恢复 11/11、安全阻塞 10/10、incorrect resume/redispatch/重复副作用均 0；三条 Trace replay。仍非完整设计故障矩阵、主机故障或 exactly-once 验收 |
| 完整 Run A/B 与来源绑定 Suite | [run_evaluation.py](../src/horizon/domain/run_evaluation.py)、[run_ab_eval.py](../src/horizon/application/run_ab_eval.py)、[scripted.py](../src/horizon/adapters/model/scripted.py) | 同一 Task/Plan/workspace 的 baseline 与单次 replan arm；先真实确认初始失败，再检查 EventLog/预算/Gateway/Trace；1 个内部、v2 的 5 个 BugsInPy 依赖裁剪案例，以及覆盖 9 个项目的 15 个完整 checkout 案例。依赖裁剪 v2 与完整 checkout v8 已由公开禁网 Docker 分别通过 5/5 与 15/15。完整案例还验证干净 Git HEAD 与 Code RAG，仍是脚本模型 |
| 完整 checkout 多阶段/重启 A/B | [bugsinpy-multi-stage-pilot-v4.yaml](../benchmarks/run_ab/bugsinpy-multi-stage-pilot-v4.yaml) | youtube-dl 872 files、Luigi 382 files 与 FastAPI 629 files 上各有三个依赖 WorkItem；六条 arm 均跨两个持久边界到 epoch 3，合计 12 次重启，并覆盖跨 revision RAG、active/stale Run Memory、完成项保留 replan、最终三项 required checks 与 Trace replay。本地可信与公开禁网 Docker 均为 3/3，doctor 的 case/Docker 计数均为 3。v1 的 CRLF、60 秒 Lease 以及本轮 Tornado 歧义替换负结果继续保留 |
| 源码绑定的写入硬崩溃 A/B | [bugsinpy-multi-stage-hard-crash-v1.yaml](../benchmarks/run_ab/bugsinpy-multi-stage-hard-crash-v1.yaml)、[设计与证据](source-bound-hard-crash-recovery.md) | Luigi 382-file checkout 的两条 arm 都由真实子进程在第一个生产写入 effect 后、receipt 前退出；父进程先记录 unknown、围栏旧 lease，再按精确后态 `accept_replace`，不重派模型，并继续两个 WorkItem 边界到 epoch 4。本地可信与公开禁网 Docker 均为 1/1、Trace replay、source unchanged、unknown/open 0；doctor 的 Docker 计数为 1 |
| 公开仓库长程可靠性指标 | [run_evaluation.py](../src/horizon/domain/run_evaluation.py)、[run_ab_eval.py](../src/horizon/application/run_ab_eval.py)、[指标合同](long-horizon-reliability-metrics.md) | 新生成的每个 case 记录单调时钟观测耗时；suite typed aggregate 直接给出 scenario/arm、P95、Worker 重启、硬崩溃恢复率、恢复重派、重复副作用、Trace replay、source isolation 与最终 open effect。旧报告仍可读取，冻结 manifest digest 不变；公开 Docker workflow 将多阶段与硬崩溃关键指标设为失败门槛并写入 Job Summary。当前 suite 共 24 个 manifest 场景，现均有公开统一验证。场景含重复 bug，仍无 20～30 个独立真实任务或真实模型成功率证据 |
| 真实模型 Pilot | [pilot.py](../src/horizon/domain/pilot.py)、[pilot.py](../src/horizon/application/pilot.py)、[real-model-pilot.md](real-model-pilot.md) | 离线预检绑定完整干净 checkout、初始失败、source snapshot、Docker image、Provider policy、Harness 源码指纹、费用 cap 和首次规划保守预留。七个付费 Run 均无编辑/验证、Trace 可重放且 source 未变；第七轮 Planner 直接定位真实 `__init__.py`，execution 再次取得 rank-1 证据，但消费回执前因 Campaign 预留不足停止。当前实际累计为 `CNY 0.2064492`，没有自动复跑或真实 Issue 成功 |
| 模型预留压力诊断 | [reservation_analysis.py](../src/horizon/application/reservation_analysis.py)、[reservation-diagnostics.md](reservation-diagnostics.md) | `trace reservation-report` 在 replay/hash-chain 校验后关联 reservation、settlement 和新 BudgetStop 请求尺寸；七轮 22 次调用的聚合预留/结算比为 6.860516。候选 `request_bytes + 1024` 可回放 5 次，并按旧领域 JSON / 当前 wire basis 分层；当前口径仅 2 次、0 次观测低估、中位比 4.230001。生产 estimator、费用门禁和付费预算均未改变 |
| 安全轮次续跑 | [agent.py](../src/horizon/domain/agent.py)、[agent_loop.py](../src/horizon/application/agent_loop.py) | 消息 Artifact + event/revision 绑定；新 Worker 续跑；PLANNING/READY 也可恢复，任意崩溃窗口对账未完成 |
| 悬空调用恢复与对账 | [recovery.py](../src/horizon/application/recovery.py)、[model_recovery.py](../src/horizon/application/model_recovery.py)、[tool_recovery.py](../src/horizon/application/tool_recovery.py) | Campaign-only 预留释放；response Artifact 跨 Worker 续跑；模型派发前持久化 client Trace ID，返回后普通落盘失败立即隔离，Artifact 前硬退出重启后保守 `unknown`；只读重试；精确 `replace_text` / `apply_patch` / 单文件 `create_file` accept/rollback；Docker `run_check` 自然退出且 request/workspace/隔离配置/完整小日志精确匹配时可恢复 success/error；created attempt 可在状态二次核验后删除并重取 revision，stopped/running 仍走原窄路径，missing 仍需人工确认；部分/漂移写入、transitional、信号/超时/OOM 验证仍阻塞 |
| Docker `created` 窗口一键 EvidencePack | [docker_created_recovery.py](../src/horizon/application/docker_created_recovery.py)、[_docker_created_crash_worker.py](../src/horizon/application/_docker_created_crash_worker.py)、[领域合同](../src/horizon/domain/docker_created_recovery.py)、[文档](docker-created-recovery-evidence.md) | Docker effect-executor 子进程在生产 `docker create` 完成、`docker start` 调用前真实 `os._exit(34)`；Lease-owning 控制器存活，先保守标 unknown，再调用生产 `agent resolve-tool` 核验 `created`、删除精确 attempt、复核 revision、持久化 `cancelled / discard_check` receipt。7 文件包可无 Docker/数据库离线重放并拒绝重哈希语义篡改；未证明 Agent Worker 自身崩溃，不是任意崩溃或 exactly-once |
| Docker `created` 后续跑到终态 | [docker_created_recovery.py](../src/horizon/application/docker_created_recovery.py)、[领域合同](../src/horizon/domain/docker_created_recovery.py)、[文档](docker-created-resume-to-terminal.md) | `horizon demo docker-created-resume` 先冻结上述 `RUNNING / safe_to_resume` 子包，再由 epoch 3 新 Worker 从恢复 session 继续；dispatch 前拒绝 Docker image ID 漂移，旧 `run_check` 不重派，新 call ID 经生产 Agent Loop 执行 protected validation 并进入 `SUCCEEDED`。根包递归验证两份子包、Trace 字节前缀、resume observation、最终 manifest/check Artifact/marker 及零 unknown/open effect；本地与[公开 Linux Docker workflow](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/39803245157)均通过。使用 Scripted Model 和合成 marker，不是 Agent Worker 崩溃自动接管或实际模型修复效果 |
| Lease-owning Agent Worker 的 Docker `created` 崩溃恢复 | [docker_worker_crash_recovery.py](../src/horizon/application/docker_worker_crash_recovery.py)、[_docker_agent_worker.py](../src/horizon/application/_docker_agent_worker.py)、[领域合同](../src/horizon/domain/docker_created_recovery.py)、[文档](docker-worker-crash-resume.md) | 父控制器先 `fsync` PID/Lease/epoch/session/image launch record 再放行直接子进程；epoch 1 子 Worker 运行生产 `CodingAgentRunner`，持久化 model receipt 与 `run_check` intent，并在 Docker `create` 后、`start` 前真实 `os._exit(34)`。父进程同步回收后，由既有 Supervisor 以 `confirmed_reaped_worker_expired` 事件绑定同一 PID、退出码、旧 Lease 和 launch sequence，epoch 2 保留 pending intent；生产 recovery 标 unknown、核验并删除精确 `created` attempt、复核 revision、写入单一 `discard_check` receipt，epoch 3 新子 Worker/new call ID 完成 protected validation 并进入 `SUCCEEDED`。九角色根包可无 Docker/数据库离线重放并拒绝重哈希 PID 叙事；本机与[公开 Linux Docker workflow](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/39809773689)均通过，统一 artifact digest `sha256:7d7aea85aa1ea8a4a33f1f3395bc847cf27ad41d862e9417f428b3be1eaf3120`。仅覆盖一个本地直接子进程和一个故障窗口，不是父控制器/OS 重启、进程树、跨主机、任意崩溃或 exactly-once |
| 受控候选提升 | [promotion.py](../src/horizon/application/promotion.py)、[promotion.py](../src/horizon/adapters/workspace/promotion.py)、[git.py](../src/horizon/adapters/vcs/git.py) | 只读 diff、源/候选 revision 与可选 Git HEAD 绑定、显式 1～8 个总变更且至多 1 个 64 KiB UTF-8 新文件、完整/部分 effect 崩溃恢复；不删除/重命名或创建 commit |
| 任务准备、计划、状态、取消、执行、导出与重放 CLI | [app.py](../src/horizon/interfaces/cli/app.py) | `run` 保持 prepare-only；`agent run` 接受 PLAN_PATH 或 `--auto-plan` 且只操作 staging；`agent run/resume --supervise` 可自动接力安全轮次，`--worker-process` 可改为真实子进程持有每个 slice；执行前 HorizonError 仅在无非 unknown 在途 reservation 时释放租约；promotion 需显式确认 |
| SiliconFlow 严格配置与 OpenAI-compatible adapter | [config.py](../src/horizon/adapters/model/config.py)、[openai_compatible.py](../src/horizon/adapters/model/openai_compatible.py) | Adapter 与预算器共用 canonical wire encoder，实际 HTTP body 的 bytes/hash 可复算；真实 Tool Calling 探针和历史 fixture Agent Run 通过；完整 checkout Pilot 已真实调用并以受控终态失败，无隐式 retry/fallback |
| CNY Campaign 与 Run 模型费用账本 | [campaign_budget.py](../src/horizon/adapters/persistence/campaign_budget.py)、[run.py](../src/horizon/domain/run.py)、[budget-stop-semantics.md](budget-stop-semantics.md) | Campaign 跨重启硬上限；Run 绑定 CNY policy 并事件化；执行请求先按三类最小余量确定性压缩可压缩历史，仍付不起则携带 reason/scope/required/available 及未派发请求 sizing evidence 原子进入 `FAILED` 并清除 Lease；unknown 用量仍保守对账；TaskSpec 旧 USD 字段尚未迁移 |
| 一键离线作品集 EvidencePack | [portfolio_demo.py](../src/horizon/application/portfolio_demo.py)、[_portfolio_crash_worker.py](../src/horizon/application/_portfolio_crash_worker.py)、[portfolio_demo.py](../src/horizon/domain/portfolio_demo.py)、[portfolio-demo.md](portfolio-demo.md) | `horizon demo run` 复用真实事件/Lease/会话/RAG/Gateway/验证主链路：epoch 1 完成错误回执交接，epoch 2 子进程在 `replace_text` effect 后、receipt 前 `os._exit(86)`，Supervisor 先标 unknown、围栏旧 Lease，再由 epoch 3 精确接纳既有 effect 且不重放写入。v3 报告锚定崩溃标记、恢复 disposition 和可从 Trace 复算的检索→写入 lineage；`demo verify-portfolio` 可在删除三个 SQLite 数据库后重放并复核，标准 CI 上传完整目录。兼容读取 v1/v2；零网络/零真实模型/零外部费用，不冒充任意故障窗口或模型能力证据 |
| 通用终态 EvidencePack | [terminal_evidence.py](../src/horizon/application/terminal_evidence.py)、[terminal_evidence.py](../src/horizon/domain/terminal_evidence.py)、[合同与边界](terminal-evidence-pack.md) | `trace bundle` 对成功、失败、取消统一导出 Trace、规范终态与 schema v2 可靠性摘要；`verify-bundle` 无数据库离线重放并复算。区分 unknown acceptance、已知 required-check failure 与 unknown/open effect；真实 retrieval degradation 从同事件工具回执进入 history，schema v1 继续兼容验证。model fallback history 尚空；这是自洽性清单，不是签名或来源认证 |
| 终态 Run 顺序汇总评测 | [terminal_suite.py](../src/horizon/application/terminal_suite.py)、[terminal_suite.py](../src/horizon/domain/terminal_suite.py)、[合同与边界](terminal-suite-evaluation.md) | schema v2 逐个复用已验证的通用 EvidencePack，保持原始成功率并分列 clean/quality-flagged success、验收未知/失败、quality flags、fallback/degradation、BudgetStop 与 unknown/open effect；`verify-terminal-suite` 无数据库递归重放并复算，兼容 v1。只做证据聚合，不执行任务、模型、工具、网络或仓库代码，不声称已有 batch scheduler |

2026-10-04 的零费用增量把原先仅用于费用预留的完整请求保守上界接入实际派发门禁。
`ContextProjection` 升为 schema v2；`InputTokenEstimate` 固定记录算法 ID、规范请求 UTF-8
字节数和上界，`InputTokenBudget` 再绑定本次 effective cap。执行投影会对每个候选重新构造包含工具
Schema 的请求，并与字符门共同决定是否继续折叠；自动规划和 probe 在联网前拒绝超限请求，
pilot preflight 的 `ready` 也包含同一门槛。费用 reservation 与上下文门复用同一个 estimate，
但 Provider 实际 usage 仍单独结算，两者没有混写。该增量没有调用真实模型、网络或产生费用。

2026-10-06 增加费用余量感知投影：每次执行调用取单 Run 余额、Campaign 余额和单调用上限的
最小值，先扣除完整输出预留，再在原保守估算器下确定 effective input cap。低余额下只压缩完整
历史单元；cap 不可表达或不可压缩时回到原 reservation gate 做派发或 BudgetStop 判定。cap 随
reservation 持久化，恢复 pending response 时复用原值，不按结算后的新余额改变请求 hash。
Scripted Model 集成测试覆盖
低余额成功闭环和 Campaign settlement 中断后的跨 Worker 无重派恢复；未联网、未调用付费模型。

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
  CNY 0.18 不可变 Campaign。已执行 Tool Calling 探针、受控 fixture Agent Run 和七个真实任务
  Pilot Run；七个 Pilot 都未修好 Issue，不构成 benchmark 成绩。

Docker 限制参数按[官方运行文档](https://docs.docker.com/reference/cli/docker/container/run/)
核对。mini-SWE-agent 的[原生工具与消息边界](https://mini-swe-agent.com/latest/advanced/v2_migration/)
仍是后续适配约束；不能以该文档代替本项目的实际联调。

## 验证结果

### 2026-10-25 历史真实模型 RAG 查询审计

- 七轮 Pilot 的 7 份 Trace 已由既有重放入口复核：272 个事件、22 个已结算模型调用、Trace
  SHA-256 与历史记录一致；全程不联网、不调用模型、不执行目标仓库代码。
- 10 次 `retrieve_code` reservation 中，6 次可将文档已公开 query 的 canonical 参数 digest 与
  Trace `arguments_hash` 精确匹配：`unescapeHTML` 4 次、`tenumerate` 1 次、
  `tenumerate definition` 1 次。保守查询策略对这 6 次均返回 `gate_bypass_v1`，没有改写。
- 另外 4 次在本轮可用 Trace 中只保留参数哈希，现有证据未恢复原文；结果文件将其原文、语言和
  门控结果保留为 `UNKNOWN`，没有用猜测补齐。完整合同和负结果见
  [RAG 查询审计结果](../benchmarks/retrieval/historical-model-query-audit-v1/result-v1.yaml)。
- 该回溯样本只有两个公开任务、同一 Provider/模型和三种已恢复 query，不能估计自然查询分布、
  CJK 误触发率或端到端收益。因此默认 runtime、词表与密封 holdout 均保持不变。
- 本轮离线全量回归为 **538 passed，11 skipped**，11 项 skip 均为需要显式本机镜像的 Docker
  合同；`ruff check src tests`、`ruff format --check src tests` 和离线 build 均通过。构建期间发现
  Hatch 默认仍会遍历已排除的本地 `.horizon` 证据树，遂只增加
  `skip-excluded-dirs = true`；sdist 从持续遍历恢复为约 3 秒完成，wheel 内容边界不变。

### 2026-10-24 主作品集 EvidencePack 独立复核与 CI 发布

- 新增 `horizon demo verify-portfolio <evidence-pack.json>`。它先验证四个交付文件的大小与
  SHA-256，再从 `trace.jsonl` 重建最终 Run，并复算检索 Artifact → 写入 ContextProjection →
  目标路径/旧文本/revision lineage、硬退出 marker、悬空 call ID 和单一恢复 receipt。
- verifier 不读取 `control.sqlite3`、`campaign.sqlite3` 或 `retrieval.sqlite3`，也不调用模型、网络、
  Docker 或存活 Worker；内容寻址 `artifacts/` 与 `hard-crash-marker.json` 是语义证据的一部分，
  因此交付单位是完整输出目录，不是孤立 JSON 文件。
- 标准 CI 在主作品集演示完成后显式删除三个数据库，再运行同一 verifier；成功后把完整目录上传为
  保留 14 天的 `horizon-portfolio-evidence`，并继续独立发布更窄的
  `horizon-process-timeout-evidence`。上传步骤缺失文件即失败，不把 CI 日志当作证据包替代品。
- CLI 主输出补充 final lease epoch、事件数、evidence-backed write 与 Trace replay 结论；
  [3 分钟面试讲解](interview-demo.md)改为以“检索→结构化错误→硬崩溃→精确接纳既有 effect→保护性
  验收”为主线，三项目 3/3 证据只作为公开评测边界，不冒充真实模型成功率。

### 2026-10-24 完整 Checkout Suite v3：五项目

- `bugsinpy-full-checkout-pilot-v3.yaml` 原样保留 v2 的 tqdm、youtube-dl、Luigi 三例，再追加固定
  BugsInPy buggy commit 的 FastAPI 629-file 与 Tornado 301-file checkout；五项目合计 2,266 个
  tracked 文件，suite digest 为
  `d878964e246f8fabb6cf162f657791e959ae6c529d61eb1143cc98ea7d4edd8a`。
- 新增两例先验证 buggy checkout 失败、fixed checkout 通过。test-only 本地 Harness 执行中两例
  Baseline 均进入 `WAITING_FOR_USER`，Treatment 均生成 Plan v2 并进入 `SUCCEEDED`；FastAPI
  Baseline/Treatment 分别为 48/74 个事件，Tornado 为 48/92 个事件，Trace 可重放且 source 未变。
- 公开 Linux [Source-bound Docker evidence](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40080364921)
  使用生产 Docker adapter、禁网/非 root/只读根/drop capabilities/资源上限完成 v3 5/5；不读取
  API Key、不调用付费模型。[标准 CI](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40080364883)
  同时通过；统一 artifact digest 为
  `sha256:f3698c74ef95dede7b2f51109c08b006d6e242640947045d1c49bee37faae80c`，大小
  131,547,546 bytes，计划保留至 2026-11-23。
- 原候选 Cookiecutter 的七个 tracked 文档符号链接在 Windows 被物化为普通文件，在 Linux 保持
  符号链接并被快照安全门拒绝。最终 v3 不计入该候选；没有放宽 link/junction 拒绝规则，也没有
  排除文件后冒充完整 checkout。工作流同时新增有界 stderr 注解，使这类公开失败可直接诊断。
- 当前 14 个执行场景含重复上游 bug；新增的是 Harness 可靠性和任务多样性证据，不是模型自主
  修复成功率、官方 BugsInPy/SWE-bench 分数或可外推的泛化率。

### 2026-10-24 三阶段完整 Checkout 恢复 A/B v4：FastAPI

- `bugsinpy-multi-stage-pilot-v4.yaml` 原样保留 v3 的 youtube-dl 与 Luigi 前缀及 digest，再追加
  FastAPI-5；suite digest 为
  `7541ec23c1a1d4a0ab6e052ae84cd256c756ecff6b5bb108f9ae086fda43cf17`，新 case digest 为
  `8ac330a5e7ec07053163695d05572e4c849155297743e742963750e666cb0fe1`。
- 三个依赖 WorkItem 依次完成 `fastapi/utils.py` 一行递归 clone、`create_file` 写入上游 91 行回归
  文件、联合关闭生产与回归合同。新文件为 2,429 bytes，SHA-256
  `f64fb11b78917f494f3101424f30219be3d8e761bff6da437f9ccd586b372c50`，与固定上游内容一致。
- 本地可信 Baseline `run_f912b31acae44763bdaad671564a8f0c` 完成前两项后进入
  `WAITING_FOR_USER`，Plan v1、10/12/12 model/tool/step、124 events；Treatment
  `run_cd272212fed04ccd8067fa5202293f3b` 保留前两项并仅修订末项后 `SUCCEEDED`，Plan v2、
  12/17/17 model/tool/step、158 events。两条 arm 都重启 2 次并到达 epoch 3，Trace ref 分别为
  `47da61cccdd0effb3855794b304850d9a738f4c0beece9b06d27b7f45870572f` 与
  `3059f06f8d273558fd2a7a237b488f178572d83973ebfd8d0b4c8404d96f297d`。
- 三个初始检查全部失败、Treatment 最终三个检查全部通过；两条 Trace 重放一致，unknown model/tool
  和开放 reservation 为 0，source checkout 保持 clean，`paid_model_called=false`、
  `network_called=false`。
- 提交 `3bd7394` 的[标准 CI](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40082814607)
  与[公开禁网 Docker](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40082814779)
  均成功；多阶段 v4 为 3/3、6 条 arm Trace、12 次总重启。统一 artifact ID `12556305483`，
  大小 155,736,676 bytes，digest
  `sha256:832ee064281dfcf954cb4840b60bcfdb24d817ab02d4a4716ce8f0dea8e2cb6a`，计划保留至
  2026-11-23。
- Tornado 多阶段候选保留为负结果：多行 exact replacement 受 CRLF 影响，缩为单行后同一锚点出现
  两次，无法在现有合同下无歧义选中 handler。没有为单个 benchmark 引入“第 N 次 occurrence”
  工具语义；改用 FastAPI，避免为扩样本过度工程化。

### 2026-10-23 父控制器硬退出后的持久 Process Worker 恢复

- 每个 `--worker-process` 子进程新增 `launch_id` 和 stdin 启动闸门。父进程必须先提交
  `PROCESS_WORKER_LAUNCHED`（精确 PID/Lease/Worker/epoch/launch cursor），再提交
  `PROCESS_WORKER_START_AUTHORIZED`，最后才把同一 launch ID 写入管道；生产 child 在闸门前不打开
  Store、模型、工具或 workspace，闸门后再次读取 SQLite 复核授权。
- Run 投影保存活动 `process_worker_launch` 与历史停止 receipts。正常 `communicate/wait`、timeout
  或取消均先写 `PROCESS_WORKER_STOPPED(synchronous_reap)`；父控制器崩溃后，活动 launch 即使 Lease
  已过期也继续阻止新 Worker，直到操作者同时提供停止确认和完全匹配的 PID。缺失或错误 PID 不写
  事件；终态包把未清除的 launch 计为 open effect。
- 新增真实故障注入：先保存一个 AgentSession，再由独立父控制器放行等待子进程；测试硬 kill
  父进程后确认 child 仍活、模型请求数不变，随后停止精确 PID。无 PID和错 PID均拒绝且 Trace
  长度不变，正确 PID 使 epoch `1→2`，RecoveryService 接受旧 session，剩余两轮完成
  `replace_text -> submit` 并到达 `SUCCEEDED`；最初的 `read_file` 只有一个 receipt，JSONL 重放一致。
- `agent resume` 增加 `--confirm-old-worker-pid`；`agent reconcile` 与 `agent resolve-tool` 的相同参数
  同时接受 cancellation target 或活动 process launch 的 PID。推荐先用离线 reconcile，再在新的
  `--confirm-paid` 授权下 resume。本批未联网、未运行真实模型或 Docker，费用为 0。
- 没有引入 daemon、自动重启、进程扫描或 liveness probe。当前只覆盖一个本地直接子进程和父
  控制器 hard exit；OS 掉电/内核恢复、PID reuse 自动判定、进程树、跨主机和 exactly-once 仍未覆盖。
  详细合同见 [父控制器硬退出恢复](parent-controller-crash-recovery.md)。

### 2026-10-23 直接子 Worker timeout 一键证据

- 新增 `horizon demo process-timeout`。父进程创建一个没有模型/工具调用的 `RUNNING` Run，
  启动真实直接子进程；子进程以只读连接复核精确 Run/Lease/Worker/epoch/launch event，`fsync`
  并原子发布 PID marker 后保持空闲，父侧随后触发有界 slice timeout。
- timeout 复用生产 `LocalProcessAgentSupervisor` 和 `cancel_and_reap_worker`：Trace 中严格保存
  `run launch < process launch < start authorization < CANCEL_WORKER_STOP_PENDING <
  PROCESS_WORKER_STOPPED < CANCEL_WORKER_STOPPED < LEASE_RELEASED < CANCEL_REQUESTED`。只有同步
  `wait` 返回并记录 exit code/stop method 后才释放 Lease、进入 `CANCELLED`。
- 根 EvidencePack 绑定 child-ready、parent-supervision、确定性摘要和通用终态子包。
  `verify-process-timeout` 无需 SQLite、活进程、模型或 Docker，递归核验文件 hash、重放 Trace、
  复算最终投影和 PID/Lease/epoch/event 语义；修改 observation PID 并同步重算根文件 hash 的
  负例仍被拒绝。
- 新增 3 项 Windows 离线回归，并把实际演示与独立脱库验证接入公共 Linux CI。此结果只覆盖
  一个本地直接子进程的父侧 timeout 与同步 reap，不扩展为父/OS 崩溃恢复、进程树、daemon、
  跨主机、任意 effect 自动恢复或 exactly-once 结论。本批未联网、未调用模型或 Docker，费用为 0。
- 标准 CI 在上述验证通过后，把根目录发布为保留 14 天的
  `horizon-process-timeout-evidence` workflow artifact；缺失文件会让 upload step 失败。新增的
  [3 分钟面试演示与架构讲解](interview-demo.md)只整理现有架构、命令和声明边界，没有新增运行时
  组件、后台服务或付费调用。artifact 通常需要登录 GitHub 下载，不冒充匿名永久发布资产。

### 2026-10-23 正式 CLI 本地子进程 Supervisor

- 新增 `LocalProcessAgentSupervisor` 与生产 `_local_agent_worker`；`agent run`、`agent resume`
  的 `--worker-process` 隐含 supervision。父进程不执行 Agent slice，只持有并同步回收直接子进程，
  然后把 PID/退出码与启动时的 Run/Lease/epoch/AgentSession/event seq 交给既有恢复门禁。
- 子进程从 SQLite、Artifact、Campaign ledger、Snapshot、RAG、Docker Gateway 和 Provider 配置
  重建完整 Worker；API Key 不进入 argv。启动身份或 workspace/ledger 漂移时在派发前拒绝。
- 离线真实进程测试以三个不同 PID 完成 `read_file -> replace_text -> submit`，epoch 1→3、两个
  reaped release 后 `SUCCEEDED`，Trace replay 完全一致；父进程模型对象调用数为 0。另验证两
  slice 上限返回无 Lease `RUNNING`，以及 pending tool intent + exit 38 返回
  `reconciliation_required`、零 recovery/模型/下一 Worker 调用、intent 和 Lease 原样保留。
- CLI 接线测试确认无需同时写 `--supervise`，并输出 mode、disposition、每个 PID/exit code 与
  pending IDs。没有联网、没有付费模型调用、没有 retry/fallback，也未扩大历史 Campaign 预算。
- `Ctrl+C` 和新增 `--worker-timeout-seconds` 已接入既有 `cancel_and_reap_worker`：触碰子进程前
  先提交精确 PID/Lease/epoch 栅栏，随后 terminate/wait，必要时 kill/wait。真实等待子进程测试
  覆盖无 effect 时进入 `CANCELLED`，以及已有 `read_file` intent 时保留 intent、返回
  `reconciliation_required`、不启动下一 Worker；两条 Trace 均可重放。CLI 输出中断原因、停止
  方式和 timeout，未配合 `--worker-process` 的 timeout 在创建 Run/派发模型前拒绝。
- 当前仍只覆盖本地直接子进程和可处理的一次键盘中断/显式 timeout；父控制器 hard exit 已由
  上述持久 launch + 人工精确 PID 确认切片覆盖，但 OS 掉电/自动重启、进程树、跨主机、后台
  daemon/队列和任意故障窗口均未完成。详见
  [本地子进程 Supervisor](local-process-supervisor.md)。

### 2026-10-10 Docker created 检查恢复与删除后 revision 门禁

- `DockerSandbox.execute` 从单条 `docker run` 改为显式 `docker create` → 状态核验 →
  `docker start --attach`，保留原有禁网、只读根、资源限制、输出截断、超时和停止结果恢复合同。
  fault worker 在生产 create 完成、start 调用前直接 `os._exit(34)`；真实 Docker 观察到精确
  attempt 保持 `created`，写文件命令的 marker 不存在，删除后仍不存在。原来的 created 合同不再
  由测试代码手工拼装容器。
- Docker attempt 状态不再只看 `State.Running`。适配器同时读取 `State.Status`，只接受一致的
  `created + Running=false`、`running + Running=true`、`exited|dead + Running=false`；paused、
  restarting、removing 和不一致组合均拒绝自动分类。Docker 官方把
  [created 列为从未启动的状态](https://docs.docker.com/reference/cli/docker/container/ls/)，但 Trace
  只记录“控制器在处置时观察到 created”，不声称跨 Docker/SQLite 的原子事实。
- `agent resolve-tool --discard-check --image ...` 遇到精确 created attempt 时不发送 kill；删除入口
  会再次要求状态仍为 created。若两次观察间转为 running/stopped，处置失败并保留 unknown。
  删除或停止路径之后统一重新捕获 workspace manifest；revision 与处置前不一致时拒绝写 tool
  receipt，避免 check 在竞态窗口改动 staging 后仍被当成安全丢弃。
- 首轮本机 Docker 复跑为 **6 passed、1 failed**。失败不是本次 created 路径，而是既有停止结果
  恢复把 runtime mount 数固定为 1；`redis:7-alpine` 自带 `/data` 匿名卷，因此合法容器有两个
  runtime mounts。修复后只要求 HostConfig 中唯一显式 mount 是精确 `/workspace` bind，并允许与
  `Config.Volumes` 完全一致的镜像声明匿名卷；任何额外显式 bind/volume 仍拒绝。该负结果与修复
  均保留，不把首轮失败改写成通过。
- 新增/扩展离线用例覆盖 create/start 顺序与清理语义、created 删除、状态变化、三种 transitional
  状态、删除后 revision drift、镜像声明卷允许和额外 bind 拒绝；所有成功的 CLI 对账案例还会
  导出 JSONL 并要求 Trace 重放到同一最终投影。随后增加的一键 EvidencePack 把真实 exit 34、
  `created → missing`、marker 缺失、生产 CLI receipt、workspace manifest 与 Trace 绑定为 7 文件
  离线可复核包，并验证修改恢复叙述后即使重新计算文件 hash 仍会被语义校验拒绝。最终无 Docker
  主套件为 **442 passed**；9 项真实 Docker 合同使用本机已有 `python:3.12-alpine` 为
  **9 passed**。无 Docker 的全量入口对应 **442 passed，9 skipped**。现有公开 Source-bound Docker evidence
  workflow 也在拉取固定验证镜像后运行同一组合同，并把 JUnit/日志加入统一 artifact；Harness 本身
  未调用网络或真实模型，费用为 0。

### 2026-10-10 终态 Run 顺序汇总评测

- 新增严格 schema v1 清单：1～100 个有序 case，`case_id` 与 `run_id` 均唯一。写输出前先确认
  所有 Run 已终态且 SQLite 投影等于 Trace 重放，清单后部非法时不创建输出目录。
- `eval terminal-suite` 为每个 Run 生成独立通用 EvidencePack，再生成规范 manifest、结构化
  report、确定性摘要和顶层 SHA-256 清单。任务成功只统计 `SUCCEEDED`；失败、取消、
  `failure_reason`、结构化 BudgetStop reason 以及 unknown/open effect 分列保留。
- `eval verify-terminal-suite` 不读取控制数据库；它验证顶层文件和每个子包、重放每条 Trace，
  从领域状态复算 BudgetStop、全部计数、报告和摘要。修改摘要并同步重算顶层哈希仍被拒绝。
- 新增 4 项离线回归，覆盖四类代表样本（成功、预算停止失败、取消、带 unknown/open effect 的
  到期失败）、CLI 脱库验证、全量预检/唯一性与重哈希假摘要拒绝。汇总过程的 Run/模型/工具/
  网络/仓库代码执行标记均为 false；这只部分满足 NFR-010，不是任务 batch scheduler。

### 2026-10-10 通用终态 EvidencePack

- 新增 `trace bundle <run-id> --output <new-dir>`，只接受终态 Run，在创建目录前重放 SQLite
  EventLog并核对当前投影。导出 `trace.jsonl`、`final-run.json`、`SUMMARY.md` 和 schema v1
  `evidence-pack.json`，既有目标目录拒绝覆盖。
- 新增独立 `trace verify-bundle <pack>`；逐文件核对字节数/SHA-256，再离线重放事件 hash 链，
  复算终态元数据、规范 final state 和确定性摘要。摘要即使被篡改后同步重算文件哈希，仍会因
  与 Trace 语义不一致而拒绝。
- `SUCCEEDED` 仅在既有 validation/WorkItem 成功门禁后输出 `task_succeeded=true`；`FAILED` 和
  `CANCELLED` 固定为 false。一个 `wall_clock_limit` 失败样本中的 unknown/open Provider 预算
  占用被原样保留，没有为生成报告做结算或重新分类。
- 新增 5 项离线回归，覆盖三种终态、unknown/open effect、非终态拒绝、不覆盖和重哈希假摘要
  拒绝。当前累计全量为 **428 passed，7 skipped**；导出和验证均不调用模型、工具、Provider 或仓库代码。

### 2026-10-10 Supervisor 直接子 Worker 的恢复安全取消

- 新增 `cancel_and_reap_worker` 窄入口。它先校验精确 Run/Lease/Worker/epoch、启动事件游标和
  AgentSession，再提交 `CANCEL_WORKER_STOP_PENDING`；测试在真正调用 `terminate` 前从独立
  SQLite 读取确认栅栏已落盘。Lease 身份不匹配时连 `poll` 都不会调用。
- 取消目标本身成为终态与新 Lease blocker。父进程只终止传入的直接子进程句柄；先 bounded
  `terminate + wait`，超时才 `kill + wait`，已退出进程仍同步 reap。只有 wait 返回后才写
  `CANCEL_WORKER_STOPPED`、退出码、停止方式与 Lease 处置证据。
- 无 pending effect 的真实等待 Worker 被回收后进入 `CANCELLED`；带持久 `read_file` intent
  的真实 Worker 被回收后只释放旧 Lease并返回 `reconciliation_required`，普通 Worker 不能
  resume，显式 recovery settlement 后才终态，模型派发为 0。
- 处理了两个竞态：Worker 在取消栅栏后协作释放 Lease时，父进程仍能补同步 reap receipt；父
  Supervisor 在栅栏后崩溃时，不会把 TTL 当停止证明，旧 Lease 过期后必须通过既有
  `--confirm-old-worker-stopped` 并回填 `--confirm-old-worker-pid` 与持久取消目标精确匹配，才写入
  `operator_confirmed` receipt。无 effect 时 reconcile CLI 直接安全取消，有 effect 时保留恢复
  Lease；缺失或错误 PID 不改变 Trace。
- Windows 故障注入发现虚拟环境 `python.exe` 可能是 launcher，`Popen.pid` 与实际解释器 PID
  不同。测试改为直接基础解释器并传递受控 Python path，使用 PID marker 握手；产品合同明确
  要求调用方持有实际直接子进程句柄，不把 launcher PID 当 Worker 证明。
- 新增 8 项离线回归，覆盖真实 terminate/reap、真实 pending intent、kill fallback、提前退出、
  协作释放、父进程崩溃后的显式确认、错误 Lease 零进程操作和 CLI 终态分支。全量为
  **417 passed，7 skipped**；未联网、未调用真实模型、未产生费用，也未引入 daemon、队列、
  自动重试或模型 fallback。

### 2026-10-10 恢复安全取消与精确 check 终止

- 取消从“立即终态”改为两阶段事件协议。静止 Run 仍直接进入 `CANCELLED`；存在未分类 effect
  时追加 `CANCEL_PENDING` 并投影 `cancel_requested=true`。active gate 返回类型化
  `RunCancellationRequested(run_id)`，recovery gate 仍可提交既有 receipt 或 unknown 证据。
- 普通/模型 reservation 在结算或保守分类后自动追加 recovery-safe final cancel；unknown tool
  reservation 继续阻塞终态，直到既有 `ToolRecoveryService` 完成 accept/rollback/discard。
  Campaign unknown 先于 Run unknown 持久化，避免 Run 已取消而外部费用账本仍悬空。
- Agent 在每个多工具调用和每个 protected check 后重读持久状态。定向测试验证：模型 response
  在取消后仍先落 receipt、工具派发数为 0；两工具 response 在第一项触发取消后只派发 1 项；
  计划阶段的迟到 response 结算后不发布 Plan。
- `horizon cancel --image ... --stop-check-sandbox` 仅接受一个在途 `run_check`：先写取消栅栏，
  再用持久 call ID 核验并停止 owner/attempt/image 完全一致的标签容器，最后写入
  `CANCEL_SANDBOX_STOPPED`。它不拉镜像、不删除恢复证据、不终止任意宿主进程。
- 新增 8 项离线回归并重放新 Trace；全量为 **409 passed，7 skipped**。本轮没有联网、没有
  调用付费模型、没有重试或 fallback，也没有改变任何历史实验费用上限。

### 2026-10-08 wall-clock 安全终态与恢复边界

- 新增类型化 `RunDeadlineExceeded(run_id)`。所有走 active gate 的 Harness command 在事务拒绝后
  会重新读取持久状态：没有未分类外部效果或可恢复工具 intent 时，追加
  `RUN_FAILED(reason=wall_clock_limit)`；异常仍返回调用方，Trace 可解释为何没有执行原命令。
- receipt/unknown/session/validation evidence 写入、Lease 释放和 reaped-worker 围栏改用只校验
  非终态与精确 Lease 身份的 recovery gate。该 gate 不允许 reserve、transition 或任何新模型/
  工具派发；普通 Worker 即使拿到 recovery Lease 也不能越过 active deadline。
- `agent reconcile` 与 `agent resolve-tool` 可在 deadline 后取得专用 recovery Lease。前者保留仍可
  精确处置的 tool intent；后者 accept/rollback/discard 并清空 intent 后统一落到到期终态。
  Agent slice 若在模型回执之后才跨过 deadline，会先结算回执和保存会话，再在 yield 边界失败，
  不发起下一次工具/模型调用。CLI 回归以一个已过不可变 Run deadline 且旧 Lease 过期的 pending
  model intent 验证：无凭据、零网络对账先标 unknown，再输出并重放 `FAILED / wall_clock_limit`。
- 新增 3 个离线回归：静止 active command 自动终态、未分类 intent 拒绝提前终态且可在恢复
  Lease 下标 unknown、迟到回执先结算后终态；另有 Agent slice 级跨 deadline 用例验证模型
  receipt 已保存、工具调用为 0、最后事件为 `RUN_FAILED`；另验证 unknown tool intent 不会被到期
  终态遮蔽。全量结果为 **401 passed，7 skipped**，
  本轮未联网、未调用付费模型、未扩大任何费用上限。

### 2026-10-07 本地顺序 Supervisor 安全接力

- 新增 `SequentialAgentSupervisor`，一次只执行有界模型 iteration；只有 Run 仍为 `RUNNING`、
  无 reservation 且 `AgentSession.next_iteration` 严格前进时，才释放旧 Lease 并重开下一 Worker。
- 每个新 Worker 从 SQLite EventLog、ArtifactStore、SnapshotManager、Code RAG、Campaign Ledger
  和 Tool Gateway 重建状态，再取得新 lease epoch；不复用内存会话作为恢复依据。
- `agent run` 与 `agent resume` 新增 `--supervise`；默认每个 Worker 一个 iteration，
  `--slice-iterations` 可调整粒度。输出记录 slice/handoff 数和是否达到 Supervisor 上限。
- 显式 slice 上限会返回无 Lease 的可续跑 `RUNNING`；`WAITING_FOR_USER`、终态、预算停止和异常
  不会被越过。未预期异常仍保留原 Lease/intent 给 reconcile，不冒充安全恢复。
- 离线 Scripted Model 集成测试覆盖三 Worker、epoch 1→3、两次释放与重开、上限后再次继续；
  CLI E2E 覆盖 `agent run --supervise`，以及先显式让出再由 `agent resume --supervise` 以两个
  slice 和一次 handoff 接续成功，源目录未变化。未联网、未调用真实模型或产生费用。
- 新增父进程确认退出后的窄接管 API。启动边界绑定 Run/Lease/epoch/event seq/AgentSession；
  活 Lease 的安全路径以带 PID、退出码和游标的 `LEASE_RELEASED` 围栏；过期 Lease 必须仍精确
  匹配启动身份，才以带旧 token 和进程证据的 `LEASE_ACQUIRED` 提升 epoch。提升 epoch 只证明
  旧进程无法再写，不代表悬空 effect 安全。真实子进程在完整 `read_file` 轮次后退出的活 Lease/
  过期 Lease 两种用例均继续到 epoch 3、成功并可重放；pending tool intent 两种用例都返回
  `reconciliation_required` 且零重派，过期情形只提供新恢复 Lease。领域负例拒绝活 Lease 和
  错误旧 token 走过期入口；零 reservation 但尚无安全 AgentSession 的活/过期负例也都返回
  `unsafe_agent_boundary`。该能力仍是 Python application API，不是常驻子进程管理平台。

### 2026-10-07 预算估计证据按 byte basis 分层

- `trace reservation-report` 不再只给出混合 byte basis 的候选公式分布；新增确定性
  `by_request_byte_basis`，逐口径记录 settled 数、planning/execution 数、观测低估数、候选上界/
  Provider input 分布和 Provider input/request-byte 分布。旧 Trace 仍按原口径重放，不追溯改写。
- 七轮真实 Trace 的 5 个可回放调用现被明确拆为：历史领域 JSON 3 个，候选比
  3.905058～4.201220；当前 canonical wire body 2 个，候选比 4.203175～4.256826、中位
  4.230001；两组观测低估均为 0。当前 wire 样本仍只有 planning/execution 各 1 个，不授权降低
  生产门禁。
- `model sizing-report` 新增 `agent_retrieval_turn`，直接复用生产 `read_file`、`retrieve_code`、
  `replace_text`、`submit` Schema；其工具字段固定为 1,873 bytes，完整合成请求 4,592 bytes，
  生产/候选 ceiling 分别为 10,208/5,616。它验证编码和 Schema 负担，不提供 Provider token usage。
- 本批未读取凭据、联网、调用模型、创建 Campaign 或产生费用；生产公式仍为
  `2 * request_bytes + 1024`。

### 2026-10-07 源码绑定的多阶段写入硬崩溃恢复

- Run A/B manifest/report 增加向后兼容的 schema v2。两条 arm 必须在同一模型调用执行参数完全相同
  的 `replace_text`、`apply_patch` 或 `create_file`；旧 schema v1 的字段和冻结 digest 不变。
- 独立 child Worker 在 durable model/tool intent 和真实写入完成后，把 call ID、PID、action ref、
  effect revision 与固定退出码 87 写入并 fsync marker，然后 `os._exit(87)`，因此不可能走正常 receipt。
- 父进程重开持久化 adapter，确认 exactly one pending write，先通过 `RecoveryService` 记录
  `TOOL_CALL_UNKNOWN`，再围栏旧 lease、获取下一 epoch，并由现有 `ToolRecoveryService` 精确接纳
  expected effect；恢复前后 model call 数不变，同一 call ID 只有一个 recovery receipt。
- 小型集成测试的两条成功 arm 均通过真实硬退出、无重派恢复和继续执行；合同负例拒绝 legacy、
  单边或不对称故障配置。Suite/case digest 为
  `3b421aca80519b65e50d230c4cb3e397dc40851796f615c5ab778a83f1e64a02` /
  `72c612e86b0c2649ed70c251be48306916e11a2638313317cacd63421b53d6aa`。
- Luigi 382-file 本地可信运行完成。Baseline/Treatment 分别是 `WAITING_FOR_USER` / `SUCCEEDED`，
  都经历 3 次 Worker 重启并到 epoch 4，恢复 disposition 为 `accept_replace`，unknown/open 均为 0；
  Trace refs 为 `fdf3dca9d4ad0de7840e7cd32681daee2f40f276454064fb5b177c979c083a26` /
  `3602869c4daf5b9df7eba2ec19ef940615c624838b25a1db5e1a98a16af7d2f7`。
- 一次性结果打印器在 evaluator 已完成并确认 `all_expectations_met=true` 后，因使用不存在的旧字段名
  抛出 `AttributeError`。没有将该辅助命令记为通过，也没有重跑核心任务；随后从落盘 SQLite/CAS
  只读复核两个投影、128/171-event Trace、marker/receipt/revision、epoch 1～4 和 source Git clean，
  复核命令通过。
- 实现提交 `b8e31a8c9108718a6553e3867d9537c68cbd25e3` 的公开工作流
  [`37569667246`](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/37569667246)
  在 `python:3.12-alpine` 与生产 `--network none` Docker adapter 上通过新增 suite 1/1，同时保持
  裁剪 5/5、完整 checkout 3/3、既有三阶段 2/2。Artifact `source-bound-docker-evidence` ID
  `11460356798`、大小 105,093,914 bytes、digest
  `sha256:30c69b28d8c38602b5e0b7a83ad355ca27bb7da8a284958b1f3aa4e0e3152ee0`；上传与 evidence summary
  步骤均成功，doctor 的硬崩溃 Docker 计数提升为 1。

### 2026-10-06 有界恢复矩阵 v1 → v3

- 新增冻结 manifest `horizon-write-recovery-v1`，digest 为
  `efacbe3f55bcf3efee78ee3397fffb44a3a855d1924be2f9307aa55aa45f77a4`。唯一 hard-crash
  案例复用一键作品集链路，真实观察子进程退出码 86、悬空写入 unknown、精确接纳一次、续跑
  验证和 Trace replay。
- 其余 18 例覆盖 3 类确定性写工具在 `pre_effect / expected_effect / diverged` 下的 accept 与
  rollback 全组合。生产 `ToolRecoveryService` 与评测共用同一纯决策函数；精确 effect 接纳不
  重派，安全 rollback 回到完整前态，缺失 effect 的 accept 和所有 diverged 决策均保持现场阻塞。
- v2 没有改写 v1：新 manifest `horizon-recovery-matrix-v2` 的 digest 为
  `5f11e000760192c5f2ebb827e97745202e77ad162ad0a1166feae426bb5d9eda`。新增第二个真实
  子进程：本地 Fake Model 已返回并 fsync client Trace marker，但 Response Artifact 发布前以
  退出码 27 停止。恢复只留下原 reservation，将 Run/Campaign 双账标为 unknown，释放新 Lease，
  `safe_to_resume=false`，且 reservation 事件仍只有 1 个，证明没有自动重派。
- v2 实际报告为 20/20；期望自动恢复 10/10，期望安全阻塞 10/10，`unrecoverable=0`、
  `incorrect_resume=0`、恢复重派 0、重复副作用 0；真实崩溃/Trace replay 各 2。
- v3 继续保留前两个 digest；新 manifest `horizon-recovery-matrix-v3` 的 digest 为
  `cb7dd9e36adffeb87b6d7cbe99e22149484eeee4d59df0d1bc4f2871b553ca31`。第三个真实子进程在
  staging→source 的精确 effect 完成、promotion receipt 前以退出码 29 停止；恢复识别完整既有
  effect，只补 1 个 `PROMOTION_SETTLED`。目标的 device/inode/mtime 与 fsync marker 一致，说明
  恢复过程没有二次写入。v3 为 21/21，自动恢复 11/11、安全阻塞 10/10，三条 Trace replay。
- 每例保存 pre/observed/expected/final revision 与内容寻址 manifest；顶层 verifier 会重放硬崩溃
  Trace，并检测普通案例 workspace 的事后篡改。该批无网络、Docker、被测仓库代码或付费模型。
  18 个状态案例不是 18 次真实进程崩溃，完整 8 故障点统一报告仍未完成。

### 2026-10-06 外部来源依赖裁剪 Suite v2

- `bugsinpy-reduced-v2.yaml` 原样保留 v1 的 Cookiecutter、FastAPI、tqdm 三个 case 对象和
  digest，再追加来源绑定的 Luigi metrics-handler 与 Tornado WebSocket nodelay 两例；两例均冻结
  buggy/fixed commit、上游测试、修复 URL、Apache-2.0 license 与 reduction note。v2 digest 为
  `0cd34fadbebec928049f5b43ae505ff375b80b1bd624341ac469686495419d90`。
- 集成测试不再只检查修复 marker，而用严格限于 `python -B <fixture-script>` 的 test-only 执行器
  启动独立 Python 进程。五例初始版本 5/5 失败、Baseline 5/5 等待、Treatment 5/5 成功，delta
  为 +10 model calls、+15 tool calls/steps、合成 CNY 0.00480；没有付费模型或网络调用。
- 初版两个 treatment 因对同一文件使用多个 patch edit，被既有“edit 文件必须互异”合同拒绝；
  改为每文件一个有界 edit 后通过，没有放宽 Gateway 合同。v1 三例的历史禁网 Docker 3/3 证据
  保持不变。后续统一公开 CI 使用生产 Docker adapter 把 v2 五例补为 5/5，并与完整 checkout
  v2 3/3 一起上传同一证据包。

### 2026-10-06 完整 Checkout Suite v2

- `bugsinpy-full-checkout-pilot-v2.yaml` 原样保留 v1 的 tqdm 82-file 与 youtube-dl 872-file case，
  新增精确固定在 BugsInPy buggy commit `1164eb6…` 的 Luigi 382-file 干净 checkout；v2 digest 为
  `be359684b9e3adfd676ed06c457197f7aff42bdcd1dbb3913321cebf5ef857b4`。
- 三项目本地可信完整 A/B 为 3/3：初始检查 3/3 失败、Baseline 3/3 等待、Treatment 3/3 成功，
  success/model/tool/step delta 为 +3/+7/+10/+10，合成成本 delta CNY 0.00336；suite report ref
  `ebca0211b92e82eb90f1f122df6bb6f48a9bf071afe3476f2745d44a7df28695`。全部 Trace 可重放，
  unknown/open reservation 为 0，三个 source checkout 均未改变。
- Luigi Code RAG 在 327 indexed / 55 skipped 的显式 degraded 状态下把 `luigi/server.py` 排为
  rank 1。保护命令用标准库 AST 提取并执行真实 `MetricsHandler`，避免安装旧 Tornado/Luigi 依赖
  栈；不是文本 marker，也不是官方 BugsInPy runner。
- 首次 Luigi Treatment 因 LF 多行 exact replacement 无法匹配 Windows CRLF checkout 而失败；
  Gateway 正确保持原 workspace。改成两个行结尾无关的原子单行替换后复跑通过，没有放宽
  exact-match 合同。随后专用 GitHub Actions 工作流重建三个固定 commit，在禁网 Docker 中以
  3/3 通过；统一公开 run 为 `37436568589`，同时验证依赖裁剪 v2 5/5，artifact digest 为
  `sha256:5bb438df11526a43476272f5622af3446c013bdc0c3ae1614b56272c8bcbc4b2`。

### 2026-10-06 三阶段完整 Checkout 恢复 A/B v2

- `RunABArm.restart_after_model_calls` 兼容原正整数并新增严格递增、无重复的正整数序列；evaluator
  按每个边界关闭旧 Lease，重新打开 SQLite event store、Artifact/Snapshot、RAG、Campaign ledger
  和 Agent runner。每次边界必须已经通过至少一个 WorkItem、持久会话已指向依赖满足的未完成项，
  否则评测直接拒绝。旧 v1 scalar manifest 和 digest 保持不变。
- 新 suite/case digest 分别为 `c57ae62f6a4bbed7a7ba22e4169822887564c41272f9e440ca0f1c2bd4d458d3` /
  `ae87c1d42c3a4464e6167ee72f885bac45997806a06f67c98cd9942404448da4`。同一 872-file youtube-dl
  checkout 上执行 production、regression、compatibility 三个依赖项，并在模型调用 3、6 后重启。
- 本地可信结果为 1/1：Baseline 通过前两项后在相同检索上安全等待；Treatment 保留前两项，只
  修订最后一项，最终三项保护检查全部通过。两 arm 都有 2 次重启、final lease epoch 3、
  Trace replay、source unchanged、unknown/open 0；model/tool/step delta 为 +2/+5/+5。suite/case
  report ref 为 `adbd34e860a171f42730a84baf886d403e14bb561966b00ae32138b4db9fedea` /
  `31c49fe98fc54c05c2876feea314064f6a8c082204c016dd9324f8358102bfc5`。
- 公开提交 `c369870d70330703f0adb795209bd66cc760f28b` 的 GitHub Actions run
  [`37442538827`](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/37442538827)
  使用生产 Docker adapter、`python:3.12-alpine` 与禁网容器通过三阶段 1/1，同时保持依赖裁剪
  5/5、完整 checkout 3/3。统一 artifact digest 为
  `sha256:cca5c0a2ac25f173c19de247d46631955b0154d621f812568a7f25ba58d684f3`。该证据仍是 Scripted
  Model、作者选择单案例和协作式 WorkItem 边界重启，不等同真实模型或任意崩溃恢复。

### 2026-10-06 三阶段完整 Checkout 恢复 A/B v3

- `bugsinpy-multi-stage-pilot-v3.yaml` 原样保留 v2 的 youtube-dl case 与 digest，再追加固定在
  BugsInPy buggy commit `1164eb6…` 的 Luigi 382-file 干净 checkout；suite/case digest 分别为
  `4f1d9c5596b5acb06986f082a42fa328f25e8954d44f56a9d02ddb50149314dd` /
  `fab149f13284d5428702d3f6ca72e618fe1004bc0dab7ae2409fc6161dd2e6c3`。
- Luigi 把上游修复拆为 collector binding、handler ownership、regression assertion 三个依赖项，
  两条 arm 都在模型调用 3、6 后重启。三个初始 required check 真实失败；检查分别覆盖源码绑定、
  标准库 AST 提取后的真实 handler 调用和上游回归断言，不安装历史依赖栈。
- 本地可信结果为新增案例 1/1、累计两个案例 2/2。Luigi Baseline 完成前两项后以 10 model / 12
  tool / 12 steps 安全等待；Treatment 保留两项、只 replan 最后一项，以 13 / 18 / 18 成功。
  两条 arm 均有 2 次重启、final epoch 3、source unchanged、unknown/open 0 和 Trace replay；Trace
  refs 为 `70750e108c0fe2172ba0f619ee07b9268ebfae9636afaf51a064a9bb8efabc00` /
  `2c1a989d3d1ba54e31a8003db66391597d8ae3df130fa313ae8dd584f7f7092d`。
- Code RAG 在每次 production revision 上把 `luigi/server.py` 排为 rank 1，并在 regression 停滞和
  replan 后把 `test/server_test.py` 排为 rank 1；内容 hash 随 revision 变化，没有把旧片段冒充当前
  事实。该结果仍是作者选择任务和 Scripted Model，不是检索泛化结论。
- 专用公开工作流 run
  [`37450253181`](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/37450253181)
  使用生产 Docker adapter、`python:3.12-alpine` 与禁网容器通过 v3 两案例 2/2；两例的四条 arm
  均记录 2 次重启和 final epoch 3。统一 artifact `source-bound-docker-evidence` 的 ID 为
  `11406606543`，digest 为
  `sha256:dfa069250ec2ee85fff1e972ce35c348ad9b576736ae12e42189ccb23a61e1af`；没有付费模型调用。

### 2026-10-06 一键演示真实进程硬退出恢复

- `horizon demo run` 不再只展示主动 Worker handoff。第二任 Worker 由独立 Python 子进程运行，
  在 `replace_text` 已精确改变 staging 文件、tool receipt 尚未提交的窗口写入并 fsync 崩溃标记，
  随即 `os._exit(86)`；父进程同步等待并核对该退出码。
- 中断状态必须同时存在一个持久 tool intent、缺失对应 receipt、保留派发前 manifest，且 live
  workspace 只能是预期唯一后态。RecoveryService 先把调用标为阻塞 `tool_effect_unknown`；
  Supervisor 只在确认子进程结束后围栏其 Lease，epoch 3 再显式 `accept_replace`。原写操作不重放，
  recovery observation 与下一 AgentSession 原子提交。
- 报告升为 schema v3，保存 crashed/recovery epoch、悬空 call ID、崩溃 marker hash、unknown
  classification、`accept_replace` disposition、唯一恢复 receipt 和恢复上下文证据；EvidencePack
  验证器还会从最终 Trace 复核同一 call 只出现一次成功恢复记录。v1/v2 报告继续可读。
- 该演示仍是固定 Scripted Model、固定 `replace_text` 提交窗和只读文本验收；它证明本演示的真实
  子进程硬退出恢复，不代表 OS reboot、主机宕机、磁盘损坏、任意工具或任意 crash window。
- 最新离线全量回归为 **401 passed，7 skipped**；7 个 skip 均为需要显式本机镜像的 Docker
  合同，已使用本机已有镜像单独复跑并全部通过。

### 2026-10-05 Docker 检查停止结果恢复

- `run_check` attempt 新增 request digest 与 recovery schema 标签，并按 Docker 官方
  [JSON File logging driver](https://docs.docker.com/engine/logging/drivers/json-file/) 选项把日志限制为
  `json-file max-size=8m,max-file=2`。带 tool call ID 的 attempt 在结果返回后保持 stopped，只有
  tool receipt 提交成功才做 best-effort 删除；普通无 attempt 的一次性执行仍在适配器内清理。
- `resolve-tool --accept-check-result --image ...` 只接纳同一镜像、TaskSpec argv/timeout、规范化
  staging workspace、非 root/禁网/只读根配置完全匹配，且自然退出、非 OOM、exit code 小于
  128、完整日志不超过 64 KiB 的停止容器。恢复结果可为 success 或 error，不把失败升级为通过。
- 工具 receipt 与下一 AgentSession 先原子提交，再删除停止容器；清理失败会显式报告
  `result_recorded_cleanup_required`，不会因先删除而丢失唯一结果。正常 Agent 路径也遵守
  “tool receipt 后清理”，并把清理失败暴露在 CLI `check_cleanup_failures`。运行中、信号/超时/OOM、日志
  超限、身份或 workspace revision 漂移继续保持 unknown；原 `discard_check` 路径保留。
- 离线单元、Agent session 续接和 CLI 测试已覆盖成功/失败、状态不一致、信号/OOM、request
  mismatch 与日志超限。真实 Docker 合同已加入“自然退出、清理前杀死控制进程、重启恢复”案例；
  本机 Docker daemon 当前未运行，因此本批尚未执行该新增真实合同，不能把它记为通过。本批
  离线全量回归为 **350 passed，7 skipped**；7 个 skip 均为显式 Docker 合同。

### 2026-10-05 单文件创建的显式崩溃恢复

- `create_file` 硬退出后仍先进入 `unknown` 且绝不自动重放；恢复端复核已记录 ModelResponse、
  参数 hash、WorkItem 权限、派发前 revision/manifest，再推导“原 manifest 加一个精确新文件”
  的唯一 expected revision。
- `pre_effect` 只允许 rollback，`expected_effect` 允许显式 accept 或 rollback；部分/错误内容、
  任意额外文件漂移均为 `diverged` 并继续阻断。成功处置会结算原 tool call 并原子保存包含恢复
  observation 的下一 AgentSession，不重放已经结算的模型响应。
- rollback 删除前复核普通文件、精确字节和稳定元数据，删除后必须回到完整 pre-revision；若
  期间发现并发漂移，会重建 expected 文件并保留 unknown。另拒绝创建到 `.venv`、
  `__pycache__`、`.pytest_cache` 等快照不可见位置。
- 真实子进程 `os._exit` 后的 accept/rollback、前态幂等 rollback、错误/部分后态、并发漂移、
  CLI 离线处置、AgentSession 续接和 Trace replay 已覆盖；没有网络或付费模型调用。当前离线
  全量回归为 **338 passed，6 skipped**，跳过项仍是需显式提供本机镜像的 Docker 合同。

### 2026-10-04 单个新增文件的显式 promotion

- 成功且已验证的 staging 候选现可在既有 1～8 个总变更上限内，显式提升至多 1 个允许路径内、
  最多 64 KiB 的 UTF-8 新文件；计划记录 `kind=created`、空 before hash、精确 after hash 和
  `/dev/null` 审阅 diff，空文件也有明确审计头。
- promotion intent 继续先于源目录副作用持久化。目标不存在时排他创建，已精确等于 after 时
  可从崩溃恢复，其他同名内容保留并阻断；新增和既有修改混合时可继续可证明的部分效果。
- 普通失败只回滚本次创建且文件身份仍一致的对象；并发替换即使内容相同也不会被误删。
  CLI dry-run/确认、幂等重入、Trace replay、真实子进程硬退出、权限/编码/大小、空文件、删除和
  多新增拒绝均已有确定性覆盖。
- 本批没有网络、Docker 或模型调用，费用为 0；离线全量回归 **330 passed，6 skipped**，跳过项
  仍是需要显式提供本机镜像的既有 Docker 合同。

### 2026-10-04 受限单文件创建

- `create_file` 已贯通 Tool Schema、WorkItem 权限、自动 Plan、Gateway、intent/receipt、Run
  Memory、NoProgress 和 Trace replay；一次只创建一个已存在父目录内、允许路径下、最多
  64 KiB 的 UTF-8 文件，既有目标拒绝覆盖。
- Gateway 覆盖精确多字节内容、post-effect manifest、越权/缺失父目录/字节超限无副作用，
  以及普通 post-write 异常按文件身份清理本次创建。
- 真实子进程在创建成功、receipt 前退出后，重启将调用保守标为 `unknown`，不重放且不允许
  复用现有 replace/patch 恢复入口；这证明安全阻断，不是 create 副作用可恢复。
- 本批无网络、Docker 或模型费用；离线全量回归 **317 passed，6 skipped**，跳过项仍是需显式
  提供本机镜像的既有 Docker 合同。

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
  两例维持 rank 1。针对性检索回归 24 项通过；全量离线回归 **295 passed，6 skipped**。
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

### 2026-10-04 精确 OpenAI-compatible payload 尺寸

- 生产 byte basis 从领域 `ModelRequest` JSON 切换到 Adapter 实际发送的 canonical body，算法 ID
  升为 `openai_payload_utf8_bytes_x2_plus_1024_v2`；`2 * bytes + 1024` 保守公式未降低。
- 新 reservation/BudgetStop 记录 payload SHA-256、总 UTF-8 字节、每个顶层字段 value 字节及
  JSON 结构字节；领域校验要求分量精确加总，v2 estimate 与 payload bytes 必须一致。Adapter
  测试再核对 post body 的 bytes/hash，避免预算器与传输层各自序列化后漂移。
- 新增零网络、零模型的 `model sizing-report`：minimal ASCII、Unicode、nested tool schema、
  JSON-in-JSON tool arguments、真实四工具 Agent 检索轮次和 8 KiB tool result 共 6 类，wire body
  为 167～9,061 bytes；相对
  v1 domain JSON 的 delta 同时出现 -96 和 +3，不能用固定开销替代实际编码。
- 历史 v1 Trace 按原 byte basis 展示，reservation report 显式给出 basis counts；第六轮 replay
  hash 仍为 `a3a9ff4b0067a6b323c14cc5f954ca1197d20b13099a63c96d5be97f1d66c4bc`。没有 Provider
  usage、新 Campaign、费用、retry 或 fallback；六类合成请求不构成候选安全性或模型效果结论。

### 2026-10-04 BudgetStop 请求证据与候选 estimator 回放

- planning/execution 在构造完整模型请求、通过 Provider 派发前，生成
  `ModelRequestBudgetEvidence`。若 Run 或 Campaign 金额门禁拒绝请求，`RUN_FAILED` 会原子保存
  call/request 标识、purpose、生产 estimator、请求字节、input cap/ceiling 和 output ceiling；
  CLI 与离线 Trace replay 读取同一证据，Provider 调用数保持 0。
- `trace reservation-report` 新增候选 `request_bytes + 1024` 的零费用历史回放。七轮真实 Trace
  有 5/22 个已结算调用含 request-byte metadata，其中 3 个使用历史领域 JSON、2 个使用当前 wire
  payload basis；观测低估为 0，上界/Provider input 比为 3.905058～4.256826，中位数
  4.201220。三个类型化 BudgetStop 中只有第七轮保存完整 wire 请求元数据。
- 生产公式仍为 `2 * request_bytes + 1024`，没有修改任何费用上限、建立 Campaign、调用 Provider、
  retry 或 fallback。第六轮历史 Trace 重放 hash 仍为
  `a3a9ff4b0067a6b323c14cc5f954ca1197d20b13099a63c96d5be97f1d66c4bc`。

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
- 新增 Provider-return 边界硬退出：Fake Gateway 已返回结构化响应并落下独立 marker，Artifact
  Gateway 在发布该 response 前 `os._exit`。派发前 Run intent 保存的 client Trace ID 与实际传给
  Gateway 的 ID 一致；重启对账把 Run/Campaign 标为 `unknown`，source 未变、模型没有重派，
  导出的 JSONL Trace 可重放为相同阻塞状态。普通 Artifact/receipt 异常则无需等重启，立即执行
  同一双账隔离。无 Provider 查询接口时仍不把内存中已丢失的响应伪造为可恢复 receipt。
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
  真实 Docker 合同覆盖已结算后的清理，并在独立 Python 验证 Worker 运行 60 秒检查时强制终止，
  再由新 Sandbox 查询、停止、删除遗留容器。新增真实子进程分别在容器创建前，以及命令完成、
  容器被外部显式删除后硬退出；两者重启后都呈现 `missing`，同时后者保留仓库内 marker 证明
  命令实际执行，因此 missing 继续只允许人工确认，不能被提升为“未执行”证明。正常主路径现已
  改为 tool receipt 提交后才清理，不再主动制造“未结算但已清理”的窗口。
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
  个 UTF-8 变更，其中至多 1 个是允许路径内、最多 64 KiB 的新文件。promotion 绑定源 path
  hash、初始 revision/manifest、候选 revision、TaskSpec 权限和可选 Git HEAD；完整 effect 后
  退出可只补 receipt，部分 effect 只有在既有目标仍精确等于 before/after、新目标仍不存在或
  精确等于 after 时才继续。真实子进程已覆盖新文件先落盘、既有文件未落盘的部分窗口。当前
  不支持多文件新增、删除/重命名，也不创建 commit。
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
- 扩展确定性 NoProgressPolicy：对单 tool-call 响应中的 read/search/retrieve/replace/patch/create，以
  `tool_name + arguments_hash + same revision` 识别完全相同行为及最小周期为 2～4 的精确循环。
  相同行为第 3 次、period-2 第 5 步、period-3/4 第 `3p-1` 步产生有 Artifact/receipt 的 error
  observation；模型收到反馈后仍继续模式，则分别在第 4/6/`3p` 步保存完整会话，持久化带
  pattern 和可选精确周期的 `HumanGuidanceRequest`，并原子进入
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
- `horizon eval reliability` 现冻结 9 个 NoProgress Trace 和 5 个执行期 replan 合同，共
  62 个逐步策略判断；当前 14/14 案例、62/62 判断通过，false positive/negative 均为 0。
  生产 Agent Loop 与 evaluator 共用领域判定函数，故意标错的负例会保留 13/14 和一次 false
  negative。Manifest digest 为 `806469bc77fe2a73be7df62cb6385caf9a31d8acfedc64bbc6bcca069264283e`，
  report ref 为 `05f5dbfa168ca00f8a3e37554dad53fd040f1ece25b5d3106e8a9681da293c96`。
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
  这是当时版本的冻结事实；当前主干允许 deadline 后只取得 recovery Lease 来对账，并在安全
  边界写入 `wall_clock_limit`，但不会追溯改写该历史 Run 或把它解释为成功。
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
- 当时新增的 schema v1 `horizon demo run` 实际产出 73-event Trace：第一任 Worker 完成检索并
  留下内容寻址的单边范围错误，第二任 Worker 以 `lease_epoch=2` 重开适配器、消费持久化错误，
  随后双边读取、精确修改、保护检查和最终 required validation 全部通过。该历史版本明确排除
  硬崩溃结论；2026-10-06 的 schema v3 演示已在其上增加真实子进程硬退出、unknown 对账和
  epoch 3 精确写入恢复，见本文最新验证段。两版均为零真实模型、零网络和零外部费用。

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
   自动 replan 触发、第二次/失败升级策略、任意 diff/edit、period-5+／语义无进展检测；当前不
   并行执行工作项。完全相同动作和精确 period-2/3/4 循环已有窄保护，执行模型有一次受限 Plan
   revision，但不能扩展宣称为通用自治规划或无进展检测。
2. **完整恢复**：安全轮次和已持久化 response receipt 边界现可续跑，模型/工具悬空 intent
   和 Run/Campaign 提交窗口可分类；单一只读工具支持显式重试，精确 `replace_text` 与有界
   `apply_patch` 和单文件 `create_file` 支持基于 manifest 的 accept/rollback；`run_check` 有可查询的 Docker attempt，
   可核验/显式停止后丢弃未知结果，missing 时仍需人工确认。Provider 返回到 response Artifact
   提交之间已有 client Trace、普通异常即时隔离和硬退出保守恢复证据，但没有 Provider receipt
   查询时仍只能人工对账，不能恢复丢失响应。单个直接子 Worker 已有栅栏优先取消和显式停止
   确认，但尚无通用进程树/Job Object 或后台 watcher。`run_check` 已有 intent-before-create 和
   create-after/start-before 的真实窄证据，并已证明处置后由新 epoch/new call ID 续跑至受保护验收
   成功；但仍需其余创建/启动竞态与不完整结果恢复、
   任意 diff、多文件新增/删除写入、旧容器隔离和 Execution Fork。`create_file` 已覆盖一个新文件的正常路径、普通异常清理、硬退出
   unknown/no-replay 及精确前/后态显式处置，但不代表部分创建、恶意并发、多文件新增或删除已实现。
3. **安全边界**：首个 Gateway 已阻止任意 shell，并把模型工具 intent/receipt 与 Run 预算
   事件化；但模型派发、文件写入和容器副作用还不是跨 SQLite/文件系统的单一原子事务，
   也没有独立安全复核或通用审批系统；当前 promotion 仅覆盖最多 8 个总变更且至多 1 个新文件。
4. **Agent 能力**：自然语言意图已有零模型规则 admission 和权限收窄，但尚无模型分类、持久 intake
   stream 或自动派发；通用审批/澄清式持久化 HITL、
   Project Memory、symbol/vector Code RAG、六层精确 tokenizer/semantic Context、安全自动 retry、
   实际模型 fallback 与终态质量摘要集成、更完整的 repair/replan。现有持久 breaker、保守 Retry/
   Fallback 资格合同、确定性字符 + 完整请求保守 token 上界
   双门投影、run-scope 证据 Memory 和
   词法 FTS 是部分能力，不是上述完整闭环。
5. **预算与隐私补全**：CNY Campaign 与 Run 模型账本现已同时工作，但 TaskSpec 的旧
   `max_cost_usd` 尚未做版本化多币种迁移；取消/截止后的迟到 receipt 已能在本地 recovery
   gate 下结算或保守分类，但没有 Provider 主动 receipt 查询、后台定时协调和生产级全链路
   脱敏。当前 Trace 包含 TaskSpec；不应在其中嵌入凭据。
6. **真实效果与验收**：七个真实模型 Pilot Run 分别因迭代上限、上下文硬门限、三次 Campaign
   费用硬门限和两次单 Run 费用硬门限停止；它们证明了费用/Trace/source isolation 路径，也分别暴露
   并修复了 Plan/搜索反馈、大文件读取/上下文/租约、Plan 假设路径/请求膨胀，以及范围参数协议
   缺口和完整 inventory 路径准入，但仍没有完成目标修复或 protected validation。定位层已有 24 个
   预冻结外部案例（16 development 已跑、8 holdout 密封），但仍缺固定 20～30 个可执行端到端真实
   任务、Agent 查询生成与修复成功率对照、长程/记忆消融和关键安全独立复核。单个负样本不能替代这些证据。

七轮负证据驱动修复、后续作品集演示、input-token 门禁、同名符号诊断、外部定位盲测和
BudgetStop 请求证据回放、精确 wire payload 尺寸、Provider-return 崩溃窗和有界恢复矩阵接入后，
当前主干继续补齐尚未统一覆盖的故障点；最新无 Docker 主套件为 607 passed；全量入口对应
607 passed、11 个显式 Docker skip，11 个 Docker 合同此前已单独通过。
第五轮 Run `run_fec07b6bf28d45d5bc428cda120959a3` 只完成一次规划调用，费用 `CNY 0.005574`；
第一条执行请求需 `CNY 0.054522`，比 Run 余额多 `CNY 0.000096`，因此在 Provider 派发前以
`run_model_cost_limit` 进入 `FAILED`。没有工具、编辑、checkpoint 或验证，Trace 14 事件可重放，
source 未变。第六轮 Run `run_7fab106a71174c77bb0a82a8a054d3ed` 使用该 v2 preflight，结算
2 次模型调用和 1 次 `retrieve_code`，费用 `CNY 0.012834`；Plan 的 `itertools.py` 是存在但错误
的实现假设，RAG 把真实 `tqdm/contrib/__init__.py` 排为 rank 1。消费证据的下一请求需
`CNY 0.05832`，Campaign 只余 `CNY 0.0497586`，因此派发前停止。无编辑或验证，23-event Trace
重放一致。第七轮 Run `run_fc9dd1ce39124f49b0081a4ed8ad2652` 的 Plan 已直接指向真实实现，
随后用 `tenumerate definition` 再次取得 rank-1 证据；2 次模型调用与 1 次只读工具结算
`CNY 0.010968`，但消费证据的下一请求需 `CNY 0.045012`，高于 Campaign 剩余
`CNY 0.0387906`，因此仍在派发前停止。Trace 23 事件可重放，无编辑或验证。当前系列累计实际
费用为 `CNY 0.2064492`，第七轮授权已消费。剩余 Campaign 或用户总额度
都不能自动扩权，任何新付费 Run 仍须取得明确授权。当前优先继续零费用可靠性验证，现有证据不支持立即引入 symbol/vector
索引、模型摘要或自动 replan。Run Memory 已形成最小垂直切片，
精确 NoProgress/replan 的冻结策略 Trace 已建立 14 案例基线；内部 A/B、五个来源绑定的依赖裁剪
案例和覆盖九个项目的十五个完整 checkout 案例均已执行初始负例或新增单案、Trace、验收、调用数和
合成费用合同；依赖裁剪 v2 与完整 checkout v8 已有公开禁网 Docker 5/5 与 15/15 报告和统一证据
artifact，v8 新增三例本地也为 3/3。
完整 suite
中历史三例、v7 新增三例及 v8 新增三例均给出 Code RAG rank 1；v8 新增三例分别保留
2/2/55 个文件跳过的显式
降级证据。重复 CAS blob 校验的
规模开销已完成前后对照优化。三个完整 checkout 的三阶段任务及其两个 WorkItem 边界、
epoch 1 → 2 → 3 Worker 恢复也已通过。`run_check` 已有不自动重派的窄丢弃合同、
可查询标签 attempt、显式 create/start 边界、created 状态的窄丢弃路径与自然退出结果恢复；正常主路径把停止容器保留到
tool receipt 提交后再清理。真实子进程覆盖 running/pre-create/created，并保留“外部删除后 missing
不足以证明未执行”的负证据。
created-state 的一键证据现另有 terminal continuation：边界 Trace 作为终态 Trace 的严格字节前缀，
恢复后的新 Worker 使用不同 call ID 运行 protected check，marker 与 check Artifact/manifest 一致，
最终 `SUCCEEDED` 且 unknown/open effect 为零。该增量不改变“只覆盖一个确定性窗口”的边界。
同一窗口现又补上真正 Lease-owning Agent Worker 的进程级故障：parent launch record 在 start gate
前绑定并落盘 PID/Lease/epoch/session/image，epoch 1 child exit 34 后由 epoch 2
`confirmed_reaped_worker_expired` 接管并保留 pending intent，精确 `discard_check` 后由 epoch 3
新 child/new call 完成 protected validation；本机真实 Docker、Trace replay 和重哈希 PID 篡改负例
已通过，仍不外推到父控制器/OS 重启、进程树、跨主机或任意崩溃。
首轮失败已经提供一个真实样本，但尚无分布
或收益证据；在形成多样本证据前不增加
自动触发、第二次修订或语义循环检测。
计划失败和确定性停滞的两条窄持久 HITL 已落地，通用审批暂不扩张。
Project Memory 等
文件级失效与显式 promotion/revoke 合同具备后再做。Campaign 的持久硬上限仍为 CNY 3 元；
它不是自动追加实验授权，本轮 Pilot 的用户累计授权上限仍单独按 CNY 0.25 计算。密钥只在
本地 `.env` 或环境变量中读取，不写入 Git、TaskSpec、
SQLite 账本或文档。
