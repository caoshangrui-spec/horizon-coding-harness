# 一键作品集演示与 EvidencePack

## 1. 目标

`horizon demo run` 提供一个无需 API Key、Docker 或网络的固定演示，用一条命令展示 Horizon
已经实现的可靠性主链路：

1. 从不可变 source snapshot 创建隔离 workspace；
2. 第一任 Worker 先执行 revision-bound 代码检索；
3. Harness 保存检索 Artifact，后续写入前绑定其模型 ContextProjection、目标路径、旧文本哈希与
   workspace revision；
4. Scripted Model 提交只有 `start_line` 的非法范围读取；
5. Typed Tool Gateway 返回结构化错误并把回执写入事件流和 ArtifactStore；
6. 控制器在安全边界释放 Lease，重新打开 SQLite、ArtifactStore、SnapshotManager 和检索器；
7. `lease_epoch=2` 的第二任 Worker 是真实子进程：它读取持久化错误、改用成对范围，并完成
   `replace_text` 的文件副作用；
8. 子进程在 tool intent 已提交、文件已改变、tool receipt 尚未提交时写入崩溃标记并调用
   `os._exit(86)`，不会执行正常清理；
9. Supervisor 等待并确认子进程退出，观察“一个悬空写 intent + 无 receipt + 唯一精确后态”，
   先保守标为 `unknown`，再围栏旧 Lease；
10. `lease_epoch=3` 的恢复 Worker 依据派发前 manifest 显式 `accept_replace`，把 tool receipt 与
    下一 AgentSession 原子提交；原写操作不重放；
11. 恢复后的模型上下文必须包含处置回执，然后运行受保护检查并提交；
12. 控制器执行最终 required validation，导出 Trace、最终投影、报告和完整性清单；
13. 导出完成前重新读取全部文件、复算 Evidence→Write lineage、硬退出标记和恢复 receipt，
    并离线重放 Trace；只有哈希、投影、恢复证据和最终状态一致才返回成功。

这是一个确定性的 Harness 演示，不是模型能力评测。它复用生产主循环、预算账本、工具网关、
Run Memory、上下文会话和事件存储，但模型动作由冻结脚本提供。

## 2. 运行

```powershell
uv run --locked --cache-dir .uv-cache horizon demo run
```

默认在 `.horizon/demos/portfolio-<随机 ID>/` 创建全新目录。也可以明确指定目录：

```powershell
uv run --locked --cache-dir .uv-cache horizon demo run `
  --output .horizon/demos/interview-demo
```

目标目录必须不存在。Horizon 不会覆盖已有 EvidencePack；重复使用同一路径会以退出码 2 拒绝。
如果执行中断，已产生的状态会保留供诊断，不会自动删除或伪装成成功。

成功时 CLI 返回一行 JSON，关键字段包括：

```json
{
  "status": "SUCCEEDED",
  "all_checks_passed": true,
  "paid_model_called": false,
  "network_called": false,
  "repository_code_executed": false,
  "external_cost_cny": "0",
  "claim_scope": "offline_deterministic_harness_demo",
  "recovery_mode": "durable_handoff_and_hard_crash",
  "worker_handoffs": 2,
  "hard_crash_recovery_verified": true,
  "crashed_worker_exit_code": 86,
  "write_recovery_disposition": "accept_replace"
}
```

其中 `simulated_model_cost_cny` 只验证 Harness 的费用记账，不是外部账单。

## 3. 数据流

```mermaid
flowchart LR
    S[Immutable source] --> SNAP[Content-addressed snapshot]
    SNAP --> W[Isolated workspace]
    W --> A[Worker 1: retrieve + invalid read]
    A --> E[(SQLite events + artifacts)]
    E --> H[Lease release and adapter reopen]
    H --> B[Worker 2 subprocess: bounded read + edit]
    B --> X[os._exit after effect before receipt]
    X --> U[Intent becomes unknown]
    U --> C[Exact manifest check + accept once]
    C --> R3[Worker 3: check + submit]
    R3 --> L[Evidence to write lineage]
    L --> V[Final required validation]
    V --> T[JSONL Trace]
    T --> R[Offline replay]
    R --> P[EvidencePack integrity manifest]
