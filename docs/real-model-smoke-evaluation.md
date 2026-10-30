# 5 条真实模型 Smoke 评测合同

更新：2026-10-30。

这份合同把“下一轮真实模型到底跑什么、怎样算成功、失败怎样归因”先离线冻结。入口是
[`benchmarks/real-model-smoke-v1.yaml`](../benchmarks/real-model-smoke-v1.yaml)，验证命令是
`horizon eval smoke-preflight`。

它解决的是前八轮 Pilot 任务过窄、每轮目标和资源边界不完全一致、负结果难以横向比较的问题。
它不会启动 SiliconFlow，不绑定模型或费用，也不会把现有 Scripted Model 修复改写成真实模型成绩。

## 1. 为什么先做 development smoke

第一批选择 5 个不同项目中的已知低复杂度 development 任务：

| 任务 | 项目 | 难度带 | 允许写入范围 |
|---|---|---|---|
| `httpie-1-unique-download-filename` | HTTPie | medium | `httpie/**` |
| `luigi-2-beam-target-path` | Luigi | easy | `luigi/**` |
| `sanic-2-asyncio-start-serving` | Sanic | easy | `sanic/**` |
| `thefuck-2-executable-path-separator` | TheFuck | easy | `thefuck/**` |
| `tornado-2-redirect-put-without-body` | Tornado | easy | `tornado/**` |

每条都必须同时满足：

- 来自已冻结的 development 分区；
- 有 exact-commit 源码物化配方；
- 有完整 checkout 执行 manifest；
- 有公开历史初始失败证据引用；
- 5 条来自 5 个不同项目；
- 不包含已经付费跑过的 `httpie-4-host-header-overwrite`。

这是**作者在已知 Scripted Model 修复后选择的 smoke 集**，目的是先确认真实模型能否穿过规划、检索、
编辑、验证和证据闭环，不是盲测、holdout、官方 BugsInPy 分数或泛化率。若 5 条均失败，优先说明
当前模型/工具协议仍未达到最小可用性；若通过，也只能说明这组已知低难任务可用。

## 2. 模型能看到什么

合同只冻结任务身份、项目级路径 allowlist、最小工具集和评测规则。每条任务允许：

```text
read_file, retrieve_code, replace_text, run_check
```

`submit` 仍是 Harness 的终态动作，不作为可扩张的任务权限。清单明确记录：

- `patch_content_included=false`；
- `gold_paths_included=false`；
- `evaluator_manifests_model_visible=false`。

现有 full-checkout A/B manifest 含 evaluator-only 的确定性修复动作，预检会用它确认来源和执行合同，
但未来真实模型请求不得读取或拼入这些 scripted arm、fixed commit 或 fix URL。零费用 per-task
preflight 仍要再次执行 solution-isolation 检查。

当前 `RealModelPilotManifest` 的 protected acceptance 是模型可见的，命令可能间接暴露文件或符号提示。
因此 `gold_paths_included=false` 只描述这份 suite 选择清单，不把 `correct_location_reached` 宣传成盲检索
指标；它只记录模型是否最终到达相关代码。若以后要测纯 RAG 定位能力，应另建 evaluator-hidden
validation，而不是把本 smoke 的结果外推成盲测。

## 3. 给模型足够纠错机会，但不无限循环

前几轮不能仅靠压低调用次数判断模型能力。v1 为每条任务冻结统一上限：

| 资源 | 上限 |
|---|---:|
| steps | 24 |
| model calls | 14 |
| tool calls | 24 |
| wall time | 900 秒 |
| repair cycles | 2 |
| execution replans | 1 |

14 次模型调用能覆盖一次自动规划、检索/读取、多次局部编辑、受保护检查、提交以及有限修复；它是硬上限，
不是必须消费的目标。Provider 隐式重试和 fallback 均关闭。Harness 内部的 repair cycle 是同一任务中
基于验证回执继续修复，不等于对失败 Provider 请求偷偷重试。

