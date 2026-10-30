# 上下文容量暂停与续跑 EvidencePack

更新：2026-10-30。

## 1. 目标

`horizon demo context-capacity-recovery` 把不可压缩上下文的恢复合同做成一条零付费、可脱离
运行数据库复核的完整证据链。它固定执行以下过程：

1. 创建一个必保任务前缀超过 5,000 字符的单 WorkItem 任务；
2. 第一任 Worker 使用 `max_context_chars=2,000`，在 Campaign/Run reservation 和 Provider
   派发前生成内容寻址 `ContextCapacityStop`；
3. 同一事务写入 `context_capacity_required`、`RUNNING → WAITING_FOR_USER` 和 Lease release；
4. 确认停止边界前没有 `MODEL_CALL_RESERVED`、`TOOL_CALL_RESERVED` 或 Campaign attempt；
5. 重新打开 SQLite、ArtifactStore、SnapshotManager 与 Campaign ledger，由下一 Lease epoch
   读取原请求；
6. 本机可信决定只提高字符/token 上限，不改任务、Plan、workspace、iteration 或消息；
7. `context_capacity_retry_authorized`、`WAITING_FOR_USER → RUNNING` 和消息不变的新
   AgentSession boundary 原子发布；
8. 冻结 Scripted Model 随后执行 `replace_text → submit`，控制器运行 required protected check，
   最终进入 `SUCCEEDED`；
9. 导出通用终态 EvidencePack，并在根包中额外保存 stop、阻断 session、续跑 session 与确定性
   摘要；返回前执行一次递归离线验真。

这条演示复用生产 `CodingAgentRunner`、ContextProjector、HITL、Lease、预算账本、工具网关、
checkpoint、protected validation 和 Trace reducer。模型动作是冻结脚本，验收器只比较 fixture
文本；不调用 API、网络、Docker，也不执行仓库代码。

## 2. 运行与独立验证

```powershell
$result = uv run --locked --cache-dir .uv-cache horizon demo context-capacity-recovery |
  ConvertFrom-Json

uv run --locked --cache-dir .uv-cache horizon demo verify-context-capacity-recovery `
  "$($result.evidence_pack)"
```

默认输出到 `.horizon/demos/context-capacity-<随机 ID>/`。也可以用 `--output <new-dir>` 指定
目录；目标已存在时拒绝覆盖。运行期 SQLite、Campaign 和 workspace 位于一次性私有目录，成功
导出后即删除，因此第二条命令只能依赖 EvidencePack，不能回读 live controller 状态。

成功输出的关键字段包括：

```json
{
  "status": "SUCCEEDED",
  "stop_reason": "protected_prefix_exceeds_limit",
  "configured_context_chars": 2000,
  "required_context_chars": 8341,
  "blocked_lease_epoch": 1,
  "recovery_lease_epoch": 2,
  "provider_dispatch_before_resume": false,
  "transcript_preserved": true,
  "required_validation_passed": true,
  "paid_model_called": false,
  "external_cost_cny": 0
}
```

`required_context_chars` 由当次实际 request/tool schema 计算，示例中的 `8341` 不是固定值；验真
只要求它严格高于冻结的 2,000 字符门限。

## 3. 文件合同

```text
context-capacity-<id>/
├── context-stop.json
├── blocked-agent-session.json
├── resumed-agent-session.json
├── SUMMARY.md
├── evidence-pack.json
└── terminal/
    ├── trace.jsonl
    ├── final-run.json
    ├── SUMMARY.md
    └── evidence-pack.json
```

根验证器先校验五个直接文件的字节数与 SHA-256，再递归调用通用终态包验证器。随后它会：

- 从完整 Trace 重放最终 Run，要求状态为 `SUCCEEDED` 且 required check 已知通过；
- 要求 HumanRequest 后紧邻等待状态和 Lease release，下一 Lease epoch 后紧邻 decision、运行状态和
  resumed-session 发布；
- 要求第一条 `MODEL_CALL_RESERVED` 严格晚于 resumed-session，且此前不存在 model/tool call
  reservation、settlement 或 unknown；
- 重新哈希 stop 与两个 AgentSession，并要求引用与 HumanRequest、HumanDecision、Trace record
  一致；
- 比较阻断/续跑 session 的全部消息，要求完全相同，同时复算 `source_digest`；
- 要求固定故事只产生 2 个离线 model receipt、3 个 tool receipt，终态 unknown/open effect 均为
  零；
- 重新生成根结构化 evidence 与 `SUMMARY.md`，拒绝只改文件后重算外层哈希的伪证据。

## 4. 可以和不能声称什么

当前可以声称：在一个固定单机任务中，Horizon 能在不可压缩前缀超限时先于模型派发持久等待，
释放旧 Lease，使用充分的本机容量决定和下一 epoch 恢复同一 transcript，并完成受保护验收；该
过程可在运行数据库和 workspace 不存在时从 Trace 与内容寻址 Artifact 递归复核。

当前不能声称：

- Horizon 已证明任意模型的官方 context window，或能自动选择最优新上限；
- 确定性截断等价于语义摘要，或已验证摘要事实保持质量；
- 该 Scripted Model 成功代表真实模型任务成功率；
- 单机 Lease 恢复覆盖跨主机、OS 掉电、分布式队列或任意并发故障；
- 自洽哈希构成来源签名、可信时间戳或第三方审计。

主 `horizon demo run` 仍只证明它实际执行的写入 effect-before-receipt 崩溃窗口；两个包各自保留
独立 claim scope，不把静态能力或另一条 Trace 拼成一个更大的结论。
