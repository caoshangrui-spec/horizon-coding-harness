# 源码绑定的多阶段写入硬崩溃恢复

更新：2026-10-07。本文记录一个刻意收窄、但走真实产品主链路的可靠性增量：在完整 Luigi
上游 checkout 的三阶段任务中，独立 Worker 子进程完成 `replace_text` 副作用后、工具回执提交前
以 `os._exit(87)` 退出；Harness 必须先把悬空调用保守标为 unknown，再由新 lease epoch 精确接纳
已经存在的唯一后态，不能重发模型调用或再次执行写入。

这不是任意崩溃恢复、分布式 exactly-once 或真实模型能力证明。它回答的是一个更具体的问题：
已有 intent/receipt、Lease fencing、Workspace Manifest、Tool Recovery 和 Trace 机制，能否在一个
来源绑定的长程软件工程任务里组合成可验证的恢复闭环。

## 1. 冻结输入与范围

- Suite：[`bugsinpy-multi-stage-hard-crash-v1.yaml`](../benchmarks/run_ab/bugsinpy-multi-stage-hard-crash-v1.yaml)，
  digest `3b421aca80519b65e50d230c4cb3e397dc40851796f615c5ab778a83f1e64a02`；
- Case：[`multi-stage-hard-crash-v1.yaml`](../benchmarks/run_ab/full/luigi-1-metrics-handler/multi-stage-hard-crash-v1.yaml)，
  digest `72c612e86b0c2649ed70c251be48306916e11a2638313317cacd63421b53d6aa`；
- 来源：BugsInPy Luigi bug 1，buggy commit
  `1164eb6b85b8a70f596dbb99452bec513e72c12e`，382 个 Git tracked files；
- 模型：`offline-scripted-v1`，无 Provider 网络调用、无付费模型；
- 验收：collector 源码绑定、AST 提取后的 handler 真实执行、上游 regression assertion 三项；
- 故障窗口：两条 A/B arm 的第 2 次模型调用，且动作必须是相同参数的 `replace_text`；
- 其余恢复边界：模型调用 3、6 后仍执行既有 WorkItem 边界重启。

Manifest schema v2 只接受 `replace_text`、`apply_patch` 或 `create_file` 三类已有精确恢复合同的
写工具。故障点必须位于至少一个安全轮次之后、最终动作之前，不能与协作式 Worker 边界重合；
Baseline 和 Treatment 必须在同一调用执行同一个规范化写入。旧 schema v1 不新增字段，其冻结
manifest/suite digest 继续由回归测试保护。

## 2. 恢复时序

```text
epoch 1 / parent       完成安全检索轮次并持久化 Agent session
epoch 1 / child        持久化 model receipt 与 tool intent
epoch 1 / child        执行一次 replace_text，得到唯一 expected-effect revision
epoch 1 / child        写入并 fsync crash marker，os._exit(87)，不提交 tool receipt
parent / recovery      重开 SQLite、Artifact、Snapshot 与 Retrieval adapter
parent / recovery      确认 exactly one pending write intent，标记 TOOL_CALL_UNKNOWN
parent / fencing       释放旧 lease，epoch 2 获取新 lease
epoch 2 / recovery     对比 pre-effect / expected-effect / diverged，精确 accept existing effect
epoch 2 / recovery     原子补一个 recovery receipt；再次 reconcile 为 safe_to_resume
epoch 2 / runner       从下一条 scripted action 继续，不重派第 2 次模型调用
epoch 3 / runner       第一个 WorkItem 边界重启
epoch 4 / runner       第二个 WorkItem 边界重启并完成后续 A/B 行为
```

子进程不是一个只改测试夹具的旁路。它重开同一 `control.sqlite3`、Campaign ledger、CAS Artifact
和 revision-aware Retriever，复用 `CodingAgentRunner`、`ScriptedModelGateway` 与
`WorkspaceToolGateway`。Gateway 完成写入后，包装器先把以下 marker 规范化写入新文件并 fsync：

- durable tool call ID；
- 固定退出码 87；
- 模型调用序号；
- 子进程 PID；
- Scripted Action 的内容寻址引用；
- 工具名与 effect 后 workspace revision。

随后直接 `os._exit`，因此正常 tool receipt 代码不会运行。父进程只在退出码、marker、持久 intent、
工具名、调用序号和 action ref 全部一致时进入恢复；出现零个或多个悬空写入、marker 漂移、旧 lease
仍可写、live workspace 不是精确预态/后态等情况都会失败，而不是猜测继续。

## 3. 报告合同

Run A/B report schema v2 为每条 arm 增加 `hard_crash` 证据：

