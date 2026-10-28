# 模型上下文绑定的写入 CAS

## 1. 目标

长程 Agent 的模型请求可能持续数秒到数分钟。请求构建完成后，用户、另一个进程或前一个工具
仍可能改变工作区。如果控制器只检查 `old` 文本是否存在，模型就可能用旧上下文覆盖一个已经
更新过、但仍保留相同局部文本的文件。

当前实现把每个模型提出的 `replace_text`、`apply_patch` 和 `create_file` 绑定到该模型请求实际
看到的 workspace tree revision。它是保守的 compare-and-swap（CAS）：任何快照可见文件发生
变化，旧请求中的写动作都拒绝执行，并要求模型在新 revision 上重新取证。

这条能力解决的是“模型请求和写入派发之间的 stale write”。它不把模型的猜测升级为仓库事实，
也不声称文件系统、SQLite 和外部副作用已经组成一个通用事务。

## 2. 权威数据流

```text
构建模型请求
  └─ MandatoryFactLedger.workspace_revision = R_expected
       └─ MODEL_CALL_RESERVED / MODEL_CALL_SETTLED = source_model_call_id

模型返回写动作
  └─ 立即捕获 live snapshot
       ├─ workspace_revision = R_observed
       └─ manifest Artifact = M_observed
            └─ 每个文件条目包含 path / sha256 / size / executable

TOOL_CALL_RESERVED
  └─ WorkspaceWritePrecondition
       ├─ source_model_call_id
       ├─ expected_workspace_revision = R_expected
       ├─ observed_workspace_revision = R_observed
       ├─ observed_workspace_manifest_ref = M_observed
       ├─ target_paths
       └─ stale = (R_expected != R_observed)

stale=true  ──> error receipt / revision 不变 / 零写入
stale=false ──> Gateway 再次核对 revision + manifest + target preimage
                  └─ 写入后只接受唯一 expected post revision
```

workspace revision 不是时间戳。它是排序后的 manifest 文件条目摘要，而每个条目又带完整文件
SHA-256。因此 `R_expected == R_observed` 表示模型请求与派发前 manifest 在所有快照可见文件上
具有相同的内容、大小和 executable 元数据；`target_paths + M_observed` 可以离线定位每个被写
文件的精确 preimage hash。控制器自动建立该绑定，不要求模型复制哈希，避免把完整性字段交给
不可信模型文本。

## 3. 派发算法

1. 每次执行模型请求前，控制器把当前 workspace revision 写入内容寻址的
   `MandatoryFactLedger`，并把 ledger 绑定到 `ModelCallReservation` 和 ContextProjection。
2. 模型响应落为不可变 Artifact、费用结算并形成 `ModelCallRecord` 后，控制器处理工具调用。
3. 对三个写工具，控制器捕获派发前 checkpoint，建立 `WorkspaceWritePrecondition`，并与普通
   `ToolCallReservation`、参数 hash、Lease/epoch 一起写入 `TOOL_CALL_RESERVED`。
4. 如果模型请求 revision 与 checkpoint revision 不同，Gateway 返回
   `StaleWriteConflict`。receipt 的前后 revision 相同，写函数不会被调用。
5. 如果两者相同，Gateway 在真正写入前再次捕获 snapshot；这会拦截“已保存 intent、尚未进入
   handler”窗口里的并发变化。
6. Gateway 从 manifest 的 CAS blob 重建目标 preimage，比较 live bytes，并计算唯一 expected
   post snapshot。写入后重新捕获 snapshot；只有实际 revision 等于 expected revision 才成功。
7. 普通异常或并发 post-check 失败时，只在目标仍精确等于本次 expected bytes 时回滚本次写入；
   外部漂移不会被回滚代码覆盖。

## 4. 三个写工具的具体语义

### `replace_text`

- 目标必须在 pre-dispatch manifest 中存在；
- live bytes 必须等于 manifest 指向的完整 preimage blob；
- `old` 的实际出现次数必须等于 `expected_occurrences`；
- 写入使用同目录临时文件、`fsync` 和原子替换；
- post snapshot 必须等于只改变该文件后计算出的 expected tree。

因此，文件其他位置的并发变化即使不影响 `old` 文本，也会被完整文件 hash/tree revision 拒绝。

### `apply_patch`

- 一次只允许 1～8 个不同文件；
- 所有文件的 manifest preimage、精确替换次数和 expected post bytes 在第一次写入前完成校验；
- 每个文件单独原子替换；普通异常按相反顺序回滚仍等于本次 expected bytes 的文件；
- 最终 tree 必须等于批量修改计算出的唯一 expected tree。

