# Horizon v0.3.0 — Controller Recovery and Reproducible Evidence

> 发布日期：2026-10-24。本版本通过 GitHub Release 分发 wheel 与 sdist，不发布到 PyPI。

v0.3.0 把 v0.2.0 的本地直接子进程监督推进到一个更难的边界：父控制器已硬退出、旧
Worker 可能仍存活时，Harness 先用持久化 launch ID、PID、Lease、epoch 和事件游标阻止重派，
只有操作员提交与原记录精确匹配的停止证据后才允许新 epoch 接管。与此同时，真实仓库评测从
“能跑通几个示例”推进到带可靠性指标、完整 Trace 和脱库 verifier 的可复核作品集证据。

## 相比 v0.2.0 的主要增量

### 1. 父控制器硬退出后的保守接管

- 每个 `--worker-process` 子进程在接触 store、模型、工具或 workspace 前等待一次性 start gate；
  父进程先持久化 `PROCESS_WORKER_LAUNCHED` 和 `PROCESS_WORKER_START_AUTHORIZED`，再放行精确
  launch ID。
- 父控制器硬退出后，只要 Trace 中仍有 active launch，就算 Lease 已过期也拒绝替换 Worker。
  `agent resume`、`agent reconcile` 和 `agent resolve-tool` 只接受与持久记录相同的旧 PID 停止
  确认；缺失或错误 PID 不写入恢复事件。
- 真实故障注入覆盖“杀死独立父进程、旧 Worker 仍存活、零重派、错误证据拒绝、精确 PID
  确认、epoch 前进、原 AgentSession 续跑到 `SUCCEEDED`、完整 Trace 重放”。它仍不是自动
  liveness probe、进程树控制、daemon、跨主机或操作系统重启恢复。

### 2. 长程可靠性指标成为类型化结果

- 来源绑定的 Run A/B suite 记录单 case 单调时长，并在聚合报告中给出场景/arm 数、P95、
  Worker 重启、注入硬崩溃恢复率、恢复重派、重复副作用、Trace replay、源 workspace 隔离和
  最终 open effect。
- 旧报告仍可读取，冻结 manifest digest 未被回写。指标证明的是当前固定场景下的 Harness
  行为，不是任意故障 exactly-once，也不是模型修复能力。

### 3. 真实仓库覆盖从 3 项扩至 5 项，多阶段从 2 项扩至 3 项

- 完整 checkout v3 在 tqdm、youtube-dl、Luigi 之外加入 FastAPI 与 Tornado，绑定 2,266 个
  tracked files；本地可信执行和公开禁网 Docker 都通过 5/5。
- 多阶段 v4 加入 FastAPI：三个 WorkItem、两个持久 Worker 边界、final epoch 3；baseline 在
  前两阶段后安全等待，treatment 保留已完成项并只 replan 最后一项。三个项目本地与公开 Docker
  都通过 3/3。
- Cookiecutter 的 Linux symlink 边界与 Tornado 的多匹配精确替换仍作为负结果保留；没有为了
  把候选塞进 benchmark 而放宽 link gate 或增加 nth-occurrence 写工具。

### 4. 主作品集证据可以完全脱离数据库复核

- `horizon demo verify-portfolio` 校验 portable manifest 和所有哈希，重放 JSONL Trace，重建
  final Run，并重新验证 retrieval → write lineage、硬崩溃 marker 与精确 effect acceptance。
- 标准 CI 在 verifier 前删除 control、campaign、retrieval 三个 SQLite 数据库；因此通过结果
  不依赖生成时数据库、模型、网络、Docker 或活 Worker。
- 一键演示明确输出 final lease epoch、事件数、evidence-backed write 和 Trace replay 结果，
  方便面试现场解释“恢复”而不是只展示最终状态。

## 发布证据快照

| 证据 | 当前结果 | 声明边界 |
|---|---|---|
| 离线回归 | `470 passed, 11 skipped` | skipped Docker 合同不计入本地通过 |
| 主作品集 Demo | `SUCCEEDED`，final epoch 3，77 events；删除三库后独立 verifier 通过 | 冻结 Scripted Model，外部模型费用 0 |
| 标准 CI（版本化发布提交 `34f7506`） | [run 40094686164](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40094686164) 全部通过；包含 470/11 回归、构建、独立 wheel 安装、三重版本一致性与安装后 `doctor` | 最终 tag 提交只追加本证据记录，并在 tag 前重复同一门禁 |
| Docker 证据（最新 runtime 基线） | [run 40092106536](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40092106536) 通过 5 项完整 checkout、3 项多阶段及既有故障切片 | 禁网、非 root 的有限合同，不是恶意代码安全认证 |
| 最终 wheel smoke | `0.3.0` wheel 在新环境安装后版本一致、`horizon doctor` 成功；从该 wheel 运行主 Demo 后删除三库，独立 verifier 仍通过 | 冻结 Scripted Model，未调用真实模型或网络 |
| 真实模型 Pilot | 七轮均未修复成功，费用与 Trace 保留 | 明确负结果，不提供真实 Issue 成功率 |

