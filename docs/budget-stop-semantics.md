# 确定性费用停止语义

更新：2026-10-03。本文描述模型请求在 Provider 派发前因 CNY 硬上限被拒绝时，Harness 如何
形成可重放终态。它只处理能够由持久账本确定证明的费用不足，不把 unknown 用量、Provider
不确定结果或一般执行错误误判为“费用已耗尽”。

## 1. 要解决的问题

历史第四轮真实模型 Pilot 在下一次请求前正确触发单 Run 费用闸门，但 CLI 只释放了 Worker
Lease，Run 投影仍停留在 `RUNNING`。账本实际上没有开放 reservation 或活跃 Worker，状态却容易
让人误以为任务仍在后台执行。

新合同要求：只要控制器能在派发前确定请求不可能进入既定费用上限，就必须原子记录明确原因并
终止该 Run；不能依靠异常字符串、进程状态或人工查看 SQLite 推断。

## 2. 类型化停止原因

领域层的 `BudgetStop` 固定以下字段：

| 字段 | 含义 |
|---|---|
| `reason_code` | 稳定的机器可判定原因 |
| `scope` | `run` 或 `campaign` |
| `currency` | 当前为 `CNY` |
| `required_cost` | 本次请求的保守费用预留 |
| `available_cost` | 对应硬边界当时可使用的金额 |

从 2026-10-04 起，新发生的模型费用停止还会在同一 `RUN_FAILED` 事件中相邻保存
`model_request_budget`。它不改变停止金额合同，而是保留“本来准备派发什么请求”的可审计证据：

| 字段 | 含义 |
|---|---|
| `call_id` / `request_hash` | 未派发调用的稳定身份和规范请求摘要 |
| `purpose` | `planning` 或 `execution`，并且必须匹配 Run 当时阶段 |
| `input_token_budget.max_input_tokens` | 本次投影实际采用的 input cap；执行调用可由当前费用余量收窄 |
| `input_token_budget.estimate.estimator` | 生产估算器版本 |
| `input_token_budget.estimate.request_bytes` | 完整规范 Provider 请求的 UTF-8 字节数 |
| `input_token_budget.estimate.token_ceiling` | 生产 estimator 算出的 input token 上界 |
| `output_token_ceiling` | 本次请求配置的最大输出 token 数 |
| `request_payload.payload_sha256` | Adapter 最终 canonical HTTP body 的 SHA-256 |
| `request_payload.payload_bytes` | 与 v2 `request_bytes` 相同的实际出站 body 字节数 |
| `request_payload.field_value_bytes` | 每个顶层 payload value 的规范 UTF-8 字节数 |
| `request_payload.json_structure_bytes` | 顶层 key、引号、冒号、逗号和外层对象等剩余字节 |

该证据不包含 Provider usage，因为请求没有派发；不得把 input/output ceiling 当作真实消费。
历史 v1 证据可以没有 `request_payload`；新 v2 estimator 若缺少它或总字节不一致会被领域合同拒绝。

当前只定义三类确定性、派发前停止：

| `reason_code` | 触发条件 |
|---|---|
| `run_model_cost_limit` | 已结算/预留费用加本次请求超过不可变单 Run 上限 |
| `campaign_call_cost_limit` | 本次请求预留超过 Campaign 单调用上限 |
| `campaign_cost_limit` | Campaign 已占用费用加本次请求超过 Campaign 总上限 |

unknown 用量阻断、wall-clock 到期和 Provider 返回后的实际账单超限不套用这三个原因：前两者仍需
原有对账/到期流程，后者必须先保存真实回执再按 usage overrun 处理。

## 3. 状态和数据流

```text
PLANNING / RUNNING
        │
        ├─ 执行期把 Run/Campaign/单调用最小余量换算为 effective input cap
        ├─ 在不改变保守估算公式的前提下，按需压缩完整历史单元
        ├─ 构造有界请求并计算保守预留
        │
        ├─ Campaign reserve gate
        │      └─ 失败：没有 Campaign attempt 被创建
        │
        └─ Run reserve gate
               └─ 失败：已有 Campaign-only reservation 先以 0 结算
                              │
                              v
          RUN_FAILED + BudgetStop + request sizing evidence
                              │
                              v
                 FAILED / Lease 自动清除
```

`HarnessService.fail_budget_stop()` 只接受没有 Run reservation 的静止边界。它在同一个事件事务中
写入 `RUN_FAILED`、稳定的 `failure_reason` 和完整 `budget_stop`。领域投影进入终态时统一清除
Lease/Worker/expiry，避免出现“终态但 Worker 仍活跃”。若停止来自已完成尺寸估算的模型请求，
同一事件还写入 `model_request_budget`；证据的 purpose 必须与 `PLANNING`/`RUNNING` 阶段一致，
且其 call ID 不得已经出现在 reservation 或 settlement 中。

