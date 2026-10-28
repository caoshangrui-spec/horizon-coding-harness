# 通用终态 EvidencePack

## 1. 目标

长程任务并不总以成功结束。`FAILED`、`CANCELLED`、费用门禁和保守 `unknown` 都是必须保留的
结果，不能只为成功 Run 生成好看的报告，也不能把“控制器已经终态化”误写成“外部副作用已经
结清”。

Horizon 因此提供一个只读、离线的通用终态导出：

```powershell
uv run --locked horizon trace bundle <run-id> --output .horizon/evidence/<name>
uv run --locked horizon trace verify-bundle .horizon/evidence/<name>/evidence-pack.json
```

导出只读取 SQLite EventLog并创建一个新目录；不调用模型、工具、Provider 或仓库代码。目标
目录必须不存在，重复命令不会覆盖旧证据。`verify-bundle` 不需要控制数据库，只读取证据目录。

## 2. 文件合同

| 文件 | 作用 |
|---|---|
| `trace.jsonl` | 完整追加式事件流；验证器重新检查事件 hash、因果链、序号、时间单调性和领域投影 |
| `final-run.json` | Trace 重放得到的规范化最终 Run 投影，不信任 SQLite 派生 view |
| `SUMMARY.md` | 面向人的终态、验收可靠性、失败原因、unknown/open effect 和声明边界 |
| `evidence-pack.json` | schema v2 清单；保存三个被校验文件的相对路径、字节数、SHA-256 与结构化终态/可靠性元数据 |

清单本身不列入自己的文件哈希，避免自引用。它使用字节长度和 SHA-256 检查三个内容文件，随后
重新播放 Trace，并要求结构化元数据、`final-run.json` 和确定性生成的 `SUMMARY.md` 都与重放结果
精确一致。即使有人修改摘要并同步更新清单中的摘要哈希，语义复算仍会拒绝不一致内容。
验证器继续接受并按 v1 规则复算已生成的 schema v1 包；新导出固定使用 schema v2。

## 3. 三种终态的解释

| RunStatus | `task_succeeded` | `failure_reason` | 可声称内容 |
|---|---:|---|---|
| `SUCCEEDED` | `true` | `null` | 当前 TaskSpec、Plan 和受保护验收满足领域成功门禁 |
| `FAILED` | `false` | 必须存在 | 控制器以可重放原因停止；不声称任务已修复 |
| `CANCELLED` | `false` | `null` | 恢复安全取消已经闭合；不声称任务已修复 |

EvidencePack 还逐项保存：

- unknown 的普通预算、模型调用和工具调用 ID；
- 仍在投影中的普通、模型和工具 reservation ID；
- 是否存在尚未落 receipt 的 promotion intent；
- `unknown_effects_present` 和 `open_effects_present` 聚合标记。

这些字段从 Trace 重放结果重新计算。导出器不会为了让报告更整洁而删除、结算或重新分类它们。

schema v2 还包含 `reliability_summary`：

- `SUCCEEDED / FAILED / CANCELLED` 分别映射为 `passed / failed / blocked`；
- required acceptance ID 的有序列表生成稳定 hash，原始与 effective hash 必须相同；
- 当前 TaskSpec、workspace revision 与证据引用匹配时才算“验收证据已知”；已知但缺少 required
  check 记为 `required_checks_failed`，没有当前证据则记为 `acceptance_evidence_unknown`；
- unknown/open external effect 同步写入 quality flags；
- `fallback_history`、`degradations` 和 `limitations` 在结构化清单与 Markdown 摘要中均可见。

生产 Agent Loop 现在会解析成功 `retrieve_code` 回执中的 typed `EvidencePack`。当其状态为
`degraded` 时，原因、策略、quality flag、Task/required-acceptance hash、工具 call ID 与内容寻址
Artifact ref 会作为同一个 `TOOL_CALL_SETTLED` 事件的可选字段原子提交；重放时再次校验它只能
绑定对应的成功检索回执。终态包因此可以真实列出 lexical fallback 或部分索引跳过等检索降级。
确定性上下文折叠现以独立 `ContextCompactionRecord` 与 `MODEL_CALL_RESERVED` 同事件提交，并在
终态 facts 中产生 `context_compacted`；它不会伪装成 retrieval degradation。自动 model fallback、
模型语义摘要和 model degradation 尚无生产事件，相关 history 仍为空。导出器不会从临时日志或
不可定位的 Artifact 猜测历史；trace-bound 范围会继续写入 `limitations`，不能把空数组解释为
“可证明从未发生过对应降级”。

## 4. 固定声明边界

每个 schema v2 清单固定保留四条边界：

1. 终态不自动证明所有外部 effect 已结清；
2. unknown 和 open effect 必须可见，不得在摘要中改写为成功；
3. 文件和 Trace 的自洽哈希不是来源签名或第三方真实性证明。
4. fallback 与 degradation history 只包含绑定进可重放 Trace 的记录。

兼容读取的 schema v1 包仍按原来的三条边界验证，不会被静默升级或改写。

因此该包适合代码评审、面试展示、故障复盘和离线归档，但不替代数字签名、可信时间戳、远端
Provider receipt 或独立审计。它也不重新执行原 Run，所以不会增加模型费用或重复副作用。

## 5. 已验证范围

离线回归分别覆盖：

- `SUCCEEDED` 只有在 checkpoint、required validation 和最后 WorkItem 通过后才标记任务成功；
- `FAILED / wall_clock_limit` 保留一个 unknown 且仍占用预算的 Provider 调用；
- 已知但未通过 required check 与完全缺失当前验收证据会得到不同 quality flag；
- lexical scan 的真实 Agent 检索降级与其工具回执同事件提交、重放并进入终态摘要；
- 确定性上下文折叠与 model reservation 同事件绑定，成功 Run 保留 task success 但报告
  `context_compacted`，不冒充 clean success 或 retrieval degradation；
- `CANCELLED` 的 CLI 导出、无任务成功误报和独立 `verify-bundle`；
- schema v1 历史包仍可按原摘要和边界离线复核；
- 非终态 Run 在创建输出目录前拒绝；
- 已存在目录不覆盖；
- 篡改摘要并同步重算文件哈希仍因 Trace 语义不一致而拒绝；
- 只篡改结构化可靠性元数据、但不改变 Trace，也会被重放复算拒绝。
- 把检索降级伪绑定到普通读取回执会使整个结算事务回滚，不会留下半结算预算。

这证明的是当前 SQLite Trace 和终态投影的有界离线可核查性，不是证据来源认证，也不代表任意
外部系统已经完成结算。
