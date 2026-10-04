# 证据驱动的 Run Memory：实现与恢复合同

更新：2026-10-02。本文描述当前已接入 `CodingAgentRunner` 的最小 Run Memory 垂直切片。
它只把控制器已经观察到的工具结果投影为有来源、可失效的运行内记忆，不把模型陈述、失败
猜测或跨项目经验冒充事实。

## 1. 已解决的问题

长轮次 Coding Agent 不能只依赖不断增长的聊天记录。旧工具轮次被压缩后，后续模型调用仍需
知道最近读取、修改和验证发生了什么；进程恢复时也必须证明重新注入的是原请求看到的同一份
记忆，而不是恢复后事件拼出的新版本。

当前实现提供：

- 从权威 `TOOL_CALL_SETTLED` / 当前 `TOOL_CALL_UNKNOWN` 事件确定性派生；
- 每条记忆绑定 Run、Task、WorkItem、事件、调用、workspace revision 和输出 Artifact；
- 区分 `success`、`error`、`cancelled` 与 `unknown`，失败证据不会被改写成成功经验；
- 工作区 revision 改变后，依赖旧 revision 的条目标为 `stale`；
- 未知副作用标为 `unresolved`，没有输出 Artifact，也不能被当作成功或失败事实；
- 每次模型调用绑定内容寻址的 `RunMemorySnapshot`，压缩后仍重新注入有界视图；
- 跨进程恢复按原 reservation 的事件序号重建快照并逐字段比较。

当前明确不提供：

- 跨 Run 的 Project Memory、用户级或跨项目记忆；
- embedding、向量库、Memory RAG 或语义聚类；
- 从模型自然语言、`submit` 总结或任意聊天内容自动提炼事实；
- 文件级依赖图、环境依赖失效、冲突合并或人工确认后的 repo-scope promotion；
- 真实任务上的记忆命中率、成功率增益或成本收益结论。

## 2. 权威关系与数据流

```text
Run EventLog + tool output Artifact + current workspace revision
  -> RunMemoryProjector
  -> RunMemoryEntry[]
  -> bounded RunMemorySnapshot
  -> content-addressed Artifact
  -> ContextProjection system binding
  -> ModelCallReservation(ref/hash/event boundary/count)
```

权威顺序是：

1. EventLog 证明工具调用是否已结算或仍未知；
2. 内容寻址 Artifact 保存精确工具输出；
3. `RunMemorySnapshot` 是可丢弃、可重建的有界投影；
4. 模型可见 system binding 只是快照的更小视图。

Memory 不覆盖 EventLog，不复制成第二套事实数据库。当前没有单独引入 `MemoryPort` 或外部服务，
因为 run-scope 投影可由现有事件和 Artifact 完整重建；等 Project Memory 真正需要查询、撤销和
跨 Run promotion 时再增加持久接口。

## 3. 条目合同

`RunMemoryEntry` 的关键字段：

| 字段 | 含义 |
|---|---|
| `memory_id` | 除自身外全部规范字段的 SHA-256，内容变化即 ID 变化 |
| `scope_type/scope_id` | 固定为 `run` 和当前 Run ID，禁止跨 Run 注入 |
| `task_spec_hash/work_item_id` | 绑定不可变任务与当前工作项 |
| `source_event_*` | 来源事件 ID、序号和事件 hash |
| `source_call_id/tool_name` | 来源工具调用 |
| `kind` | `observation`、`workspace_change`、`validation` 或 `unresolved_effect` |
| `outcome` | `success`、`error`、`cancelled` 或 `unknown` |
| `confidence` | 当前只能是 `observed`，不接受模型自报置信度 |
| `status` | `active`、`stale` 或 `unresolved` |
| `dependency_revision` | 该观察依赖的 workspace revision |
| `evidence_ref/hash` | 已结算输出的内容地址；二者必须相同 |
| `statement/excerpt` | 控制器生成的说明和有界原始输出片段 |

支持投影的工具仅为 `search_repo`、`read_file`、`retrieve_code`、`replace_text`、
`apply_patch`、`create_file` 和 `run_check`。三个写工具都投影为 `workspace_change`；`submit` 被明确排除，
因为它是模型提出的完成声明，不是外部验证事实。

已结算条目必须满足：输出 ref 等于输出 hash、Artifact 存在、大小不超过 512 KiB 且可按
UTF-8 解码。任一条件不成立均以完整性错误停止，不生成“差不多正确”的记忆。当前未知调用
必须没有 evidence/excerpt，并保留为 `unresolved`。悬空 `run_check` 经显式
`discard_check` 处置后会形成 outcome=`cancelled` 的 validation 条目；停止依据可以是操作者确认，
也可以是控制器对 tool call ID 标签容器的验证/停止/删除。其证据只说明结果被丢弃，不得投影为
检查通过或失败。

