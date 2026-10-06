# 外部来源 Run A/B Suite：BugsInPy 依赖裁剪复现

更新：2026-10-06。该 suite 把完整 Run A/B 从一个本仓自建 shell fixture 扩展到五个有固定
上游 buggy/fixed commit、测试入口和许可证的历史 Python 缺陷。它验证来源绑定、初始失败、
可恢复等待、单次 replan、受保护验收、预算结算和 Trace 重放能否在多个任务上保持一致。

它不是完整上游仓库 benchmark。五个 fixture 均明确标记为 `dependency_reduced`：保留生产缺陷的
关键语句、行为和回归断言，移除旧 Python、tox、框架及其他环境依赖，以便执行确定性回归检查。

## 1. 冻结来源与版本

- v1：[`bugsinpy-reduced-v1.yaml`](../benchmarks/run_ab/bugsinpy-reduced-v1.yaml)，3 个案例，
  digest `d41d0b6edbe69f9029235e118d386a8fc77500ef55e36c6e053a656092e13b9b`。
- v2：[`bugsinpy-reduced-v2.yaml`](../benchmarks/run_ab/bugsinpy-reduced-v2.yaml)，原样保留 v1
  的前三个 case 对象，再追加 Luigi 和 Tornado，digest
  `0cd34fadbebec928049f5b43ae505ff375b80b1bd624341ac469686495419d90`。

每个案例同时冻结仓库 URL、BugsInPy bug ID、buggy/fixed 40 位 commit、上游测试入口、修复提交、
SPDX license、license URL 和 reduction note；路径逃逸、重复 case、重复来源或非字面 manifest 路径
会在执行前拒绝。测试还固定校验 v1/v2 digest、v1 case 不变及 v2 追加顺序，避免扩集时改写旧证据。

