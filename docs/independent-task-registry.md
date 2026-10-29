# 24 条独立真实任务注册表与零费用预检

更新：2026-10-29。

本批把已经冻结的 BugsInPy 外部定位集合整理为一份可机读、可交叉验证的独立任务注册表：
[`benchmarks/independent-tasks-v1.yaml`](../benchmarks/independent-tasks-v1.yaml)。注册表原样保留
16 条 development 与 8 条 holdout，没有把同一上游 bug 的多种恢复场景重复计数。

这一步解决的是“后续 20～30 条真实任务评测到底是哪几条、目前各自准备到哪一层”。它不是模型
跑分，也不把历史 Scripted Model 场景改名为真实模型成功。

## 1. 独立性的定义

一条独立任务由标准化后的二元组唯一标识：

```text
(GitHub repository URL, BugsInPy bug_id)
```

预检同时要求：

- 24 个 `task_id` 唯一；
- 24 个上游 bug 二元组唯一；
- buggy/fixed commit 都是精确 40 位 SHA，且二者不同；
- 每条任务都绑定仓库、commit-bound 许可证元数据、失败测试与选择原因；
- 24 条必须与 2026-10-24 已冻结的选择逐字段一致，不能在查看可执行性后删换案例；
- 注册表不包含 patch 内容或 gold production path。

因此，“24 条独立任务”只表示 24 个不同上游缺陷，不表示 24 个不同项目。当前涉及 7 个项目。

## 2. 三层证据，而不是一个含糊的 ready

每条任务分别报告三个维度：

| 维度 | 状态 | 含义 |
|---|---|---|
| Source | `frozen_materialization_recipe` | 已有绑定 exact commit 与 tree digest 的源码物化配方；不等于本机此刻已有 checkout |
| Source | `metadata_only` | 只有冻结来源元数据；当前不物化密封 holdout |
| Initial failure | `public_historical_run_declared` | 已绑定执行 manifest 与公开历史 workflow；本次离线预检不重新联网核验或执行 |
| Initial failure | `benchmark_declaration_only` | 失败测试来自 BugsInPy 冻结选择，尚未由当前 Harness 复现 |
| Execution | `full_checkout_manifest` | 已有 exact-commit 完整 checkout Agent A/B manifest |
| Execution | `dependency_reduced_manifest` | 已有依赖裁剪复现，但不是完整上游 checkout |
| Execution | `not_prepared` | 尚无可直接交给 Harness 的执行 manifest |

预检不会把“有 source recipe”写成“源码已物化”，也不会把 manifest 中的
`expected_initial_failed_checks` 写成“本次重新观察到失败”。

## 3. 当前零费用基线

运行：

```powershell
uv run --locked --cache-dir .uv-cache horizon eval task-registry-preflight `
  benchmarks/independent-tasks-v1.yaml `
  --state-dir .horizon/task-registry-preflight-v11
```

该命令只读 YAML，并把报告写入内容寻址 ArtifactStore。它不读取 API Key、不联网、不运行 Docker、
不打开或执行第三方仓库代码。当前确定性结果：

| 指标 | 数量 |
|---|---:|
| 冻结任务 / 唯一上游 bug | 24 / 24 |
| development / holdout | 16 / 8 |
| 有源码物化配方 / 仅元数据 | 16 / 8 |
| 绑定公开历史初始失败证据 / 仅 benchmark 声明 | 15 / 9 |
| 完整 checkout manifest | 14 |
| 依赖裁剪 manifest | 1 |
| 尚未准备执行 manifest | 9 |
| 本次模型调用 / 网络调用 / 仓库代码执行 | 0 / 0 / 0 |

注册表 digest 为
`8779c1effc8dee794322c7530e171b78ab5df89995086fcf7d5c1e69a1b1acbe`；预检 report ref 为
`4749677f5f10551057c1701cc3a0bbfe90c86697e259b3ddf826f1bd6cd3fac0`。它绑定的原始选择
digest 为 `7789f0c7e9cbd63afe23c523627a1c3921facc97dda4df74c19b140a3b20f9c7`。

## 4. 当前能说什么，不能说什么

可以说：

- 已固定 24 个不重复的公开 BugsInPy 缺陷身份；
- 来源、commit、许可证、失败测试、分区和现有证据引用可以离线交叉验证；
- 其中 15 条已有 Harness 执行 manifest，14 条为完整 checkout、1 条为依赖裁剪；
- 这份报告可以作为后续批量执行的确定性输入基线。

不能说：

- 24 条都已物化、都能执行或都复现了初始失败；
- 24 条都被真实模型解决；
- 公开历史 workflow 的在线状态在本次离线命令中被重新核验；
- 每个 commit 的许可证文件与 SPDX 结论在本次预检中被联网下载并独立审计；
- 密封 holdout 的 patch 或 gold path 已经检查；
- 这是官方 BugsInPy、SWE-bench 或模型泛化成绩。

## 5. 下一批最短路径

当前 development 已有 15/16 条执行 manifest。唯一剩余的 Cookiecutter-3 候选 checkout 含 7 个
tracked 文档符号链接，生产快照安全门按既有合同拒绝 links/junctions；本批没有排除链接、改写上游
内容或放宽安全门来凑齐 16/16。其余 8 条 `not_prepared` 全部属于仍密封的 holdout，不物化、不揭示
patch 或 gold path。下一步应先决定是否为 Cookiecutter-3 设计不降低安全性的来源包装；真实模型
预算仍需单独明确授权。
