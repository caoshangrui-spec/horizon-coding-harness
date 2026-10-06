# 确定性上下文投影：实现与恢复合同

更新：2026-10-06。本文描述当前已经接入 `CodingAgentRunner` 的实现，不把确定性裁剪冒充
语义摘要或 RAG。权威完整消息始终保留；投影只是一次模型调用的可重放视图。证据驱动的
Run Memory 已作为独立派生层接入，其边界见 [Run Memory](run-memory.md)。

## 1. 目标与非目标

当前增量解决五个具体问题：

1. 长轮次工具输出持续累积时，模型请求不能无限增长；
2. assistant tool call 与 tool result 不能在裁剪时被拆开或形成孤立消息；
3. 已结算模型响应跨进程恢复时，必须证明新 Worker 重建的是同一个请求上下文。
4. 字符预算通过时，完整请求仍可能因 UTF-8 内容或工具 Schema 膨胀；派发前必须有独立、
   可重放的保守 input-token 上界。
5. 执行期 Run/Campaign 余额不足以容纳完整历史时，应先在同一保守公式下压缩可压缩历史，
   而不是直接停止或暗中放宽费用上限。

当前明确不解决：

- tokenizer 精确预算或模型最大窗口探测；
- LLM 生成的语义摘要、语义等价验证或摘要重试；
- 模型生成的语义 Memory 或跨 Run 的 Project Memory；当前 Run Memory 只接受控制器可重建的
  工具观察，不保存模型 `submit` 声明、猜测或人类对话摘要；词法 Code RAG 是独立工具；
- 用压缩结果覆盖或删除完整 transcript；
- 证明真实任务成功率、关键信息召回率或成本得到提升。

## 2. 三层数据及权威关系

| 层 | 对象 | 持久化 | 权威性 |
|---|---|---|---|
| 完整会话 | `AgentSession.messages` | 内容寻址 Agent session Artifact | 恢复与审计的唯一权威消息序列 |
| 强制事实 | `MandatoryFactLedger` | 每次模型调用前的内容寻址 Artifact | Task/Plan/权限/验收/预算/策略/工具 Schema/workspace 的控制器事实 |
| Run 记忆 | `RunMemorySnapshot` | 每次模型调用前的内容寻址 Artifact | 工具事件与输出证据的有界、可失效投影 |
| 调用投影 | `ContextProjection.messages` | 每次模型调用前的内容寻址 Artifact | 该次请求的派生、可验证视图，并绑定强制事实、Run Memory 和 input-token budget |
| Provider 请求 | `ModelRequest` | request hash + Provider response Artifact | 实际工具 Schema、模型参数与投影消息的请求合同 |

`ContextProjection.source_digest` 对完整源消息的规范 JSON 做 SHA-256；schema v2 保留算法、
字符预算与实际字符数、近期单元数、源/投影消息数、强制事实 ref/hash、Run Memory
ref/hash/count、`InputTokenBudget` 和实际投影消息。`ModelCallReservation` 再绑定 Fact Ledger
Artifact、Memory Artifact 及历史事件边界、投影 Artifact、两个消息数、同一 token budget 及
完整 `ModelRequest` hash。各对象不互相替代。

`MandatoryFactLedger` 不复制秘密或完整任务文本。schema v2 记录 TaskSpec/objective、Plan/当前
WorkItem、按 Plan 顺序排列的已完成 WorkItems、
allowed/denied paths、预算、模型策略、工具 Schema 的 hash，以及 required acceptance IDs、权限模式
和当前 workspace revision。完整 objective、路径和 WorkItem 仍来自不可压缩的初始任务合同；
ledger ref 对完整事实集合提供内容寻址绑定。

## 3. 确定性算法

输入必须至少以一个 system 消息和一个 user 消息开头。这两个消息是不可压缩前缀。其余
消息从前到后组装为单元：

- 没有 tool calls 的普通消息是一个单消息单元；
- 一个包含 N 个 tool calls 的 assistant 消息，以及紧随其后的、ID 一一对应的 N 个 tool
  results，是一个不可拆分单元；
