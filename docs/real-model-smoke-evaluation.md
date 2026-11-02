# 5 条真实模型 Smoke 评测合同

更新：2026-11-02。

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

前八轮的单次与累计费用授权没有自动继承到这 5 条任务；随后 TheFuck-2 取得的一次性授权也已由
第九轮唯一 Run 消费。任何后续任务仍需新的零费用预检和明确预算授权；成功或失败都先冻结证据，
再决定是否扩到其余 4 条。

## 8. 第一条候选：TheFuck-2

第一条 easy 任务已在 2026-10-30 按一次性明确授权执行并冻结为**预算门限负结果**：

- Pilot：[`real-model-smoke-pilot-v1.yaml`](../benchmarks/run_ab/full/thefuck-2-executable-path-separator/real-model-smoke-pilot-v1.yaml)；
- Provider：[`siliconflow-thefuck2-smoke-v1.yaml`](../config/providers/siliconflow-thefuck2-smoke-v1.yaml)；
- 模型：`deepseek-ai/DeepSeek-V4-Flash`；
- 工具：`read_file/retrieve_code/replace_text/run_check`；
- 路径：`thefuck/**`，不把精确目标文件写进 allowlist；
- Provider `max_attempts=1`、`fallback_enabled=false`；
- 单 Run / 独立 Campaign 硬上限均为 `CNY 0.060`。

本地用已有 `python:3.12-alpine` 镜像执行：

```powershell
uv run --locked --cache-dir .uv-cache horizon eval pilot-preflight `
  benchmarks/run_ab/full/thefuck-2-executable-path-separator/real-model-smoke-pilot-v1.yaml `
  --image python:3.12-alpine `
  --config config/providers/siliconflow-thefuck2-smoke-v1.yaml `
  --state-dir .horizon/real-model-smoke-thefuck2-v1
```

当时 Harness 的历史预检结果为 `ready=true`：

- 干净 HEAD 精确等于 buggy commit `40ab4eb...`，380-file source capture 后保持 unchanged；
- Docker 中 protected check 如预期失败，实际错误把 Windows-style PATH 错拆为
  `['C', '/foo;C', '/bar;C', '/baz']`；
- solution isolation 通过，fixed commit、fix URL 和精确修复 literal 不在 prepared TaskSpec；
- 初次规划请求为 5,715 bytes，保守 input ceiling 12,454，预留 `CNY 0.041970`，低于提议上限；
- `credential_loaded=false`、`paid_model_called=false`、`network_called=false`；仓库代码只在本地禁网
  Docker 中用于初始失败验证；
- Pilot manifest digest 为
  `565416d2335e7a1342b9fcc85138234959bb58aeb0254df4decb6f8d0059ce1c`，prepared TaskSpec ref 为
  `e17a0969acc70b548ee7a090803e1c8721525e55b864ef6f976a8818d1470f95`，preflight report ref 为
  `c41e0196e94ad006b035bc90b5f062cca27ceeea3b1cae16b489e6cf3e30858b`。

该历史 `ready=true` 只检查了首次规划请求，没有证明执行轮在工具回执进入上下文后仍可由相同
费用合同支持。一次性授权后的唯一 Run 为
`run_9cfbde76d85a4bfcbf1f3463fbacf8ed`，终态 `FAILED`，原因
`campaign_call_cost_limit`：

- 规划调用使用 1,702 input / 241 output tokens，结算 `CNY 0.007275`；生成一个端到端 WorkItem，
  Plan 结构有效；
- 第一轮执行使用 1,690 input / 102 output tokens，结算 `CNY 0.005988`；模型在同一响应中发出
  两个相近的 `retrieve_code`。第一次已命中 `thefuck/utils.py`，但片段是同文件中本来就正确使用
  `os.pathsep` 的 `which()`；第二次误中 `thefuck/system/unix.py`，均未定位到目标
  `get_all_executables()`；
- 合计 2 次模型调用、2 次只读工具调用，实际结算 `CNY 0.013263`。Campaign remaining 为
  `CNY 0.046737`，reserved/unknown 均为 0；没有重试、fallback 或第二个 Run；
- 下一执行请求为 10,251 wire bytes，生产保守 input ceiling 为 21,526，需预留
  `CNY 0.069186`。它既高于单调用硬上限 `CNY 0.060`，也高于 Campaign 剩余额度，因此在
  Provider 派发前停止，未产生第三次费用；
