# SiliconFlow Provider 接入与受控联调

更新：2026-10-02。本文描述已经实现并验证的 Provider 垂直切片和首个受控 Agent Loop，
不把单 fixture 成功扩展为完整长程 Agent 或真实软件工程任务效果。

## 1. 当前结论

已接入 SiliconFlow 的 OpenAI-compatible `/chat/completions`，固定主模型
`deepseek-ai/DeepSeek-V4-Flash`。有两个必须显式付费确认的真实入口：单请求 Tool Calling
探针和 `horizon agent run`。Agent 入口接受人工 Plan，也支持一次受预算 Tool Calling 提出
顺序 WorkItem DAG；自动计划只通过 Fake Model 验证，已冻结的真实付费 fixture 仍是人工计划、
单 WorkItem。普通 `horizon run` 仍只允许 `--prepare-only`，
不会调用模型或执行仓库代码。

2026-10-02 的单次真实探针结果：

- `finish_reason=tool_calls`，返回唯一 `horizon_probe` 调用且参数为 `HORIZON_OK`；
- 输入 308 tokens，输出 49 tokens，cache 0，reasoning 0；
- 按配置中的保守常规时段价格估算为 `CNY 0.001365`；
- Campaign 上限 `CNY 3.00`，本次后剩余估算额度 `CNY 2.998635`；
- 无重试、无 fallback，供应商 Trace ID 为 `ti_avtkirl3tvgxq0kzfw`。

这里的金额是依据 token 用量和冻结 PriceCard 计算的估算账，不冒充供应商最终账单。
价格配置使用常规时段上限，不依赖更便宜的分时优惠。

同日的首个真实 Agent fixture Run 结果：

- Run ID `run_c6c8234c58f5441e80a4b9b72da481c4`，终态 `SUCCEEDED`；
- 5 次模型调用、6 次受控工具调用，5,043 input / 433 output tokens，reasoning 0；
- Run 模型估算费用 `CNY 0.019026`，无 retry、无 fallback、unknown 为 0；
- 模型先读取 `src/greet.sh`，执行一次精确替换，调用受保护检查并 submit；Harness 再次执行
  最终保护检查，`greeting_regression` 在 Docker 中通过；
- 源 fixture 的内容 hash 不变，候选修改只存在于 `.horizon/staging/agent-...`；
- committed checkpoint、验证 Artifact、模型/工具 receipt 均进入 Run Trace；JSONL 离线
  重放与数据库投影 hash 一致；
- 探针与 Agent Run 后 Campaign 累计估算 `CNY 0.020391`，剩余 `CNY 2.979609`。

这是一个人为构造的小型联调 fixture，不是 SWE-bench、真实 GitHub Issue 或长程恢复证据。

## 2. 文件与责任

| 文件 | 责任 | 是否包含秘密 |
|---|---|---|
| `.env` | 本地 `SILICONFLOW_API_KEY` | 是；Git 忽略，禁止输出 |
| `.env.example` | Key 名和占位格式 | 否 |
| `config/providers/siliconflow.yaml` | endpoint、模型、请求上限、PriceCard、Campaign/Run 上限 | 否 |
| `adapters/model/config.py` | 严格 Schema、canonical endpoint 校验、密钥解析与脱敏 | 否 |
| `adapters/model/openai_compatible.py` | 单次非流式 HTTP 请求、Tool Call/usage 严格解析 | 仅内存持有 Key |
| `adapters/persistence/campaign_budget.py` | 跨进程 CNY 预留、结算、unknown 和记账 | 否 |
| `application/model_probe.py` | 先预留、后派发、再结算的受控探针 | 否 |
| `application/agent_loop.py` | Run 内模型/工具循环、有限 repair、checkpoint 和终态门禁 | 否 |
| `application/planning.py` | 一次性计划请求、Run/Campaign 记账、控制器校验和 response 复用 | 否 |
| `application/context.py` | 模型调用前生成可重放的有界 ContextProjection，完整会话不被覆盖 | 否 |
| `adapters/retrieval/sqlite_fts.py` | revision/scope-bound SQLite FTS5 索引与 EvidencePack；无 FTS 时显式 scan 降级 | 否 |
| `tools/gateway.py` | 受限搜索/读取/精确替换/检查/submit，生成内容寻址证据 | 否 |
| `adapters/sandbox/validation.py` | 只执行 TaskSpec 冻结的 Docker 验收命令 | 否 |