- 缺少部分 results 的 assistant 单元标为 incomplete，绝不压缩；
- 孤立、重复、未知 ID 的 tool result 直接触发 `Conflict`，不向 Provider 发送坏消息链。

字符尺寸定义为消息列表规范 JSON 的 Python 字符数：

```text
context_chars = len(canonical_json(messages))
```

字符数不是 UTF-8 字节数，也不是 token 数，因此它只作为第一道确定性裁剪门。对每个候选
投影，控制器还构造将被实际派发的完整 `ModelRequest`，包含模型 ID、消息、工具 Schema、
tool choice、输出上限和 thinking 设置，并计算：

```text
wire_payload = canonical_openai_compatible_json(ModelRequest)
request_bytes = len(utf8(wire_payload))
input_token_ceiling = 2 * request_bytes + 1024
```

`wire_payload` 与 OpenAI-compatible Adapter 最终交给 HTTP 层的 body 共用同一编码函数，包括
`max_tokens` 字段名、非流式/thinking 控制字段、工具 Schema，以及已完成 JSON-in-JSON 转义的
tool-call arguments。算法 ID 为 `openai_payload_utf8_bytes_x2_plus_1024_v2`；每次 reservation
还记录 payload SHA-256、各顶层字段 value 的 UTF-8 字节数和其余 JSON key/标点结构字节，所有
分量必须精确加总为 `request_bytes`。乘 2 与额外 1,024 仍是费用和输入门禁共用的保守余量，
不是 tokenizer 预测，也不等于供应商最终 usage。

历史 `request_utf8_bytes_x2_plus_1024_v1` 以领域 `ModelRequest` 的规范 JSON 为 byte basis。
旧 Trace 继续按原字节和 projection hash 重放；恢复旧 pending Run 时控制器按 reservation 中的
estimator ID 重建 v1 投影，新请求才使用 v2，不把两种 byte basis 静默混为同一证据。

每次调用先在投影的 system 消息末尾追加控制器拥有的有界 binding：MandatoryFactLedger 包含
ledger ref、关键 hash、WorkItem ID、required acceptance IDs、权限模式与 workspace revision；
Run Memory 包含 snapshot ref、失效/未知/省略计数，以及最近 2 条观察的来源 WorkItem、工具、
outcome、status 和最多 80 字符 active excerpt。stale excerpt 不注入。完整 canonical transcript
不被改写。
执行调用在投影前读取当前 Run、Campaign 和单调用三类费用余量，并按同一 `PriceCard` 先为
`max_output_tokens` 保留完整输出费用，再把剩余金额换算为可负担的 input-token 数：

```text
available = min(run_remaining, campaign_remaining, campaign_max_per_call)
output_reserve = reserve_cost(0, max_output_tokens)
affordable_input = floor((available - output_reserve) * 1_000_000 / input_price)
effective_input_cap = min(configured_input_cap, affordable_input)
```

输入单价为 0 时直接使用配置上限。若可负担值低于领域合同可表达的 2,000 token，或不可压缩
前缀/未完成回合无法满足该值，则改用配置上限重建请求，让既有 Run/Campaign reservation gate
做最终派发或类型化 `BudgetStop` 判定；不会把费用不足伪装成上下文损坏，也不会弱化
`2 * request_bytes + 1024` 公式。

只有加上 binding 后同时满足 `projected_chars <= max_context_chars` 和
`input_token_ceiling <= effective_input_cap`，除 system binding 外其余投影消息才与源消息相同。
超过任一硬上限时：

1. 保留初始 system/user；
2. 优先保留最后 `preserve_recent_context_units` 个单元；这是软保留目标，不覆盖任一硬上限；
3. 从第一个 incomplete 单元开始全部保留；
4. 更旧且完整的单元折叠成一个 user 消息；
5. 折叠消息记录全部被折叠单元的聚合 digest；最多最近 20 个旧单元另记录工具名、参数 hash、
   内容 hash 和最多 240 字符片段；更早单元只有聚合 digest，不伪造细节；
