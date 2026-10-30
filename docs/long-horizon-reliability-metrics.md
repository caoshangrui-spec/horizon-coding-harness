# 公开仓库长程任务可靠性指标

更新：2026-10-29。本页把 Horizon 已有的来源绑定 Run A/B 证据收敛为一组机器可验证、面试时
可直接解释的指标。指标由 `RunABEvaluator` 在完整 Agent 主链路结束后生成，不再由 CI 中的临时
脚本根据日志猜测。

它回答的是：在冻结 Scripted Model、公开上游源码、受保护验收和明确故障注入条件下，Harness
能否跨多个 WorkItem/Worker 保持 Trace、源目录隔离和恢复不变量。它不回答真实模型的自主修复
成功率，也不是 BugsInPy 或 SWE-bench 官方成绩。

## 1. 数据流

```text
公开仓库固定 commit
  -> 初始失败复现
  -> Baseline / Treatment 两条完整 Agent Run
  -> 可选 Worker 边界重启与真实写入硬退出
  -> protected validation
  -> 每条 Trace 脱离 SQLite 重放
  -> 每条 arm 的 acceptance / quality flag / degradation facts
  -> retrieval delivery / write-context lineage
  -> case reliability
  -> typed suite reliability + GitHub Actions summary
```

领域合同位于 `src/horizon/domain/run_evaluation.py`，聚合逻辑位于
`src/horizon/application/run_ab_eval.py`。终态 EvidencePack 与 Run A/B 现在复用同一个
status-independent facts 计算器，避免 required-check 和 degradation 在两种报告中出现不同口径。
旧的 arm JSON 可以没有 `reliability` 或嵌套 `retrieval_lineage`，旧的 case/suite JSON 可以没有
新增质量/lineage 字段；新运行会同时保存原始 arm 事实和聚合指标。冻结 suite/manifest 内容与
digest 没有被改写。

## 2. 指标定义

| 字段 | 分母或计算方式 | 严格含义 |
|---|---|---|
| `scenario_count` | suite 中的 case 数 | 执行场景数；同一上游 bug 以不同恢复配置出现时分别计数，不冒充独立任务数 |
| `observed_duration_ms` | 各 case 的 `perf_counter_ns` 观测之和 | 包含 snapshot、初始验收、两条 arm、恢复与最终验收；受 runner/磁盘影响，不是跨机器性能排名 |
| `p95_scenario_duration_ms` | case 耗时的 nearest-rank P95 | 小样本只用于发现明显慢例；不外推生产延迟分布 |
| `arm_count` | 每个场景固定 Baseline + Treatment | 实际完整 Run 数，不等于独立任务数 |
| `terminal_success_count` | 最终为 `SUCCEEDED` 的 arm | Baseline 按合同进入 `WAITING_FOR_USER` 时不会被包装成成功 |
| `reliability_evidence_arm_count` | 带 Trace-derived reliability facts 的 arm | 新报告必须覆盖全部 arm；0 只用于兼容旧报告，不能解释为验收已知 |
| `unknown_acceptance_arm_count` | 当前 Task/workspace 没有有效 required-check receipt 的 arm | 等待中的 Baseline 通常计入；不把“未验证”写成失败或通过 |
| `required_check_failure_arm_count` | 验收证据已知但 required check 未全过的 arm | 与 unknown acceptance 分开计数 |
| `quality_flagged_arm_count` | 至少一个 quality flag 的 arm | success 与质量状态正交；通过 required checks 仍可能带检索降级 |
| `clean_terminal_success_count` | `SUCCEEDED` 且无 quality flag | 与下一项之和严格等于 `terminal_success_count` |
| `quality_flagged_terminal_success_count` | `SUCCEEDED` 且带 quality flag | 明确表示“任务成功，但执行质量有降级”，不冒充 clean success |
| `fallback_arm_count` | 含至少一条 Trace-bound fallback history 的 arm | 当前生产执行仍为 0；不会从日志文本猜测 fallback |
| `degradation_arm_count` / `degradation_record_count` | 至少一条 degradation 的 arm / 全部记录数 | 同一 arm 多次真实降级调用会保留多条记录；NoProgress 拒绝回执不伪装成检索降级 |
| `retrieval_degraded_arm_count` | 带 `retrieval_degraded` 的 arm | 来自成功 `retrieve_code` 回执内的 typed EvidencePack，并与 call ID、Artifact、Task 和 acceptance hash 绑定 |
| `retrieval_lineage_arm_count` | 带 retrieval lineage 的 arm | 新报告必须覆盖全部 arm；0 只表示旧报告没有该证据，不能解释为“没有使用 RAG” |
| `successful_retrieval_count` / `delivered_retrieval_count` | 成功 EvidencePack 数 / 至少进入一个后续已保存模型投影的数量 | delivery 必须是 exact tool message，或 replan 后 active RunMemory top-two ranked-chunk excerpt；只有 Artifact ref 不算交付，且没有后续投影的成功检索不得伪报为 delivered |
| `successful_write_count` | 成功 `replace_text/apply_patch/create_file` receipt 数 | 只统计已结算成功写入，不包含 proposal、拒绝或 unknown effect |
| `write_context_bound_count` | source model context 中可见至少一条 retrieval 的成功写入数 | 证明投影绑定，不证明检索导致了写入决策 |
| `evidence_backed_write_count` | context-bound 且 EvidencePack revision 与写前 CAS revision 一致、全部 target path 被可见 chunk 覆盖的写入数 | 是结构化 provenance 门，不是代码正确性；正确性仍由 protected validation 决定 |
| `quality_flag_counts` | 每个 flag 覆盖的 arm 数 | 一个 arm 可有多个 flag，因此各项之和不等于 quality-flagged arm 数 |
| `worker_restart_count` | 两条 arm 的持久化 Worker 切换总数 | 包含 manifest 指定边界；硬崩溃场景还包含故障后的新 epoch |
| `hard_crash_arm_count` | 注入真实写入 effect-before-receipt 退出的 arm | 恢复成功率只以这些 arm 为分母；没有故障注入的 suite 报 0.0，而不是伪造 100% |
| `recovered_hard_crash_arm_count` | exact effect accepted、无模型重派且只有一个 recovery receipt | 只证明冻结故障窗口的恢复闭环 |
| `recovery_success_rate` | recovered / hard-crash arms | 当前不是任意崩溃成功率 |
| `recovery_redispatch_count` | 恢复期间重新派发已结算模型动作的观察数 | 目标为 0 |
| `duplicate_side_effect_count` | 未同时满足 exact accept、无重派、单一 receipt 的硬崩溃 arm 数 | 0 只代表已注入窗口未观察到重复写入，不构成通用 exactly-once |
| `trace_replay_rate` | 投影与 JSONL replay 一致的 arm / 全部 arm | 衡量 Trace 完整性；不证明外部服务事实真实性 |
| `source_workspace_unchanged_rate` | 固定上游 checkout 未变化的 arm / 全部 arm | 证明修改只发生在 staging workspace |
| `final_open_effect_count` | 最终 unknown model、unknown tool、open reservation 总数 | 非零时不能把 Run 当成已结清 |

