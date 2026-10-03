# Horizon 逐项需求追踪矩阵

版本 0.2.0，日期 2026-09-19。覆盖[主设计](coding-agent-development-design.md)原 39 项 FR、10 项 NFR，以及[Agent 能力设计](agent-capabilities-design.md)新增 30 项 FR，共 79 项。

本表是完整开发与验收导航。2026-09-30 已开始基础内核实现，实际证据列记录当前子范围与剩余缺口。目标路径相对 `src/horizon/`；计划测试路径相对 `tests/`；实际证据列的 `tests/` 路径相对仓库根。同一测试文件可验证多个 ID，但不能把子范围通过等同于整项通过。

当前结果：2026-09-30 可靠性内核为 **92 项测试通过**；2026-10-03 的最新离线回归为
**282 passed，5 skipped**，此前还单独补跑 **5 项真实 Docker 合同并全部通过**；另已完成一次 Tool Calling 探针和一次受预算
保护的真实单 WorkItem Agent fixture Run。完整长程 Agent 未实现，未做独立安全复核。命令、环境、证据与限制见
[开发进度](development-progress.md)。仍为“待填”的条目没有实现证据；“部分/初步”不代表
完整需求通过。普通工作直接自检，关键风险及用户指定验收按需独立复核，
不自动启用三角色。原里程碑与 Reviewer 列保留为历史导航，不构成固定流程要求。

## 原功能需求：保留全部 39 项

