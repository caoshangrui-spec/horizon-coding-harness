# Provider 重试、熔断与 Fallback 合同

更新：2026-10-26。

## 1. 目标与当前边界

本增量把 Provider 可靠性从“配置里预留一个 `max_attempts` 字段”推进为三类可测试合同：

1. 统一 Retry 决策：限制逻辑请求的总尝试次数、累计等待和单次 transport 尝试；
2. SQLite 持久化 Circuit Breaker：跨进程重启保留健康状态，只允许一个 half-open 探针；
3. Model Fallback 与降级摘要合同：只有预授权、能力/数据边界/预算兼容的候选才可能通过，
   且不得改变 TaskSpec 或 required acceptance。

当前仍**不自动重试模型请求，也不自动切换模型**。原因不是遗漏：现有 OpenAI-compatible
请求没有可证明的服务端幂等键；连接超时、429 或 5xx 都不能证明请求未执行或未计费。因此，
Provider 派发后的失败继续保留原费用 reservation，并以
`unknown_dispatch_or_cost` 明确停止。只有调用方能证明“未派发且未产生费用”的失败，纯
Retry 决策才可能返回 `retry`；当前 probe/planning/execution 服务不会据此启动循环。

## 2. 运行数据流

```text
ProviderHealthStore.acquire
  ├─ CLOSED              -> 返回普通 permit
  ├─ OPEN 且冷却未到     -> 派发前拒绝，不创建费用 reservation
  └─ OPEN 且冷却已到     -> 原子进入 HALF_OPEN，只发放一个 probe permit

permit -> Campaign/Run reservation -> 单次 transport dispatch
  ├─ trusted response
  │    -> Run/Campaign receipt 落盘 -> breaker CLOSED / failures=0
  └─ ProviderError
       -> 费用与执行效果标 UNKNOWN
       -> Retry 分类与 stop 原因写入 provider_health_events
       -> 符合规则的临时故障累计，达到阈值后 OPEN
```

顺序很重要：

- breaker 在费用 reservation 之前检查，因此 OPEN 不会制造悬空调用；
- half-open permit 若在派发前因预算/存储失败而放弃，会回到可再次探测的 OPEN；
- ProviderError 必须先完成未知费用记账，再写健康投影；
- trusted response 必须先完成 Run/Campaign receipt，再把 breaker 关闭；
- SQLite 健康投影失败不会被降级成进程内状态继续执行。

生产入口 `models probe`、自动 planning、同进程 execution 和直接子进程 execution 都已接入
同一控制器。健康表与 Campaign 表位于同一个配置的 `ledger_path`，但使用独立
`provider_health_schema` 版本表，不修改 Campaign 的 `PRAGMA user_version`。

## 3. Retry 合同

`RetryPolicy` 的关键约束：

- `max_total_attempts ∈ [1, 3]`；
- `max_total_wait_seconds <= 60`；
- 指数退避受单次和累计等待上限约束；
- `transport_attempts_per_dispatch` 固定为字面量 `1`，SDK 内层重试不能与控制器相乘；
- 预算、deadline、总次数或累计等待任一不满足都返回结构化 stop。

| 失败 | 分类 | 当前决策 |
|---|---|---|
| 401/403 | authentication / non-transient | 不重试，不计入临时故障阈值 |
| 404/422 | configuration / non-transient | 不重试 |
| 响应协议/usage 不可信 | protocol / non-transient | 不重试，保留原始失败 |
| 429、明确 retryable HTTP | transient，但派发与费用未知 | 保留 reservation，不重试；计入 breaker |
| connection timeout/disconnect | transient，但派发与费用未知 | 保留 reservation，不重试；计入 breaker |
| 已证明未派发且未产生费用 | transient pre-dispatch | 纯策略可允许下一 attempt，但当前生产服务不自动循环 |

配置中的 `request.max_attempts` 现在绑定 RetryPolicy 的总次数上限，但它不等于自动重试授权。

## 4. Circuit Breaker 合同

健康 key 为：

```text
(endpoint, provider_id, model_id, operation, credential_ref)
```

`credential_ref` 只保存环境变量名，不保存 API Key。operation 分开记录
`probe / planning / execution`，防止一种请求形态的失败无证据地污染另一种形态。

