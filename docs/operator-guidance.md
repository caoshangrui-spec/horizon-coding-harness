# 执行停滞后的可恢复人工指导

更新：2026-10-02。本文描述已经实现的窄 Human-in-the-loop 路径：当确定性
NoProgressPolicy 证明 Agent 在同一 workspace revision 上持续重复完全相同动作，或陷入精确
`A→B→A→B` 双动作循环时，Run 不直接失败，而是持久化一个人工指导请求。它不是通用审批
平台，也不允许人类扩大原任务权限、验收或预算。

## 1. 触发条件

当前唯一原因码仍是单 tool-call 轮次的 `repeated_action_no_progress`，但请求用 `pattern` 区分
两类证据：

| `pattern` | 软阻断 | 进入人工等待 |
|---|---|---|
| `identical_action` | 第三个相同精确动作 | 第四个仍相同 |
| `alternating_two_action_cycle` | `A,B,A,B` 后继续提交 A | 下一轮继续提交 B，形成 `A,B,A,B,A,B` |

精确动作由 `tool_name + arguments_hash` 定义；窗口内所有 receipt 都必须具有相同且不变的
workspace revision。当前仅覆盖 `search_repo`、`read_file`、`retrieve_code`、`replace_text` 和
`apply_patch`。成功修改 workspace、改变动作、Plan/WorkItem 切换或已消费的人工指导会切断
窗口。

Harness 总是先记录拒绝 observation 并保存完整 AgentSession，再创建请求。请求创建与 Trace
重放都会复核精确 receipt 形状、最后一条 call ID、policy 文本的 SHA-256 Artifact，以及当前
Task/Plan/WorkItem/workspace/session 绑定。普通工具错误、被篡改的文本或只有相似含义的动作
不能满足这份合同。

## 2. 持久合同

`HumanGuidanceRequest` 位于 [`domain/human.py`](../src/horizon/domain/human.py)，绑定：

| 字段 | 约束 |
|---|---|
| `reason_code` | 固定为 `repeated_action_no_progress` |
| `pattern` | `identical_action` 或 `alternating_two_action_cycle` |
| `source_tool_call_id` / `evidence_artifact_ref` | 指向最后一条拒绝 receipt 及其内容寻址证据 |
| `detail` | 必须与证据 Artifact 的 SHA-256 一致，并包含对应 policy 模式标记 |
| `task_spec_hash` | 人工指导不能修改任务合同 |
| `plan_version` / `plan_hash` | 指导只适用于当前 Plan |
| `work_item_id` | 指导只适用于当前未完成工作项 |
| `workspace_revision` | 工作区漂移后不得消费旧指导请求 |
| `agent_session_artifact_ref` / `next_iteration` | 绑定已保存的完整会话和准确续跑位置 |
| `resume_state` | 固定为 `RUNNING` |

`HumanGuidanceDecision` 记录本机 `local_cli` actor、request ID，以及加入指导后的新
AgentSession Artifact。指导正文保存在完整会话 Artifact 中；事件流保存内容地址而不是复制
指导正文。旧 Trace 未带 `pattern` 时按 `identical_action` 解析，保留已有相同行为请求的重放
兼容性。

## 3. 原子状态变化

触发侧在一个 SQLite 命令事务中提交：

```text
HUMAN_REQUEST_CREATED
STATE_CHANGED: RUNNING -> WAITING_FOR_USER
LEASE_RELEASED
```

因此进程在返回 CLI 前退出，也不会留下“已经要求人工但 Worker 仍持有”的半状态。等待期间
不调用模型、不执行工具，也不增加费用。`agent reconcile` 将其识别为已知人工边界，返回
`next_action=provide_operator_guidance`，不会误报成未知副作用。

消费指导时，CLI 取得短 Lease，核验请求和原 AgentSession，追加一条带固定权限声明的 user
message，并写入新内容寻址 Artifact。随后在一个事务中提交：

```text
HUMAN_DECISION_RECORDED
STATE_CHANGED: WAITING_FOR_USER -> RUNNING
AGENT_SESSION_GUIDED
LEASE_RELEASED
```

`AGENT_SESSION_GUIDED` 保持原 `next_iteration`，所以人工输入不伪装成一次模型调用，也不跳过
model iteration；它必须增加消息数、保持 Task/Plan/WorkItem/workspace 不变，并覆盖紧邻的状态
事件。新 Worker 随后按普通 RUNNING 安全边界恢复。

## 4. 使用方式

初次运行或续跑若因停滞暂停，JSON 输出包含 `human_request`（含 `pattern`）和可复制的
`next_action`：

```powershell
uv run --locked horizon agent guide <run-id> .\guidance.txt
uv run --locked horizon agent resume <run-id> `
  --image redis:7-alpine --confirm-paid
```

指导文件必须是 UTF-8，文件不超过 32 KiB，去除首尾空白后的正文为 1～8000 字符。
`agent guide` 本身离线且不调用付费模型；后续 `agent resume` 仍需显式 `--confirm-paid`。

建议只写具体证据或下一动作，例如“检查 parser 的空输入分支，做最小精确替换后 submit”。
即使指导要求越权，Typed Tool Gateway 仍按原 TaskSpec、Plan 和 WorkItem 的交集执行；指导不会
新增路径、工具、验收命令或预算。

## 5. 重置与恢复语义

成功消费指导时，Run 把 `no_progress_reset_tool_count` 移到当前 receipt 尾部，后续两种模式都
只检查新边界之后的工具历史。否则 Agent 恢复后的第一个动作可能被旧循环立即误拦截。Plan
变化和 WorkItem 切换也重置该边界；普通进程重启不会重置。

新 session event 已纳入 Agent 安全边界检查、response receipt 恢复、RecoveryService、Run
Memory、JSONL Trace 导出、重放和 projection hash。取消或硬截止进入终态时，未决请求从当前
投影清除，原 request 事件仍留在追加式 Trace 中。

## 6. 已验证与未完成

离线测试已覆盖两种 pattern 的精确触发、非循环负例、证据文本/revision 篡改拒绝、请求原子
提交、Lease 释放、重启 reconciliation、人工决定、新 session、新 Worker 恢复，以及 CLI
`run → WAITING → guide → resume → SUCCEEDED`。这些测试不产生网络或模型费用。

仍未实现：自由文本澄清、多选审批、审批 TTL/过期、多用户身份、通用高风险工具批准、
period-3+ 或语义等价循环、自动 replan，以及真实模型上指导的成功率/成本收益评测。因此准确
表述是“一个由可重放 NoProgress 证据触发的可恢复人工指导通道”，不是“通用 HITL 系统”。
