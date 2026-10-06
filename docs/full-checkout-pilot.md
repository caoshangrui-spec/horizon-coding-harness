# 完整上游 Checkout Suite：BugsInPy 三项目

更新：2026-10-06。本 suite 把来源绑定 A/B 从依赖裁剪 fixture 推进到三个不同项目的完整历史
checkout：固定 BugsInPy 声明的 buggy commit，分别保留 tqdm 的 82 个、youtube-dl 的 872 个和
Luigi 的 382 个 tracked 文件，先执行初始失败门，再运行 Baseline 与单次 replan Treatment。执行
使用冻结脚本模型，验证的是 Harness、Code RAG、预算、可恢复等待和 Trace 合同，不是模型能力跑分。

## 1. 来源门与案例

- v1：[`bugsinpy-full-checkout-pilot-v1.yaml`](../benchmarks/run_ab/bugsinpy-full-checkout-pilot-v1.yaml)，
  tqdm 与 youtube-dl 两例，digest
  `e6856e2e974cd855b535c9bae46bdd2795c3e5cb0bbdafcbd481ef54d649d69e`。
- v2：[`bugsinpy-full-checkout-pilot-v2.yaml`](../benchmarks/run_ab/bugsinpy-full-checkout-pilot-v2.yaml)，
  原样保留 v1 两个 case，再追加 Luigi，digest
  `be359684b9e3adfd676ed06c457197f7aff42bdcd1dbb3913321cebf5ef857b4`。