- workspace revision 始终为
  `ff858630d710386dc5b28ffe28166c079850db7fc1ab0a73047b2412540cd282`；无编辑、checkpoint、
  validation 或 passed item，源 checkout 未变；
- Trace 位于
  `.horizon/real-model-smoke-thefuck2-v1/run_9cfbde76d85a4bfcbf1f3463fbacf8ed.trace.jsonl`，
  SHA-256 为 `cf4400e744c51a3c61d0bb11f57eae3f5e9c4aff74adaf2ab3c1926e2b5b44d5`，离线
  projection hash 为 `6945892ed2f334addbf695ddeb97e25555cdbe30b5ebc1c5a65225b547aa6e6a`；
  schema v2 EvidencePack 已离线复核，unknown/open effect 均为 false，验收证据因未运行而明确为
  unknown，不能声称任务修复成功。

该负结果驱动了一个窄的零费用修复：新 Pilot preflight 除首次 planning 外，还构造一个使用任务
真实工具 Schema、包含一个完整工具轮和最多 8 KiB 有界回执的 execution sizing case；该投影必须
落在配置的 context cap 内，并同时满足 input、单调用、Run 和 Campaign 四个门限。对当前 TheFuck-2
配置，离线 case 为 8,000 context chars、7,453 tool-result chars、10,293 wire bytes、21,610
input ceiling，需预留 `CNY 0.069438`，所以 `CNY 0.060` 合同会在付费前得到
`ready=false`。这没有降低生产保守估算、修改历史 Trace 或放宽预算；Harness 源码指纹变化也使旧
preflight 不能再次启动付费 Run。

同一修改随后在精确 TheFuck-2 buggy checkout 和本机已有 `python:3.12-alpine` 上完成真实禁网 Docker
preflight：初始失败、source unchanged、solution isolation 与 planning 门继续通过；bounded execution
的 per-call/Run/Campaign 三项费用门为 false，最终 `ready=false`，CLI 以非零码阻止 launch。
`credential_loaded/paid_model_called/network_called` 均为 false；新 report ref 为
`8f389d990604629bcfc89b9d5fd3616e41fe3d45f0e127c0ef51cf412512f4f2`。

## 9. 剩余四条的精确预检与下一候选

TheFuck-2 的负结果还暴露出第二个预算边界：planning 和 bounded execution 可能分别低于 Run/
Campaign cap，但两次 reservation 的**合计**仍超过同一 cap。Pilot preflight 现在额外记录并门控：

- `planning_execution_reserved_cost`；
- `planning_execution_fits_run_cap`；
- `planning_execution_fits_campaign`。

三项缺失的历史 report 仍可读取，但不能再启动付费 Run。新 report 只有在 planning、bounded
execution 及二者累计序列都可预留时才会 `ready=true`。

剩余四条共用
[`siliconflow-smoke-candidate-preflight-v1.yaml`](../config/providers/siliconflow-smoke-candidate-preflight-v1.yaml)
做**零费用可比预检**：同一模型、8,000-char context、512 output tokens、单次尝试、无 fallback，
proposal-only Run/Campaign cap 为 `CNY 0.120`。这个数只给离线 sizing 留足空间，不是费用预测，
更不是付费授权。精确 buggy checkout 与本机既有 `python:3.12-alpine` 的结果为：

| 任务 | planning 预留 | planning + execution | RAG 目标排名 | Scripted 修复形状 | 结果 |
|---|---:|---:|---:|---|---|
| HTTPie-1 | CNY 0.036018 | CNY 0.105456 | 1 | 5 次替换、5 处 | ready |
| Luigi-2 | CNY 0.040680 | CNY 0.110118 | 1 | 1 次替换、1 处表达式 | ready |
| Sanic-2 | CNY 0.035046 | CNY 0.104484 | 1 | 1 次替换、插入两个代理方法 | ready |
| Tornado-2 | CNY 0.040320 | CNY 0.109758 | 3 | 1 次替换、2 处谓词 | ready |

四条都实际观察到 protected initial failure，`source_unchanged=true`、solution isolation 通过，累计
Run/Campaign 门通过；`credential_loaded/paid_model_called/network_called` 全为 false。planning
数字含本机绝对路径，Linux CI 可以略有不同，因此公开门禁检查公式与 cap，不硬编码本机数值。