```

这里同时包含两种边界：第一段是受控 durable Worker handoff；第二段是真实操作系统子进程
硬退出。Supervisor 只在同步等待确认该 PID 已结束后，才使用已知 Lease token 将悬空 intent
标为 unknown 并围栏旧 Worker。该证据只覆盖一个固定的 `replace_text` “effect 已发生、receipt
未提交”窗口，不扩展为任意进程、主机、文件系统或恶意并发故障恢复。

## 4. 输出目录

| 文件或目录 | 内容 | 验证方式 |
|---|---|---|
| `source/` | 原始缺陷 fixture | 结束后重新快照，revision 必须不变 |
| `workspace/` | Agent 修改的隔离副本 | revision 必须变化，最终文本检查必须通过 |
| `control.sqlite3` | Run 事件与投影缓存 | `trace.jsonl` 可独立重放出相同状态 |
| `campaign.sqlite3` | 离线模拟费用账本 | 无 reserved/unknown，外部费用固定为 0 |
| `retrieval.sqlite3` | revision-bound 词法检索派生缓存 | Trace 中保留 `retrieve_code` 回执 |
| `artifacts/` | 会话、错误、响应、快照等内容寻址产物 | Run 中引用 SHA-256 |
| `hard-crash-marker.json` | 子进程在副作用后、硬退出前 fsync 的 PID/tool/call/exit 标记 | SHA-256 锚定在 schema v3 报告，验证器重读并匹配悬空 call ID |
| `trace.jsonl` | 追加事件导出 | 重放后的 projection hash 必须匹配报告 |
| `final-run.json` | Trace 对应的规范化最终 Run | 字节内容必须等于重放投影的 canonical JSON |
| `report.json` | 结构化结果、崩溃恢复证据、声明边界与 Evidence→Write lineage | schema v3 严格解析；v1/v2 历史报告仍可读取 |
| `SUMMARY.md` | 面试展示用的人类可读摘要 | SHA-256 进入 EvidencePack |
| `evidence-pack.json` | 四个交付文件的路径、大小和 SHA-256 | 写入后立即重新读取并验证 |

`evidence-pack.json` 不把 SQLite 派生缓存声明为不可变交付物。可移植证据由 Trace、最终投影、
结构化报告和摘要组成；报告再锚定硬退出标记与内容寻址 Artifact。运行数据库与 ArtifactStore
留在目录中供进一步审计。

## 5. 成功门槛

`all_checks_passed=true` 只有在以下条件同时成立时出现：

- 初始缺陷检查确实失败；
- 非法单边范围读取产生唯一的内容寻址错误回执；
- 硬退出后的恢复模型请求仍包含该持久化错误；
- 写入模型请求包含原始检索回执，且 retrieval tool call 配对完整；
- 写入目标路径和 exact preimage 出现在检索 chunk 中，EvidencePack revision 与写入前 revision 一致；
- 子进程返回预期退出码 86，硬退出标记与唯一悬空 `replace_text` call ID 一致；
- 中断投影同时满足“intent 存在、receipt 缺失、精确写入后态存在”；
- reconciliation 先产生阻塞的 `tool_effect_unknown`，没有自动重放；
- `accept_replace` 只生成一份成功 receipt，并把处置回执带入恢复后的模型上下文；
- 恢复 Lease epoch 必须从 2 增至 3；
- 最终 required validation 通过；
- Trace 重放与终态投影完全一致；
- source snapshot 未变化而 staging workspace 已变化；
- 三个进程阶段的 Scripted Model 动作全部消费；
- 没有 unknown 模型/工具调用；
- 没有未结算 Run/模型/工具 reservation；
- EvidencePack 中四个文件的大小和 SHA-256 均匹配。

流程到达报告阶段但任一验证项不满足时，报告会保留负结果，CLI 以退出码 3 返回；如果更早发生
结构性错误，已写入的数据库和 Artifact 仍留在新目录中供诊断，不把部分成功包装成完成。

## 6. 明确不证明什么

该演示在报告和 EvidencePack 中固定排除以下结论，调用者不能删除这些边界：

- `real_model_quality`：没有调用 SiliconFlow 或其他真实模型；
- `official_benchmark_score`：内置 fixture 不是 SWE-bench/BugsInPy 官方成绩；
- `untrusted_code_sandboxing`：验收器只读取固定文本，没有执行仓库代码。

因此，它适合在面试中解释 Harness 的状态、恢复、验证和证据链，但不能替代真实模型成功率、
外部任务泛化、Docker 安全边界或完整故障矩阵。它确实证明本命令启动的子进程在指定写入提交窗
硬退出后可以保守恢复，但不证明任意 crash window、OS reboot、宿主机宕机、磁盘损坏或网络
分区。lineage 证明“模型输入里有证据，且写入与证据一致”，不证明冻结脚本或真实模型是通过
推理因果地利用了该证据。