环境变量优先于 `.env`。配置、CLI 输出、异常、SQLite 账本和测试均不得保存 Key；
SiliconFlow 配置还把 endpoint 锁定为 `https://api.siliconflow.cn/v1`，且 HTTP 层禁止
重定向，避免 Authorization 被转发到另一个主机。

## 3. 预算语义

配置中的预算为：

```yaml
campaign:
  campaign_id: siliconflow-integration-2026-10
  currency: CNY
  max_cost: "3.00"
  max_cost_per_call: "1.00"
run_budget:
  currency: CNY
  max_cost: "1.00"
```

派发前计算输入 token 的保守上界：

```text
reserved_input_tokens = 2 * utf8_bytes(canonical_request) + 1024
```

双倍空间覆盖 Tool 参数的 JSON-in-JSON 转义，固定余量覆盖供应商消息 framing 和特殊
token。按 PriceCard 预留：

```text
R = reserved_input_tokens * input_rate / 1e6
  + max_output_tokens * output_rate / 1e6
```

真实探针执行时使用第一版上界并预留 `CNY 0.004242`；复核 JSON-in-JSON 转义后将公式
加固为上面的双倍字节上界，当前同一探针预检会预留 `CNY 0.007332`。两者均小于单次
`CNY 1.00`，本次实际估算费用也未超过原预留。成功响应后用供应商返回的
input/output/cache token 重新估算并结算；未完成或不可确认是否计费的派发保留完整预留并
标记 `unknown`，不能当作 0 元继续调用。账本事务规则：

1. `BEGIN IMMEDIATE` 下检查累计占用并写 reservation；超过单次或 Campaign 上限则不派发。
2. `settled` 占实际估算金额；`reserved` 和 `unknown` 占完整预留金额。
3. 重启重新读取 SQLite，不清零额度；Campaign ID 的币种、上限、Provider 和模型不可变。
4. 若供应商回执超过预留或硬上限，先提交真实用量，再向上报告 `BudgetExceeded`；不能回滚账单事实。

当前有两层同时生效的 CNY 费用门禁：Campaign ledger 限制跨 Run 累计占用；Run event
stream 绑定 `ModelPolicyBinding`，对每次调用先记 reservation，成功后按 usage 结算，
不确定调用进入 unknown 并阻止继续派发。Run 的 CNY 上限当前由 provider policy 注入。
TaskSpec 的旧通用预算仍保留 `max_cost_usd`，尚未完成带版本迁移的多币种合同；因此这是一条
已工作的 CNY overlay，而不是最终统一预算 Schema。

真实任务 Pilot 使用独立的
[`siliconflow-pilot.yaml`](../config/providers/siliconflow-pilot.yaml)：Campaign 仍共享已授权的
CNY 3.00 累计账本，但单 Run 上限收紧到 CNY 0.25、模型调用总数由 TaskSpec 固定为 6，
`max_attempts=1` 且 fallback 关闭。配置摘要与 preflight report 绑定；修改价格、模型、上下文或
上限都会让旧报告在首个模型调用前失效。

## 4. CLI

只做本地配置和密钥存在性检查，不联网：

```powershell
uv run --locked --cache-dir .uv-cache horizon model check
```

查看已持久化的累计账本，不联网：

```powershell
uv run --locked --cache-dir .uv-cache horizon model budget
```

真实探针会产生极小费用，必须显式确认：

```powershell
uv run --locked --cache-dir .uv-cache horizon model probe --confirm-paid
```

真实 Agent Run 同样必须显式确认，并要求本机已有 Linux 镜像：

```powershell
uv run --locked --cache-dir .uv-cache horizon agent run `
  examples/agent-task.yaml examples/agent-plan.yaml `
  --image redis:7-alpine --confirm-paid
```

使用一次额外的规划调用代替人工计划：

```powershell
uv run --locked --cache-dir .uv-cache horizon agent run `
  examples/agent-task.yaml --auto-plan `
  --image redis:7-alpine --confirm-paid
```

真实 checkout 在付费前先运行不读取 Key、不调用模型的预检：

```powershell
uv run --locked --cache-dir .uv-cache horizon eval pilot-preflight `
  benchmarks/run_ab/full/youtube-dl-3-unescape-html/real-model-pilot.yaml `
  --image python:3.12-alpine `
  --config config/providers/siliconflow-pilot.yaml