| ID | 里程碑 | 目标模块 | 计划检查与测试文件 | 实际证据 / Reviewer |
|---|---|---|---|---|
| FR-001 | M1 | `domain/task.py` | `unit/test_task_spec.py`：缺字段/错误预算拒绝，合法合同可序列化 | 初步实现：`domain/task.py`；`tests/unit/test_contracts.py`（字段、预算、权限、不可变性）；仅 Schema 自检 |
| FR-002 | M1 | `orchestration/planner.py` | `unit/test_plan.py`：每项包含目标/依赖/产物/验收 | 部分：`domain/plan.py` 与 `application/planning.py` 支持人工计划或一次 Tool Calling 生成 1～8 项 DAG；执行模型可显式提交一次剩余 DAG revision；控制器校验 Schema/依赖/验收唯一归属/工具并记录来源。首个真实 Plan 因三项复用一个最终验收而不可执行，该结构现会在准入时拒绝；历史 Trace 仍可按旧合同重放。无计划成功率、自动触发或多次修订 |
| FR-003 | M1 | `orchestration/plan_validator.py` | `unit/test_plan.py`：环、自依赖拒绝 | 已实现 DAG 校验：`domain/plan.py`；`tests/unit/test_contracts.py` 覆盖重复 ID、自依赖、未知依赖和环 |
| FR-004 | M1 | `orchestration/planner.py` | `integration/test_plan_revision.py`：修订追加事件，历史不覆盖 | 已实现连续版本事件及单次执行期 revision：`domain/run.py`、`application/services.py`；完成项不可变，工具 receipt、Plan vN+1、新 session 同事务；旧版本与 Trace 重放保留 |
| FR-005 | M1 | `domain/plan.py`, `application/agent_loop.py` | `integration/test_agent_loop.py`：前置未通过不得派发，项间会话/权限切换，边界恢复 | 部分：按 Plan 声明顺序选择首个 dependency-ready WorkItem，原子提交 pass→RUNNING→下一 session；单次 replan 可替换剩余项并保留完成项。无并行、自动优先级、自动/多次 replan 或饥饿策略 |
| FR-101 | M1 | `adapters/persistence/sqlite.py` | `integration/test_event_store.py`：事务回滚后状态/事件一致 | 部分：SQLite 原子追加/幂等回执/缓存已实现；`tests/integration/test_event_store.py`、`tests/fault_injection/test_database_crash.py` |
| FR-102 | M2 | `adapters/workspace/promotion.py` | `fault_injection/test_step_boundary.py`：完整 checkpoint 间的写入可从产物链恢复 | 部分：promotion intent 先于源文件副作用提交；1～8 个既有文件提升可从完整 effect 补 receipt，或从每个目标均为 before/after 的部分 effect 继续，真实子进程退出已覆盖；通用步骤链未覆盖 |
| FR-103 | M2 | `domain/checkpoint.py` | `integration/test_checkpoint.py`：显式与每 N 步触发都有有效 manifest | 部分：`application/checkpoints.py` 原子提交文件快照引用；Agent submit 后自动提交 checkpoint；`tests/integration/test_snapshots.py`、`test_agent_loop.py`；每 N 步策略未接入 |
| FR-104 | M2 | `application/resume_run.py` | `integration/test_rebuild.py`：删除投影后恢复得到相同规范化状态 hash | 部分：PLANNING/READY、安全执行轮次、人工指导后的 session 及已持久化 model response receipt 可由新 Worker继续，且不重复旧模型调用；确定性和子进程硬退出均覆盖；任意崩溃窗口未完成 |
| FR-105 | M2 | `orchestration/recovery.py` | `fault_injection/test_pending_operations.py`：模型/工具/验证悬空操作逐项对账 | 部分：Campaign-only 释放、可信 response receipt 补账续跑、只读显式重试；`replace_text` 与最多 8 文件的结构化 `apply_patch` 可基于派发前 manifest 精确 accept/rollback；Docker `run_check` 以 tool call ID 绑定标签 attempt，可查询/显式停止/删除后丢弃未知结果，missing 时仍需人工停止确认；不推断 pass/fail、不重放。部分 patch、无停止证明的 check 与任意写工具仍保持 unknown |
| FR-106 | M2 | `adapters/vcs/git.py` | `fault_injection/test_workspace_restore.py`：HEAD/diff/untracked 内容损坏不被接受 | 部分：内容寻址快照恢复；promotion 绑定源/候选 revision，且仅当 source 自身为 Git 根时绑定并复核 HEAD；外部 drift 拒绝，未恢复 Git objects/untracked 全状态 |
| FR-107 | M2 | `orchestration/recovery.py` | `fault_injection/test_unknown_effect.py`：未知副作用不重复派发，查询或人工对账 | 部分：unknown 阻止重派；只读可显式重试；`replace_text` / `apply_patch` 仅在 live workspace 精确等于前态/唯一后态时 accept/rollback，部分写入和额外漂移继续阻塞；`run_check` 已有确定性 attempt 身份、标签核验与显式停止，尚无容器创建前的持久启动回执或检查结果恢复 |
| FR-201 | M3 | `context/builder.py` | `unit/test_context_layers.py`：必保/事实/近期/历史内容正确分层 | 部分：`application/context.py` 固定保留初始合同和所有未完成工具对，优先保留最近 N 个完整单元；若硬字符上限仍超限，则确定性折叠最少数量的最老近期完整单元。尚非设计中的完整六层 ContextPack |
| FR-202 | M3 | `domain/context.py`, `application/context.py` | `unit/test_context.py` + `integration/test_agent_loop.py`：必保 ID 与内容 hash 均一致 | 部分：`MandatoryFactLedger` v2 对 Task/Plan/当前 WorkItem/已完成 WorkItems、路径权限、required acceptance、预算、模型策略、工具 Schema 和 workspace revision 做内容寻址绑定，并在压缩/恢复前后机器校验；尚无模型语义摘要的事实等价 QA，也未纳入 HITL 决定 |
| FR-203 | M3 | `context/compactor.py` | `integration/test_compaction_trace.py`：范围/模型/token/校验记录完整 | 部分：每次调用保存 ContextProjection Artifact、字符上限、近期单元数、源/投影消息数和 request hash；尚无 tokenizer 或模型摘要元数据 |
| FR-204 | M3 | `context/projector.py` | `integration/test_compaction_trace.py`：压缩前后原始事件字节摘要不变 | 部分：完整 canonical transcript Artifact 不被投影覆盖，projection 保存 source digest；`tests/integration/test_agent_loop.py` 验证完整会话长于模型视图且可恢复 |
| FR-205 | M3 | `context/compactor.py` | `fault_injection/test_compaction_failure.py`：坏摘要拒绝，旧上下文保全 | 部分：孤立/重复/错配工具结果、不可压缩前缀或 incomplete 单元超预算、Artifact 损坏和恢复重算不一致均拒绝；近期完整单元可整体应急折叠，122,665 字符 Pilot 尺寸已有回归。没有语义摘要，因此语义 QA 尚未实现 |
| FR-301 | M3 | `domain/budget.py` | `unit/test_budget.py`：逐项验证 token/费用/调用/步骤/时间/返修上限 | 部分：Run 已同时约束模型/工具/step/token/repair，并用绑定 CNY policy 记录模型费用；Campaign 跨 Run 限额；`test_budget.py`、`test_campaign_budget.py`、`test_model_run_accounting.py`；TaskSpec 多币种迁移未完成 |
| FR-302 | M3 | `domain/budget.py` | `unit/test_budget.py`：软阈与硬上限具有不同动作 | 待填 / 未审核 |
| FR-303 | M3 | `orchestration/policies.py` | `integration/test_budget_gate.py`：软阈动作可见，硬上限后新调用为 0 | 部分：模型/工具派发前执行 Run 与 Campaign 硬门禁，unknown 阻止后续调用；软阈动作未实现 |
| FR-304 | M3 | `trace/projector.py` | `integration/test_budget_replay.py`：重建账本与摘要完全一致 | 部分：`domain/run.py` 可重建通用、模型 CNY、工具和 unknown 账本；真实 Run JSONL 重放 projection hash 与 SQLite 一致；完整跨组件恢复仍待实现 |
| FR-305 | M3 | `domain/budget.py` | `fault_injection/test_unknown_cost.py`：未知费用不结算为 0 | 部分：Run 与 Campaign 均保留 unknown 占用；reconciliation 可补齐已存在的可信 Run receipt，但供应商主动查询/迟到回执尚未接入 |
| FR-401 | M4 | `tools/gateway.py`, `application/agent_loop.py` | `integration/test_agent_loop.py`：逐项验收与最终 required 全量回归，缺结构化结果拒绝 | 部分：中间项运行自身 acceptance，最后项重跑全部 required checks；控制器可执行注册检查而模型仍受当前项 ID 限制；多类型检查/环境分类未实现 |
| FR-402 | M4 | `validation/engine.py` | `e2e/test_completion_gate.py`：submit 只触发验收，失败不能成功 | 已在首个闭环实现：自然语言完成不被接受，`submit` 只触发 protected validation；`test_agent_loop.py` 与真实 fixture Run |
| FR-403 | M4 | `validation/reporters.py` | `integration/test_validation_report.py`：命令/退出码/时间/输出/产物俱全 | 部分：保存 check ID、exit code、timeout、bounded output/hash、Artifact 和工作区 revision；尚未记录独立起止时间/环境分类 |
| FR-404 | M4 | `orchestration/recovery.py` | `e2e/test_repair_loop.py`：断言/超时/环境错误分类后正确反馈 | 部分：失败证据有界回送模型并从 VALIDATING→REPAIRING→RUNNING；`test_agent_loop.py`；细粒度错误分类未实现 |
| FR-405 | M4 | `orchestration/policies.py` | `e2e/test_repair_loop.py`：返修耗尽终止，保留最后 patch/失败报告 | 部分：repair 计数和硬上限已接入；耗尽终止路径存在，完整故障矩阵与终态包未完成 |
| FR-406 | M4 | `domain/run.py`, `application/services.py`, `application/agent_loop.py` | `integration/test_agent_loop.py`：全部 WorkItems 与最终 required checks 通过才成功；后项回归前项触发 repair | 部分：顺序 DAG 的中间交接和最终成功均原子提交，最终全量回归已由 Fake E2E 验证；真实付费+Docker 证据仍只有历史单项 fixture，无并行或真实长程任务结论 |
| FR-501 | M1/M5 | `domain/events.py` | `contract/test_trace_schema.py`：唯一 ID、连续提交 seq、父引用、Schema 合法 | 初步实现：`domain/events.py`、`domain/run.py`；连续 seq、因果链、Schema 与哈希校验；当前控制面自检 |
| FR-502 | M3/M5 | `adapters/miniswe/model.py` | `contract/test_model_trace.py`：模型/参数/用量/延迟/请求 ID/状态，且无秘密 | 部分：Run Trace 持久化 policy、request hash、模型、usage、费用、response/trace ID、finish reason 和完整 response Artifact 引用；完整延迟和生产级脱敏审计未实现 |
| FR-503 | M1/M4 | `tools/gateway.py` | `integration/test_tool_trace.py`：参数/授权/起止/状态/输出闭合 | 部分：`tools/gateway.py` + Run 事件记录参数 hash、授权后 intent、状态、输出/Artifact、前后 revision；时间戳由事件提供，崩溃后工具副作用对账未完成 |
| FR-504 | M5 | `trace/exporter.py` | `integration/test_trace_export.py`：JSONL 可逐行校验并重读 | 当前控制面已实现 JSONL 导出；`horizon demo run` 还把 Trace、最终投影、报告和摘要写入带大小/SHA-256 的 EvidencePack，并在返回前重读文件、重放 Trace：`application/portfolio_demo.py`、`tests/integration/test_portfolio_demo.py` |
| FR-505 | M5 | `trace/replay.py` | `e2e/test_projection_replay.py`：禁止模型/执行依赖仍能重建状态和账本 | 部分：控制面及模型/工具/验证/checkpoint 事件可在无模型、无 Docker下重放；真实 fixture JSONL 与 SQLite projection hash 一致；Execution Fork 未实现 |
| FR-506 | M5 | `application/replay_run.py` | `e2e/test_execution_fork.py`：新 Run/预算/工作区；父事件产物 hash 不变 | 待填 / 未审核 |
| FR-601 | M0/M4 | `adapters/sandbox/swerex.py` | `contract/test_sandbox.py`：默认容器执行，未授权无宿主执行路径 | 部分：`adapters/sandbox/docker.py` 无宿主 Shell 回退，已接入 `agent run` 保护验收；5 项真实 Docker 契约和一次真实 Agent fixture；SWE-ReX 未接入 |
| FR-602 | M4 | `tools/gateway.py` | `integration/test_command_limits.py`：超时、超量输出、越界 cwd 被约束 | 部分：`adapters/sandbox/docker.py` 的超时、输出上限、临时目录边界；`tests/contract/test_docker.py`；非完整安全验收 |
| FR-603 | M2/M4 | `application/cancel_run.py` | `e2e/test_cancel.py`：所有非终态取消，持久标记后派发为 0 | 部分：控制面取消使终态与 Lease 失效；`tests/integration/test_event_store.py`；在途工具联动未接入 |
| FR-604 | M2/M4 | `approval/service.py` | `e2e/test_approval.py`：未批准高风险调用为 0，永久禁令不可审批绕过 | 待填 / 未审核 |
| FR-605 | M5 | `trace/projector.py` | `e2e/test_terminal_artifacts.py`：成功/失败/取消均有摘要及现有证据引用 | 部分：成功 Agent Run 有 checkpoint、validation evidence、模型/工具 Artifact 和终态摘要；固定离线作品集成功路径已有自检 EvidencePack。失败/取消终态包仍未统一 |

