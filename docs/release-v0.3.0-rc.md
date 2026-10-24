# Horizon v0.3.0 Release Candidate — Controller Recovery and Reproducible Evidence

> 候选准备日期：2026-10-24。本文冻结的是下一版本的候选范围，不是已发布版本。
> 当前 `pyproject.toml`、安装包元数据与 `horizon.__version__` 仍为 `0.2.0`；尚未创建
> `v0.3.0` tag、GitHub Release 或任何包仓库发布。版本号只在最终发布提交中更新，发布动作仍需
> 用户明确批准。

v0.3.0 候选把 v0.2.0 的本地直接子进程监督推进到一个更难的边界：父控制器已硬退出、旧
Worker 可能仍存活时，Harness 先用持久化 launch ID、PID、Lease、epoch 和事件游标阻止重派，
只有操作员提交与原记录精确匹配的停止证据后才允许新 epoch 接管。与此同时，真实仓库评测从
“能跑通几个示例”推进到带可靠性指标、完整 Trace 和脱库 verifier 的可复核作品集证据。

## 相比 v0.2.0 的冻结增量

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

## 候选证据快照

| 证据 | 当前结果 | 声明边界 |
|---|---|---|
| 离线回归 | `470 passed, 11 skipped` | skipped Docker 合同不计入本地通过 |
| 主作品集 Demo | `SUCCEEDED`，final epoch 3，77 events；删除三库后独立 verifier 通过 | 冻结 Scripted Model，外部模型费用 0 |
| 标准 CI（候选准备基线） | [run 40092106530](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40092106530) 通过，并上传 `horizon-portfolio-evidence` | 该 run 位于候选发布门禁加入前；最终发布提交仍须有自己的成功 run |
| Docker 证据（最新 runtime 基线） | [run 40092106536](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40092106536) 通过 5 项完整 checkout、3 项多阶段及既有故障切片 | 禁网、非 root 的有限合同，不是恶意代码安全认证 |
| wheel smoke（本地候选准备） | wheel 构建成功；新虚拟环境安装后，元数据／公共版本一致且 `horizon doctor` 成功 | 当前验证的是尚未改号的 `0.2.0` 候选代码 |
| 真实模型 Pilot | 七轮均未修复成功，费用与 Trace 保留 | 明确负结果，不提供真实 Issue 成功率 |

标准 CI 现在还会在构建后创建干净 Python 3.12 环境，只安装锁定的生产依赖和刚生成的唯一
wheel，校验 `pyproject.toml`、installed metadata、`horizon.__version__` 三者一致，解析安装后
`horizon doctor` 的 JSON，并把 wheel、sdist 与 receipt 保存为 14 天 artifact。它不会自动发布。

## 本地候选复现

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

候选目录必须是新路径；上述命令不会覆盖已有虚拟环境。CI 使用 `uv export --locked
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
- 本候选没有新增真实模型运行，也没有使用此前付费预算；七轮真实模型负结果保持冻结。

## 从候选到正式发布的剩余门禁

以下事项在获得明确发布批准前保持未执行：

1. 将 `pyproject.toml` 与 `horizon.__version__` 从 `0.2.0` 同步改为 `0.3.0`，刷新并检查
   `uv.lock`；
2. 在精确发布提交上通过标准 CI，确认 `horizon-built-distributions` artifact 中的 wheel、sdist
   与 doctor receipt 都来自该提交；
3. 保留最新相关 runtime 提交的 Docker success；如果发布前再改 runtime，则必须重跑 Docker，
   不能用旧 run 冒充；
4. 从 CI artifact 安装最终 `0.3.0` wheel，再运行版本一致性与 `horizon doctor` smoke；
5. 用户明确批准后才创建并推送 `v0.3.0` tag、创建 GitHub Release、附加 wheel 与 sdist；
6. 不发布到 PyPI，除非用户另行明确授权并提供对应凭据与目标。

因此，当前状态是“v0.3.0 范围和门禁已形成候选”，不是“v0.3.0 已发布”。