它是有界的补偿式原子性，不是文件系统事务。进程在多个替换之间硬退出时，既有恢复逻辑仍将
部分态标为 `unknown/diverged`，不会凭 CAS 字段自动声称 exactly-once。

### `create_file`

- 目标在 pre-dispatch manifest 中必须不存在；
- 路径、权限范围、快照可见性、父目录和 64 KiB UTF-8 上限先验证；
- 使用 exclusive create，避免覆盖同时创建的文件；
- post snapshot 必须等于新增唯一文件后的 expected tree；
- 异常清理只删除 inode/device identity 仍属于本次 create 的目标。

## 5. Trace 与重放不变量

新 Trace 满足以下语义校验：

- write precondition 只能出现在三个写工具的 reservation 上；
- precondition 的 observed revision/manifest 必须等于 reservation checkpoint；
- `source_model_call_id` 必须是当时最新、已结算的 execution model call；
- `stale` 必须严格等于 expected 与 observed revision 不相等；
- stale reservation 只能结算为前后 revision 相同的 error receipt；
- fresh reservation 若成功，receipt preimage 必须同时等于 expected 和 observed revision。

攻击者即使重新计算单个事件 hash，把 precondition 改绑到更旧的模型调用，Reducer 也会语义
拒绝。旧 Trace 没有 `write_precondition` 时继续按历史 wire shape 重放；兼容不代表旧运行被
追溯升级为 CAS 证据。

## 6. 多工具响应与恢复

同一模型响应中的全部工具都来自同一个 `R_expected`。只读工具不改变 revision；第一个成功写入
会改变 revision，因此同一响应里的第二个写入会被保守拒绝。需要原子修改多个文件时应使用一次
`apply_patch`；需要依赖前一个写入继续决策时应进入下一模型轮次。

写入 effect 后、receipt 前硬退出时，precondition 和 pre-dispatch manifest 已经持久化。既有
`accept/rollback` 恢复流程继续只接受完整前态或唯一预期后态；CAS 不会让 unknown effect 自动
重派。

## 7. 已验证场景

- 正常 `read_file -> replace_text -> protected validation -> submit`：write reservation 绑定第二次
  execution model call、相同 expected/observed revision、目标路径和含完整 preimage SHA-256 的
  manifest；Trace 重放一致。
- 模型请求进行期间外部追加同一目标文件：model revision 与 live revision 不同，写入返回
  `StaleWriteConflict`，外部内容保留，候选 replacement 未出现，receipt 前后 revision 相同。
- 对 rehashed reservation 把 source model call 改为更旧调用：Reducer 拒绝重放。
- 原有 replace/apply/create 的普通成功、校验失败、异常回滚、真实硬退出与恢复测试继续通过。

## 8. 明确限制

- 当前绑定的是模型请求的完整 workspace tree，不是“某条 read receipt 必须先于某个 write”的
  因果证明。模型仍可能在没有读取目标文件时提出一个恰好可应用的候选。
- denied、控制目录和快照明确排除的路径不进入 revision；CAS 不对这些路径作变化检测。
- 每次 revision 依赖有界全工作区 snapshot，优先正确性，不代表大型 monorepo 的性能目标。
- 没有 OS 级全局文件锁。恶意或高频并发写会被拒绝、回滚或进入 unknown，而不是透明合并。
- 使用 SHA-256 内容寻址，安全边界依赖其碰撞抗性；当前没有外部签名、可信时间戳或远端证明。
- 该能力不替代 Git merge、冲突合并、通用审批、生产级恶意代码隔离或跨主机事务。

## 9. 公开验收证据

实现提交 `f18dfec` 的三个公开 workflow 均已通过：

- [标准 CI `40701404771`](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40701404771)：
  Python 3.12 离线质量门禁、EvidencePack 与发行包构建；
- [来源绑定 Docker 证据 `40701404803`](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40701404803)：
  Docker 合同以及 dependency-reduced、full-checkout、multi-stage、hard-crash 四组 suite；
- [外部检索证据 `40701405187`](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40701405187)：
  16 个 development cases 的重建和验证。

这些 workflow 没有读取模型 API Key，也没有运行或计费真实模型。它们证明的是该提交的离线合同、
重放、既有来源 suite 和构建门禁通过，不等同于真实模型在并发编辑环境中的成功率结论。