## 原非功能需求：保留全部 10 项

| ID | 里程碑 | 目标模块 | 计划检查与测试文件 | 实际证据 / Reviewer |
|---|---|---|---|---|
| NFR-001 | M2～M5 | `orchestration/recovery.py` | `fault_injection/`：原 8 类故障全覆盖，可恢复样本真实恢复，阻塞单列 | 部分：确定性集成覆盖多种悬空/冲突；真实进程硬退出覆盖数据库提交、模型双账本、response Artifact、单/多文件精确写 effect 和 promotion receipt 等窗口；仍非完整统一矩阵 |
| NFR-002 | M1 | `adapters/persistence/sqlite.py` | `integration/test_event_store.py`：重启/并发追加无重复或丢失已提交 seq | 当前事件存储已自检：`tests/integration/test_event_store.py`、`tests/fault_injection/test_database_crash.py` |
| NFR-003 | M2 | `tools/gateway.py` | `fault_injection/test_idempotency.py`：重复同键仅一个已提交结果 | 部分：管理命令幂等且回执固定历史 seq；工具已有唯一 call ID 和 intent/receipt，但副作用重派 idempotency 与崩溃对账未实现 |
| NFR-004 | M1/M5 | `trace/projector.py` | `contract/test_state_causation.py`：每个状态迁移有源事件 | 控制面及当前模型/工具/验证/Agent 状态均由事件重放；真实 JSONL 与 SQLite projection hash 一致 |
| NFR-005 | M0/M5 | `adapters/sandbox/`, `interfaces/cli/` | Windows 入口 + Linux Docker 实测；`e2e/test_platform_workflow.py` | 部分：Windows CLI → SiliconFlow → staging → Linux Docker 的单 fixture E2E 已执行；长期/恢复/其他平台未验证 |
| NFR-006 | M1 | `domain/ports.py`, `adapters/sandbox/fake.py` | `contract/test_ports.py`：Fake Model/Sandbox 可驱动全循环 | 部分：Scripted Fake Model + fake protected check 驱动成功与 repair 全循环；`test_agent_loop.py`；统一 Fake Sandbox 契约未完成 |
| NFR-007 | M0/M4 | `adapters/` | `contract/test_dependency_boundaries.py`：模型/环境替换无领域层修改，上游依赖不泄漏 | 部分：domain 只依赖 Model/Tool/Acceptance ports；Fake 与 SiliconFlow adapter 均驱动同一 loop；上游 mini-SWE-agent/SWE-ReX 仍待验证 |
| NFR-008 | M1/M5 | `trace/`, `tools/gateway.py` | `integration/test_redaction.py`：密钥/环境值/禁止路径内容不进入 Trace 导出 | 部分：Key 不进 repr/body/CLI/账本/Run Trace，禁止路径不被工具读取；真实 Trace 已导出检查；生产级消息脱敏和独立安全复核未完成 |
| NFR-009 | M0/M5 | `config.py`, `benchmarks/` | 复现脚本验证代码 SHA/依赖/镜像/模型/任务/种子 manifest | 部分：`uv.lock`、Provider policy/PriceCard hash、TaskSpec hash、workspace revision 和真实模型 usage 已记录；Pilot preflight 绑定 clean Git commit、source manifest、prepared Task、Provider config、Docker image digest、初始失败、费用上限及当前加载的 Horizon Python 源码指纹，启动时重算，旧无指纹报告拒绝付费启动。真实失败 Run 已保存全部模型/工具 receipt 与可重放 Trace；依赖 lock 尚未并入同一 report，也未定义随机种子，仍不是完整统一 manifest |
| NFR-010 | M5 | `interfaces/cli/eval.py` | `e2e/test_batch_sequential.py`：单机顺序任务均生成终态/记录，无并发性能承诺 | 待填 / 未审核 |