## 4. 怎样才算一条任务成功

`task_success=true` 必须同时满足以下六项，不能只看模型是否输出补丁：

1. `initial_failure_observed`：本次执行实际观察到冻结 required check 失败；
2. `workspace_changed`：staging workspace 有允许范围内的有效修改；
3. `required_checks_passed`：全部 required protected validation 通过；
4. `source_unchanged`：冻结 source checkout 没被污染；
5. `trace_replay_verified`：导出的 Trace 可离线重放并得到相同终态；
6. `no_open_effects`：没有 unknown/open 模型、工具或 promotion effect。

每条结果至少记录：

- `task_success`；
- `correct_location_reached`；
- `valid_edit_produced`；
- `protected_validation_passed`；
- `actual_cost_cny`；
- `model_calls`；
- `stop_category`。

其中“定位到正确位置”“产生合法编辑”“通过验证”分开统计，可以区分 RAG、工具协议和修复逻辑问题，
避免再次把所有失败压成一个 `FAILED`。

## 5. 失败分类

终态或安全停止必须归入一个主类别；证据不足时保留未知，不强行解释：

| 类别 | 典型含义 |
|---|---|
| `model_planning` | 计划不可执行、重复空转或未形成修复步骤 |
| `retrieval` | 未定位到相关代码，或证据排序把模型带到错误区域 |
| `tool_protocol` | 模型产生非法工具名、参数、行范围或越权动作 |
| `context_capacity` | 最小安全上下文仍超过派发门限 |
| `budget_gate` | Run/Campaign 余额不足，模型派发前被拒绝 |
| `validation_failure` | 已修改但 required checks 仍失败 |
| `harness_recovery` | Harness 无法安全恢复已支持的故障边界 |
| `unknown_effect` | 外部 effect 后态无法证明，必须保守停止 |

分类不覆盖事实：例如一次 Run 可以主类为 `validation_failure`，同时保留检索 degraded、修复两轮和实际
费用。失败报告必须附可重放 Trace 或明确说明哪一段证据仍为 `UNKNOWN`。

## 6. 零费用预检

运行：

```powershell
uv run --locked --cache-dir .uv-cache horizon eval smoke-preflight `
  benchmarks/real-model-smoke-v1.yaml `
  --state-dir .horizon/smoke-preflight-v1
```

当前结果：

| 项目 | 结果 |
|---|---:|
| 任务 / 唯一项目 | 5 / 5 |
| frozen source recipe | 5 / 5 |
| full-checkout manifest | 5 / 5 |
| declared public initial-failure evidence | 5 / 5 |
| 模型 / 网络 / 仓库代码执行 | 0 / 0 / 0 |
| 外部费用 | CNY 0 |

suite digest 为
`2c8ca4da9b88ab047944cdbce98537c8ab017f4a5a6236da1067c53759453554`，内容寻址 report ref 为
`bc297aaaf85e8bd600198f333f6af23b03396f55dcbd5ee893cbd7c9133bc6e7`。预检只读本仓库 YAML 并写入
ArtifactStore；不会物化或执行第三方仓库，也不会在线重验历史 GitHub Run。

## 7. 付费运行仍缺什么

当前报告故意输出 `paid_launch_ready=false`。开始任何一条真实模型运行前，仍需另行完成：

1. 绑定 exact Provider policy，保持隐式 retry/fallback 关闭；
2. 绑定 exact model ID，并为该任务重新执行零费用 pilot preflight；
3. 由用户重新明确授权**单 Run 上限和包含失败在内的累计 Campaign CNY 上限**。

历史八轮的单次与累计费用授权已经用于对应 Run，不能自动继承到这 5 条任务。后续最短路径是先
只为 1 条 easy 任务生成 model-visible Pilot manifest 和 Provider 配置，零费用预检通过后再请求
一次明确预算授权；成功或失败都先冻结证据，再决定是否扩到其余 4 条。