| 字段 | 必须证明的事实 |
|---|---|
| `observed_exit_code` | 子进程确实在固定边界以 87 退出 |
| `crashed_worker_epoch` / `recovery_worker_epoch` | 恢复使用紧邻的新 epoch，旧 Worker 被围栏 |
| `pending_tool_call_id` | marker、unknown 事件和最终 receipt 指向同一 intent |
| `scripted_action_ref` / `crash_marker_ref` | 故障输入与进程证据均为内容寻址 Artifact |
| `reservation_without_receipt` | 崩溃时只有 intent，没有成功/失败 receipt |
| `conservative_unknown_recorded` | 恢复前先进入阻塞 unknown，而非乐观重试 |
| `expected_effect_present` / `exact_effect_accepted` | live workspace 恰好是 manifest 推导的唯一后态 |
| `recovered_without_model_redispatch` | 恢复前后 model call 数量未增加 |
| `one_recovered_tool_receipt` | 同一 call ID 最终只有一个带 disposition 的 receipt |

`RunABArmResult.worker_restarts` 同时统计一次硬崩溃和两次协作式边界重启，因此本案例每条 arm
必须为 3，`final_lease_epoch` 必须为 4。报告仍要求完整 Trace replay、source workspace unchanged、
最终 workspace revision 与 active session 一致，以及 unknown model/tool/open reservation 全部为 0。

## 4. 本地可信结果

本地运行使用固定 382-file checkout 与严格 test-only Python 验收器。初始 checkout 的三项 required
check 均失败；核心 evaluator 返回 `all_expectations_met=true`。一次性的结果打印器随后因读取了
不存在的旧字段名而以 `AttributeError` 退出；没有把这个辅助脚本错误当作成功，也没有重跑核心
任务。之后直接从落盘 SQLite/CAS 进行只读复核，逐项重放 Trace、核对 marker/receipt/revision、
事件数量、租约序列和上游 Git clean 状态，复核命令通过。

| 指标 | Baseline | Single replan |
|---|---:|---:|
| Run | `run_d1a2715058c1477b86f58a34827e54ae` | `run_81da5a12550d481d93f27b207fcb2cf2` |
| 最终状态 | `WAITING_FOR_USER` | `SUCCEEDED` |
| Worker restarts / final epoch | 3 / 4 | 3 / 4 |
| Plan replans | 0 | 1 |
| Model calls | 10 | 13 |
| Tool calls / steps | 12 / 12 | 18 / 18 |
| 恢复 disposition | `accept_replace` | `accept_replace` |
| unknown model/tool/open | 0 / 0 / 0 | 0 / 0 / 0 |
| Event count | 128 | 171 |
| Trace ref | `fdf3dca9d4ad0de7840e7cd32681daee2f40f276454064fb5b177c979c083a26` | `3602869c4daf5b9df7eba2ec19ef940615c624838b25a1db5e1a98a16af7d2f7` |

两条 Trace 都包含一次 `TOOL_CALL_UNKNOWN`、四次顺序 `LEASE_ACQUIRED`（epoch 1～4）和一个
`accept_replace` receipt；最终投影与 JSONL replay 投影相同。Treatment 的三个 required checks
全部通过；Baseline 完成两个 production WorkItem 后按既有 NoProgress 合同安全等待。源 checkout
在运行后仍为固定 HEAD 且 `git status --porcelain --untracked-files=all` 为空。

## 5. 自动化验证

快速集成测试使用小型 shell fixture，同时覆盖两条成功 arm：真实 child exit、unknown→accept、
无模型重派、一个恢复 receipt、后续 WorkItem 继续执行、Trace replay 和 source unchanged。合同测试
拒绝以下配置：schema v1 带硬崩溃字段、只有一条 arm 配置故障、两条 arm 的故障点或写参数不同、
非可恢复工具、故障点与边界重启重合。

公开工作流会重建精确 Luigi commit，在 `python:3.12-alpine` 且 `--network none` 的生产 Docker
验收路径运行独立一例 suite，并上传完整状态目录。当前尚未获得这次新增 suite 的公开 Docker
结果，所以 `horizon doctor` 中
`source_bound_run_ab_multi_stage_hard_crash_docker_verified_count` 保持 0；本地可信结果不能冒充
Docker 通过。工作流成功后才允许把该值提升为 1 并补充 run、artifact ID 与 digest。

## 6. 明确边界

当前可以声称：一个作者选择的完整上游任务，已真实经历“写入 effect 已发生、receipt 未提交”的
进程退出，并沿产品主链路完成保守隔离、lease fencing、精确接纳、无模型重派和后续多阶段执行。

当前不能声称：

- 任意指令、任意工具、容器/主机断电或多个并发悬空副作用都可恢复；
- `accept` 是自动策略；这里是 evaluator 对冻结 expected effect 的受信任决定；
- Scripted Model 结果代表真实模型能自主定位、修复或选择 replan；
- 单个 Luigi 案例代表 BugsInPy、SWE-bench 或任何官方基准成绩；
- 一次精确 accept 构成通用 exactly-once 语义。

下一步只在主链路需要时扩展故障窗口。当前不新增分布式队列、向量数据库、多 Agent supervisor、
通用工作流 DSL 或自动恢复策略学习，避免把一个可解释的可靠性贡献稀释成平台工程。
