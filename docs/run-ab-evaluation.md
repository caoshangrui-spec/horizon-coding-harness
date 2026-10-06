# 完整 Run A/B：停滞等待与单次 Replan

更新：2026-10-03。本评测把同一 TaskSpec、同一初始 Plan、同一冻结 workspace 分成两个完整
Agent Run：Baseline 在确定性重复读取后继续原模式，进入可恢复人工等待；Treatment 在相同软阻断
证据后显式提交一次受限 replan，完成精确编辑，再由 Docker 执行控制器保护的验收命令。

该切片用于验证 Harness 的完整数据流和可量化开销，不用于证明真实模型会自主选择正确 replan。

## 1. 冻结输入

- Manifest：
  [`benchmarks/run_ab/stalled-reader-replan-v1.yaml`](../benchmarks/run_ab/stalled-reader-replan-v1.yaml)
- 小型仓库 fixture：
  [`benchmarks/run_ab/fixtures/stalled-reader`](../benchmarks/run_ab/fixtures/stalled-reader)
- 初始缺陷：`src/parser.sh` 原样输出输入；验收要求空输入输出 `[]`，普通输入输出 `[value]`。
- 初始 Plan：一个只允许 `read_file` 的调查 WorkItem，但仍覆盖 required acceptance。
- 两个 arm 的模型输出均由冻结 `ScriptedModelGateway` 提供；每次回执固定 100 input / 20 output
  tokens。脚本动作若不在当前 Tool Schema 中，评测立即失败。

Baseline 和 Treatment 的前三个动作完全相同：两次真实读取后，第三次相同读取被 NoProgress
策略软阻断。随后：

```text
Baseline  -> 再次 read_file -> hard stop -> WAITING_FOR_USER
Treatment -> revise_plan -> replace_text -> submit -> protected Docker validation -> SUCCEEDED
```

Treatment 的新 Plan 只把未通过的调查项替换为 edit-ready WorkItem，没有改变 TaskSpec、路径、
required check、预算或 execution mode。

## 2. 执行与隔离

```powershell
uv run --locked --cache-dir .uv-cache horizon eval run-ab `
  benchmarks/run_ab/stalled-reader-replan-v1.yaml `
  --image redis:7-alpine
```

CLI 只接受本机已有镜像；`DockerSandbox` 使用 `--pull never`、`--network none`、只读根文件系统、
非 root 用户、capability drop、资源限制和唯一 owner label。fixture 先进入内容寻址快照，再分别恢复
到初始预检、Baseline 和 Treatment 三个新的 staging workspace；源 fixture 不被修改。初始预检
真实执行 required acceptance，并要求失败项与 `expected_initial_failed_checks` 完全一致，避免把本来
就通过的任务计为恢复成功。若 `--state-dir` 位于 fixture 内部，命令会在创建目录前拒绝。

每个 arm 使用独立 SQLite EventLog、Campaign ledger、retrieval cache 和 workspace，共享不可变
ArtifactStore。结束后 evaluator：

1. 导出完整事件 JSONL 并内容寻址保存；
2. 立即离线 replay，比较完整 Run projection 和 projection hash；
3. 重新捕获 arm workspace 与源 fixture revision；
4. 检查期望状态、Plan version、replan 数、passed items、调用数、unknown/reservation 和脚本消费；
5. 保存 typed A/B report Artifact。

## 3. 当前真实 Docker 结果

2026-10-03 使用本机已有镜像
`sha256:520775a41a63e77e06c73e35d2fd9cc15921a609516818796b4ecbb813078bc7` 完成一次运行：

| 指标 | Baseline | Single replan | Delta |
|---|---:|---:|---:|
| 初始 protected validation | `parser-contract` 失败，符合预期 | 同一冻结输入 | — |
| 最终状态 | WAITING_FOR_USER | SUCCEEDED | +1 success |
| Plan version | 1 | 2 | +1 |
| Execution replan | 0 | 1 | +1 |
| Model calls | 4 | 6 | +2 |
| Tool calls / steps | 4 / 4 | 7 / 7 | +3 / +3 |
| Input / output tokens | 400 / 80 | 600 / 120 | +200 / +40 |
| 合成模型成本（CNY） | 0.00192 | 0.00288 | +0.00096 |
| Event 数 | 48 | 74 | +26 |
| Unknown / open reservation | 0 / 0 | 0 / 0 | 0 |
| Trace replay | 通过 | 通过 | — |
| 源 fixture 未变化 | 是 | 是 | — |

Treatment 的 final validation 由容器内 `sh tests/test_parser.sh` 实际执行并通过。`submit` 自身按一次
tool/step 记账，随后控制器再派发一次 protected `run_check`，因此 Treatment 是 7 次 tool/step，
不是把验收隐藏在模型调用里。

本次证据：

- Manifest digest：`4356d19ec9cd4435b7f8b9cb7da24fcb6cfd4a6c650db0a1d7106d82fb6f6ab5`
- A/B report ref：`e222e84f0bf246981d9281aea7597853c98b6d84182c5d373b509fcdde61fedf`
- Baseline trace ref：`15bd31574c357595a3e13aab74625d4151e3b95fdf47a26a59d503a661c86027`
- Baseline projection hash：`0847adc37749a863088c0fce54da98b0446db30bea2e61efa2585cce48f54beb`
- Treatment trace ref：`3560f67d91225d27e074c64bfe944db32bc90574895a8bd0615818067b236b9c`
- Treatment projection hash：`36db9c39c159170a4c1b64596c17ee34cd69b63d2f098e940f4a7c60f8d1e6c2`

两份 Trace 还分别经过独立 `horizon trace replay` 命令重建出相同 projection hash。

## 4. 成本和结论边界

上述 CNY 是冻结 PriceCard 对脚本 token 的合成估算，用于比较 Harness 路径开销；没有请求
SiliconFlow，也不是供应商账单。评测报告固定记录 `paid_model_called=false`、
`network_called=false` 和 `repository_code_executed=true`。后者只表示 fixture 验收确实在禁网
Docker 中运行。

`treatment_recovered=true` 的准确含义是：在这份人为冻结的策略对中，Baseline 等待、Treatment
成功。它不证明 replan 对任意任务具有因果收益，因为两个 arm 的第四个脚本动作由作者预先指定，
也没有真实模型随机性或盲测。本页 fixture 是本仓自建的小型 shell 项目，不是外部 Issue；它只有
一个任务和一次正式运行，也没有独立安全复核。

同一 runner/report 合同现已扩展到五个来源与许可证明确的 BugsInPy 依赖裁剪案例；五例本地
可信与公开禁网 Docker 均为 5/5，见
[外部来源 Run A/B Suite](external-run-ab-suite.md)。其中 tqdm-1、youtube-dl-3 与 Luigi-1 还完成了
[三项目完整 checkout suite](full-checkout-pilot.md)，包含 Git 来源门和真实 Code RAG；v2 本地可信
与专用公开禁网 Docker 均为 3/3。872 文件下
重复 CAS blob 校验的开销也已完成前后对照优化。youtube-dl-3 随后完成
[完整 checkout 两阶段 A/B](multi-stage-full-checkout-pilot.md)：两条 arm 都在第一项通过后切换到
epoch 2 Worker，Baseline 等待、Treatment replan 后成功，Trace replay 与来源未变均通过。
真实模型 arm 仍只在用户明确授权的费用上限内增加。
