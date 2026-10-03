# 完整上游 Checkout Suite：BugsInPy tqdm-1 与 youtube-dl-3

更新：2026-10-03。本 suite 把来源绑定 A/B 从依赖裁剪 fixture 推进到两个不同项目的完整历史
checkout：固定 BugsInPy 声明的 buggy commit，分别保留 tqdm 的 82 个和 youtube-dl 的 872 个
tracked 文件，先执行初始失败门，再运行 Baseline 与单次 replan Treatment。执行使用冻结脚本模型，
验证的是 Harness、Code RAG、预算、可恢复等待和 Trace 合同，不是模型能力跑分。

## 1. 来源门与案例

Suite manifest：
[`benchmarks/run_ab/bugsinpy-full-checkout-pilot-v1.yaml`](../benchmarks/run_ab/bugsinpy-full-checkout-pilot-v1.yaml)。

| Case | Buggy → fixed | 完整 checkout | 保护验收 |
|---|---|---:|---|
| [`tqdm-1-tenumerate-start-full`](../benchmarks/run_ab/full/tqdm-1-tenumerate-start/manifest.yaml) | [`8cc777f`](https://github.com/tqdm/tqdm/commit/8cc777fe8401a05d07f2c97e65d15e4460feab88) → [`c0dcf39`](https://github.com/tqdm/tqdm/commit/c0dcf39b046d1b4ff6de14ac99ad9a1b10487512) | 82 files | `tenumerate(..., start=42)` 与 `enumerate(..., 42)` 一致 |
| [`youtube-dl-3-unescape-html-full`](../benchmarks/run_ab/full/youtube-dl-3-unescape-html/manifest.yaml) | [`f5469da`](https://github.com/ytdl-org/youtube-dl/commit/f5469da9e6e259c1690c7ef54f1da1c19f65036f) → [`95f3f7c`](https://github.com/ytdl-org/youtube-dl/commit/95f3f7c20a05e7ac490e768b8470b20538ef8581) | 872 files | `unescapeHTML('&a&quot;') == '&a"'` |

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

uv run --locked --cache-dir .uv-cache horizon eval run-ab-suite `
  benchmarks/run_ab/bugsinpy-full-checkout-pilot-v1.yaml `
  --image python:3.12-alpine `
  --state-dir .horizon/run-ab-suite-full-two-v1
```

两个保护命令都直接导入完整 checkout 中的真实生产代码。它们复现修复提交新增或强化的断言，
但不运行项目原 Python 版本和完整依赖矩阵，不能替代官方 BugsInPy runner。

## 3. 双项目 A/B 证据

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
随后显式 replan、精确修改真实生产文件并通过 Docker 验收。

| 项 | tqdm-1 | youtube-dl-3 |
|---|---|---|
| EvidencePack ref | `a938a05da068cf0d456460a8a788a9fb16da8d2e27aa9d16864f7e103a5cdfe9` | `ef1c855b529fd8a4ec65b67ffd6932cba99bf7d69540a24e9a09b473a60f988f` |
| Backend | SQLite FTS5 | SQLite FTS5 |
| 可索引 / 跳过 | 73 / 9 | 870 / 2 |
| 目标生产文件 | rank 1，`tqdm/contrib/__init__.py` | rank 1，`youtube_dl/utils.py` |
| 状态 | `degraded` | `degraded` |
| 降级原因 | non-UTF-8、超限文件 | non-UTF-8 文件 |

两个目标文件都排在 rank 1，但 11 个跳过结果必须保留，不能写成全仓无损索引。查询由作者冻结、
动作由脚本给定，也没有无 RAG arm，因此这只是 revision-bound 检索路径证据，不是检索增益结论。

## 5. 性能观察与保留负结果

首次双项目运行暴露了重复生成 workspace manifest 的规模瓶颈。根因是 `ArtifactStore.put()` 在
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
“依赖无关成功”；该候选被明确放弃，不计入 2 / 2。

## 6. 结论边界

可以声称：两个不同项目、共 954 个 tracked 文件的精确完整 checkout 已通过来源门、初始负例、
真实 Code RAG、可恢复等待、单次 replan、禁网 Docker 验收和 Trace replay。不能声称：

- 这是 BugsInPy 官方分数、原 Python 版本或完整依赖环境的测试结果；
- 脚本模型证明真实模型能自主生成查询、定位缺陷或选择 replan；
- 2 个作者选择案例足以估计泛化成功率、误触发率或成本收益。

后续增量已把 youtube-dl 案例扩展为[两阶段完整 checkout](multi-stage-full-checkout-pilot.md)，并在
production WorkItem 通过后换用 epoch 2 Worker，联合观察上下文、Memory、检索、replan 与恢复。
真实模型 arm 仍只在用户明确授权费用后执行；当前不增加向量库、无界 replan、分布式队列或
多 Agent 编排。