冻结选择文件为
[`real-model-smoke-candidate-selection-v1.yaml`](../benchmarks/real-model-smoke-candidate-selection-v1.yaml)，
下一候选是 `luigi-2-beam-target-path`。选择顺序先看 ready 和 easy，再看是否为单次精确替换、单处
表达式及冻结 RAG rank 1，最后才比较 reservation。Sanic-2 虽更便宜，但修改面更大；Tornado-2
目标 rank 3 且要改两处；HTTPie-1 是多编辑任务。Luigi-2 的检索状态为 degraded，原因是
`non_utf8_file_skipped` 与 `oversized_line_skipped`，即使目标路径仍为 rank 1 也不删除该限制。

这只是作者选择的 development 候选排序，不是 holdout、真实模型成功或泛化证据。选择冻结时
`paid_execution_authorized=false`；后续 Luigi-2 的一次性明确授权、唯一 Run 及其失败结果见第 11 节，
不能把后来授权倒写进该选择文件。

## 10. Run 后七指标与部分覆盖聚合

`smoke-preflight` 只回答“任务与付费前边界是否就绪”，通用 Terminal EvidencePack 只回答“终态
控制状态和 Trace 是否自洽”。二者原先都不会自动产出第 4 节冻结的七项 Smoke 指标。现在新增的
controller-only [`real-model-smoke-evaluator-v1.yaml`](../benchmarks/real-model-smoke-evaluator-v1.yaml)
为五条 exact buggy checkout 冻结目标源码区域；该文件不进入 Pilot preflight、planning、RAG 或
Agent context。普通离线回归核验 evaluator 与冻结 manifest 的 repair path；source-bound workflow
物化 exact checkout 后，再把每个区域与 scripted repair 的真实 `replace_text` preimage 行区间交叉
核验，避免目标行号静默漂移。

单 Run 离线评估命令为：

```powershell
uv run --locked --cache-dir .uv-cache horizon eval smoke-result `
  benchmarks/real-model-smoke-evaluator-v1.yaml `
  thefuck-2-executable-path-separator `
  --suite benchmarks/real-model-smoke-v1.yaml `
  --pilot-preflight <原始内容寻址 preflight report> `
  --terminal-evidence <terminal EvidencePack> `
  --run-artifacts .horizon/artifacts `
  --output <新的结果目录>
```

评估器不调用模型、Provider、网络或 protected check。它会拒绝错误拼接，并同时核验：

- suite/evaluator digest 与任务集合；
- 原始内容寻址 preflight、prepared TaskSpec 和本次 Run 的变换后 TaskSpec；
- source snapshot/manifest/Git HEAD、模型策略、模型标识、币种及 Run cap；
- Terminal EvidencePack 文件哈希、Trace 重放和最终投影；
- 模型响应中的工具参数与 durable reservation 哈希；
- retrieval EvidencePack 的 path/line span，以及成功写入前后的内容寻址 workspace manifest。

`correct_location_reached`、`valid_edit_produced`、`protected_validation_passed` 都允许 `null`：支持工件
缺失、外部 effect 未知或根本没有执行 validation 时不猜测 false。`actual_cost_cny` 是 Trace 中已结算
调用按冻结 price card 得到的估算总和，不冒充 Provider invoice。`source_unchanged` 在 Run 后评估时
重新 capture；它能确认评估时源码仍等于 launch snapshot，但不能证明期间从未短暂修改后又还原。

对冻结 TheFuck-2 Run 的实际离线输出为：

| 字段 | 自动结果 |
|---|---:|
| `task_success` | false |
| `correct_location_reached` | false |
| `valid_edit_produced` | false |
| `protected_validation_passed` | unknown / null |
| `actual_cost_cny` | 0.013263 |
| `model_calls` | 2 |
| `stop_category` | budget_gate |

结果 report ref 为
`bcf94baf895f3d5e59224bb9118394dcb255ed4efe7cf0311f78f7f8866a4f19`。第一次 retrieval 虽命中
`thefuck/utils.py:36-75`，但 evaluator target 是 `get_all_executables()` 所在 108–128 行，因此不会
把“同文件错误函数”包装成正确定位。六个成功门同时保留：initial failure/source unchanged/Trace/no
open effect 为 true，workspace changed 为 false，required checks 为 unknown，最终任务仍为失败。

已有一个或多个 `smoke-result.json` 后，可在零执行条件下聚合：

```powershell
uv run --locked --cache-dir .uv-cache horizon eval smoke-aggregate `
  benchmarks/real-model-smoke-v1.yaml <result-1.json> [<result-2.json> ...] `
  --output <新的汇总目录>
