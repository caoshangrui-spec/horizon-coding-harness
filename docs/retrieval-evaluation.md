# 词法检索离线诊断：协议、指标与当前结果

更新：2026-10-03。该诊断用于回答“当前 Code RAG 在一组冻结查询上能否找到指定源码路径”，
不能回答“Agent 能否解决真实 Issue”，也不能作为 SWE-bench 或跨项目泛化成绩。

## 1. 固定输入

首个清单位于
[`benchmarks/retrieval/horizon-lexical-v1.yaml`](../benchmarks/retrieval/horizon-lexical-v1.yaml)，
包含 5 个针对本仓库 `src/**` 的查询：Campaign 预算、上下文投影、崩溃恢复 promotion、未知
只读工具恢复、immutable retrieval evidence。每个案例固定：

- `case_id` 和不超过 200 字符的 query；
- 至少一个精确 `expected_paths`；
- 可选 `forbidden_paths`，用于识别混淆路径泄漏；
- `max_chunks`，范围 1～8。

expected/forbidden 必须是无 glob 的相对 POSIX 路径、二者不相交；案例 ID 不得重复；expected
必须在 manifest 的 allowed/denied scope 内。评测时 `SnapshotManager` 只捕获 allowed scope，
因此 `.uv-cache`、构建产物和其他无关文件既不进入 64 MiB 快照预算，也不能成为检索证据。

## 2. 执行与可重放输出

```powershell
uv run --locked --cache-dir .uv-cache horizon eval retrieval `
  benchmarks/retrieval/horizon-lexical-v1.yaml --source .
```

命令不加载 Provider 配置、不读取 API Key、不调用模型或网络，也不执行被评仓库代码。它：

```text
manifest + scoped immutable snapshot
  → revision-bound SQLite FTS5/scan retriever
  → 每案例 EvidencePack
  → typed RetrievalEvalReport
  → 内容寻址 report Artifact + stdout JSON
```

默认派生状态位于 `<source>/.horizon/retrieval-eval/`；也可用 `--state-dir` 指到 source 之外。
若 state 位于 source 内但不在 `.horizon/` 下，命令在创建目录前拒绝，避免评测产物污染输入。
报告固定包含 `paid_model_called=false` 和 `network_called=false`。

## 3. 指标定义

对案例 `i`，返回 chunk 排名为 `1..K_i`：

```text
hit_i       = 1，若任一 expected path 在前 K_i 个 chunk 中；否则 0
recall_i    = 命中的不同 expected path 数 / expected path 总数
RR_i        = 1 / 第一个 expected chunk 的 rank；未命中为 0
leakage_i   = 返回的不同 forbidden path 数
Hit@case-K  = Σ hit_i / 案例数
MicroRecall = Σ 命中的不同 expected path 数 / Σ expected path 总数
MRR         = Σ RR_i / 案例数
```

`empty_count` 与 `degraded_count` 单独统计：empty 是完整范围内无词法命中；degraded 表示 FTS
不可用或输入因边界被跳过。命中但 degraded 仍计入命中，同时保留降级计数，不能被总分掩盖。
返回同一路径的多个 chunk 只对 path recall 计一次，但第一个相关 chunk 的真实 rank 用于 MRR。

## 4. 当前冻结样例结果

在当前 Windows/Python/SQLite FTS5 环境中，固定 5 案例得到：

| 指标 | 结果 |
|---|---:|
| 案例 | 5 |
| Hit@case-K | 1.0（5/5） |
| Micro path recall | 1.0（5/5） |
| MRR | 0.9 |
| forbidden leakage | 0 |
| empty | 0 |
| degraded | 0 |

`crash-recoverable-promotion` 的目标 adapter 首次出现在 rank 2，其余四项 rank 1；报告保留这一
非满分排序，不做答案后处理。该结果只能证明当前实现对这 5 个内部、词项较明确的查询可用。
样例由实现者编写、规模小、与代码同仓，不是独立测试集，也没有测跨项目、自然语言 Issue、
中文查询、代码变体或最终修复成功率。

本轮最终报告的内容地址为
`30f76f6b41a5d6ded7afc4278747f249869e84b3312e6e5205e6d5d51498fe7f`，对应 scoped source
workspace revision `faf080297de017960f7ab9bd67f27eb3b034a878f8c916339b9557cfa6f3d0da`。
它是本机 `.horizon/retrieval-eval` 下的可回读证据，不是 Git commit 或公开 benchmark ID。

## 5. 测试与失败保留

离线测试还覆盖：汇总 Hit/Recall/MRR、forbidden leakage、empty、显式 scan fallback/degraded、
重复案例、路径重叠/glob 拒绝、CLI report Artifact，以及 scoped snapshot 不读取范围外大文件。
未来新增 symbol、路径先验或 reranker 时必须保留本 manifest，并增加关闭新能力的对照；
不得删除失败案例或把 degraded 当正常命中。

新增固定清单
[`youtube-dl-identifier-variants-v1.yaml`](../benchmarks/retrieval/youtube-dl-identifier-variants-v1.yaml)
只捕获完整 checkout 的 `youtube_dl/**`，不执行源码，包含两个作者选定案例：自然词组
`unescape HTML` → camelCase 定义，以及 camelCase 查询 `registerSocksProtocols` → snake_case
定义。最终 2/2 命中，两个目标均 rank 1，Recall/MRR=1.0，empty/degraded/leakage=0；报告 ref 为
`d711215e24ebf7dd98610a1d96a6a2bc84c2cb3901267e0524d091f1239c08c4`。

负结果没有删除：仅加入命名拆分/组合时，第一案例 Hit@5=0，报告 ref
`52922a0aaf100589f6a84faa547b3094966c24718bd79e3b6702c05d3ae242e6`；只把精确组合 token 放在
普通 BM25 前时仍为 Hit@5=0，报告 ref
`79676ed48a4f0a7ea01aad3b377198598e1793681044333b235b717e5a201f6d`。诊断发现目标定义 chunk 在
151 个精确 token 命中中约排第 100，原因是大文件长度惩罚与大量 import/call。最终规则只在
最多 256 个候选中优先规范化同名 `def/class`，没有引入 AST、向量库或模型 reranker。

下一批有价值的增量是：dirty-revision 查询对、同名符号混淆和更多外部小仓库盲测。当前
youtube-dl 清单由实现者选题，不能算盲测；若这些结果仍显示稳定的词法缺口，再决定是否承担
embedding/vector 的依赖、成本和索引一致性复杂度。

补充证据不并入上述固定 5 案例分数：[双项目完整 checkout suite](full-checkout-pilot.md)用作者
冻结的查询在 82/872 文件上游仓库中都返回目标生产文件 rank 1；分别有 73/870 个文件可索引、
9/2 个因非 UTF-8 或超限被跳过，因此 EvidencePack 正确保留为 `degraded`。它验证外部仓库
执行路径，不是盲测或真实模型查询质量。
