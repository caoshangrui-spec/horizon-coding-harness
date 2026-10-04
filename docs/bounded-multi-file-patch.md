# 有界多文件精确 Patch 与崩溃恢复

更新：2026-10-02。本功能让 Agent 能在一次工具调用中修改少量相互关联的既有文件，同时沿用
Horizon 的持久化 intent/receipt、workspace revision、manifest 和显式恢复合同。它解决的是
“一个实现需要同步改源码与测试，但进程可能在文件之间退出”的主执行链问题，不是通用 diff
解释器或文件系统事务。

## 1. 工具合同

WorkItem 必须显式授权 `apply_patch`。模型参数是结构化的精确替换列表：

```json
{
  "edits": [
    {
      "path": "src/parser.py",
      "old": "return [value]",
      "new": "return [] if value == '' else [value]",
      "expected_occurrences": 1
    },
    {
      "path": "tests/test_parser.py",
      "old": "assert parse('') == ['']",
      "new": "assert parse('') == []",
      "expected_occurrences": 1
    }
  ]
}
```

首版边界故意较窄：

- 每批 1～8 个 edit，且每个 path 只能出现一次；
- 只允许 TaskSpec allowlist 内、denylist 外的既有普通文件；
- 禁止绝对路径、反斜杠、盘符、遍历、symlink 和 junction；
- 文件必须是 UTF-8，修改前后均不超过 256 KiB；
- `old` 非空，`old/new` 各不超过 64 KiB，二者必须不同；
- `old` 实际出现次数必须等于 `expected_occurrences`，范围 1～10；
- 不新增、不删除、不重命名文件，不解释 unified diff，不做模糊上下文匹配。

这些限制让预期后态能完全由“派发前 manifest + 已持久化参数”重建，而不是让恢复过程猜测
模型意图。

## 2. 正常执行顺序

```text
校验 Tool Schema 与 WorkItem 权限
  → 保存 TOOL_CALL_RESERVED（arguments hash + pre revision + pre manifest）
  → 解析全部路径并从 pre manifest 读取不可变前像
  → 在内存中验证全部 occurrence、编码和大小
  → 再次确认 live 文件仍等于前像
  → 逐文件用同目录临时文件 + fsync + os.replace 写入
  → 捕获 after manifest，并与推导出的唯一 expected revision 比较
  → 保存内容寻址输出和 TOOL_CALL_SETTLED
```

任何一个 edit 在预校验阶段失败，第一处写入都不会发生。普通 Python 异常若出现在批次中间，
Gateway 只回滚仍精确等于本次预期后像的已写文件，避免覆盖并发外部修改。若回滚本身无法证明
安全，异常继续向上冒泡，持久 intent 保持待对账，而不是伪造成功 receipt。

## 3. 硬退出后的状态判定

进程可能在任意 `os.replace` 后直接消失，多个文件无法与 SQLite receipt 形成跨资源原子事务。
新 Worker 因此只接受三态确定性判定：

| live workspace | 判定 | 可执行处置 |
|---|---|---|
| revision 等于 pre manifest | `pre_effect` | 显式 rollback，按 cancelled 结算 |
| revision 等于从全部 edit 推导出的 expected manifest | `expected_effect` | 显式 accept，或显式 rollback |
| 仅部分文件改变、目标被再次修改、或存在任意额外漂移 | `diverged` | 保持 unknown，禁止自动接纳、回滚和重派 |

因此“两个文件里只写完一个”是明确的负结果，不会被当作部分成功。恢复不会再次调用模型，也
不会重放原工具。可信本地入口为：

```powershell
horizon agent resolve-tool <run-id> <tool-call-id> --accept-write
horizon agent resolve-tool <run-id> <tool-call-id> --rollback-write
```

旧的 `--accept-replace` / `--rollback-replace` 保留为兼容别名。处置结果分别记录
`accept_patch` / `rollback_patch`，并把原 assistant tool call 与新 tool observation 原子写入
下一版 AgentSession；随后 reconciliation 才能允许继续。

## 4. 与上下文、Memory 和预算的关系

- 工具 Schema 进入 MandatoryFactLedger；恢复时 Schema 漂移会阻止消费旧响应。
- 成功、错误、取消和 unknown 都进入权威事件流；`apply_patch` 的已结算结果在 Run Memory 中
  属于 `workspace_change`，旧 revision 的观察按既有规则变为 stale。
- `apply_patch` 进入 NoProgressPolicy。成功改变 revision 的调用不会形成同 revision streak；
  没有改变 revision 的重复错误调用会被有界拒绝。
- accept/rollback 是离线对账，不联网、不调用付费模型；原不确定尝试仍按既有工具预算语义
  计数，不通过恢复“退费”。

## 5. 已验证与未覆盖

离线测试覆盖：两文件成功修改、第二文件预校验失败时零副作用、第二次写入抛异常时回滚、
expected effect 的 accept/rollback、部分 effect 拒绝、AgentSession 不重放模型续跑，以及真实
子进程在完整多文件 effect 后 `os._exit` 再由新 Worker 接纳。完整测试命令与最新总数见
[开发进度](development-progress.md)。

仍未覆盖：OS/文件系统提供的真正多文件原子提交、多文件新增、删除/重命名、任意 diff、同文件
多段编辑、冲突合并、恶意并发写入的生产级隔离，以及对真实 Issue 成功率的提升。source
promotion 已可显式提升最多 8 个 UTF-8 变更（其中至多 1 个新文件），并能从每个目标仍处于 before/after 的部分
effect 继续；它仍不创建 commit，也不会接纳出现第三种内容或额外漂移的源目录。