当前固定策略与设计基线一致：

- 连续 3 个符合规则的 transient failure：`CLOSED -> OPEN`；
- 冷却 30 秒；
- 冷却后只原子发放一个 half-open probe；
- probe 成功：`HALF_OPEN -> CLOSED` 且计数归零；
- probe transient failure：重新 OPEN；
- 401、协议错误等非 transient failure 记录为 ignored，不增加 transient 计数；
- half-open Worker 消失时，30 秒 probe lease 到期后可由新进程接管；
- 旧的并发结果不能覆盖新 OPEN/HALF_OPEN 状态，记录为 `late_result_ignored`。

投影可变，`provider_health_events` 追加保留 initialized、failure、opened、probe、success/closed
等决策证据。breaker 只减少持续故障时的无效付费请求，不参与任务验收。

## 5. Fallback 与降级摘要

`decide_model_fallback` 只做资格判断，不执行切换。候选最多一个，并逐项检查：

- fallback 已启用且 target 在预授权清单；
- 不是要求可比性的 benchmark 运行；
- provider 在允许清单，数据边界一致；
- API 消息格式、工具调用、结构化输出和输入窗口满足原要求；
- 币种一致，Run/Campaign 剩余预算都覆盖新 reservation；
- 原调用费用未知时，其 reservation 仍被保留；
- TaskSpec hash 与 required acceptance hash 完全不变。

`RunReliabilitySummary` 要求 degradation 保存原错误类型/Artifact、实际策略、quality flag 和
限制。即使使用 lexical scan 等降级，只要 required checks 的证据已知且全部通过，可以标记为
“passed with quality flag”；若验收证据未知，领域合同拒绝 `passed`。

该合同现已接入通用终态 EvidencePack schema v2。导出器从重放后的 Run 推导
`passed / failed / blocked`、required-check 状态、unknown acceptance，以及 unknown/open effect
quality flag；CLI 导出与离线验证都会返回相同结构。为保持证据边界，fallback/degradation history
只接受绑定进 Trace 的记录。生产 `retrieve_code` 的 typed EvidencePack 若明确为 `degraded`，会将
原因、策略、call ID 和 Artifact ref 与工具 receipt 原子写入同一个事件，并在重放后进入终态
`degradations`。确定性上下文折叠已有独立的 `ContextCompactionRecord` 和
`context_compacted` quality flag，但不混入 Provider degradation。model fallback、模型语义摘要和
model degradation 仍没有生产事件，因此对应 history 为空并在 `limitations` 中明确范围；不会从
非定位日志猜测或把空数组宣传为“从未降级”。
schema v1 历史包仍可按旧规则验证。

## 6. 已验证与未验证

离线测试覆盖：

- retry 次数/等待/预算/deadline 决策和 inner transport attempt 固定为 1；
- 503/connection 的未知费用不自动重试，401 不重试且不打开 breaker；
- breaker 跨 SQLite 重启保持 OPEN；
- 冷却后只允许一个 half-open probe，成功后关闭；
- ModelProbe 按默认阈值连续失败 3 次后，第四次在 reservation 和网络派发前被拒绝；
- 未授权、不兼容、超预算、正式比较中的 fallback 全部拒绝；
- 降级必须保留原失败、quality flag 和原 required acceptance；
- unknown 或未通过的验收证据不能生成 passed summary；
- 三种终态的 EvidencePack v2 会复算可靠性摘要，区分未知验收与已知 required-check 失败；
- lexical scan 检索降级与成功工具 receipt 原子绑定，Trace 重放和终态包保留原原因/Artifact；
- 伪绑定到非检索回执的 degradation 会使预算与工具结算一并回滚；
- 历史 schema v1 EvidencePack 仍可读取和语义复核。

尚未完成：

- Provider 主动 receipt/usage 查询和 unknown 费用自动对账；
- 自动执行安全 pre-dispatch retry；
- fallback 实际切换、ContextPack 跨 provider 重建和 Run 事件；
- 为 model fallback、模型语义摘要/model degradation 增加生产 Trace 事件；
- 分布式 breaker、跨主机时钟协调或生产监控；
- 任何新的付费模型验证。