标准 CI 现在还会在构建后创建干净 Python 3.12 环境，只安装锁定的生产依赖和刚生成的唯一
wheel，校验 `pyproject.toml`、installed metadata、`horizon.__version__` 三者一致，解析安装后
`horizon doctor` 的 JSON，并把 wheel、sdist 与 receipt 保存为 14 天 artifact。它不会自动发布。
版本化发布提交的 `horizon-built-distributions` artifact ID 为 `12559894636`，大小 1,007,388
bytes，digest 为
`sha256:6f5378705f99a33f52e3733b348ed797fe523bb9656fd052e733fc9b446bc878`，保留至
2026-11-07；同一 run 的 portfolio 与 timeout EvidencePack 也均生成成功。最终 tag 提交的
distribution artifact 将作为 GitHub Release 资产来源。

## 本地发布复现

以下 PowerShell 命令不需要 API Key、Docker 或付费模型。项目内 cache 参数避免依赖系统级
`uv` cache 的权限状态：

```powershell
uv --cache-dir .uv-cache sync --locked --python 3.12
uv run --locked --cache-dir .uv-cache ruff check src tests
uv run --locked --cache-dir .uv-cache ruff format --check src tests
uv run --locked --cache-dir .uv-cache pytest -q -p no:cacheprovider
uv --cache-dir .uv-cache build

$version = uv run --locked --cache-dir .uv-cache python -c `
  "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])"
$wheel = (Get-Item "dist\horizon_coding_harness-$version-py3-none-any.whl").FullName
uv --cache-dir .uv-cache venv .horizon\release-wheel-smoke --python 3.12
uv --cache-dir .uv-cache pip install `
  --python .horizon\release-wheel-smoke\Scripts\python.exe $wheel
.horizon\release-wheel-smoke\Scripts\python.exe -c `
  "from importlib.metadata import version; import horizon; assert horizon.__version__ == version('horizon-coding-harness')"
.horizon\release-wheel-smoke\Scripts\horizon.exe doctor
```

smoke 目录必须是新路径；上述命令不会覆盖已有虚拟环境。CI 使用 `uv export --locked
--no-dev --no-emit-project` 先安装锁定生产依赖，再以 `--no-deps` 安装 wheel，从而同时验证 lock
和构建产物，而不是依赖 editable source tree。

## 兼容性与严格边界

- Python 支持仍为 3.12～3.13；CLI、领域对象和 EvidencePack 在 1.0 前仍可能变化。
- SQLite `user_version` 仍为 1，仍没有任意 v0.1/v0.2 运行库到下一版本的通用 migration 验收。
  重要旧证据目录应只读保留，新运行使用新的 state directory。
- `TaskSpec.max_cost_usd` 仍是历史字段；真实 Provider 的权威上限仍是 CNY Campaign、Run policy
  和单调用 reservation。自动跨模型 fallback 继续关闭。
- Project Memory、向量/图 RAG、通用多用户 HITL、并行 WorkItem、跨主机接管、自动进程存活
  探测和任意外部副作用 exactly-once 仍未实现。
- EvidencePack 哈希证明文件自洽和 Trace 可重放，不等于数字签名、可信时间戳或第三方来源认证。
- 本版本没有新增真实模型运行，也没有使用此前付费预算；七轮真实模型负结果保持冻结。

## 发布验收合同

本版本按以下条件发布：

1. `pyproject.toml`、`horizon.__version__`、`uv.lock`、wheel 与 sdist 版本一致；
2. 精确 tag 提交的标准 CI 通过，并产生 distribution、portfolio 与 timeout artifacts；
3. 最新相关 runtime 提交的公开 Docker evidence 通过；仅文档和版本元数据变化不冒充 runtime
   复验；
4. 最终 `0.3.0` wheel 在干净环境中安装，版本一致性与 `horizon doctor` smoke 通过；
5. 用户已明确批准 `v0.3.0` GitHub Release；
6. wheel 与 sdist 附加到 GitHub Release，不发布到 PyPI。