费用感知投影不是降低硬门禁：它先为完整输出上限预留费用，只把剩余额度按输入单价换算为
effective cap。若该 cap 低于 2,000、不可压缩前缀/未完成工具回合仍放不下，或压缩后费用依然
超限，控制器回到配置 cap，让原 reservation gate 做最终派发或 BudgetStop 判定。恢复已结算
响应时使用 reservation 保存的原 cap，不因稍后结算释放余额而生成另一个请求。

## 4. 不变量

确定性费用停止必须同时满足：

1. Provider Gateway 尚未被调用；
2. Run 内没有开放 model/tool/generic reservation；
3. 如果 Campaign reservation 已先创建，则在终止 Run 前以 0 结算；
4. `failure_reason == budget_stop.reason_code`；
5. `required_cost > available_cost`；
6. JSONL Trace 重放得到相同投影；
7. CLI/status 同时展示原因、所需金额和可用金额；
8. unknown 用量存在时不得走该终态捷径。
9. 新模型停止的 request sizing evidence 必须与停止原子提交，并能由 JSONL Trace 重建。

历史 Trace 可能没有 `budget_stop`，较新的历史停止也可能只有 `budget_stop` 而没有
`model_request_budget`。`Run.as_dict()` 只在字段实际存在时输出它们，因此旧 Trace 重放 hash
不因新增能力而变化。第四轮 Pilot 仍保留其原始非终态投影，不做追溯改写；第六轮原
projection hash 也保持不变。

## 5. CLI 输出

未来的确定性费用停止会返回类似：

```json
{
  "run_id": "run_...",
  "status": "FAILED",
  "failure_reason": "run_model_cost_limit",
  "budget_stop": {
    "reason_code": "run_model_cost_limit",
    "scope": "run",
    "currency": "CNY",
    "required_cost": "0.067530",
    "available_cost": "0.059648"
  },
  "model_request_budget": {
    "schema_version": 1,
    "call_id": "model_run_example_2",
    "purpose": "execution",
    "request_hash": "58cdaeb5b81c09339449b87ebb4594cc4f0f0c2002f50ea206e3490c4bd5c790",
    "input_token_budget": {
      "schema_version": 1,
      "max_input_tokens": 60000,
      "estimate": {
        "schema_version": 1,
        "estimator": "openai_payload_utf8_bytes_x2_plus_1024_v2",
        "request_bytes": 916,
        "token_ceiling": 2856
      }
    },
    "output_token_ceiling": 512,
    "request_payload": {
      "schema_version": 1,
      "encoding": "openai_compatible_canonical_json_v1",
      "payload_sha256": "d9b03438f86132d3176af0ed5406752105aaa568c7ee4b3d72c0426412906ad2",
      "payload_bytes": 916,
      "field_value_bytes": {
        "enable_thinking": 5,
        "max_tokens": 3,
        "messages": 451,
        "model": 31,
        "stream": 5,
        "temperature": 3,
        "tool_choice": 6,
        "tools": 308
      },
      "json_structure_bytes": 104
    }
  },
  "continuation_required": false
}
```

终态 Run 不在原地追加预算或恢复执行。若未来需要换合同继续，应创建新 Run，并重新绑定任务、
Provider policy、源码和预算；真实模型 Pilot 还必须重新预检并取得新的外发/费用授权。

## 6. 离线验收

- `test_planning_budget_stop_is_terminal_and_replayable_before_dispatch`：规划阶段 Run 上限不足；
- `test_agent_run_reports_planning_budget_stop_before_model_dispatch`：规划阶段 CLI 在零模型调用时
  返回 Run ID、终态和结构化金额；
- `test_agent_loop_terminalizes_pre_dispatch_run_budget_stop`：执行阶段 Run 上限不足；
- `test_agent_loop_terminalizes_pre_dispatch_campaign_budget_stop`：Campaign 总余额不足；
- `test_agent_run_reports_terminal_budget_stop_as_structured_json`：CLI 输出和持久投影一致；
- Campaign/Run 账本单测分别检查 reason code、所需金额和可用金额；
- 第四轮历史 Trace 继续重放为原 projection hash
  `ad88aada8bd5342c41ee02fb570ae37cfc24ec8e59347dd1ec615f1e7163a8a9`。

这些测试不联网、不调用真实模型，也不修改历史账本。
