# 受限单文件创建

更新：2026-10-05。本能力补齐 Coding Agent 在真实任务中“需要新增一个小型源码或测试文件”的
最小垂直路径，同时保留 Harness 的核心原则：权限先于执行、intent 先于副作用、成功必须有
receipt、未知效果不自动重放。

## 1. 支持范围

`create_file` 的模型参数只有：

```json
{
  "path": "src/generated.py",
  "content": "def generated():\n    return 1\n"
}
```

控制器固定执行以下约束：

- Task 必须是 `workspace_write`；
- 当前 WorkItem 必须显式包含 `create_file`；
- `path` 必须是无盘符、无反斜杠、无 `.`/`..` 的相对 POSIX 路径；
- 路径必须是可移植的字面文件名，拒绝 glob 字符、NTFS 保留设备名和尾随点/空格别名；
- 路径必须命中 TaskSpec `allowed_paths` 且不命中 `denied_paths`；
- workspace、父目录链和目标都不能是 symlink 或 junction；
- 父目录必须已经存在且位于 workspace 内，工具不会顺带创建目录；
- 目标必须不存在，使用排他创建，永不覆盖同名文件；
- `content` 必须是字符串，UTF-8 编码后最多 64 KiB；空文件允许；
- 一次调用只创建一个文件，不接受目录树、base64 二进制、diff 或多文件数组。

字符数限制同时出现在模型 Tool Schema 中，真正的 64 KiB 字节限制仍在 Gateway 内执行，避免
多字节 Unicode 用较少字符绕过边界。

## 2. 正常执行与证据链

执行顺序为：

```text
recorded ModelResponse(arguments with exact content)
  -> TOOL_CALL_RESERVED(arguments_hash, pre_revision, pre_manifest)
  -> exclusive create + UTF-8 bytes + flush/fsync
  -> capture post-effect snapshot
  -> TOOL_CALL_SETTLED(output Artifact, pre/post revision, post_manifest)
```

intent 不复制可能较长的明文 content，而是保存规范化参数的 SHA-256；完整参数保存在先于工具
调用落盘的不可变 ModelResponse Artifact 中。恢复或审计时可以重新计算参数 hash，确认 path 和
content 与 intent 相同。成功输出记录新文件的 UTF-8 字节数和内容 SHA-256，receipt 再绑定输出
Artifact、前后 workspace revision 与包含新文件的 post-effect manifest。

成功 receipt 会进入 EventLog、Trace 和 Run Memory。Memory 将它分类为
`workspace_change`；后续 workspace revision 改变时，该观察按既有规则变为 stale，而不会被当成
跨 revision 的当前事实。

## 3. 失败与崩溃语义

可在当前进程中观察到的普通异常采用“本调用创建、本调用清理”规则。Gateway 保存排他创建后
文件的设备/文件标识；如果写入、同步或后置 snapshot 失败，只在 live path 仍指向该文件时删除
它，避免误删并发替换的另一个对象。参数、权限、目标存在、父目录缺失或字节超限都发生在成功
receipt 之前，并返回有界 error observation。

进程硬退出不同：文件系统副作用可能已经完整发生，也可能只写入部分字节，但 SQLite 中只有
持久化 intent。重启 reconciliation 仍先把该调用标为 `unknown`，阻止 Agent 自动继续、再次创建
同一路径或把“目标存在”直接当成成功；系统绝不自动重放未知创建。

操作者随后可以使用 `agent resolve-tool --accept-write` 或 `--rollback-write`。恢复端从已落盘的
ModelResponse 重新取得 path/content，复核参数 hash、当前 WorkItem 权限、派发前 revision 和
pre-manifest，再确定性构造唯一 expected manifest：原文件清单加上一个 path、UTF-8 字节数、
content SHA-256 和 `executable=false` 均精确匹配的新文件。只接受三种分类：

| live workspace | 分类 | 允许决定 |
|---|---|---|
| 精确等于 pre-manifest，目标不存在 | `pre_effect` | 仅 rollback；以 cancelled 结算，不创建文件 |
| 精确等于唯一 expected manifest | `expected_effect` | accept 为 success；或 rollback 删除精确新文件 |
| 部分/错误内容、额外文件漂移或其他 revision | `diverged` | 两种决定都拒绝，继续保持 unknown |

rollback 会在删除前再次核对目标是普通文件、内容和文件元数据未变；删除后必须恢复到完整
pre-revision。若期间发现并发漂移，会尽力重建精确 expected 文件并保持原调用 unknown，不会把
冲突现场结算成功。成功处置把原 tool receipt 与包含 recovery observation 的下一 AgentSession
原子提交，因此续跑不会重放已结算的模型响应。

