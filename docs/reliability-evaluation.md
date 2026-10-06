# 控制器可靠性策略离线评测

更新：2026-10-02。该评测用于回答两个窄问题：精确 NoProgress 规则在冻结动作 Trace 上是否按
预期允许、软阻断或硬停止；执行期 Plan revision 是否保持已完成工作、验收覆盖和工具权限。
它不调用模型、不联网、不打开 fixture 仓库，也不执行验收命令，因此不能回答模型是否会正确
触发 replan、真实任务成功率是否提升或 SWE-bench 表现如何。

## 1. 冻结输入

首个清单位于
[`benchmarks/reliability/horizon-controller-v1.yaml`](../benchmarks/reliability/horizon-controller-v1.yaml)，
包含 12 个案例和 41 个逐步策略判断：

- 7 个 NoProgress Trace：完全相同动作、精确 A/B 循环、A/B/A/B/C 负例、workspace revision
  变化、成功写入后的 revision 切换、非受保护工具边界、显式 reset 边界；
- 5 个 replan 合同：合法替换未完成项，以及改写完成项、空修订、丢失 required check、扩展
  工具权限四类拒绝案例。

每个动作固定 tool、JSON arguments、执行前后 revision、可选 reset 和期望决策。每个 replan
案例固定同一 TaskSpec、Plan v1、passed item 集合、候选 items 和期望接受/拒绝。YAML alias 只
复用完全相同的冻结对象；加载后 manifest 以展开后的规范 JSON 计算内容 hash。

## 2. 与生产路径共享的判定函数

NoProgress 判定不是评测代码对生产逻辑的复制。生产 Agent Loop 和离线 evaluator 都调用
[`classify_no_progress`](../src/horizon/domain/human.py)。对当前动作
`s_t = (tool_name, sha256(canonical_arguments))`，控制器只查看最近 reset 之后、同一 revision、
执行前后 revision 未变化且工具属于受保护集合的连续后缀：

```text
完全相同：已有 2 个相同尾项 -> soft_block；已有 3 个 -> hard_stop
精确 period-2：A/B/A/B/A -> soft_block；A/B/A/B/A/B -> hard_stop
其他序列、revision 变化、非受保护 receipt 或 reset -> 不沿用旧 streak
```

它不做语义相似度、参数归一化、period-3+ 或跨 revision 推断。评测会按观察到的决策追加
success/error receipt，再判断下一步，因此软阻断本身也进入后续证据链。

Replan 案例直接调用生产 [`check_execution_replan`](../src/horizon/domain/plan.py)。本批在建立
负例时发现并修复了一条领域层缺口：写模式 Plan 过去主要依赖模型 Tool Schema 限制工具枚举；
现在所有人工、模型生成和执行期 revision 都由 `Plan.check_task()` 再次校验领域白名单，未知
`shell_exec` 一类工具即使绕过 Schema 也会被拒绝。

## 3. 执行与证据

```powershell
uv run --locked --cache-dir .uv-cache horizon eval reliability `
  benchmarks/reliability/horizon-controller-v1.yaml
```

命令只读取 manifest，输出 typed `ReliabilityEvalReport`，并把规范 JSON 写入
`.horizon/reliability-eval/artifacts/<prefix>/<sha256>`。可用 `--state-dir` 指定其他派生目录；
ArtifactStore 不覆盖已有内容。报告固定声明：

- `paid_model_called=false`；
- `network_called=false`；
- `repository_code_executed=false`。

`case_pass_rate` 按案例计算；`policy_decision_accuracy` 同时统计每个 NoProgress step 和每个
replan 决策。NoProgress 的 false positive 定义为 ground truth 为 allow、观察为任一 block；
false negative 定义为 ground truth 为 block、观察为 allow。软/硬或 pattern 不匹配也会使该步
失败，但不会被错误塞进 false positive/negative；原始 expected/observed 均保留在报告中。

## 4. 当前冻结结果

| 指标 | 结果 |
|---|---:|
| 案例 | 12 |
| NoProgress / replan 案例 | 7 / 5 |
| 通过案例 | 12/12 |
| 策略判断 | 41/41 |
| policy decision accuracy | 1.0 |
| NoProgress false positive | 0 |
| NoProgress false negative | 0 |

Manifest digest 为
`4d27b02f9254bfe792dbd8a782cb1ba67f4b5f83a420ab96f437870a81d4a66f`；本机内容寻址 report
ref 为 `debc94da6a00cd26419d6f9b450e81b3f23a8c132e71687513a013c41893e5fe`。单元测试另外故意把
一个负例标错，确认 evaluator 会保留 11/12、false negative=1，而不是后处理成满分。

## 5. 证据边界与下一步

这是由实现者编写、同仓、确定性的策略一致性诊断。41/41 只能说明当前精确规则与这份冻结
ground truth 一致，不能说明 ground truth 覆盖真实模型失败分布，也不能证明 replan 带来任务
收益。YAML 中的动作 Trace 不是完整 Run JSONL；完整事件原子性、Artifact 绑定和重放仍由 Agent
E2E 与 `trace replay` 测试承担。

内部 shell fixture 的“无 replan / 单次 replan”完整 Run 对照现已落地，包含真实 Docker
验收、两份内容寻址 Trace、调用数和合成费用，见[完整 Run A/B](run-ab-evaluation.md)。同一合同
也已扩展到五个来源绑定的 BugsInPy 依赖裁剪案例；五例本地可信和公开禁网 Docker 均为 5/5，
见[外部来源 A/B Suite](external-run-ab-suite.md)。
其中 tqdm-1、youtube-dl-3 与 Luigi-1 又在[完整固定 checkout](full-checkout-pilot.md)上通过并实际
运行 Code RAG；v2 三例已有本地可信执行和公开禁网 Docker 3/3。重复 CAS blob 校验的
规模开销已优化；youtube-dl-3 的
[完整 checkout 多阶段任务](multi-stage-full-checkout-pilot.md)还在 WorkItem 边界切换 Worker，
验证 epoch fencing、持久会话、跨 revision Memory/RAG 与最终检查。真实模型决策证据出现前，
仍不增加自动 replan、第二次修订或语义循环检测。
