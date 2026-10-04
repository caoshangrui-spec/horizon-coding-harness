# 模型预算预留诊断

长程 Agent 不能只限制最终结算费用：模型派发前还必须为最坏情况下的输入、输出申请预算，
否则一次超长回复就可能穿透 Run 或 Campaign 上限。但预留若远高于实际结算，也会让本来付得起的
下一轮在派发前停止。本诊断把两件事分开：**安全门禁继续保守执行，离线报告负责量化保守程度**。

## 使用方式

```text
horizon trace reservation-report TRACE_1.jsonl TRACE_2.jsonl ...
```

命令只读取本地 Trace，不读取凭据、不连接模型，也不执行仓库代码。每个输入都会先走完整 Trace
replay 和 hash-chain 校验；同一 Run 重复输入会被拒绝，避免累计值被重复计算。

报告逐调用关联以下证据：

- `BUDGET_RESERVED`：派发时保留的 input/output token 上界；
- `MODEL_CALL_RESERVED`：币种、模型、request hash、保守费用与可选 estimator 元数据；
- `MODEL_CALL_SETTLED`：Provider 回执中的 token usage，以及本地冻结 PriceCard 算出的结算费用；
- `RUN_FAILED.budget_stop`：未派发请求的 required/available/shortfall。

金额字段中的 `settled_price_card_cost` 是本地 PriceCard 对 Provider usage 的复算值，不冒充供应商
最终账单。未派发的 budget stop 没有 Provider usage，不能虚构“如果派发会花多少钱”。

## 六轮真实模型样本

2026-10-04 对六个 replayable Trace 的离线结果为：

| 指标 | 结果 |
|---|---:|
| Trace / Run | 6 / 6 |
| 已结算模型调用 | 20 |
| 派发时预留费用累计 | CNY 1.341318 |
| 本地 PriceCard 结算累计 | CNY 0.1954812 |
| 聚合预留/结算比 | 6.861621 |
| 单调用预留/结算比中位数 | 6.847547 |
| input token 上界/Provider input 中位数 | 7.100207 |
| output token 上界/Provider output 中位数 | 4.491228 |
| 类型化 pre-dispatch BudgetStop | 2 |

这里的“预留费用累计”是各调用在派发瞬间的压力之和；每次结算后多余预留都会释放，不能把
`CNY 1.341318` 解释成已消费费用。20 次调用都保存了 token 上界和实际 usage，但只有最新 3 次
保存 `request_bytes`/estimator 元数据，因此当前证据足以确认**存在系统性预留放大**，不足以直接
证明某个新估算公式是安全上界。

## 决策边界

该报告不会自动改变 estimator、调低安全系数、扩大 Campaign 或触发复跑。下一步应先让后续
pre-dispatch stop 也保留可审计的请求尺寸元数据，并对候选公式做零费用历史回放；只有在新的
公式仍满足硬上界合同和足够样本的情况下，才替换生产门禁。