这证明的是“完整前态/唯一完整后态可由人工显式处置”，不是任意崩溃点自动恢复。恶意并发、
同内容对象替换、目录元数据事务和跨 SQLite/文件系统的单一原子提交仍不在保证范围内。

## 4. 自动计划、上下文与停滞保护

写模式的自动 Planner 可以把 `create_file` 分配给 WorkItem。完整 repository inventory 通常会
拒绝不存在的路径；对同一 WorkItem 已授权 `create_file` 的缺失路径，控制器只在它仍满足
TaskSpec allow/deny 范围时允许 Plan 通过。父目录和目标状态继续由执行时 Gateway 校验。Planner
prompt 要求只有不可变任务确实需要新路径时才使用该能力，不能把路径猜测伪装成检索证据。

`create_file` 也进入精确 NoProgressPolicy。第一次成功会改变 revision，因此不会形成无进展
窗口；若模型反复对已存在目标提交相同参数，错误 receipt 保持同一 revision，既有相同动作与
精确 A/B 循环规则会阻止继续浪费预算。

## 5. 显式源目录 promotion

`create_file` 仍只作用于隔离 staging workspace；Run 成功并通过保护性验证后，操作者可以先用
`horizon agent diff` 审阅计划，再用 `horizon agent promote --confirm-promote` 把候选结果显式
应用回源目录。promotion 总共接受 1–8 个变更，其中最多一个 `created` 文件；新增文件继续受
TaskSpec 路径权限、UTF-8 和 64 KiB 上限约束，且父目录必须已经存在。

新增文件的 PromotionPlan 固定记录：

- `kind=created`；
- `before_sha256=null`；
- `after_sha256` 为候选文件的精确内容哈希；
- 审阅 diff 从 `/dev/null` 指向新增路径，空文件也保留明确的审计头；
- 原始 source revision/manifest、候选 checkpoint revision/manifest，以及 Git 仓库存在时的
  HEAD 都绑定进计划。

应用时，目标不存在才会排他创建；目标已经等于 `after_sha256` 时按已发生效果恢复；同名但内容
不同则判定 source divergent 并停止。普通异常只回滚本次创建且身份仍一致的文件。若进程在
新增文件落盘后硬退出，重启可以从精确 after-state 恢复；新增与既有文件修改混合时，也可识别
已完成的局部效果并继续剩余变更。最终必须得到与候选 manifest 完全一致的 source revision，
随后才写入 promotion receipt，因此重复执行保持幂等。

这仍不是通用文件事务：不接受一次 promotion 新增多个文件，也不支持删除、重命名、创建目录
或自动 Git commit。

## 6. 已验证场景

- Gateway：精确 UTF-8/多字节写入、字节数和 SHA-256、post-effect manifest；
- Gateway：已存在目标、越权/快照不可见路径、缺失父目录、多字节超限均无副作用；
- Gateway：后置 snapshot 普通失败时仅清理本次创建；
- Tool Schema：只有 WorkItem 显式授权时才向模型暴露；
- Planner：完整 inventory 接受范围内的新路径，拒绝 denied/traversal 路径；
- Agent E2E：`create -> run_check -> submit -> protected validation` 成功，intent/receipt、Run
  Memory 和 Trace replay 一致；
- 真实子进程故障注入：文件创建后、receipt 前 `os._exit`，重启先保持 `unknown`、不重放；
  精确后态可显式 accept 或 rollback，处置后 Trace 可离线重建同一 Run；
- Create recovery：前态 rollback、精确后态 accept/rollback、错误/部分内容和额外漂移拒绝、CLI
  离线处置及下一 AgentSession 恢复均已覆盖；
- Create rollback：删除后检测到并发 workspace 漂移时重建 expected 文件并继续阻断；
- Promotion：新增文件 dry-run 审阅、显式应用、重复调用幂等，以及 Trace replay 一致；
- Promotion：空文件、权限/编码/大小边界、多个新增或删除均按约束处理；
- Promotion 故障注入：普通失败按对象身份回滚，硬退出后的精确已发生效果可恢复，错误同名文件
  则保留现场并阻断。

这些证据证明的是受限 Harness 路径，不是生产级文件系统事务、安全沙箱或真实模型任务成功率。

## 7. 明确不做

- 不创建父目录；
- 模型工具不提供覆盖、append、delete、rename、chmod 或生成 symlink；恢复 rollback 只删除
  与未知 intent 唯一后态完全一致的那个新文件；
- 不一次创建多个文件；
- 不支持二进制内容或任意 patch/diff；
- 不在一次 promotion 中新增多个文件，也不删除、重命名或自动提交 Git；
- 不把硬退出后的“目标存在”单独推断为成功，必须匹配完整 expected manifest；
- 不因此宣称完整 CRUD、通用写工具或任意副作用恢复已经完成。
