# 执行证据驱动的单次受限 Replan

更新：2026-10-02。Horizon 现支持一个刻意收窄的执行中计划修订切片：执行模型可基于本 Run
已经获得的代码、工具错误、NoProgress 或验证证据，调用一次 `revise_plan`，把当前 Plan vN
修订为 vN+1。该能力用于修正剩余工作结构，不是无限自我规划、额外 Planner 调用或权限扩张。

## 1. 触发与预算边界

`revise_plan` 是控制器工具，只在尚未成功 replan 时出现在模型 Tool Schema 中。它要求：

- `reason`：1～2000 字符，说明哪条执行证据使原计划不再合适；
- `items`：1～8 个完整 WorkItem，字段与初始 Plan 相同；
- 一次模型响应只能调用 `revise_plan`，不能与读、写、检查或 submit 混用；
- 每个 Run 最多成功修订 1 次；失败提案不消耗成功次数，但仍计 1 次 tool/step；
- replan 使用当前执行模型已经结算的响应，不另发 Planner 请求；所有模型、工具、step、token、
  wall-time 和 CNY Campaign 上限继续累计，不重置、不增加。

当前没有控制器自动触发器。模型必须显式选择 `revise_plan`；NoProgress 的软阻断只是可见证据，
不是“检测到一次就自动改计划”。如果模型继续精确停滞模式，仍进入现有可恢复人工指导路径。

## 2. 控制器不变量

模型提案先转成 `ExecutionReplanProposal`，再由控制器构造连续版本 Plan。接受前重复检查：

1. `new.version == old.version + 1`；
2. 新 Plan 仍是合法 DAG，最多 8 项；
3. 不虚构 acceptance ID，继续覆盖全部 required acceptance；
4. WorkItem 工具仍是当前 execution mode 的允许子集；
5. 所有已通过 WorkItem 必须以相同 ID 和逐字段完全相同的内容保留；
6. 剩余结构必须实际改变，且至少留下一个 dependency-ready 未完成项；
7. TaskSpec、路径权限、验收命令、模型策略和预算完全不变。

因此 replan 可以替换、拆分或重排尚未通过的部分，但不能改写已经验收的历史，也不能借计划
修订获得新工具、路径、检查或费用。最终 WorkItem 提交时仍重跑全部 required checks。

## 3. 原子事件合同

`revise_plan` 不调用 Workspace Tool Gateway，也没有文件副作用；它的唯一效果是控制平面状态。
Harness 把以下 8 个事件放在同一个 SQLite command 事务中：

```text
BUDGET_RESERVED(tool_calls=1, steps=1)
TOOL_CALL_RESERVED(name=revise_plan, arguments_hash=proposal_hash)
BUDGET_SETTLED
TOOL_CALL_SETTLED(status=success, unchanged workspace revision)
STATE_CHANGED: RUNNING -> REPAIRING
PLAN_REVISED(vN -> vN+1, execution_replan provenance)
STATE_CHANGED: REPAIRING -> RUNNING
AGENT_SESSION_SAVED(new Plan, exact next iteration)
```

事务提交前退出时 8 个事件都不可见；提交后退出时，新 Worker 直接从新 session 恢复，不会重复
模型调用或留下“Plan 已换但会话仍属于旧版本”的半状态。控制器同时绑定：

- 最新 settled execution model call ID；
- `revise_plan` receipt 和规范参数 hash；
- old/new Plan version 与 hash；
- 当前 workspace revision；
- 按旧 Plan 顺序记录的已完成 WorkItem ID；
- 新 session 的 Task、Plan version、active WorkItem、workspace、iteration 和事件游标。

Plan 修订、工具记账和 session 发布没有外部副作用，所以不需要另建未知副作用恢复协议。

## 4. Context、Memory 与 Trace

