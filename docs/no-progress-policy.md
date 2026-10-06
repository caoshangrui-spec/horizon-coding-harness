# 精确模式无进展保护

更新：2026-10-02。该策略用于阻止长程 Agent 在 workspace 没有变化时反复执行可证明的
无进展动作，持续消耗模型与工具预算。它是确定性的窄 circuit breaker，不是通用重试、
fallback、语义级停滞判断或自动 replan。

## 1. 受保护动作与证据键

当前只保护单 tool-call 模型轮次中的：

- `search_repo`
- `read_file`
- `retrieve_code`
- `replace_text`
- `apply_patch`
- `create_file`

一个精确动作签名是：

```text
(tool_name, normalized arguments_hash)
```

检测只读取持久化 Tool receipt，并从最近一次 `no_progress_reset_tool_count` 之后取连续尾部。
尾部中每条 receipt 必须满足：

```text
tool 属于受保护集合
workspace_revision_before == 当前 revision
workspace_revision_after  == 当前 revision
```

因此，成功改变 revision 的编辑、`run_check`、`submit`、其他工具、人工指导、Plan 变化或
WorkItem 切换都会形成边界，不会与更早历史拼成循环。普通进程重启不会清零，因为 receipt 和
reset 边界都由 EventLog 重建。

## 2. 当前两个精确模式

### 2.1 完全相同动作

默认 `max_identical_no_progress_actions=2`：

1. `A, A` 正常派发到 Tool Gateway；
2. 第三次 `A` 不再执行真实读取/检索/编辑，控制器写入一条有 Artifact 的 error receipt；
3. 模型收到反馈后若第四次仍选择 `A`，控制器再写一条拒绝 receipt，保存完整
   AgentSession，并进入人工指导等待。

### 2.2 双动作交替循环

新增的固定 period-2 模式要求 `A != B`，且 A、B 都按精确签名比较：

1. `A, B, A, B` 允许实际派发；
2. 第五步继续为 `A` 时，控制器识别 `A, B, A, B, A`，写入软阻断 error receipt；
3. 模型收到反馈后第六步仍为 `B` 时，证据窗口成为 `A, B, A, B, A, B`，控制器写入第二条
   拒绝 receipt 并进入人工指导等待。

A、B 可以是不同工具，也可以是同一工具的两组不同参数。只要参数 hash、顺序或 revision
不同，就不是这个模式。固定窗口而非任意循环图，是为了先覆盖常见浪费模式，同时限制误拦截
面和实现复杂度。

## 3. 阻断、证据和人工等待

软阻断不会伪装成 Gateway 成功，也不会把工具用量退回为零。每条拒绝仍经过 tool reservation
和 settlement，具有内容寻址 Artifact、一次 tool/step 用量，以及完整 assistant→tool 消息配对。
error observation 会进入 Run Memory，使压缩后的上下文仍能看到近期阻塞原因。

硬阻断前，Harness 先保存包含最后一条 observation 的 AgentSession。随后服务层与事件重放层
共同调用同一纯校验器，检查：

- `identical_action`：最近两条都是相同精确签名、同一不变 revision 的拒绝 receipt；
- `alternating_two_action_cycle`：最近六条签名严格为 `A,B,A,B,A,B`，A 与 B 不同，全部位于
  同一不变 revision，且最后两条是拒绝 receipt；
- 最后一条 call ID、Artifact 引用、请求中的 policy 文本及其 SHA-256 内容地址一致；
- 所有动作均属于受保护集合，请求绑定当前 Task、Plan、WorkItem、workspace 和 AgentSession。

校验通过后，同一 SQLite 命令原子提交 `HUMAN_REQUEST_CREATED`、
`RUNNING → WAITING_FOR_USER` 和 `LEASE_RELEASED`。等待与 `agent guide` 不调用模型；恢复仍受
原 TaskSpec/Campaign 预算约束。详细合同见
[执行停滞后的可恢复人工指导](operator-guidance.md)。

## 4. 明确保留的边界

当前实现不会：

- 判断两个不同参数是否语义等价；
- 检测 period-3 或更长循环、跨 revision 循环或多 tool-call 响应内的图模式；
- 根据错误类型自动改参数、切换工具或切换模型；
- 自动重试、提高预算或修改 Plan；
- 覆盖现有 model-iteration、tool/step、repair-cycle 和费用硬上限。

Provider 的 `retryable` 标签同样不会绕过这条边界：没有服务端幂等证据时，未知请求仍按未知
费用/副作用处理，而不是偷偷重派。

## 5. 当前验证证据

离线 Scripted Model 和领域测试覆盖：

- `A,A` 正常执行，第三个 A 被拒绝，模型改为编辑后可成功；
- 第四个 A 继续重复时，请求记录 `pattern=identical_action` 并进入可重放 WAITING；
- `A,B,A,B` 正常执行，第五个 A 软阻断，第六个 B 触发
  `pattern=alternating_two_action_cycle`；
- `A,B,A,B,C` 不被误拦截；
- 篡改 policy 文本、模式或任一 workspace revision 后，证据校验失败；
- 指导决定重置检测边界，新 Worker 可从原 iteration 续跑并完成保护验收；
- CLI Fake E2E 完成 `run → WAITING → guide → resume → SUCCEEDED`，指导步骤零模型调用。

另有一份与生产 Agent Loop 共用 `classify_no_progress` 的冻结策略清单，覆盖 revision、mutation、
非受保护 receipt 和 reset 等负例；当前 7 个 NoProgress 案例的逐步判断无 false positive/negative。
完整输入、公式和内容寻址报告见[控制器可靠性策略离线评测](reliability-evaluation.md)。

这些结果证明确定性合同和恢复路径可工作，不证明真实模型上的误拦截率、成本收益或任务成功率
提升。执行模型现在可在软阻断后显式提交一次受限 replan；五个来源绑定的依赖裁剪案例已在
本地可信与公开禁网 Docker 路径形成完整 Run Trace 5/5；三个不同项目的完整 checkout 也已有
本地可信 Trace 和公开禁网 Docker 3/3，但仍缺真实模型决策。是否增加
period-3、语义
检测、自动 replan 触发或第二次修订，继续由真实模型失败分布和对照收益决定。