6. 每次候选变化都重建完整请求；如果字符数或 token 上界仍超限，按最旧到最新删除摘要中的
   单元细节，同时记录 retained/omitted fact count；
   聚合 digest、合同和 Memory binding 不删除；
7. 若优先保留的近期完整单元仍使请求超限，则从最老的近期完整单元开始，确定性扩大折叠前缀，
   只折叠满足上限所需的最少单元；完整 canonical transcript 不变；
8. incomplete 单元始终不折叠。若不可压缩前缀加 incomplete 单元本身仍超过任一上限，则抛出
   `Conflict`，不向 Provider 发送越界请求。

因此首版是“结构化、可审计、有损裁剪”。它能证明哪些字节被折叠、摘要如何重建，不能证明
240 字符片段包含所有语义关键事实。

## 4. 模型调用与提交顺序

一次新模型调用的关键顺序为：

```text
完整 AgentSession
  → 从当前 Run/tool schema/workspace 重建 MandatoryFactLedger
  → 写 ledger Artifact 并按 hash 回读校验
  → 从 EventLog/tool Artifact 派生 RunMemorySnapshot
  → 写 memory Artifact 并按 hash 回读校验
  → 读取 Run/Campaign/单调用余量，计算本次 effective input cap
  → 对候选投影重建完整 ModelRequest，应用字符 + effective input-token 双硬门槛
  → 绑定 InputTokenBudget，写 ContextProjection 并按 hash 回读校验
  → 写 Artifact 并按 hash 回读校验
  → 构造 ModelRequest 和 request hash
  → Campaign reservation
  → Run MODEL_CALL_RESERVED（绑定 ledger + memory boundary + projection）
  → Provider
  → response Artifact
  → Run receipt
  → Campaign settlement
```

投影 Artifact 写入后、Campaign 预留前退出只会留下无引用的内容寻址对象，不会计费。Campaign
预留后、Run intent 前退出沿用已有 campaign-only hold 对账。Run intent 之后的 Provider/receipt
窗口沿用模型调用恢复规则；投影并未放宽 unknown 费用处理。

## 5. 跨进程恢复校验

当 Run 已有可信 response Artifact/receipt，但上次进程未完成 Campaign settlement 或工具轮次
时，新 Worker 会：

1. 读取最后一个权威 `AgentSession`；
2. 校验 TaskSpec、Plan、iteration、event boundary 和 workspace revision；
3. 从当前 Run、工具定义与 workspace 重建 MandatoryFactLedger，要求其 hash 等于 reservation，
   并回读 ledger Artifact 做逐字段比较；
4. 回读 RunMemorySnapshot，再从 reservation 记录的历史事件边界重建，要求 ref/hash、revision、
   count 与完整对象一致；恢复后新增的 unknown/cancelled 事件不得倒灌原请求；
5. 使用已记录的 estimator 与当时持久化的 effective input cap，加上当前已验证的
   ledger/memory，重新计算 ContextProjection；不按恢复时已经变化的 Campaign 余额另算 cap；
6. 重新构造 ModelRequest，要求 request hash 等于已记录 response 的 request hash；
7. 读取 reservation 绑定的 projection Artifact，要求对象、ledger/memory binding、字符预算、
   token 估算算法/请求字节/上界/effective cap 和消息数逐字段一致；
8. 通过后消费原 response，不再次调用 Provider、不重复计费。

Artifact 缺失/损坏、任务/权限/验收/预算/模型策略/工具 Schema/workspace 漂移、影响请求的
上下文配置漂移、消息变化或投影算法输出变化都会保守拒绝。恢复历史响应时，配置的 input cap
不覆盖 reservation 已绑定的 effective cap；升级前缺少 MandatoryFactLedger 的悬空模型
响应不会跨版本自动恢复；已完成的历史 Trace 仍可读取。新调用一律产生 ledger 和 projection。