## Agent 能力增量：30 项

| ID | 里程碑 | 目标模块 | 计划检查与测试文件 | 实际证据 / Reviewer |
|---|---|---|---|---|
| FR-701 | M1 | `intake/router.py` | `unit/test_intent_router.py`：结构化路由、原文依据和 admission 费用关联 | 待填 / 未审核 |
| FR-702 | M1/M4 | `intake/router.py`, `tools/gateway.py` | `e2e/test_readonly_intent.py`：只读/计划不写；取消/状态零模型调用 | 部分：TaskSpec/Plan 与 Typed Gateway 执行权限交集已自检，控制 CLI 零模型；自然语言意图入口未实现 |
| FR-703 | M1/M2 | `orchestration/plan_validator.py` | `integration/test_plan_coverage.py`：遗漏/环/非法验收拒绝，澄清可恢复 | 部分：required 验收覆盖、每项验收唯一归属、DAG、工具权限与最多 8 项限制已用于人工/模型计划；非法模型提案留 receipt、不自动重试，并持久请求人工计划；自然语言澄清未实现 |
| FR-704 | M1/M4 | `domain/task.py` | `integration/test_spec_revision.py`：合同历史保留、Run 版本绑定、影响检查失效 | 部分：合同版本/用户来源/精确绑定与保守验证失效已实现；`tests/integration/test_event_store.py`；完整变更执行未接入 |
| FR-801 | M3 | `domain/memory.py`, `application/memory.py` | `integration/test_run_memory.py`：Run scope、事件/调用/任务/WorkItem/revision/evidence/置信属性与有界快照 | 部分：run-scope `RunMemoryEntry/Snapshot` 已接入每次模型调用；没有 Repo scope、跨 Run recall 或持久查询服务 |
| FR-802 | M3 | `application/memory.py` | `integration/test_run_memory.py`：模型 submit 声明排除，失败 outcome 保留，unknown 不产生证据 | 部分：只允许控制器结算的工具事件派生 observed 条目；无 Project promotion、人类确认或候选审核流程 |
| FR-803 | M3 | `application/memory.py`, `application/context.py` | `integration/test_run_memory.py` + `unit/test_context.py`：revision 变化标 stale，stale excerpt 不注入，证据 hash 不符拒绝 | 部分：当前按整个 workspace revision 保守失效；尚无文件/lockfile/环境依赖重检、撤销或冲突合并 |
| FR-804 | M3/M5 | `domain/memory.py`, `application/agent_loop.py` | `integration/test_run_memory.py`：scope ID 必须等于 source Run，快照绑定 Task/WorkItem；恢复按历史事件边界重建 | 部分：当前只能同 Run 注入，结构上阻止跨 Run 泄漏；尚无 repo identity、benchmark instance/variant namespace 或跨 Run 隔离实验 |
| FR-901 | M3 | `retrieval/retriever.py` | `integration/test_retrieval.py`：三路召回融合，片段数/token 有界且带来源 | 部分：`adapters/retrieval/sqlite_fts.py` 实现 path+文本 FTS5/BM25，最多 8 个带 path/range/hash 的 chunk；camelCase/snake_case 双向词项和最多 256 候选的同名 `def/class` 优先已测；固定 5 案例与 youtube-dl 2 案例记录 Hit/Recall/MRR/失败证据；尚无 AST/symbol/memory 三路融合、tokenizer 预算或外部盲测 |
| FR-902 | M3 | `retrieval/indexer.py` | `integration/test_dirty_index.py`：HEAD 不变的编辑、删除、恢复均切换正确 revision | 部分：index key 绑定 immutable manifest、workspace revision、path scope 和 chunker 配置；dirty revision 与旧 manifest 隔离、派生缓存有界并可重建；删除/恢复完整矩阵未测 |
| FR-903 | M3 | `retrieval/evidence.py` | `integration/test_evidence_freshness.py`：错误权限/hash 拒绝，使用证据可追溯 | 部分：EvidencePack 记录 revision/manifest/scope/index，Gateway 应用 allow/deny；每个 FTS 命中回查 immutable file Artifact，篡改、越权路径、行号/hash/content 不一致拒绝；生产脱敏未完成 |
| FR-904 | M3 | `retrieval/retriever.py` | `fault_injection/test_retrieval_fallback.py`：empty/degraded 分开、无虚构/混版本证据 | 部分：ok/empty/degraded 分离，非 UTF-8/大小预算显式降级；无 FTS 时 bounded lexical scan 并标 `fts5_unavailable`；尚未测存储中断与真实任务降级质量 |
| FR-1001 | M0/M4 | `tools/registry.py`, `tools/gateway.py` | `contract/test_tool_adapter.py`：只提议未执行；网关禁止后无副作用 | 部分：模型只返回 typed tool proposal；未知/未授权工具由 Gateway 拒绝；Fake 与真实模型闭环均验证；`retrieve_code` 也只能经 Gateway 返回 EvidencePack |
| FR-1002 | M1/M4 | `tools/schemas.py`, `gateway.py` | `unit/test_tool_validation.py`：未知工具/非法参数/越界/旧版本/超预算拒绝 | 部分：Pydantic 参数、path allow/deny、symlink、大小、工具 allowlist、Run 预算门禁已实现；`read_file` Schema/错误回执强制行号上下界成对；expected file hash/旧 revision CAS 未实现 |
| FR-1003 | M2 | `tools/gateway.py` | `fault_injection/test_fenced_dispatch.py`：旧 epoch 无效，同调用意图与 receipt 可对账 | 部分：Lease epoch、工具 intent/receipt/unknown 已接入；只读可取消重试；`replace_text` 与最多 8 文件的结构化 `apply_patch` 可精确 accept/rollback；`run_check` 把 reservation call ID 传到 Docker 标签并可控制器核验/显式停止；任意 diff、新增/删除、missing attempt 证明和真正多副作用事务未完成 |
| FR-1004 | M2/M4 | `adapters/workspace/promotion.py` | `e2e/test_staged_workspace.py`：命令修改禁止路径只在临时副本，权威树不变 | 部分：staging 与源隔离；`agent diff/promote` 复核路径权限、源/候选 revision 和可选 Git HEAD，允许显式提升 1～8 个既有 UTF-8 文件并恢复可证明的部分 effect；不支持新增/删除/重命名/commit |
| FR-1005 | M3/M4 | `context/message_pairs.py` | `contract/test_tool_pairs.py`：多调用/失败/取消/摘要后无孤立消息 | 部分：ContextProjector 把 assistant tool calls 与全部 results 当作不可拆单元，孤立/重复/错配结果拒绝；只读与精确 write 恢复会追加完整 observation；更广多调用崩溃矩阵未完成 |
| FR-1101 | M2 | `approval/service.py` | `integration/test_approval_binding.py`：请求及参数/版本绑定在重启后保留 | 部分：`domain/human.py` + `application/services.py` 已把非法规划请求绑定 response/Task/version；NoProgress 请求绑定 tool evidence、Task/Plan/WorkItem/workspace/session，事件重放后保留；仍非通用请求模型 |
| FR-1102 | M2 | `approval/service.py` | `fault_injection/test_approval_resume.py`：重复点击单次消费，变参变版本失效 | 部分：replacement Plan 与 operator guidance 均单事务消费；hash/version/session 不匹配拒绝。指导保持 iteration、重置 streak 并由新 Worker 续跑；尚无过期/stale 和通用重复决策矩阵 |
| FR-1103 | M2/M4 | `approval/control_channel.py` | `e2e/test_approval_actor.py`：模型/仓库伪造决策拒绝，可信 CLI 可提交 | 部分：本机 `plan set` 与 `agent guide` 持久化 local_cli actor/decision/request；`agent resolve-tool` 与 promotion 仍是各自窄入口；无多用户身份或通用审批渠道 |
| FR-1104 | M2/M3 | `approval/service.py`, `cancel_run.py` | `integration/test_wait_deadline.py`：等待零调用、超时/取消关闭、按 resume_state 回转 | 部分：规划失败与 NoProgress 均原子进入 WAITING、保存准确 resume_state 并释放 Lease，等待零模型调用；分别回转 READY/RUNNING，通用超时/过期策略未实现 |
| FR-1201 | M3 | `reliability/retry.py`, `breaker.py` | `fault_injection/test_retry_breaker.py`：总次数有界，breaker 重启持续，隐藏重试不倍增 | 待填 / 未审核 |
| FR-1202 | M3/M5 | `reliability/fallback.py` | `contract/test_model_fallback.py`：未授权/不兼容/超预算模型拒绝，正式比较不换模型 | 待填 / 未审核 |
| FR-1203 | M3 | `reliability/fallback.py` | `integration/test_degraded_summary.py`：原错误、降级策略、质量标记存在且必需检查不变 | 待填 / 未审核 |
| FR-1204 | M2/M3 | `reliability/classifier.py` | `fault_injection/test_conservative_failure.py`：未知费用和效果保守处理，存储失败/截止后停机 | 部分：恢复服务分类模型未知、模型响应缺失、工具未知和不安全会话边界；确定性的 Run/Campaign 派发前费用不足现在以结构化 `BudgetStop` 原子进入 `FAILED`，记录 required/available 并清除 Lease，unknown 用量不走该终态路径；不自动重试。迟到回执查询和统一 retry/breaker 仍待实现 |
| FR-1301 | M3 | `domain/context.py`, `domain/memory.py`, `application/context.py` | `unit/test_context.py` + `integration/test_agent_loop.py`：每次模型调用绑定 ledger、Run Memory、投影及恢复边界 | 部分：每次 reservation 绑定 schema-versioned ContextProjection、MandatoryFactLedger 和 RunMemorySnapshot Artifact；ledger 显式包含工具 Schema、Task/Plan/权限/验收/预算/策略/workspace hash，memory 绑定事件来源/revision/evidence；完整六层内容来源和逐层 token 预算仍未实现 |
| FR-1302 | M3/M5 | `context/compactor.py` | `unit/test_compaction_contract.py` + 冻结事实 QA：hash 保留和语义结果分开记录 | 部分：控制器事实 ledger 和旧单元/content hash 均确定性保留，完整 transcript 独立留存；当前明确无模型语义摘要和语义 QA，不能宣称事实等价 |
| FR-1303 | M1/M4 | `application/agent_loop.py`, `domain/plan.py` | `integration/test_agent_loop.py` + `benchmarks/reliability/horizon-controller-v1.yaml`：精确模式无进展反馈/等待，返修/replan 有界，合同验收不改动 | 部分：模型 iteration/repair 有硬上限；相同动作与 A/B period-2 先反馈、继续模式进入人工指导；模型可基于证据显式 replan 一次，完成项/合同/工具权限不变且原子恢复。冻结策略诊断 7 个 NoProgress Trace、5 个 replan 合同当前 41/41；没有 period-3+、语义检测、自动触发或多次修订 |
| FR-1304 | M4/M5 | `validation/engine.py` | `e2e/test_protected_validation.py`：不可改验收/伪造 passed，gold 答案不可检索 | 部分：模型只能提交 check ID，实际命令来自 TaskSpec 并由 Docker executor 运行；模型文本不能直接通过。首个 youtube-dl Pilot 把 fixed commit/fix URL/solution marker 留在 controller-side manifest，检查它们不进入 acceptance-visible TaskSpec，并从 snapshot 排除 `.git`；启动时任务/source/image/policy 漂移均拒绝。尚非隐藏测试或独立泄漏审计 |
| FR-1305 | M5 | `benchmarks/`（仓库根） | 固定长程/检索/Memory manifests、A/B/C/D 与单变量关闭报告、恢复/阻塞分别计数 | 部分：固定 5 案例检索诊断、youtube-dl 2 案例命名变体诊断及其两次 Hit@5=0 负结果、12 案例/41 判断的控制器策略诊断、1 个内部完整 Run A/B、3 个 BugsInPy 依赖裁剪 A/B，以及 2 个干净完整 checkout A/B（tqdm 82 files、youtube-dl 872 files）均保留内容寻址证据；两个完整案例词法 RAG 目标均 rank 1 并保留 degraded。youtube-dl 两阶段案例形成 86/116-event Trace并保留 60 秒 Lease 负结果。四个真实模型 Run 均无编辑/验证：首轮以 `model_iteration_limit` 失败，6 次调用费用 CNY 0.0652398，Trace hash `7b553f09...898`；第一次复跑的 Plan/RAG 收敛到单项和目标文件，但 122,665 字符无范围读取触发上下文硬门限，3 次调用费用 CNY 0.032979，Trace hash `7841d09e...e1b6d5b`；第二次复跑中 Plan 猜测 `common.py` 压过两次 rank 1 `utils.py` 证据，第四次请求需预留 CNY 0.158592，被 Campaign 硬门限在派发前拒绝，3 次调用费用 CNY 0.0285024，Trace hash `503bba58...349f6`；第三次复跑的 RAG 稳定命中 `utils.py`，但三次单边 `read_file` 参数被校验拒绝，第六次模型请求需预留 CNY 0.067530、超过单 Run 余额，5 次调用费用 CNY 0.050352，Trace hash `ad88aada...a9`。四轮负结果分别驱动 Plan/搜索反馈、有界读取/应急压缩/租约释放、不猜路径/证据优先/rank-1 默认，以及读取范围成对约束/规划输入收窄。同一 CNY 0.18 Campaign 尚余 CNY 0.0681666，本系列累计实际费用 CNY 0.1770732；旧 preflight 已因源码修复失效。仍无 Memory A/B、外部盲测或真实模型收益结论，不构成官方 benchmark 验收 |

## 验收记录合同

逐项记录 `requirement_id -> implementation_ref -> evidence_ref -> verdict`。证据至少包括环境、依赖/镜像/模型配置、实际命令、退出状态、结果摘要和 Artifact hash；涉及操作流程的要求要有真实 E2E，不能只用模拟返回值。原计划文件名不要求机械补齐；实际合并模块或测试文件必须在证据列明确指向。

自检与独立复核分别标识。凡需要独立复核的结论不能由实现者自批；`failed / conditional / unverified` 均不能算通过。可选增强不替代上述强制要求。当前仍有大量未实施条目，不能宣称整体完成。