报告固定携带 `claim_scope=source_bound_scripted_agent_runs`，并排除以下结论：

- `real_model_task_success_rate`；
- `official_bugsinpy_or_swe_bench_score`；
- `arbitrary_crash_exactly_once`。

## 3. 当前公开任务层级

| Suite | 公开来源 | 场景 | 长程/故障结构 |
|---|---|---:|---|
| `bugsinpy-reduced-v2` | Cookiecutter、FastAPI、tqdm、Luigi、Tornado | 5 | dependency-reduced A/B；用于较快回归 |
| `bugsinpy-full-checkout-pilot-v8`（历史精确质量基线） | v7 十二例 + Sanic-5、TheFuck-2、Luigi-2 | 15 | 9 个项目、4,491 tracked files；三个新增案例本地完整 Harness 为 3/3，生产目标检索均为 rank 1；统一质量聚合公开 run `40682108470` 已通过 15/15 |
| `bugsinpy-full-checkout-pilot-v9` | v8 十五例 + HTTPie-5、Tornado-2 | 17 | 9 个项目、4,806 tracked files；两个新增案例的本地 buggy/candidate 保护探针通过；公开 run `40794561712` 已通过 17/17 |
| `bugsinpy-full-checkout-pilot-v10` | v9 十七例 + HTTPie-1、Tornado-4 | 19 | 9 个项目、5,177 tracked files；两个新增案例的本地完整 Harness 为 2/2；公开 run `40799174668` 已通过 19/19 |
| `bugsinpy-full-checkout-pilot-v11` | v10 十九例 + Sanic-3、TheFuck-5、Luigi-7 | 22 | 9 个项目、6,062 tracked files；三个新增案例的本地完整 Harness 为 3/3；公开 run `40804131201` 已通过 22/22 |
| `bugsinpy-multi-stage-pilot-v4` | youtube-dl、Luigi、FastAPI | 3 | 每条 arm 为 3 WorkItem、2 次 Worker 重启；合计 6 条 Trace、12 次重启 |
| `bugsinpy-multi-stage-hard-crash-v2` | Luigi、FastAPI | 2 | v1 的 `replace_text` 原样保留，新增 `create_file`；4 条 arm 均真实写入后、receipt 前退出并继续后续 WorkItem；合计 4 次恢复尝试、12 次重启 |

