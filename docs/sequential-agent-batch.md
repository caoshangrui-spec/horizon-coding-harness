# 可恢复的顺序 Agent 任务批次

## 1. 目标

`horizon agent batch` 读取 1～100 条独立 TaskSpec，严格按 manifest 顺序调用现有生产 Agent
入口。它解决 NFR-010 的首版执行边界：单机可以跑一组任务，在任务之间持久化 checkpoint，重启后
跳过已经终态化的 Run，并在全部结束后复用 `terminal-suite` 生成逐 Run EvidencePack 和聚合报告。

批量层不复制 Agent Loop、工具 Gateway、预算账本、Worker Lease、恢复服务或验收引擎。每条任务
仍是普通 Horizon Run，拥有自己的 staging workspace、TaskSpec、Plan、Trace、费用与终态证据；
所有任务共享 Provider 配置中的 Campaign 累计费用硬上限。

## 2. Manifest

路径必须是 manifest 目录内的相对 POSIX 路径，不能包含通配符或目录穿越。`case_id` 必须唯一，
清单顺序就是派发和最终报告顺序。每条 case 必须恰好选择人工计划或自动计划之一；Pilot preflight
只能与自动计划一起使用。

```yaml
schema_version: 1
batch_id: internship-mini-benchmark-v1
cases:
  - case_id: parser-bug
    task_path: tasks/parser/task.yaml
    plan_path: tasks/parser/plan.yaml
    auto_plan: false
  - case_id: scheduler-bug
    task_path: tasks/scheduler/task.yaml
    auto_plan: true
    pilot_preflight_path: tasks/scheduler/pilot-preflight.json
```

启动新批次：

```powershell
uv run --locked horizon agent batch benchmarks/agent-batch-v1.yaml `
  --image horizon-python:3.12 `
  --output .horizon/batches/agent-batch-v1 `
  --config config/providers/siliconflow.yaml `
  --dotenv .env `
  --confirm-paid
```

同一 manifest 和输出目录续跑：

```powershell
uv run --locked horizon agent batch benchmarks/agent-batch-v1.yaml `
  --image horizon-python:3.12 `
  --output .horizon/batches/agent-batch-v1 `
  --config config/providers/siliconflow.yaml `
  --dotenv .env `
  --confirm-paid `
  --resume
```

`--max-cases N` 只限制当前命令最多新派发多少条，适合把长批次拆成可控窗口。默认每个 Agent
slice 使用直接子进程；`--worker-timeout-seconds` 继续沿用已有的先取消、再停止并同步 reap 合同。
`--in-process` 主要用于确定性本地测试，不是长批次的推荐模式。

## 3. 持久状态与确定性身份

输出结构：

```text
agent-batch-v1/
├── batch-manifest.json
├── batch-state.json
├── cases/
│   ├── 001-parser-bug/
│   │   ├── run-receipt.json
│   │   ├── stdout.log
│   │   └── stderr.log
│   └── 002-scheduler-bug/
│       └── ...
└── terminal-suite/
    ├── suite-manifest.json
    ├── suite-report.json
    ├── SUMMARY.md
    ├── suite-pack.json
    └── cases/...
```

`batch-state.json` 采用原子替换写入，记录 manifest digest、单调 revision、每条 case 的
`pending / running / attention / terminal`、attempt、确定性 invocation、Run ID、真实 Run 状态和
最后错误。它是恢复 checkpoint，不是带签名的审计日志；最终结果证据仍来自每个 Run 的追加式
EventLog 和递归验证的 terminal-suite。

每次派发的 invocation 由 manifest digest、case ID 和 attempt 确定性生成。Agent 在创建 Run 后、
规划和执行前，发布只写一次的 `run-receipt.json`，绑定 invocation、Run/Task、TaskSpec hash、来源
revision 和 staging 路径 hash。如果控制器在 receipt 写入前退出，恢复会用该 invocation 派生的
唯一 Task ID 扫描控制库；零匹配才允许下一 attempt，多匹配直接拒绝。

## 4. 继续与阻塞规则

批次只在以下边界继续下一条：

- 当前 Run 已是 `SUCCEEDED`、`FAILED` 或 `CANCELLED`；
- live projection 与后续 terminal EvidencePack 都能从 Trace 重放；
- case 与确定性 Task/Run identity 一致。

终态失败和取消不会被改写成成功，但它们属于“本任务已经闭合”，因此会记录后继续下一条。以下
情况会把 case 标为 `attention` 并阻止后续 case：

- `WAITING_FOR_USER`；
- 仍在 `PLANNING / READY / RUNNING`；
- 旧 Worker、Lease 或悬空模型/工具 effect 尚未按已有恢复合同闭合；
- receipt 与控制库不一致，或同一 invocation 意外映射多个 Run。

操作者使用现有 `agent guide`、`agent reconcile`、`agent resolve-tool`、`agent resume` 或 `cancel`
把该 Run 安全推进到终态后，再运行批次 `--resume`；调度器会重新读取真实 Run，而不是相信旧的
batch 摘要。只有在 executor 抛错且控制库确认没有创建任何 Run 时，下一次 `--resume` 才会生成
新的 attempt。这样不会因一个普通异常重复派发已经存在但尚未识别的任务。

## 5. 完成与证据

全部 case 终态化后，批次把真实 case/Run 映射交给既有 `export_terminal_suite`：

1. 每条 Run 先重放并导出通用终态 EvidencePack；
2. 聚合报告保留成功、失败、取消、BudgetStop、unknown/open effect；
3. schema v2 分列验收未知/失败、clean/quality-flagged success、fallback 和 degradation；
4. `verify-terminal-suite` 可在没有控制数据库时递归复核。

批次调用返回的 `complete=true` 只表示全部 case 已终态化且 suite 验证通过，不表示全部任务成功。
真实任务成功率仍只由 suite 中的 `SUCCEEDED` 计数得出。

## 6. 已验证场景与限制

离线回归已覆盖：

- 三条终态 case 分两次命令执行，续跑不重复第一条，最后生成可复核 suite；
- 第一条停在非终态时第二条不派发；外部将第一条取消后，续跑识别真实终态并继续；
- executor 在创建 Run 前失败时保留负结果，下一次使用新 attempt；
- 两条隔离 TaskSpec 使用确定性 Fake Model 实际完成修改、保护验收和终态聚合，源目录不变；
- CLI 路径不把 dotenv 密钥写进 batch state。

当前明确不声明：并发执行、跨主机队列、自动处理人工请求、自动判定旧 PID 已停止、任务级独立
Campaign、任意控制器崩溃 exactly-once、20～30 个真实模型任务成功率或官方 SWE-bench 成绩。
批次上限 100 是合同边界，不是吞吐性能结论。