```

聚合器按 evaluated tasks 计算成功率，同时强制报告 suite coverage 和未运行 task IDs。当前已有
TheFuck-2、Luigi-2 与 Sanic-2 三条唯一付费 Run，所以输出是 coverage `3/5`、evaluated success
`0/3`、budget gate `3`、总费用 `CNY 0.1455048`、总模型调用 17；HTTPie-1、Tornado-2 保持
`unevaluated`，不会被偷偷计成失败或形成“0/5”的虚假完整成绩。聚合 report ref 为
`2d6cfea5fb238ae3899385c3601cec17303dc2b36d3b86398d25dda676b308b9`。聚合不重新执行 Run，也不
扩大任何付费权限。

## 11. Luigi-2 唯一真实 Run：定位成功、工具协议失败

用户随后明确授权 Luigi-2 唯一一次付费 Run：模型为 `deepseek-ai/DeepSeek-V4-Flash`，专属独立
Run/Campaign 上限均为 `CNY 0.120`，`max_attempts=1`、无 retry、无 fallback。专属
[`policy`](../config/providers/siliconflow-luigi2-smoke-v1.yaml) 与
[`Pilot`](../benchmarks/run_ab/full/luigi-2-beam-target-path/real-model-smoke-pilot-v1.yaml)
先在精确 buggy commit 上重新执行禁网、零费用 preflight：初始失败、source unchanged、solution
isolation 及 planning+execution 累计门通过，`ready=true`，report ref
`a1af1286b4b4c9e2a6e8241b999b02f525c6cd2d9da7ff084a956b36171f64eb`。

唯一 Run `run_940f6a82909a444f82b58c142191b716` 结算 7 次模型调用、11 次工具调用、18,853 input /
927 output tokens和 `CNY 0.064902`。5 次 retrieval 成功，其中 4 次返回
`luigi/contrib/beam_dataflow.py:456-479`，覆盖 evaluator-only 目标 473–477 行并直接包含错误表达式；
因此 `correct_location_reached=true`。但模型随后 5 次调用 `read_file` 都只提供 `start_line`，遗漏
`end_line`，被当时的严格参数合同拒绝；另一次近重复 retrieval 被 revision-bound 策略拒绝。没有
write、validation、checkpoint 或 passed item。

第 8 次模型请求派发前，保守预留为 `CNY 0.071094`，高于 Campaign 剩余 `CNY 0.055098`，因此
Run 以 `FAILED/campaign_cost_limit` 安全停止。reserved/unknown 为 0，source 与 staging workspace
均未变化，unknown/open effect 为 false。Terminal EvidencePack 离线 replay projection hash 为
`a819d85179f49615ad0a64e86a52545847ba3b4d46ff9dee7d32a361ce6e07ae`；Smoke report ref 为
`d9167f36833f1fe7714795443146290be6d295a32cac6a213eb029c45739d0d3`。七指标为：

| 字段 | 自动结果 |
|---|---:|
| `task_success` | false |
| `correct_location_reached` | true |
| `valid_edit_produced` | false |
| `protected_validation_passed` | unknown / null |
| `actual_cost_cny` | 0.064902 |
| `model_calls` | 7 |
| `stop_category` | budget_gate |

该负结果只驱动一个有界、零费用的 Harness 修复：当 `read_file` 恰好收到一个行边界时，控制器
当时确定性补成最多 400 行的窗口；第 12 节的 Sanic-2 证据随后把自动单边窗口进一步收窄到
100 行。显式双边界仍保留最大 400 行，并继续执行 path allowlist、32 KiB 输出上限、UTF-8 与
workspace revision 约束。Scripted Agent 回归证明同类单边调用可继续到 edit、protected
validation 和 Trace replay，但没有复跑 Luigi-2，也不能据此把真实结果升级为成功。Harness 源码
变化使本节 preflight 失效；任何未来真实调用都需要新的零费用 preflight 和新的明确授权。

为避免只用缩小 fixture 自证，另建了
[`luigi2-one-sided-read-recovery-v1`](../benchmarks/run_ab/luigi2-one-sided-read-recovery-v1.yaml)
source-bound development suite。它在 382-file exact checkout 中冻结真实 Run 的原始 retrieval query 与
`start_line=440`，Scripted treatment 实际完成 `440-479` 读取、一次 replan、精确编辑、protected
validation 和 Trace replay；Baseline 保持 `WAITING_FOR_USER`。该禁网回归不调用外部模型、费用为 0，
并由公开 full-checkout workflow 作为独立门禁执行；它只证明修复后的 Harness 路径可闭环，不是对
唯一付费 Run 的复跑、holdout 提升或真实模型成功。

## 12. Sanic-2 唯一真实 Run：读取成功、目标代码在压缩后丢失

用户随后明确授权 Sanic-2 唯一一次付费 Run：模型固定为
`deepseek-ai/DeepSeek-V4-Flash`，新建独立 Run/Campaign，上限均为 `CNY 0.120`，Provider
`max_attempts=1`、无 retry、无 fallback。专属
[`policy`](../config/providers/siliconflow-sanic2-smoke-v1.yaml) 与
[`Pilot`](../benchmarks/run_ab/full/sanic-2-asyncio-start-serving/real-model-smoke-pilot-v1.yaml)
先在精确 buggy commit `ba9b432993019b0af0c4827a5ed42aaa091bd17d`、188-file checkout 上完成
禁网零费用 preflight。初始失败、source unchanged、solution isolation 与 planning+execution 累计门
均通过；保守预留分别为 `CNY 0.034926`、`CNY 0.069222`，合计 `CNY 0.104148`，report ref
`aa7104564a718054b36eb0926ccd11ad921325e67eb9c77f90981632372612a0`。

唯一 Run `run_977d419fd17f4bbd9e1588515e8d0192` 结算 8 次模型调用、7 次只读工具调用、
20,484 input / 731 output tokens 和 `CNY 0.0673398`。第一次检索把
`sanic/server.py:666-705` 排为 rank 1；模型随后以 `start_line=666` 和 `start_line=646` 两次成功读取
文件，另有重复读取与被策略拒绝的近重复检索。没有 `replace_text`、`run_check`、checkpoint 或
passed item。第 9 次模型请求需要预留 `CNY 0.072048`，但 Campaign 只剩 `CNY 0.0526602`，因此在
Provider 派发前以 `FAILED/campaign_cost_limit` 安全停止；reserved/unknown 与 open effect 均为 0，
source 和 workspace revision 未变化，也没有第二个 Run。

这次失败不是单纯“没有找到文件”，而是读取与上下文压缩的交互缺陷。旧单边补界会自动扩成最多
400 行，两次实际读取分别产生约 11.9K 和 12.4K 字符；Provider policy 的 context 上限只有 8K。
从第 3 次执行调用开始，Context Projection 必须折叠历史，压缩后的消息只保留读取范围摘要和
Run Memory 的短前缀，没有保留 `AsyncioServer` 类及插入位置上下文。模型因此再次检索、再次读取，而
不是进入编辑。5 次模型请求带有 `context_compacted` 证据，这条事实保留为质量标记。

Smoke evaluator 只接受与冻结目标区域匹配的 location-supporting Artifact 或有效写入。物理读取范围
诊断上覆盖了目标区域，但终态包没有满足该自动证据合同，所以 `correct_location_reached` 必须保留
为 `unknown/null`，不能按人工阅读 Trace 擅自升级为 true。最终七指标为：

| 字段 | 自动结果 |
|---|---:|
| `task_success` | false |
| `correct_location_reached` | unknown / null |
| `valid_edit_produced` | false |
| `protected_validation_passed` | unknown / null |
| `actual_cost_cny` | 0.0673398 |
| `model_calls` | 8 |
| `stop_category` | budget_gate |

Terminal EvidencePack replay 通过，projection hash 为
`377cec5eff18be49c3256e5f0882882c8c239e95672797cf7c7168e1ac670247`；Smoke report ref 为
`d75df3f1e01968f1aad1527045aa8276a3bd3b8a9da8960dc684873f547ba388`。Trace SHA-256 为
`765434badc1e05c3a98294da5dc3a528d7152d687a09a74a415c77be8c9b9279`。

这条负结果只驱动一个窄、零费用的修复：自动单边读取从 400 行收窄为 100 行；模型显式提供双边界
时仍可读取最多 400 行，其余路径、字符数、UTF-8、revision 与权限门不变。新的 8K-context
Scripted Agent 回归按 Sanic 形态构造第 646 行附近目标和远端长尾，证明单边读取只返回 646–745，
目标代码仍可见，并能继续到精确编辑、protected validation 和 Trace replay。它没有调用真实模型，
没有复跑 Sanic-2，也不能把上述真实 Run 改写为成功。生产费用 estimator 未因这一个样本下调；任何
未来付费尝试都需要基于新源码重新 preflight 并取得新的明确授权。
