# 有界恢复矩阵评测

## 1. 目的

`horizon eval recovery` 把“能恢复”和“应该安全停下”分开计数。它不是再造一个运行时，
而是直接复用生产路径中的写入状态评估、恢复决策函数、一键硬崩溃链路、内容寻址 Artifact
和 Trace replay。

冻结清单位于
[`benchmarks/recovery/horizon-write-recovery-v1.yaml`](../benchmarks/recovery/horizon-write-recovery-v1.yaml)，
manifest digest 为
`efacbe3f55bcf3efee78ee3397fffb44a3a855d1924be2f9307aa55aa45f77a4`。

## 2. 运行

```powershell
uv run --locked --cache-dir .uv-cache horizon eval recovery `
  benchmarks/recovery/horizon-write-recovery-v1.yaml
```

也可用 `--output <新目录>` 固定证据目录。命令不读取 Provider 配置或 API Key，不访问网络，
不启动 Docker，也不执行被测仓库代码；模型动作来自本地冻结脚本，外部费用为 0。

## 3. v1 案例

总计 19 例：

| 组 | 数量 | 实际动作 | 期望结果 |
|---|---:|---|---|
| 真实硬崩溃 | 1 | 子进程在 `replace_text` effect 后、receipt 前 `os._exit(86)`；随后 unknown → exact accept → 续跑验证 → Trace replay | `auto_recovered` |
| `replace_text` | 6 | `pre_effect / expected_effect / diverged` × `accept / rollback` | 3 自动恢复、3 安全阻塞 |
| `apply_patch` | 6 | 同一状态/决策笛卡尔积；diverged 使用真实的双文件部分 effect | 3 自动恢复、3 安全阻塞 |
| `create_file` | 6 | 同一状态/决策笛卡尔积；diverged 使用错误内容的同名文件 | 3 自动恢复、3 安全阻塞 |

生产决策表只有三条：

- `expected_effect + accept`：接纳既有精确效果，不重派工具；
- `pre_effect/expected_effect + rollback`：恢复或保持完整前态；
- 其余组合：阻塞，保留现场，不猜测成功，也不自动回滚偏离状态。

## 4. 指标

评测分别输出：

- `recovery_success_rate = 期望自动恢复且实际自动恢复的案例数 / 期望自动恢复案例数`；
- `safe_block_rate = 期望安全阻塞且实际安全阻塞的案例数 / 期望安全阻塞案例数`；
- `unrecoverable_count` 与 `incorrect_resume_count`，二者不能被并入成功；
- `recovery_redispatch_count` 与 `duplicate_side_effect_count`；
- 实际进程崩溃数和成功 Trace replay 数。

当前冻结 v1 的本地执行结果是 19/19：期望自动恢复 10/10，期望安全阻塞 9/9，
`unrecoverable=0`、`incorrect_resume=0`、恢复重派 0、重复副作用 0；其中 1 例观察到真实子进程
退出并完成 Trace replay。

这里的“恢复成功率”只使用期望自动恢复的分母。安全阻塞单独报告，不把阻塞包装成恢复成功。

## 5. 证据与复核

每个写入状态案例保存：

- 工具与精确参数；
- 注入状态、实际分类和请求的 accept/rollback 决策；
- pre、observed、expected、final revision；
- 对应的内容寻址 manifest；
- 是否发生恢复期 redispatch 或重复副作用。

硬崩溃案例引用完整 Portfolio EvidencePack。矩阵 verifier 会再次校验该 pack 的文件 hash、崩溃
marker、intent → unknown → settled 顺序、最终投影和 Trace replay。对普通状态案例，verifier
会重读内容寻址证据并核对隔离 workspace 仍等于 final revision；任意事后篡改都会失败。

顶层 `report.json` 同样进入本次目录的 ArtifactStore，CLI 返回 `report_ref`。目录因此既可读，
又能用 hash 验证，不依赖终端摘要作为权威证据。

## 6. 严格边界

v1 明确不证明：

- 任意进程、OS reboot、主机宕机或磁盘损坏恢复；
- 分布式 exactly-once；
- 恶意代码沙箱安全；
- 付费模型质量；
- 设计文档中的完整故障注入矩阵。

18 个写入状态案例使用生成的本地 fixture；只有第 1 例真实杀死子进程。Docker 检查恢复、
Provider 丢响应、数据库提交窗和 promotion 等已有独立测试与历史证据，但尚未全部统一进入此
报告。因此本命令应称为“有界恢复矩阵 v1”，不能简称为全故障矩阵。