成功修订后，旧对话不会复制到新 Plan 会话；新 session 从新 active WorkItem 的 canonical 两消息
前缀开始。EventLog、模型 response Artifact、工具 outcome Artifact 和旧 AgentSession 仍完整
保留。Run Memory 按每个事件发生时的 Plan version 验证历史 session，因此被替换的未完成
WorkItem 证据不会被误报为损坏；它仍带原 WorkItem ID 和 revision，可作为有来源观察进入当前
有界 Memory。TaskSpec 修订则会清空旧任务 epoch 的 Memory 投影。

MandatoryFactLedger 在 replan 前包含 `revise_plan` Tool Schema；成功次数达到 1 后，该工具从
后续 schema 中消失，新的 tool-schema hash、Plan hash/version 和 active WorkItem 一起绑定每次
模型请求。JSONL Trace 可重放 `ExecutionReplanRecord`，并重建相同 projection hash。

## 5. 拒绝与 fallback

Schema 错误、无效 DAG、遗漏验收、越权工具、无实际变化、无可运行后继或修改已完成项时，
Harness 不修改 Plan。它记录一条 `ExecutionReplanPolicy` error receipt，给模型一个完整 tool
observation，并按原 session 的下一 iteration 继续。包含 `revise_plan` 的多 tool-call 响应会被
整体拒绝，其他调用也不执行，避免在旧/新计划边界混合副作用。

成功 replan 次数耗尽后，`revise_plan` 从声明给模型的工具列表移除；Provider 若仍虚构该调用，
普通 Tool Gateway 权限拒绝路径会记录 error，而不会静默执行第二次修订。

## 6. 已验证与未完成

离线 Fake Model E2E 已覆盖：

- 两次相同读取后第三次触发 NoProgress 软阻断，模型提交 Plan v2，随后编辑、submit 并成功；
- 双 WorkItem Run 完成第一项后，只替换剩余项，已完成项和 completed-memory 绑定保持不变；
- 试图修改已完成项时返回 policy error，Plan v1 继续并最终成功；
- 试图丢失 required check 或加入 `shell_exec` 等未知工具时在领域层拒绝；
- 成功后 `revise_plan` 从下一请求 Tool Schema 消失；
- replan 工具记账、Plan provenance、新 session 和状态变化原子提交；
- SQLite 导出的 JSONL Trace 重放得到相同 Run 投影。

冻结[控制器可靠性策略离线评测](reliability-evaluation.md)另含 5 个 replan 接受/拒绝案例，并与
生产路径共用 `check_execution_replan`；它只验证合同判断，不是模型决策质量或任务收益证据。
首个[完整 Run A/B](run-ab-evaluation.md)进一步保存 baseline 等待与单次 replan 成功的完整事件
Trace，并执行真实 Docker required check；同一 runner/report 合同还扩展到五个
[BugsInPy 来源的依赖裁剪案例](external-run-ab-suite.md)，本地可信与公开禁网 Docker 均为 5/5。
两个 arm 仍由冻结脚本指定动作，裁剪
案例本身不是完整上游 checkout；随后 tqdm、youtube-dl 与 Luigi 三个
[完整 checkout](full-checkout-pilot.md)也通过了相同对照并实际调用 Code RAG；v2 三例除本地可信
执行外，已由专用公开 CI 在禁网 Docker 中通过 3/3。youtube-dl 还完成了
[两阶段完整 checkout](multi-stage-full-checkout-pilot.md)：production 项通过后切换至 epoch 2
Worker，Treatment 保留完成项并只 replan regression 项。所有 arm 仍由冻结脚本指定动作，因此
都不是实际模型效果或官方 benchmark 分数。

全量测试和这些 E2E 都不联网、不调用 SiliconFlow。尚未验证真实模型是否会在正确时机 replan、
是否改善真实 Issue 成功率或成本，也未实现自动触发、第二次修订、replan 专用 Planner、并行
分支、Execution Fork、低置信澄清或失败 replan 的人工 Plan 替换。因此准确表述是“单次、
模型显式请求、控制器验证、原子可恢复的执行期 Plan revision”。
