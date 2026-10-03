# 外部来源 Run A/B Suite：BugsInPy 依赖裁剪复现

更新：2026-10-03。该 suite 把完整 Run A/B 从一个本仓自建 shell fixture 扩展到三个有固定
上游 buggy/fixed commit、测试入口和许可证的历史 Python 缺陷。它验证来源绑定、初始失败、
可恢复等待、单次 replan、受保护验收、预算结算和 Trace 重放能否在多个任务上保持一致。

它不是完整上游仓库 benchmark。三个 fixture 均明确标记为 `dependency_reduced`：保留生产缺陷的
关键语句、行为和回归断言，移除旧 Python、tox、Pydantic、Click 等环境依赖，以便用一个固定的
禁网 Python 镜像重复执行。

## 1. 冻结来源

Suite manifest：
[`benchmarks/run_ab/bugsinpy-reduced-v1.yaml`](../benchmarks/run_ab/bugsinpy-reduced-v1.yaml)。
每个案例同时冻结仓库 URL、BugsInPy bug ID、buggy/fixed 40 位 commit、上游测试入口、修复提交、
SPDX license、license URL 和 reduction note；路径逃逸、重复 case、重复来源或非字面 manifest 路径
会在执行前拒绝。

| Case | 上游缺陷 | Buggy → Fixed | License | 裁剪后仍验证 |
|---|---|---|---|---|
| `cookiecutter-1-utf8-context` | [Cookiecutter fix #1414](https://github.com/cookiecutter/cookiecutter/commit/7f6804c4953a18386809f11faf4d86898570debc) | `c156337…` → `7f6804c…` | BSD-3-Clause | 默认 ASCII 解码失败；显式 UTF-8 后通过 |
| `fastapi-5-nested-field-clone` | [FastAPI fix #889](https://github.com/fastapi/fastapi/commit/75a07f24bf01a31225ee687f3e2b3fc1981b67ab) | `7cea84b…` → `75a07f2…` | MIT | 嵌套字段别名泄漏；递归 clone 后隔离 |
| `tqdm-1-tenumerate-start` | [tqdm start fix](https://github.com/tqdm/tqdm/commit/c0dcf39b046d1b4ff6de14ac99ad9a1b10487512) | `8cc777f…` → `c0dcf39…` | MPL-2.0 | `start` 错传 wrapper；交给 `enumerate` 后索引正确 |

BugsInPy 的项目元数据用于固定 commit 和原测试入口；上游修复提交用于人工交叉核对实际生产行。
fixture 代码不是完整仓库，也没有把新增上游测试、依赖图或历史构建脚本冒充为已执行内容。

## 2. 初始失败门与完整执行

单案例 manifest 现在必须声明至少一个 `expected_initial_failed_checks`。Evaluator 在创建 A/B 两个
arm 之前，先从同一 immutable snapshot 恢复第三个隔离 workspace，并实际执行所有 acceptance：

```text
initial snapshot -> protected validation -> exact failed-check comparison
                 -> Baseline full Agent Run
                 -> Single-Replan full Agent Run
```

观测失败集合必须与 manifest 完全一致。初始检查意外通过、失败项遗漏、额外失败、timeout 或错误
check ID 都不会被算作 suite 通过。初始 receipt（exit code、timeout、截断标志、输出和输出 hash）
进入单案例内容寻址报告。

两个 arm 继续共享 TaskSpec、Plan v1、fixture revision 和脚本模型价格合同，但使用独立 EventLog、
Campaign ledger、retrieval cache 与 staging workspace。案例通过仍要求：状态、Plan revision、replan
次数、WorkItem、HumanRequest pattern、模型/tool/step 数、脚本消费、Trace replay、workspace revision、
源 fixture 不变、unknown=0 和 open reservation=0 全部匹配。

## 3. 执行命令

```powershell
uv run --locked --cache-dir .uv-cache horizon eval run-ab-suite `
  benchmarks/run_ab/bugsinpy-reduced-v1.yaml `
  --image python:3.12-alpine
```

CLI 先解析全部 case、fixture 和路径边界，再创建 suite 状态。所有案例复用同一已存在镜像；生产
路径保持 `--pull never`，不会在评测中下载镜像。容器仍为禁网、非 root、只读根文件系统、drop
capabilities、no-new-privileges 和资源受限。每份 case report 与 suite report 都进入同一个
ArtifactStore，可用报告中的 SHA-256 直接回读。

## 4. 当前真实 Docker 结果

2026-10-03 使用官方镜像 digest
`sha256:0687a6bc9716edc2a6ee0fbfb0f87e7ee358b262b67c9215de91bc9b2d38ba71`：

| Case | 初始检查 | Baseline | Single replan | Case report ref |
|---|---|---|---|---|
| Cookiecutter | 失败，符合预期 | WAITING_FOR_USER | SUCCEEDED | `805574b5dbde7370b0222aeaa610a3ba685cb750844834b2fef1819d28e67eae` |
| FastAPI | 失败，符合预期 | WAITING_FOR_USER | SUCCEEDED | `0f2012e67d847c7677556926c26425400b35422f9ad1e34c7ca2273976f9d71a` |
| tqdm | 失败，符合预期 | WAITING_FOR_USER | SUCCEEDED | `bc8f856ef466c3e2d50e7c311e35e60e1987579965585927060d10136c1e6d4e` |

聚合结果：

| 指标 | 结果 |
|---|---:|
| Cases / passed | 3 / 3 |
| 初始失败确认 | 3 / 3 |
| Treatment recovered | 3 / 3 |
| Success delta | +3 |
| Model-call delta | +6 |
| Tool-call / step delta | +9 / +9 |
| 合成模型成本 delta | CNY 0.00288 |
| Paid model / network | false / false |
| Repository code executed | true |

- Suite manifest digest：`d41d0b6edbe69f9029235e118d386a8fc77500ef55e36c6e053a656092e13b9b`
- Suite report ref：`da8c8557b15aa0fc6955f44d987b41bdd57aac6eb34b2490392a11d533639790`

每个 case report 内仍保存两份完整 Trace ref、projection hash、初始失败 receipt、最终验收、调用数、
token、合成成本、workspace manifest 和全部边界检查。Suite 汇总只保存可比较指标和 case report
引用，不复制 Trace。

## 5. 结论边界与下一步

当前证据比内部 synthetic fixture 更强，因为缺陷类型、生产修复行和 commit 来自三个公开历史
任务；但依赖裁剪会降低仓库规模、检索难度和环境复杂度，脚本模型也预先知道修复动作。因此：

- 可以声称多任务完整 Harness A/B、初始负例和 Docker 验收可重复；
- 不能声称在完整 Cookiecutter/FastAPI/tqdm checkout 上通过；
- 不能声称真实模型会定位这些文件或自主选择 replan；
- 不能把 3/3 写成 SWE-bench、BugsInPy 官方跑分或泛化成功率。

同一合同已把 tqdm-1 与 youtube-dl-3 升级为[完整固定 checkout suite](full-checkout-pilot.md)：CLI
额外验证 Git HEAD 和清洁度，82/872 文件快照上的 Code RAG 都返回目标文件 rank 1，并显式保留
9/2 个文件跳过导致的 degraded 状态。重复 CAS blob 校验的规模开销已完成前后对照优化；
youtube-dl-3 还完成了[两阶段完整 checkout 与 Worker 重启](multi-stage-full-checkout-pilot.md)。
在真实模型决策证据出现前，不增加第二次自动 replan、向量库、通用审批流或复杂 Planner。