同一 bug 在不同 suite 中用于不同实验变量，因此不能把当前 32 个场景宣传成 32 个独立真实任务。
当前 suite 共 32 个 manifest 场景、64 条 arm，现均有公开 Docker 统一验证；历史
v1/v2/v3/v4/v5/v6/v7 版本不重复累加。
按 `(benchmark, project, bug_id, buggy_commit)` 去重后，完整 checkout 是 22 个作者选择、gold 揭示后
制作端到端 manifest 的 development 案例；因此仍没有 20～30 个独立盲测/holdout 真实任务，也没有
真实模型成功率。32 个场景和 64 条 arm 不能改写成泛化样本量。

## 4. 运行与读取

以三个完整 checkout、三阶段场景为例：

```powershell
uv run --locked --cache-dir .uv-cache horizon eval run-ab-suite `
  benchmarks/run_ab/bugsinpy-multi-stage-pilot-v4.yaml `
  --image python:3.12-alpine `
  --state-dir .horizon/multi-stage-metrics
```

命令要求本机已经存在所指定镜像，不会自行拉取。CLI 的规范 JSON 中可直接读取：

```powershell
$result = Get-Content .horizon/multi-stage-metrics/cli-result.json -Raw |
  ConvertFrom-Json
$result.report.reliability
```

公开 `Source-bound Docker evidence` workflow 会运行 dependency-reduced、完整 checkout、三阶段和
写入硬崩溃四个 suite。Job Summary 现在直接展示三阶段的场景/arm/Worker 重启/Trace/耗时，以及
硬崩溃的恢复成功率、恢复重派、重复副作用、最终悬空 effect 和 P95。上传 artifact 保留各 case
report、JSONL Trace、content-addressed Artifact 与顶层 CLI JSON，可对摘要逐项复核。

2026-10-28 的公开统一复跑
[`40682108470`](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40682108470)
首次用生产 Docker 路径发布新增质量字段，以下是当时 v8 的精确结果：

| Suite | Cases | Arms | Unknown acceptance | Clean / flagged success | Known check failure | Retrieval-degraded arms | Degradation records |
|---|---:|---:|---:|---:|---:|---:|---:|
| dependency-reduced v2 | 5/5 | 10 | 5 | 5 / 0 | 0 | 0 | 0 |
| full-checkout v8 | 15/15 | 30 | 15 | 1 / 14 | 0 | 28 | 56 |
| multi-stage v4 | 3/3 | 6 | 3 | 0 / 3 | 0 | 6 | 27 |
| hard-crash v1 | 1/1 | 2 | 1 | 0 / 1 | 0 | 2 | 9 |

2026-10-29 的 v9 公开复跑
[`40794561712`](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40794561712)
把 full-checkout 升至 v9。workflow 明确门控 full-checkout 17/17、初始失败 17、Treatment 恢复 17、
unknown acceptance 17、known check failure 0，并要求质量标记与检索降级均有 Trace-bound 记录；
同一 run 也再次通过 dependency-reduced 5/5、multi-stage 3/3 与 hard-crash 1/1。此处不把未从
207,784,842-byte artifact 下载复算的 v9 精确 clean/flagged 与 degradation 数值填入表格。当前
统一 artifact ID 为 `12862738627`。

同日的 v10 历史公开复跑
[`40799174668`](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40799174668)
把 full-checkout 再升至 19/19，并门控初始失败 19、Treatment 恢复 19、unknown acceptance 19、
known check failure 0；同一 run 再次通过 dependency-reduced 5/5、multi-stage 3/3 与 hard-crash
1/1。统一 artifact ID `12865471959`，214,329,764 bytes，digest
`sha256:d33a1f98990849b6a0f1510b2d7728cf055f1dae0d0b6f9eef0f643fcddc3110`。此处同样不在未下载
214 MB artifact 的情况下追写 v10 精确 clean/flagged 与 degradation 数值。

随后同日的当前 v11 公开复跑
[`40804131201`](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40804131201)
把 full-checkout 再升至 22/22，并门控初始失败 22、Treatment 恢复 22、unknown acceptance 22、
known check failure 0；同一 run 再次通过 dependency-reduced 5/5、multi-stage 3/3 与 hard-crash
1/1。统一 artifact ID `12866973813`，234,783,960 bytes，digest
`sha256:f49be3f5a38d75524fbf74ec0fd761081c42320a14b1b7ef4de49e7d54bf06b3`，保留至
2026-11-28。此处同样不在未下载并复算该 artifact 的情况下追写 v11 精确 clean/flagged 与
degradation 数值。

上述 v8 质量聚合中，三阶段与硬崩溃 suite 还分别记录 12/6 次 Worker restart；硬崩溃的 2 条 arm 均恢复，恢复率
1.0、模型重派 0、重复副作用 0。统一 artifact ID `12813023460`，201,900,712 bytes，digest
`sha256:e1a37b2308e7992244d096cde9f629ceea5df3c2290d6cb1ea2e474bf6dd7735`，保留至
2026-11-27。

2026-10-30 的 v2 公开 run
[`41027080498`](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/41027080498)
把 hard-crash suite 升为 2/2：Luigi `replace_text/accept_replace` 与 FastAPI
`create_file/accept_create` 共 4 条故障 arm，恢复 4/4、recovery rate 1.0、redispatch 0、duplicate
side effect 0、Trace replay/source unchanged 4/4、final open effect 0。四组当前 suite 合计
32 个场景、64 条 arm，最终 summary gate 全部通过。统一 artifact ID `12964696027`，
260,301,498 bytes，digest
`sha256:bdcbd86bdcd2a64c6f9d8d447d83f5dad2d7144a04fc6236e6379be7cf1ea722`，保留至
2026-11-29。

## 5. 验收门槛

CI 不只检查 `all_expectations_met`。三阶段 suite 还必须满足：三个 timed scenario、Trace replay
rate 1.0、source unchanged rate 1.0、final open effect 0。写入硬崩溃 suite 必须满足：两个场景、
四条故障 arm 全部恢复、recovery rate 1.0、redispatch 0、duplicate side effect 0、Trace replay
rate 1.0、final open effect 0；同时逐案例锁定 `replace_text/accept_replace` 与
`create_file/accept_create`、崩溃/恢复 epoch、单一恢复 receipt 和 final epoch 4。四组 suite 还必须
让 reliability facts 覆盖全部 arm，并让 clean 与
quality-flagged success 之和等于原始 terminal success；dependency-reduced suite 固定为 5 个 clean
Treatment，完整 checkout 必须实际观察到 retrieval-degraded arm 与 quality-flagged success。
新报告还要求 retrieval lineage 覆盖全部 arm；完整 checkout 的每个案例都必须至少有一次 retrieval
进入后续保存的模型投影，并至少有一条成功写入同时满足 same-revision、target-path 和非空 snippet
覆盖。`delivered` 只能是成功检索的子集，门禁不要求凭空消费没有后续模型调用的结果。
三阶段 Baseline 虽已完成部分 WorkItem 检查，但当前 Task 的全部 required acceptance 尚无最终
receipt，因此保留为 `acceptance_evidence_unknown`；只有存在当前、已知且未全过的最终验收
receipt 才计入 `required_checks_failed`。任一门槛失败都会让证据发布步骤失败，而不是只在
Markdown 中显示警告。

首次新增质量门禁的公开 run
[`40673288132`](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40673288132)
已经成功执行并上传四组 suite，却因 CI 将上述 3 条 unknown 误期望为 known failure 而在摘要步骤
失败。门禁按 Trace 语义修正为 known failure 0，并增加失败时打印全部 suite reliability facts；
随后 run `40682108470` 全部通过。这里保留第一次失败，避免把门禁错误包装成任务执行失败或静默
删除负结果。

首次加入逐案例 lineage 门的公开 run
[`40858869570`](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40858869570)
四组执行 suite 均通过、62/62 arm 有 lineage、88/88 成功检索进入后续模型投影，但只有 18 个完整
checkout 案例产生 evidence-backed write。缺口是 HTTPie-2、Sanic-3、TheFuck-5、Luigi-7：四个
写入目标都在 EvidencePack 排 rank 2，而 primary-only RunMemory 只把 rank 1 内容带过 replan 边界。
该 run 保留为负结果；随后代码只扩大有界 memory excerpt 到前两块，没有改 query、排序、manifest
或验收门。

修复后的公开禁网 Docker run
[`40862188498`](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40862188498)
通过全部四组 suite、62/62 arm lineage coverage，以及 22/22 个 full-checkout 案例的逐案例
retrieval-delivery 与 evidence-backed-write 门。统一 artifact ID `12891897965`，234,856,281 bytes，
digest `sha256:3336208089c946818e9aab1aed6e49fc18166bf0f9cb35f9e91a4efcd328713e`，保留至
2026-11-28。该成功结果与上面的 18/22 诊断共同保留：前者证明修复后的结构门通过，后者记录缺口
及修复依据。

`network_called=false` 表示评测中的 Harness/模型路径没有出站调用；GitHub job 在评测前获取公开
源码和 Docker image 仍需要网络。`paid_model_called=false` 表示全部动作来自冻结 Scripted Model，
因此这些指标是 Harness 可靠性证据，不是 LLM 能力证据。

新增的 test-only 回归会在同一小型任务中加入一个不可解码源码文件：两条 arm 的真实检索都留下
`retrieval_degraded`，Treatment 仍在 required check 通过后成为 quality-flagged success；四次真实
降级检索记录被聚合，后续 NoProgress 策略回执不计作 degradation。该回归验证报告语义，不是新的
公开任务或 RAG 泛化成绩。
