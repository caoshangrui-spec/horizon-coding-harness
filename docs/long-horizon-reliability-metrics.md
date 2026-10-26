# 公开仓库长程任务可靠性指标

更新：2026-10-25。本页把 Horizon 已有的来源绑定 Run A/B 证据收敛为一组机器可验证、面试时
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
  -> case reliability
  -> typed suite reliability + GitHub Actions summary
```

领域合同位于 `src/horizon/domain/run_evaluation.py`，聚合逻辑位于
`src/horizon/application/run_ab_eval.py`。旧的 case/suite JSON 没有 `duration_ms` 或
`reliability` 时仍可读取；新运行会同时保存原始 arm 证据和聚合指标。冻结 suite/manifest
内容与 digest 没有被改写。

## 2. 指标定义

| 字段 | 分母或计算方式 | 严格含义 |
|---|---|---|
| `scenario_count` | suite 中的 case 数 | 执行场景数；同一上游 bug 以不同恢复配置出现时分别计数，不冒充独立任务数 |
| `observed_duration_ms` | 各 case 的 `perf_counter_ns` 观测之和 | 包含 snapshot、初始验收、两条 arm、恢复与最终验收；受 runner/磁盘影响，不是跨机器性能排名 |
| `p95_scenario_duration_ms` | case 耗时的 nearest-rank P95 | 小样本只用于发现明显慢例；不外推生产延迟分布 |
| `arm_count` | 每个场景固定 Baseline + Treatment | 实际完整 Run 数，不等于独立任务数 |
| `terminal_success_count` | 最终为 `SUCCEEDED` 的 arm | Baseline 按合同进入 `WAITING_FOR_USER` 时不会被包装成成功 |
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
| `bugsinpy-full-checkout-pilot-v8` | v7 十二例 + Sanic-5、TheFuck-2、Luigi-2 | 15 | 9 个项目、4,491 tracked files；三个新增案例本地完整 Harness 为 3/3，生产目标检索均为 rank 1。v7 公开 run `40305739627` 已通过 12/12；v8 公开 15-case 统一门待执行 |
| `bugsinpy-multi-stage-pilot-v4` | youtube-dl、Luigi、FastAPI | 3 | 每条 arm 为 3 WorkItem、2 次 Worker 重启；合计 6 条 Trace、12 次重启 |
| `bugsinpy-multi-stage-hard-crash-v1` | Luigi | 1 | 两条 arm 均真实写入后、receipt 前退出，再跨两个 WorkItem 边界；合计 2 次恢复尝试、6 次重启 |

同一 bug 在不同 suite 中用于不同实验变量，因此不能把当前 24 个场景宣传成 24 个独立真实任务。
当前 suite 共 24 个 manifest 场景；既有 21 个已有公开 Docker 统一验证，v8 新增 3 个已有本地
Harness 证据、公开统一门待执行；历史 v1/v2/v3/v4/v5/v6/v7 版本不重复累加。
按 `(benchmark, project, bug_id, buggy_commit)` 去重后仍是作者选择的小样本。当前项目继续保留
“尚无 20～30 个独立真实任务和真实模型成功率”的负面边界。

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

## 5. 验收门槛

CI 不只检查 `all_expectations_met`。三阶段 suite 还必须满足：三个 timed scenario、Trace replay
rate 1.0、source unchanged rate 1.0、final open effect 0。写入硬崩溃 suite 必须满足：两个故障
arm 全部恢复、recovery rate 1.0、redispatch 0、duplicate side effect 0、Trace replay rate 1.0、
final open effect 0。任一条件失败都会让证据发布步骤失败，而不是只在 Markdown 中显示警告。

`network_called=false` 表示评测中的 Harness/模型路径没有出站调用；GitHub job 在评测前获取公开
源码和 Docker image 仍需要网络。`paid_model_called=false` 表示全部动作来自冻结 Scripted Model，
因此这些指标是 Harness 可靠性证据，不是 LLM 能力证据。