## 6. 配置

当前 SiliconFlow 非秘密配置位于 `config/providers/siliconflow.yaml`：

```yaml
request:
  max_context_chars: 60000
  max_input_tokens: 120000
  preserve_recent_context_units: 6
```

Schema 约束为：字符上限 2,000～1,000,000；保守 input-token 上限
2,000～2,000,000；近期单元 1～50。这里的 120,000 是 Horizon 的请求策略，不声明为模型官方
窗口。`agent run`、`agent resume` 和自动规划都读取同一 Provider 上限；探针也在联网前检查。
改变配置不会修改历史 Artifact；如果存在待恢复 response，变化会因投影不一致而拒绝。

## 7. 已执行验证

离线测试覆盖：

- 预算内投影恒等；
- 字符预算仍有余量、但完整请求 token 上界超限时会折叠最老完整单元；
- 不可压缩 incomplete 单元超过 token 上界时在 Provider 派发前拒绝；
- 多个旧完整工具单元确定性折叠；
- 摘要细节超预算时确定性丢弃最旧事实并保留聚合 digest 与遗漏计数；
- 近期完整工具对在硬上限需要时可整体折叠，输出不存在孤立 tool result；
- incomplete tool call 原样保留；
- 单个 122,665 字符的近期完整工具结果在 60,000 字符生产配置下可确定性折叠；
- 孤立/错配 tool result 拒绝，不可压缩前缀或 incomplete 单元自身超限时拒绝；
- 模型请求使用投影，而 Agent session 继续保存更长的完整 transcript；
- 压缩前后 system binding 都保留同一个 MandatoryFactLedger ref/hash；
- 压缩前后 Run Memory ref/hash/count 保持绑定，active excerpt 可见而 stale excerpt 隐藏；
- canonical system/user 任务前缀与当前 Run 重建结果不一致时拒绝；
- workspace 写入生成新 ledger，Task/Plan/required acceptance 保持绑定；
- 待恢复 response 遇到工具 Schema 漂移时在调用 Provider 前拒绝；
- reservation 中的 projection Artifact 可解析且与实际模型请求一致；
- projection 与 reservation 保存相同 `InputTokenBudget`，其 estimate 可由完整请求逐字节重算；
- 字符预算仍宽裕时，执行调用会按 Run/Campaign/单调用的最小费用余量降低 effective input cap，
  折叠完整历史工具单元并在低余额 Campaign 内成功闭环；
- 自动规划在 input-token 上界超限时不产生模型 reservation，也不调用 Provider；
- 费用余量触发压缩后的 pending response 在 Campaign settlement 中断后，由新 Worker 使用原
  effective cap 恢复，原响应不重派、不重复计费；显式只读重试产生后续事件时仍按原历史边界
  重建 Memory。

这些测试使用 Scripted Fake Model，不联网、不产生模型费用。全量数字以
[开发进度与验证记录](development-progress.md)的最新一次完整回归为准；Docker skip 不算通过。

## 8. 下一增量的进入条件

下一步不是直接接一个向量数据库。优先补以下两层：

1. 当前 ledger 和 Run Memory 已覆盖控制器结构化事实与工具观察；下一步只有在实现 repo identity、
   文件级依赖重检、显式 promotion/revoke 后，才增加 Project Memory；人类决定必须绑定可信
   actor 和请求版本，不能解析自由文本猜测；
2. revision-aware lexical Code RAG 已加入 camelCase/snake_case 词项、有界定义优先、
   dirty-revision、同名符号路径语境和三项目外部定位盲测，并保留失败/成功证据；下一步须先冻结
   新 holdout，不能继续针对已揭示 gold 的三个案例调参。

只有固定诊断任务显示 lexical retrieval 的召回不足，才评估 embedding/vector 依赖。语义摘要
进入主循环前还需独立事实保留 QA；在此之前 `semantic_compaction=false` 保持为公开能力边界。
