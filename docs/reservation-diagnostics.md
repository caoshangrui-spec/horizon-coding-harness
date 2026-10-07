# 模型预算预留诊断

长程 Agent 不能只限制最终结算费用：模型派发前还必须为最坏情况下的输入、输出申请预算，
否则一次超长回复就可能穿透 Run 或 Campaign 上限。但预留若远高于实际结算，也会让本来付得起的
下一轮在派发前停止。本诊断把两件事分开：**安全门禁继续保守执行，离线报告负责量化保守程度**。

## 使用方式

```text
horizon trace reservation-report TRACE_1.jsonl TRACE_2.jsonl ...
horizon model sizing-report --config config/providers/siliconflow.yaml
```

两个命令都不连接模型，也不执行仓库代码。`reservation-report` 只读取本地 Trace；每个输入都会
先走完整 replay 和 hash-chain 校验，同一 Run 重复输入会被拒绝，避免累计值被重复计算。
`sizing-report` 只读取无凭据 Provider 配置，并让六类固定请求通过生产 wire encoder。

报告逐调用关联以下证据：

- `BUDGET_RESERVED`：派发时保留的 input/output token 上界；
- `MODEL_CALL_RESERVED`：币种、模型、request hash、保守费用与可选 estimator 元数据；
- `MODEL_CALL_SETTLED`：Provider 回执中的 token usage，以及本地冻结 PriceCard 算出的结算费用；
- `RUN_FAILED.budget_stop`：未派发请求的 required/available/shortfall；
- `RUN_FAILED.model_request_budget`：新停止事件相邻保存的 call/request 标识、purpose、
  `request_bytes`、生产 estimator、input cap/ceiling 与 output ceiling。v2 还保存实际出站 payload
  hash、总字节、各顶层字段 value 字节和 JSON 结构字节。旧 Trace 可以没有这些字段。

金额字段中的 `settled_price_card_cost` 是本地 PriceCard 对 Provider usage 的复算值，不冒充供应商
最终账单。未派发的 budget stop 没有 Provider usage，不能虚构“如果派发会花多少钱”。

## 七轮真实模型样本

2026-10-07 对七个 replayable Trace 的离线结果为：

| 指标 | 结果 |
|---|---:|
| Trace / Run | 7 / 7 |
| 已结算模型调用 | 22 |
| 派发时预留费用累计 | CNY 1.416348 |
| 本地 PriceCard 结算累计 | CNY 0.2064492 |
| 聚合预留/结算比 | 6.860516 |
| 单调用预留/结算比中位数 | 6.847547 |
| input token 上界/Provider input 中位数 | 7.122579 |
| output token 上界/Provider output 中位数 | 4.491228 |
| 类型化 pre-dispatch BudgetStop | 3 |
| 可回放候选 estimator 的已结算调用 | 5 |
| 当前 wire basis 的已结算调用 | 2 |
| 混合 basis 兼容统计：候选 `request_bytes + 1024` 上界/Provider input 中位数 | 4.201220 |
| 候选公式在可观测样本中的低估次数 | 0 |

这里的“预留费用累计”是各调用在派发瞬间的压力之和；每次结算后多余预留都会释放，不能把
`CNY 1.416348` 解释成已消费费用。22 次调用都保存了 token 上界和实际 usage，但只有 5 次
保存 `request_bytes`/estimator 元数据：3 次使用历史 v1 领域请求 JSON byte basis，最新 2 次使用
Adapter 实际 canonical wire body。候选公式在混合 basis 样本中的上界/实际 input 比范围为
`3.905058～4.256826`；这足以继续确认**存在系统性预留放大**，不足以证明新公式是所有请求形态
的安全上界。三个类型化 BudgetStop 中只有最新一次保存完整 wire 请求元数据；未派发请求没有
Provider usage，不能用它判断候选公式是否低估。

为了避免把不可直接比较的 byte basis 合成一个“看似更大的样本”，报告同时输出
`by_request_byte_basis`：

| request byte basis | settled | purpose | candidate / Provider input | Provider input / request byte | observed underestimate |
|---|---:|---|---|---|---:|
| 历史领域 JSON v1 | 3 | planning 2 / execution 1 | 3.905058～4.201220；中位 4.137869 | 0.289792～0.310450；中位 0.302185 | 0 |
| 当前 canonical wire v2 | 2 | planning 1 / execution 1 | 4.203175～4.256826；中位 4.230001 | 0.281451～0.295586；中位 0.288519 | 0 |

混合口径总计仍为兼容性统计，不能用于 estimator promotion。当前真正相关的 wire v2 只有 2 个
真实 settled 调用和 1 个无 Provider usage 的 BudgetStop；没有足够证据降低硬门禁。

## 六类零费用 wire payload 边界

`model sizing-report` 覆盖 minimal ASCII、中文/emoji、多层工具 Schema、带多字节嵌套参数的
assistant tool call、复用真实四工具 Schema 的 Agent 检索轮次，以及 8 KiB tool result。默认
SiliconFlow 配置的确定性结果为：

| case | wire payload bytes | v1 domain bytes | wire - v1 | production ceiling | candidate ceiling |
|---|---:|---:|---:|---:|---:|
| minimal ASCII | 167 | 227 | -60 | 1,358 | 1,191 |
| Unicode messages | 271 | 367 | -96 | 1,566 | 1,295 |
| nested tool schema | 532 | 529 | +3 | 2,088 | 1,556 |
| tool-call arguments | 916 | 973 | -57 | 2,856 | 1,940 |
| Agent retrieval turn | 4,592 | 4,562 | +30 | 10,208 | 5,616 |
| 8 KiB tool result | 9,061 | 9,118 | -57 | 19,146 | 10,085 |

六类请求的字段 value 字节加 JSON 结构字节均精确等于 Adapter body，总范围为 167～9,061 bytes。
Agent retrieval case 直接读取生产 ToolDefinition，四个工具的 `tools` 字段为 1,873 bytes，与第七轮
未派发请求记录的工具字段一致；它没有复刻完整 Agent prompt，因此只证明 Schema/wire 尺寸合同。
正负 delta 都存在，说明不能用一个固定“领域对象开销”修正 wire size；生产路径因此直接测最终
body。该诊断没有 Provider usage，不能证明 `request_bytes + 1024` 候选公式安全，也不是模型或
真实任务效果证据。

## 决策边界

报告不会自动调低安全系数、扩大 Campaign 或触发复跑。生产门禁的公式仍是
`2 * request_bytes + 1024`，但新请求的 `request_bytes` 已由近似的领域对象 JSON 切换为 Adapter
实际发送的 canonical wire body；`request_bytes + 1024` 仍只作为候选回放。历史报告新增
`request_byte_basis_counts` 与 `by_request_byte_basis`，防止把 v1/v2 样本静默混合。下一步是积累
带 v2 wire metadata 的 settled call 与 BudgetStop，并明确验证 Provider token usage 覆盖边界；只有候选公式在足够样本
和边界测试中仍满足硬上界合同，才考虑替换生产公式。
