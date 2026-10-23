# 直接子 Worker 超时取消：零费用一键 EvidencePack

## 目的

这个演示把正式 `LocalProcessAgentSupervisor` 已实现的 slice timeout 路径变成一条可直接展示、
可脱离数据库复核的命令。它关注一个窄而真实的问题：父进程持有一个直接子 Worker 的句柄时，
如何在超时后先把精确取消目标写入 Trace，再停止并同步回收该进程，最后安全进入
`CANCELLED`。

它不会调用模型、Tool Gateway、网络、Docker 或仓库代码，外部费用固定为 0。

## 一条命令运行

```powershell
uv run --locked --cache-dir .uv-cache horizon demo process-timeout
```

如需固定输出目录与 timeout：

```powershell
uv run --locked --cache-dir .uv-cache horizon demo process-timeout `
  --output .horizon/demos/process-timeout-review `
  --worker-timeout-seconds 0.2
```

命令拒绝覆盖已有目录。`--worker-timeout-seconds` 的演示范围是 `0.05`～`5` 秒；计时从子进程
完成 ready handshake、父进程取得真实 `Popen` 句柄之后开始。

## 实际执行链

```text
父进程创建 RUNNING Run + Lease epoch 1
  -> 启动一个可导入的直接 Python 子进程
  -> 子进程只读重开 SQLite，核对 Run/Lease/Worker/epoch/launch seq
  -> 子进程 fsync + 原子发布 child-ready.json，随后保持空闲
  -> 父进程开始 bounded communicate(timeout)
  -> timeout
  -> 追加 CANCEL_WORKER_STOP_PENDING（精确 PID/Lease/epoch/launch seq）
  -> terminate + wait；仅在有界等待失败时 kill + wait
  -> 追加 CANCEL_WORKER_STOPPED（PID/exit code/stop method）
  -> 追加 LEASE_RELEASED
  -> 追加 CANCEL_REQUESTED，Run 进入 CANCELLED
  -> 导出并立即离线复核 EvidencePack
```

子进程不执行 Agent iteration，也不构造模型或工具适配器。它只验证启动身份、发布 ready
observation，然后等待父进程触发生产取消路径。因此该演示隔离验证的是 Harness 的进程控制与
可恢复取消合同，不把 Scripted Model 或假工具返回混入结果。

## 必须成立的事件顺序

| 顺序 | Trace 事件 | 证明内容 |
|---:|---|---|
| 1 | launch event | 子进程观察到的 RUNNING 边界，绑定 event hash |
| 2 | `CANCEL_WORKER_STOP_PENDING` | 在停止进程前持久化精确 PID/Lease/epoch/launch seq |
| 3 | `CANCEL_WORKER_STOPPED` | `wait` 已返回，并记录 exit code 与 stop method |
| 4 | `LEASE_RELEASED` | 被回收进程的旧 Lease 已释放 |
| 5 | `CANCEL_REQUESTED` | 没有 pending effect，Run 才进入终态 `CANCELLED` |

根验证器要求五个序号严格递增，且最后一个事件就是终态取消。它还要求 Trace 中不存在任何
`MODEL_*` 或 `TOOL_*` 事件、最终投影没有 unknown/open effect、只有一份进程停止 receipt。

## EvidencePack 结构

```text
process-timeout-*/
├── evidence-pack.json
├── child-ready.json
├── supervision-observation.json
├── SUMMARY.md
└── terminal/
    ├── evidence-pack.json
    ├── trace.jsonl
    ├── final-run.json
    └── SUMMARY.md
```

- `child-ready.json`：由真实子进程写入，包含 PID、Run、Lease、Worker、epoch、launch event seq
  与 event hash。
- `supervision-observation.json`：父进程的 timeout、stop method、exit code 和 process receipt 摘要。
- `terminal/`：复用通用终态 EvidencePack；保存完整 Trace、规范最终投影和确定性摘要。
- 根 `evidence-pack.json`：绑定上述文件的字节数与 SHA-256，并保存从 observation + Trace 复算
  的结构化结论。

成功返回前，Runner 已调用与公开 CLI 相同的独立验证器。运行结束后，临时 SQLite 和 Campaign
数据库会被删除，最终证据目录不包含数据库。

## 脱库复核

```powershell
uv run --locked --cache-dir .uv-cache horizon demo verify-process-timeout `
  .horizon/demos/process-timeout-review/evidence-pack.json
```

验证过程不启动子进程，不读取控制数据库，也不调用模型、网络、Docker、工具或仓库代码。它会：

1. 校验根包四个角色及每个文件的大小和 SHA-256；
2. 递归调用通用终态 EvidencePack 验证器；
3. 重放 `trace.jsonl` 的事件 hash 链与最终 Run 投影；
4. 把 child-ready 和 parent-supervision observation 重新绑定到 Trace 中的同一 PID、Lease、
   epoch、launch event、stop receipt；
5. 复算根 evidence 与 `SUMMARY.md`，拒绝“改叙述再重算文件 hash”的语义篡改。

## 已验证用例

- 真实直接子进程 ready 后发生 timeout，终态为 `CANCELLED`；
- 根包在临时 SQLite 已删除后仍可离线验证；
- CLI 一条命令运行、独立命令复核、已有目录不覆盖；
- 修改 parent observation 的 PID 并同步重算根文件 hash，仍因 PID 与 ready/Trace 不一致而拒绝。

CI 也单独运行演示和脱库验证；普通离线测试覆盖 Windows，公共 workflow 覆盖 Linux。标准
[CI workflow](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/workflows/ci.yml)
成功后还会上传保留 14 天的 `horizon-process-timeout-evidence` artifact。GitHub 通常要求登录后
下载；解压后的根 `evidence-pack.json` 可直接交给上述独立 verifier，不需要原始数据库。

面试场景的三分钟讲解顺序、架构图和常见追问见
[面试演示与架构讲解](interview-demo.md)。

## 严格声明边界

这个 EvidencePack 证明的是：在一次实际运行中，一个本地直接子进程完成身份握手后触发了父侧
slice timeout；正式控制路径先提交取消栅栏，再通过所持句柄停止并同步 reap 该 PID，最终 Trace
可重放到无悬空 effect 的 `CANCELLED`。

它不证明：

- 父控制器硬崩溃、操作系统重启或掉电后的自动恢复；
- 子进程树、孤儿进程、daemon、队列或跨主机 Worker 管理；
- 任意模型/工具副作用的自动对账；
- exactly-once 执行或外部时间/身份认证；
- Coding Agent 的修复能力、真实 Issue 成功率或 benchmark 成绩。

包内哈希证明自洽性，不是签名、可信时间戳或第三方来源证明。
