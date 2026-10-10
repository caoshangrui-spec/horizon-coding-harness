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
| `SUMMARY.md` | 面向人的终态、失败原因、unknown/open effect 和声明边界 |
| `evidence-pack.json` | schema v1 清单；保存三个被校验文件的相对路径、字节数、SHA-256 与结构化终态元数据 |

清单本身不列入自己的文件哈希，避免自引用。它使用字节长度和 SHA-256 检查三个内容文件，随后
重新播放 Trace，并要求结构化元数据、`final-run.json` 和确定性生成的 `SUMMARY.md` 都与重放结果
精确一致。即使有人修改摘要并同步更新清单中的摘要哈希，语义复算仍会拒绝不一致内容。

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

## 4. 固定声明边界

每个 schema v1 清单固定保留三条边界：

1. 终态不自动证明所有外部 effect 已结清；
2. unknown 和 open effect 必须可见，不得在摘要中改写为成功；
3. 文件和 Trace 的自洽哈希不是来源签名或第三方真实性证明。

因此该包适合代码评审、面试展示、故障复盘和离线归档，但不替代数字签名、可信时间戳、远端
Provider receipt 或独立审计。它也不重新执行原 Run，所以不会增加模型费用或重复副作用。

## 5. 已验证范围

离线回归分别覆盖：

- `SUCCEEDED` 只有在 checkpoint、required validation 和最后 WorkItem 通过后才标记任务成功；
- `FAILED / wall_clock_limit` 保留一个 unknown 且仍占用预算的 Provider 调用；
- `CANCELLED` 的 CLI 导出、无任务成功误报和独立 `verify-bundle`；
- 非终态 Run 在创建输出目录前拒绝；
- 已存在目录不覆盖；
- 篡改摘要并同步重算文件哈希仍因 Trace 语义不一致而拒绝。

这证明的是当前 SQLite Trace 和终态投影的有界离线可核查性，不是证据来源认证，也不代表任意
外部系统已经完成结算。