```

正式运行必须同时提交 preflight 输出的 `prepared_task_path` 和 `report_path`，并继续显式提供
`--confirm-paid`。启动门会重新验证 source snapshot/Git HEAD、Docker image digest、Provider
config、6-call 上限、CNY 0.25 上限和 Campaign 当前 headroom。详细证据见
[真实模型 Pilot](real-model-pilot.md)。

该命令先把本地源目录内容寻址快照恢复到唯一 staging 目录，再创建 Run、Lease 和计划；
所有模型动作只能经 Typed Tool Gateway。`agent run` 成功不会自动写回源目录，但之后可用
`agent diff` 只读检查，并通过独立的 `agent promote --confirm-promote` 提升最多 8 个既有
UTF-8 文件修改。当前只接受 local repository；Plan 可含多个 WorkItem，只做 dependency-ready
的确定性顺序执行。`--auto-plan` 最多生成 8 项，并将一次规划调用计入同一 Run/Campaign；它
不读取验收命令正文、非法结果不自动重试，崩溃后可复用已结算 response。执行模型可基于当前
Run 证据显式提交一次受限 Plan revision；该 revision 不新增模型调用，工具记账、Plan vN+1 与
新 session 原子提交，但当前不自动触发、不允许第二次修订，也不并行。promotion 不支持
新增、删除、重命名或自动 commit。

Provider 配置同时固定 `request.max_context_chars=60000`、
`request.max_input_tokens=120000` 和 `preserve_recent_context_units=6`。字符门只测量投影消息；
input-token 门对包含工具 Schema 的最终 OpenAI-compatible HTTP body 使用
`2 * UTF-8 bytes + 1024` 上界。Adapter 与预算器共用同一个 canonical wire encoder；新
reservation 还保存 payload hash、总字节、顶层字段 value 字节和 JSON 结构字节。120,000 是
Horizon 的可配置请求策略，不冒充模型官方窗口或 tokenizer 精确计数。完整 canonical transcript
继续作为会话 Artifact 保存；每次请求绑定另一份 ContextProjection Artifact，记录 source digest、
算法 Schema、字符使用、请求字节、token 上界和实际消息；模型 reservation 绑定同一预算。
恢复 pending response 时按历史 estimator ID 重新生成并逐字段比较，防止配置或会话漂移。自动
规划与 probe 也在 Provider 派发前检查该上限，离线 pilot preflight 将其纳入 `ready`。

探针固定为单次请求，不读取 `request.max_attempts` 进行隐式重试。配置中的最多两次尝试是
未来统一 Retry Gateway 的上限，当前尚未实现；自动 fallback 固定关闭。

## 5. 已验证与未验证

已经由确定性测试覆盖：

- Config 禁止未知字段、错误 endpoint 和币种不一致；
- 环境变量优先级、`.env` 重复定义拒绝、Key 不出现在 repr/CLI；
- 请求非流式、关闭 thinking、输出 token 有界、Authorization 只在 header；
- Adapter 实际 body 与预算器测量的 payload 字节/hash 完全一致，字段分量精确加总；
- Tool Call arguments 严格 JSON object，模型静默切换、缺 usage、坏响应拒绝；
- Campaign 原子预留、幂等、重启、unknown、硬上限和超额回执先记账；
- 字符预算与完整请求 input-token 上界分别生效，规划/probe 超限时不派发；
- fake gateway 的成功结算和派发失败 unknown 路径；
- Run 内 CNY policy 绑定、模型 reservation/settlement/unknown、实际超预留先记账后失败；
- one-shot PlanningContext 的 scope 过滤、Plan DAG/验收/工具校验、plan-to-model receipt 关联，
  以及 Plan 事件前崩溃后从 PLANNING 复用 response；非法计划不会触发第二次模型请求；
- Typed Tool Gateway 的路径白名单、禁止路径、链接拒绝、文件/输出上限和 LF 保留；
- 模型完成声明不能直接成功，`submit` 只触发 controller-owned validation；
- Fake Model 的成功闭环与一次验证失败后 repair 闭环；
- 安全轮次会话 Artifact 可由新 SQLite/Artifact 实例和新 Worker 恢复；workspace drift、
  会话后的额外操作事件与悬空 reservation 均在新模型调用前拒绝；
- 离线 reconciliation 校验 Run/Campaign identity，可由可信 Run receipt 补齐 Campaign；
  response Artifact 允许新 Worker 不重复计费地继续；Provider 派发前的 Campaign-only hold
  可按 0 释放，无 receipt 的模型/工具 intent 进入 unknown 且不自动重派；
- 新模型 intent 在派发前保存实际传给 Provider 的 client Trace ID；Provider 返回后的 response
  Artifact/Run receipt 普通写入失败会立即把 Run/Campaign 隔离为 unknown，进程硬退出则由重启
  reconciliation 执行相同保守分类。该 ID 是对账线索，不等于可查询的 Provider receipt；
- 单一 `search_repo`/`read_file`/`retrieve_code` 悬空 intent 可由离线 CLI 显式选择 retry；
  实现会校验原响应、
  参数 hash、事件尾部和 workspace revision，旧尝试以 `cancelled` 保守计数；其他写/验证工具
  默认拒绝；
- Docker `run_check` 用已持久化 tool call ID 派生唯一名称和 owner/attempt/image 标签；悬空 intent
  可查询停止状态，显式停止仍运行的精确 attempt，或在 missing 时由操作者确认。workspace
  revision 未漂移后才允许 `discard_check`；原检查输出不进入会话，不推断 pass/fail，也不重放；
- 单一 `replace_text` 或最多 8 个不同既有文件的结构化 `apply_patch`，可在 live workspace 精确
  等于派发前 manifest 或唯一预期后态时显式 accept/rollback；部分写入或额外 drift 拒绝；
- 单一 `create_file` 可在允许路径和已存在父目录中排他创建最多 64 KiB 的 UTF-8 文件；成功
  receipt 绑定 content 参数 hash、前后 revision 和 manifest，硬退出后保持 unknown 且不重放；
- 成功候选的只读 diff 和最多 8 个既有文件的显式 promotion 绑定源/候选 revision 与可选 Git
  HEAD；完整 effect 后、receipt 前可只补 receipt，部分 effect 可在每个目标仍为 before/after
  时继续；
- 确定性 ContextProjection 保留初始合同、近期/未完成工具单元，拒绝孤立/错配工具结果；
  压缩 pending response 能跨 Worker 恢复而不重复计费；
- MandatoryFactLedger 和证据驱动的 Run Memory 均按内容寻址绑定到每次模型请求；Memory 只从
  工具事件/输出派生，区分 active/stale/unresolved，并按原事件边界恢复；
- 同一 revision 下，相同精确 read/search/retrieve/replace/patch/create 的第 3 次或精确 A/B 循环的
  第 5 步会被 NoProgressPolicy 拒绝实际执行；若模型收到反馈后仍延续模式，则进入有证据的
  可恢复人工等待；该策略尚无真实模型误拦截/收益证据；
- `retrieve_code` 从当前 immutable manifest 生成 path/range/hash 证据，每个 FTS 命中回查源
  Artifact；dirty revision、权限 scope、索引篡改、empty/degraded 和 scan fallback 有离线测试；
- 6 项真实 Docker 合同，以及一次真实 SiliconFlow + Docker fixture Run；
- 模型/工具/验证/checkpoint 事件导出后无需模型和 Docker即可重建相同 projection hash。

尚未实现或尚未验证：

- 任意崩溃点的可继续恢复；`run_check` 已覆盖真实 running 窗口，仍缺容器创建前、命令退出后
  清理前以及 missing attempt 启动证明；`create_file` 的未知效果也尚无 accept/rollback，另缺
  多文件新增、删除/重命名或任意 diff 的写工具副作用处置；
- retry/backoff、Circuit Breaker、迟到回执主动对账；
- 生产级消息/Trace 脱敏，以及完整多调用崩溃/取消配对矩阵；
- 自然语言 TaskSpec intake、自动触发/多次执行期 replan、并行 WorkItem、任意 diff/edit、
  新增/删除/重命名 promotion 和自动 commit；当前单文件创建不会由 promotion 提升到源目录，
  one-shot 自动计划、顺序 WorkItem DAG 与
  一次显式受限 revision 仅由 Scripted Model 离线 E2E 验证；
- tokenizer-aware/语义 Context、跨 Run Project Memory、symbol/vector RAG、通用 HITL 与
  预授权 fallback；当前已实现 MandatoryFactLedger、run-scope 证据投影和 revision-aware
  词法检索，但没有真实模型利用这些新增上下文后的效果证据；
- 真实代码任务、长程恢复、benchmark 成功率和成本结论；
- 独立安全复核。

上文历史 SiliconFlow fixture 发生在 `retrieve_code` 加入工具面之前。当前词法 RAG 只由
Scripted Model 离线走通过，尚未授权或执行新的付费模型调用，因此不能声称真实模型已经会用
该证据包改善任务结果。

接口与计费字段依据 SiliconFlow 的
[Chat Completions API](https://docs.siliconflow.cn/docs/api/chat-completions-post)、
[Function Calling 指南](https://docs.siliconflow.cn/docs/userguide/guides/function-calling)及
[价格更新记录](https://docs.siliconflow.cn/docs/release-notes/overview)。