| Case | Buggy → fixed | 完整 checkout | 保护验收 |
|---|---|---:|---|
| [`tqdm-1-tenumerate-start-full`](../benchmarks/run_ab/full/tqdm-1-tenumerate-start/manifest.yaml) | [`8cc777f`](https://github.com/tqdm/tqdm/commit/8cc777fe8401a05d07f2c97e65d15e4460feab88) → [`c0dcf39`](https://github.com/tqdm/tqdm/commit/c0dcf39b046d1b4ff6de14ac99ad9a1b10487512) | 82 files | `tenumerate(..., start=42)` 与 `enumerate(..., 42)` 一致 |
| [`youtube-dl-3-unescape-html-full`](../benchmarks/run_ab/full/youtube-dl-3-unescape-html/manifest.yaml) | [`f5469da`](https://github.com/ytdl-org/youtube-dl/commit/f5469da9e6e259c1690c7ef54f1da1c19f65036f) → [`95f3f7c`](https://github.com/ytdl-org/youtube-dl/commit/95f3f7c20a05e7ac490e768b8470b20538ef8581) | 872 files | `unescapeHTML('&a&quot;') == '&a"'` |
| [`luigi-1-metrics-handler-full`](../benchmarks/run_ab/full/luigi-1-metrics-handler/manifest.yaml) | [`1164eb6`](https://github.com/spotify/luigi/commit/1164eb6b85b8a70f596dbb99452bec513e72c12e) → [`aec5dc2`](https://github.com/spotify/luigi/commit/aec5dc2ed8db53fc282a0bd24aabe59031b6d1ba) | 382 files | payload 由 collector 生成，HTTP handler 也必须由同一 collector 配置 |

缺陷元数据来自 [BugsInPy](https://github.com/soarsmu/BugsInPy)。执行前 CLI 会拒绝以下任一情况：

- suite case ID 与 case benchmark ID 不一致；
- case `base_commit` 与来源 `buggy_commit` 不一致；
- fix URL 不指向同一仓库的声明 `fixed_commit`，或 license URL 不属于该仓库；
- `full_checkout` fixture 不是 Git 仓库、HEAD 不等于 buggy commit，或存在 tracked、untracked、
  ignored 额外内容。

因此 `reduction=full_checkout` 只能由精确、干净的上游 checkout 产生；`.git` 不进入 Agent
workspace，工作副本从内容寻址快照恢复。

## 2. 可重复准备

完整第三方源码不纳入本仓 Git；各 case 的 `fixture/` 被明确忽略。首次运行需要联网抓取固定
commit，之后评测自身不联网、不安装依赖：

```powershell
$tqdm = "benchmarks/run_ab/full/tqdm-1-tenumerate-start/fixture"
New-Item -ItemType Directory -Path $tqdm
git -C $tqdm init
git -C $tqdm remote add origin https://github.com/tqdm/tqdm.git
git -C $tqdm fetch --depth 1 origin 8cc777fe8401a05d07f2c97e65d15e4460feab88
git -C $tqdm checkout --detach FETCH_HEAD

$youtube = "benchmarks/run_ab/full/youtube-dl-3-unescape-html/fixture"
New-Item -ItemType Directory -Path $youtube
git -C $youtube init
git -C $youtube remote add origin https://github.com/ytdl-org/youtube-dl.git
git -C $youtube fetch --depth 1 origin f5469da9e6e259c1690c7ef54f1da1c19f65036f
git -C $youtube checkout --detach FETCH_HEAD

$luigi = "benchmarks/run_ab/full/luigi-1-metrics-handler/fixture"
New-Item -ItemType Directory -Path $luigi
git -C $luigi init
git -C $luigi remote add origin https://github.com/spotify/luigi.git
git -C $luigi fetch --depth 1 origin 1164eb6b85b8a70f596dbb99452bec513e72c12e
git -C $luigi checkout --detach FETCH_HEAD

uv run --locked --cache-dir .uv-cache horizon eval run-ab-suite `
  benchmarks/run_ab/bugsinpy-full-checkout-pilot-v2.yaml `
  --image python:3.12-alpine `
  --state-dir .horizon/run-ab-suite-full-three-v2
```

前两个保护命令直接导入完整 checkout 中的真实生产代码。Luigi 的历史模块导入依赖 Tornado、
`pkg_resources` 和完整 Luigi 包，因此第三个命令用标准库 AST 从真实 `luigi/server.py` 提取并执行
`MetricsHandler` 类，再以独立 payload/collector 假对象验证所有权；它不是文本 marker 检查，也
没有安装历史依赖栈。三个命令均复现修复提交的目标行为，但不运行项目原 Python 版本和完整依赖
矩阵，不能替代官方 BugsInPy runner。

## 3. 当前 A/B 证据

### 3.1 v2 三项目本地可信执行

2026-10-06 使用 Python 3.12 的 test-only 本地执行器实际运行三个冻结保护命令；Harness 的源码门、
快照、RAG、Gateway、预算、Run/Trace 和重放路径保持不变，只有生产 Docker acceptance adapter 被
替换。因此这是完整 Harness 本地证据，不是容器隔离证据：

| 指标 | tqdm-1 | youtube-dl-3 | Luigi-1 | Suite 汇总 |
|---|---:|---:|---:|---:|
| 初始 protected check | 失败，符合预期 | 失败，符合预期 | 失败，符合预期 | 3 / 3 |
| Baseline | `WAITING_FOR_USER` | `WAITING_FOR_USER` | `WAITING_FOR_USER` | 3 / 3 |
| Single replan | `SUCCEEDED` | `SUCCEEDED` | `SUCCEEDED` | 3 / 3 |
| 成功增量 | +1 | +1 | +1 | +3 |
| Model-call 增量 | +2 | +2 | +3 | +7 |
| Tool-call / step 增量 | +3 / +3 | +3 / +3 | +4 / +4 | +10 / +10 |
| 合成模型成本增量（CNY） | 0.00096 | 0.00096 | 0.00144 | 0.00336 |

- Suite report ref：`ebca0211b92e82eb90f1f122df6bb6f48a9bf071afe3476f2745d44a7df28695`
- tqdm / youtube-dl / Luigi case report refs：
  `7e775ea79ebf5bbf2c0b45cf1c9ce698c868660248c6d1c6c096dce0170ac687`、
  `6cf005f1f4cdd05de1c93da3cec030f43e8fc09f3bde4d95ea0c3523117f71e6`、
  `dc7ac71c1eef08d88cfa8282f7f973d8413c3f85bfefe7a38d0d1127d24d953f`。
- 全部 arm 的 unknown model/tool calls 与开放预留为 0，Trace replay 通过，三个 source checkout
  保持干净；`paid_model_called=false`、`network_called=false`、`repository_code_executed=true`。

首次 Luigi 单案运行保留了一个负结果：manifest 用 LF 多行 exact replacement 匹配 Windows CRLF
checkout，Gateway 正确返回 `Exact replacement occurrence count did not match`，Treatment 最终失败。
修正为两个行结尾无关的原子单行替换后，单案和三项目 suite 均通过；没有放宽 exact-match 合同。

### 3.2 v1 双项目历史 Docker 结果

2026-10-03 使用本机已有禁网镜像 digest
`sha256:0687a6bc9716edc2a6ee0fbfb0f87e7ee358b262b67c9215de91bc9b2d38ba71`：

| 指标 | tqdm-1 | youtube-dl-3 | Suite 汇总 |
|---|---:|---:|---:|
| 初始 protected check | 失败，符合预期 | 失败，符合预期 | 2 / 2 |
| Baseline | `WAITING_FOR_USER` | `WAITING_FOR_USER` | 2 / 2 |
| Single replan | `SUCCEEDED` | `SUCCEEDED` | 2 / 2 |
| Baseline model / tool / step | 4 / 4 / 4 | 4 / 4 / 4 | 8 / 8 / 8 |
| Treatment model / tool / step | 6 / 7 / 7 | 6 / 7 / 7 | 12 / 14 / 14 |
| 成功 / model / tool / step 增量 | +1 / +2 / +3 / +3 | +1 / +2 / +3 / +3 | +2 / +4 / +6 / +6 |
| 合成模型成本增量（CNY） | 0.00096 | 0.00096 | 0.00192 |
| Trace replay / source unchanged | 通过 / 是 | 通过 / 是 | 4 traces / 是 |

所有 arm 的 unknown model/tool calls 与开放预留均为 0；`paid_model_called=false`、
`network_called=false`、`repository_code_executed=true`。

内容寻址证据：

- Suite manifest digest：`e6856e2e974cd855b535c9bae46bdd2795c3e5cb0bbdafcbd481ef54d649d69e`
- Suite report ref：`78127dc48acccf9d4768f4ec4c3a47551d385fdf379c2f421bd397283686830a`
- tqdm case manifest / fixture revision：
  `6be9b2518809db0561ad1e7f2ac3e37693ba3072b9db6c4551fe18b79e1b58e0` /
  `0facaee0ad34b048338c656cdbfc61dfc443229991d7b6573ccf0bcc8e9effc2`
- tqdm case report / Baseline trace / Treatment trace：
  `7553925f5d77b626bd9d6de466aa484e23e586827328f0ae91ab56a6118cc24a` /
  `d0b3b09d498fc226f350fa9be412bfcc9d54ed401effd9a8dd2305ac1924c72d` /
  `7bae18f5e2da38f11e29b32677894550480676f9ee82ec3c288b28fa7c3444f8`
- youtube-dl case manifest / fixture revision：
  `19584467865ce1d3adb40ea07e92f36bcb8e61488716577c0ef7dd62e2875e50` /
  `fb97a849f99bbdd13922ac4a4a471c36b4b463b313493bf03479c464fed94b10`
- youtube-dl case report / Baseline trace / Treatment trace：
  `0949e4558d8fab058b583fa888d4280fd9c352d3a518ea3c266728a8023edd4a` /
  `614b8c1be47fc56e1ef69c90aec6c254256dc61c145f91ddaa83450e05228ad3` /
  `ef1268b2f8f54d1da63b13b521243b18217d367b2a263a17a52b36330ae37b0a`

## 4. 完整仓库 Code RAG

每个 arm 先重复同一条 `retrieve_code` 查询，第三次相同查询由 NoProgressPolicy 阻断；Treatment
随后显式 replan、精确修改真实生产文件并通过对应保护验收。v1 两例的历史结果来自 Docker，
v2 三例的本批结果来自 test-only 本地 Python 执行器。

| 项 | tqdm-1 | youtube-dl-3 | Luigi-1 |
|---|---|---|---|
| EvidencePack ref | `6c2fdfec5472b6d9250a63c0088a142bae81ae9af27c2965a4421b486140365f` | `1de34743323676b1099704c3dc311f253d53d65722c36ad48d1d4ff0add77ffc` | `a221d9a8703d5e3276a0069151f6513335b42d395c1c54bf7372e14f4d1519b5` |
| Backend | SQLite FTS5 | SQLite FTS5 | SQLite FTS5 |
| 可索引 / 跳过 | 73 / 9 | 870 / 2 | 327 / 55 |
| 目标生产文件 | rank 1，`tqdm/contrib/__init__.py` | rank 1，`youtube_dl/utils.py` | rank 1，`luigi/server.py` |
| 状态 | `degraded` | `degraded` | `degraded` |
| 降级原因 | non-UTF-8、超限文件 | non-UTF-8 文件 | non-UTF-8、超长行文件 |

三个目标文件都排在 rank 1，但 66 个跳过结果必须保留，不能写成全仓无损索引。查询由作者冻结、
动作由脚本给定，也没有无 RAG arm，因此这只是 revision-bound 检索路径证据，不是检索增益结论。

## 5. 性能观察与保留负结果

历史 v1 双项目运行暴露了重复生成 workspace manifest 的规模瓶颈。根因是 `ArtifactStore.put()` 在
每次 CAS 去重命中时都会重新读取并计算完整 Artifact 摘要；workspace 文件已被读取一次，随后又
读取一遍相同的 CAS blob。现在每个 Store 实例用有界缓存记住自己已写入或完整验证的 Artifact
元数据签名；签名变化会立即回退到完整摘要校验，新进程也会重新验证，显式 `read()` 始终校验内容。

| 对照 | 优化前 | 优化后 | 降幅 |
|---|---:|---:|---:|
| 872 文件首次 capture | 4.752 s | 4.778 s | 无实质变化 |
| 同进程同 revision 再 capture | 19.931 s | 2.247 s | 88.7% |
| tqdm Baseline Trace | 32.33 s | 7.47 s | 76.9% |
| tqdm Treatment Trace | 58.03 s | 13.24 s | 77.2% |
| youtube-dl Baseline Trace | 310.23 s | 67.67 s | 78.2% |
| youtube-dl Treatment Trace | 527.37 s | 117.10 s | 77.8% |
| 完整双项目 suite 墙钟 | 1124.65 s | 340.00 s | 69.8% |

优化后的 suite 仍产生相同 manifest digest、fixture revision 和两个 EvidencePack ref，2/2 结果、
事件数（每个 Baseline 48、Treatment 74）、预算、source unchanged 与 Trace replay 均未变化。
这证明加速没有通过跳过 workspace 内容哈希、共享 arm 状态或放宽完整性门取得。

候选选择也保留一个负结果：PySnooper bug 3 的 BugsInPy `requirements.txt` 为空，但真实源码导入
`decorator`，官方 `setup.sh` 也会安装它。为避免为一个案例维护专用依赖镜像，本批没有把它包装成
“依赖无关成功”；该候选被明确放弃，不计入历史 v1 的 2/2 或当前 v2 的 3/3。

## 6. 结论边界

可以声称：三个不同项目、共 1,336 个 tracked 文件的精确完整 checkout 已通过来源门、初始负例、
真实 Code RAG、可恢复等待、单次 replan、本地可信验收和 Trace replay；其中原 v1 两个项目另有
历史禁网 Docker 证据。不能声称：

- 这是 BugsInPy 官方分数、原 Python 版本或完整依赖环境的测试结果；
- v2 三项目已经通过 Docker；Luigi 的正式 Docker 验收仍待 daemon 可用后执行；
- 脚本模型证明真实模型能自主生成查询、定位缺陷或选择 replan；
- 3 个作者选择案例足以估计泛化成功率、误触发率或成本收益。

后续增量已把 youtube-dl 案例扩展为[两阶段完整 checkout](multi-stage-full-checkout-pilot.md)，并在
production WorkItem 通过后换用 epoch 2 Worker，联合观察上下文、Memory、检索、replan 与恢复。
真实模型 arm 仍只在用户明确授权费用后执行；当前不增加向量库、无界 replan、分布式队列或
多 Agent 编排。