| Case | 上游缺陷 | Buggy → Fixed | License | 裁剪后仍验证 |
|---|---|---|---|---|
| `cookiecutter-1-utf8-context` | [Cookiecutter fix #1414](https://github.com/cookiecutter/cookiecutter/commit/7f6804c4953a18386809f11faf4d86898570debc) | `c156337…` → `7f6804c…` | BSD-3-Clause | 默认 ASCII 解码失败；显式 UTF-8 后通过 |
| `fastapi-5-nested-field-clone` | [FastAPI fix #889](https://github.com/fastapi/fastapi/commit/75a07f24bf01a31225ee687f3e2b3fc1981b67ab) | `7cea84b…` → `75a07f2…` | MIT | 嵌套字段别名泄漏；递归 clone 后隔离 |
| `tqdm-1-tenumerate-start` | [tqdm start fix](https://github.com/tqdm/tqdm/commit/c0dcf39b046d1b4ff6de14ac99ad9a1b10487512) | `8cc777f…` → `c0dcf39…` | MPL-2.0 | `start` 错传 wrapper；交给 `enumerate` 后索引正确 |
| `luigi-1-metrics-handler` | [Luigi metrics fix](https://github.com/spotify/luigi/commit/aec5dc2ed8db53fc282a0bd24aabe59031b6d1ba) | `1164eb6…` → `aec5dc2…` | Apache-2.0 | 生成结果与 collector 的职责错位；由 collector 配置 HTTP handler |
| `tornado-1-websocket-nodelay` | [Tornado nodelay fix](https://github.com/tornadoweb/tornado/commit/4677c54cc18bbfbdf0f4dadf11610fab6203fd63) | `6a5a0bf…` → `4677c54…` | Apache-2.0 | handler→protocol→stream 委托及 protocol 抽象合同 |

BugsInPy 的项目元数据用于固定 commit 和原测试入口；上游修复提交用于人工交叉核对实际生产行。
fixture 代码不是完整仓库，也没有把新增上游测试、依赖图或历史构建脚本冒充为已执行内容。

## 2. 初始失败门与完整执行

单案例 manifest 必须声明至少一个 `expected_initial_failed_checks`。Evaluator 在创建 A/B 两个 arm
之前，先从同一 immutable snapshot 恢复第三个隔离 workspace，并实际执行所有 acceptance：

```text
initial snapshot -> protected validation -> exact failed-check comparison
                 -> Baseline full Agent Run
                 -> Single-Replan full Agent Run
```

观测失败集合必须与 manifest 完全一致。初始检查意外通过、失败项遗漏、额外失败、timeout 或错误
check ID 都不会被算作 suite 通过。初始 receipt（exit code、timeout、截断标志、输出和输出 hash）
进入单案例内容寻址报告。

两个 arm 共享 TaskSpec、Plan v1、fixture revision 和脚本模型价格合同，但使用独立 EventLog、
Campaign ledger、retrieval cache 与 staging workspace。案例通过仍要求：状态、Plan revision、replan
次数、WorkItem、HumanRequest pattern、模型/tool/step 数、脚本消费、Trace replay、workspace revision、
源 fixture 不变、unknown=0 和 open reservation=0 全部匹配。

## 3. 生产执行命令

```powershell
uv run --locked --cache-dir .uv-cache horizon eval run-ab-suite `
  benchmarks/run_ab/bugsinpy-reduced-v2.yaml `
  --image python:3.12-alpine
```

CLI 先解析全部 case、fixture 和路径边界，再创建 suite 状态。所有案例复用同一已存在镜像；生产
路径保持 `--pull never`，不会在评测中下载镜像。容器为禁网、非 root、只读根文件系统、drop
capabilities、no-new-privileges 和资源受限。每份 case report 与 suite report 都进入同一个
ArtifactStore，可用报告中的 SHA-256 直接回读。

## 4. 当前证据

### 4.1 v2 本地可信集成测试

2026-10-06 的集成测试用一个严格限于 `python -B <fixture-script>` 的 test-only executor 替代
Docker adapter，实际启动独立 Python 进程运行五个 dependency-free 回归脚本。它不是生产执行器，
也不提供容器隔离。结果为：

| 指标 | 结果 |
|---|---:|
| Cases / passed | 5 / 5 |
| 初始失败确认 | 5 / 5 |
| Baseline waiting | 5 / 5 |
| Treatment recovered | 5 / 5 |
| Success delta | +5 |
| Model-call delta | +10 |
| Tool-call / step delta | +15 / +15 |
| 合成模型成本 delta | CNY 0.00480 |
| Paid model / network | false / false |

第一次加入 Luigi/Tornado 时，两个 treatment manifest 对同一文件提交了多个 patch edit，被 Gateway
按“每个 edit 必须是不同文件”的既有合同拒绝。修正为每文件一个有界 edit 后，五个初始版本仍
全部失败、五个 treatment 全部通过。该失败没有通过放宽生产合同来掩盖。

这份测试证明确实执行了裁剪后的回归脚本及 Harness A/B 路径，但临时测试目录不会作为正式
suite report 长期保存。本机 Docker daemon 当时不可用，因此先保留为本地可信证据；后续公开
Docker 工作流已独立补齐同一 v2 的正式容器执行。

### 4.2 v2 公开 Docker 5/5

2026-10-06 的统一 [Source-bound Docker evidence](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/37436568589)
工作流使用生产 `DockerAcceptanceExecutor` 和本机拉取的 `python:3.12-alpine`，在禁网、非 root、
只读根文件系统、drop all capabilities 与资源上限下运行冻结 v2：

- 5/5 初始保护检查失败，5/5 Baseline 进入 `WAITING_FOR_USER`，5/5 Treatment 进入 `SUCCEEDED`；
- success/model/tool/step delta 保持 +5/+10/+15/+15，未调用付费模型或模型网络；
- 同一 job 还复跑完整 checkout v2 3/3；两份 suite 的 report、Trace 和具体 Docker image ID 一并
  上传为 32.3 MB artifact；
- artifact digest 为
  `sha256:5bb438df11526a43476272f5622af3446c013bdc0c3ae1614b56272c8bcbc4b2`，保留 30 天。

工作流成功证明的是 dependency-reduced fixture 的生产 Docker Harness 路径，不把裁剪案例扩写成
完整上游 checkout 或官方 BugsInPy 分数。

### 4.3 v1 历史 Docker 结果

2026-10-03 使用官方镜像 digest
`sha256:0687a6bc9716edc2a6ee0fbfb0f87e7ee358b262b67c9215de91bc9b2d38ba71`：

| Case | 初始检查 | Baseline | Single replan | Case report ref |
|---|---|---|---|---|
| Cookiecutter | 失败，符合预期 | WAITING_FOR_USER | SUCCEEDED | `805574b5dbde7370b0222aeaa610a3ba685cb750844834b2fef1819d28e67eae` |
| FastAPI | 失败，符合预期 | WAITING_FOR_USER | SUCCEEDED | `0f2012e67d847c7677556926c26425400b35422f9ad1e34c7ca2273976f9d71a` |
| tqdm | 失败，符合预期 | WAITING_FOR_USER | SUCCEEDED | `bc8f856ef466c3e2d50e7c311e35e60e1987579965585927060d10136c1e6d4e` |

聚合为 3/3 初始失败、3/3 Baseline 等待、3/3 Treatment 成功；success delta +3，model-call
delta +6，tool-call/step delta +9/+9，合成成本 delta CNY 0.00288。`paid_model_called=false`、
`network_called=false`、`repository_code_executed=true`。Suite report ref 为
`da8c8557b15aa0fc6955f44d987b41bdd57aac6eb34b2490392a11d533639790`。

每个历史 case report 保存两份完整 Trace ref、projection hash、初始失败 receipt、最终验收、调用数、
token、合成成本、workspace manifest 和全部边界检查。Suite 汇总只保存可比较指标和 case report
引用，不复制 Trace。

## 5. 结论边界与下一步

当前证据比内部 synthetic fixture 更强，因为缺陷类型、生产修复行和 commit 来自五个公开历史
任务；但依赖裁剪会降低仓库规模、检索难度和环境复杂度，脚本模型也预先知道修复动作。因此：

- 可以声称 v2 五个来源合同、本地可信 Harness A/B 和公开禁网 Docker Harness A/B 均通过；
- 不能声称在完整 Cookiecutter/FastAPI/Luigi/Tornado checkout 上通过；
- 不能声称真实模型会定位这些文件或自主选择 replan；
- 不能把 5/5 写成 SWE-bench、BugsInPy 官方跑分或泛化成功率。

同一合同已把 tqdm-1 与 youtube-dl-3 升级为[完整固定 checkout suite](full-checkout-pilot.md)：CLI
额外验证 Git HEAD 和清洁度，82/872 文件快照上的 Code RAG 都返回目标文件 rank 1，并显式保留
9/2 个文件跳过导致的 degraded 状态。重复 CAS blob 校验的规模开销已完成前后对照优化；
youtube-dl-3 还完成了[两阶段完整 checkout 与 Worker 重启](multi-stage-full-checkout-pilot.md)。
Luigi-1 也已进入[三项目完整 checkout v2](full-checkout-pilot.md)，并与 tqdm/youtube-dl 一起取得
本地可信和公开禁网 Docker 3/3。下一步优先增加一个多阶段完整 checkout 的真实任务深度，而不是
继续堆叠同类裁剪案例；在真实模型决策证据出现前，不增加第二次自动 replan、向量库、通用审批流
或复杂 Planner。
