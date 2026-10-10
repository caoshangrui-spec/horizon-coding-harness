# 终态 Run 顺序汇总评测

## 1. 用途与边界

`terminal-suite` 把同一 Horizon SQLite 控制库中一组**已经结束**的 Run，按清单顺序导出为
独立的终态 EvidencePack，并生成一份可脱离数据库复核的聚合报告。它面向长程任务回归、失败
复盘和作品集证据整理，解决的是“多次 Run 如何用同一口径保存和汇总”，不是再造一个执行器。

生成过程只读取已有 EventLog；不会启动 Run、Worker、模型、工具、Provider、Docker 或仓库
代码，也不会重试失败任务。`SUCCEEDED` 才计为任务成功；`FAILED`、`CANCELLED`、预算停止、
unknown 和 open effect 均原样保留并单独统计。

因此当前能力是 NFR-010 的一部分：它证明单机可以按确定顺序为既有终态 Run 生成统一记录，
但还没有“读取任务集并顺序执行到终态”的 batch scheduler，也没有并发吞吐或性能声明。

## 2. 清单

清单是严格 schema v1 YAML/JSON。顺序即报告顺序；`case_id` 和 `run_id` 都必须唯一，数量为
1～100。清单不写“期望成功”，避免把预期失败的用例通过误报成任务修复成功。

```yaml
schema_version: 1
suite_id: nightly-terminal-runs
cases:
  - case_id: parser-success
    run_id: run_0123456789abcdef
  - case_id: budget-stop
    run_id: run_fedcba9876543210
  - case_id: operator-cancelled
    run_id: run_0011223344556677
```

运行命令：

```powershell
uv run --locked horizon --db .horizon/control.sqlite3 eval terminal-suite `
  terminal-suite.yaml --output .horizon/evaluations/nightly-terminal-runs

uv run --locked horizon eval verify-terminal-suite `
  .horizon/evaluations/nightly-terminal-runs/suite-pack.json
```

输出目录必须不存在。开始写文件前会预检清单里的所有 Run：每个 Run 都必须是终态，且 SQLite
当前投影必须与完整 Trace 重放结果一致。这样，清单后部的非终态或损坏 Run 不会留下一个看似
完整的前半套报告。运行期磁盘故障的临时目录事务性不在当前声明范围内。

## 3. 输出结构

```text
nightly-terminal-runs/
├── suite-manifest.json
├── suite-report.json
├── SUMMARY.md
├── suite-pack.json
└── cases/
    ├── 001-parser-success/
    │   ├── trace.jsonl
    │   ├── final-run.json
    │   ├── SUMMARY.md
    │   └── evidence-pack.json
    └── 002-budget-stop/
        └── ...
```

每个 `cases/<序号>-<case_id>/` 都是现有
[通用终态 EvidencePack](terminal-evidence-pack.md)，可单独运行 `trace verify-bundle`。顶层
文件的含义如下：

| 文件 | 内容 |
|---|---|
| `suite-manifest.json` | 输入清单的规范 JSON 和可复算 digest |
| `suite-report.json` | 每个 Run 的真实终态、失败原因、预算停止原因、Trace/投影 hash 与子包引用 |
| `SUMMARY.md` | 确定性人读表格、聚合计数和声明边界 |
| `suite-pack.json` | 顶层三个文件的字节数、SHA-256 和结构化报告 |

`suite-report.json` 的聚合字段包括：

- `succeeded_run_count`、`failed_run_count`、`cancelled_run_count`；
- 只按 `SUCCEEDED` 计算的 `task_success_rate`；
- `terminal_capture_verified_count`，它表示证据捕获已复核，不等于任务成功数；
- 带 unknown/open effect 的 Run 数；
- 全部 `failure_reason` 计数，以及从终态 Trace 复算的 `budget_stop.reason_code` 计数；
- `runs_executed=false`、`paid_model_called=false`、`network_called=false`、
  `tool_called=false`、`repository_code_executed=false`。这些字段只描述本次汇总操作，不抹掉
  原 Run 历史上可能发生过的模型、工具、网络或费用。

## 4. 离线验证链

`verify-terminal-suite` 不打开控制数据库，按以下顺序复核：

1. 校验顶层 manifest、report、summary 的相对路径、字节数和 SHA-256；
2. 重新解析严格清单和报告，并核对 `suite_id` 与 manifest digest；
3. 按规范路径逐个调用子 EvidencePack 验证器；
4. 对每条子 Trace 重放 Run，核对清单中的 `run_id`，并读取结构化 BudgetStop；
5. 从重放结果重建每个 case、全部聚合计数和确定性摘要；
6. 要求重建结果与 `suite-report.json`、`SUMMARY.md`、`suite-pack.json` 完全一致。

所以只修改摘要、报告或子包再重算某一层文件哈希并不足以通过验证。该链仍然只是内容与领域
语义自洽性：没有数字签名、可信时间戳、外部 Provider 查询或来源认证。

## 5. 固定声明边界

schema v1 固定保留四条边界：

1. Suite 只聚合既有终态 Run，不执行任务；
2. 终态证据捕获通过不等于任务成功；
3. 失败、取消、unknown 和 open effect 必须可见；
4. 自洽哈希不等于来源认证。

当前也不汇总历史模型费用总额：unknown 用量和不同币种不能安全地被折算成一个“准确总费用”。
预算停止只按 Trace 中已经持久化的 `reason_code` 分类，不从普通失败文本猜测。