## 4. 快照、失效与上下文分层

`RunMemorySnapshot` 绑定 Run、Task、Plan version、WorkItem、覆盖到的事件序号、当前 workspace
revision 和所有相关事件的聚合 digest。默认最多保留最近 12 条，每条完整快照片段最多 240
字符；`total/included/omitted` 和 `active/stale/unresolved` 计数必须与条目一致。

多 WorkItem Run 中，Entry 的 `work_item_id` 来自产生工具事件时最近一次持久 AgentSession；
Snapshot 的 `work_item_id` 则是当前模型请求所处阶段。因此当前快照可以携带早期阶段的有来源
条目，不能把它们重写成当前阶段事实。

当前失效策略有意保守：条目的 dependency revision 与当前 revision 不同即为 `stale`。因此任意
工作区修改会使此前读取或验证失效，即使实际修改的是无关文件。这会损失召回精度，但不会把旧
证据继续冒充当前事实；文件级依赖重检属于后续增量。

模型可见层比快照更小：只注入最近 2 条，每条带来源 WorkItem，active excerpt 最多 80 字符，
并附快照 ref、omitted/stale/unresolved 计数。stale 条目保留来源状态但隐藏 excerpt；
unresolved 条目没有事实文本。完整快照 Artifact 仍保存最多 12 条和 240 字符证据片段，
EventLog/原输出仍完整保留。

当完整 transcript 超过字符预算，或包含工具 Schema 的完整请求超过保守 input-token 上界时，
ContextProjector 先保留不可压缩合同、最近/未完成工具单元和 Run Memory binding，再把旧完整
单元折叠为带 digest 的确定性摘要。若摘要仍超限，
按最旧到最新顺序丢弃摘要细节，并记录 retained/omitted fact count；聚合 digest 始终保留。
如果连合同、记忆 binding、聚合 digest 和必须保留的近期单元都放不下，则拒绝请求。

## 5. 模型调用与精确恢复

每次新模型调用按以下顺序执行：

```text
capture one workspace revision
  -> build/store MandatoryFactLedger
  -> project/store RunMemorySnapshot at current Run.seq
  -> build/store ContextProjection
  -> reserve ModelCall with all refs/hashes/counts
  -> dispatch Provider
```

`ModelCallReservation` 记录 memory ref/hash、`covered_event_seq` 和 included count。恢复一个已持久化
响应时，新 Worker：

1. 回读 reservation 绑定的 Memory Artifact 并校验内容地址、计数和 session revision；
2. 用 `EventStore.get(..., at=covered_event_seq)` 取得历史 Run；
3. 只读取该边界以前的事件重建 Memory；
4. 要求重建对象与记录对象完全相等；
5. 再重建 ContextProjection 和 ModelRequest，校验请求 hash 后消费原 response。

必须使用历史边界：只读调用在原 response 后可能被标记 unknown，再经人工授权取消并重试；
这些恢复事件不能倒灌到原模型请求的记忆，否则请求 hash 会漂移并错误触发二次模型调用。

## 6. 配置与公开能力

`AgentLoopConfig` 当前相关项：

```text
max_run_memory_entries = 12
run_memory_excerpt_chars = 240
```

配置范围分别为 1～50 条和 0～1,000 字符。它们属于请求恢复合同；存在待恢复 response 时，
改变配置会使重建快照不一致并保守拒绝。`horizon doctor` 公开报告：

```json
{
  "memory_enabled": true,
  "memory_profile": "evidence_backed_run_projection",
  "run_memory_enabled": true,
  "project_memory_enabled": false,
  "model_claim_memory_promotion": false,
  "memory_revision_invalidation": true
}
```

## 7. 已验证边界

离线测试覆盖：

- 成功读取与失败验证均保留真实 outcome；
- workspace revision 变化使旧观察变为 stale；
- `submit` 模型声明不进入 Memory；
- 条目上限与 omitted count；
- unknown effect 无 evidence、无 excerpt、不可推断成功/失败；
- output hash 与 Artifact ref 不一致时拒绝；
- active excerpt 可见，stale excerpt 不注入模型；
- Run Memory ref/hash/count 在字符压缩后仍绑定；
- 正常 read/edit/check 流中旧 read 失效、最新 edit/check 保持 active；
- response settlement 中断与显式只读重试后，按历史事件边界恢复且不重复模型调用。

这些验证使用 Scripted Fake Model 和本地 Artifact/Event Store，不联网、不调用付费模型。它们
证明投影、失效和恢复合同，不证明模型会有效利用记忆。Project Memory 只有在 repo identity、
文件级依赖重检、显式 promotion/revoke 和 benchmark namespace 隔离同时具备后才应启用。
