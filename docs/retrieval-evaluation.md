# 词法检索离线诊断：协议、指标与当前结果

更新：2026-10-04。该诊断用于回答“当前 Code RAG 在一组冻结查询上能否找到指定源码路径”，
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

v6 本轮报告的内容地址为
`4fb244757043f2a211fac19b7fc622c7bd98b34e98e250ec4de72771ca335103`，对应 scoped source
workspace revision `11b930dabb207447a8a6a80b716285a112437e446f923c83a964575f99709943`。
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
v6 报告 ref 为
`84448f19b56575084e257323a3f29ab429f05aa262f2dc7b8a09560739897560`；v5 历史成功报告
`d711215e24ebf7dd98610a1d96a6a2bc84c2cb3901267e0524d091f1239c08c4` 继续保留。

负结果没有删除：仅加入命名拆分/组合时，第一案例 Hit@5=0，报告 ref
`52922a0aaf100589f6a84faa547b3094966c24718bd79e3b6702c05d3ae242e6`；只把精确组合 token 放在
普通 BM25 前时仍为 Hit@5=0，报告 ref
`79676ed48a4f0a7ea01aad3b377198598e1793681044333b235b717e5a201f6d`。诊断发现目标定义 chunk 在
151 个精确 token 命中中约排第 100，原因是大文件长度惩罚与大量 import/call。最终规则只在
最多 256 个候选中优先规范化同名 `def/class`，没有引入 AST、向量库或模型 reranker。

## 6. 同名符号与 dirty-revision 诊断

新增固定清单
[`horizon-symbol-ambiguity-v1.yaml`](../benchmarks/retrieval/horizon-symbol-ambiguity-v1.yaml)
及其[双模块夹具](../benchmarks/retrieval/fixtures/symbol-ambiguity-v1/)。`orders` 与 `audit` 模块
都定义 `render_invoice`，并故意在说明文本中高频提及对方模块；两个查询都只取 rank 1，并把
另一个同名实现列为 forbidden path。

修复前 v5 的真实离线结果为 Hit@1=0/2、MRR=0、leakage=2，report ref
`7ef81a374121e54b1e6e6cde6ed4fe6ca3b345f218fff275ba79a3399a4287a9`。负结果显示整句
`orders renderInvoice` 被错误组合成一个符号候选，随后 BM25 被交叉说明文本误导。v6 分开保留
token 内的 `renderinvoice` 与整句组合候选，并只在确实定义该符号的片段之间比较路径 token；
结果为 Hit@1=2/2、MRR=1.0、leakage=0，report ref
`022383e9996623c69335c9b1d8e4ca6cec8426cadccd89c06adfae59eed10465`。

配套 dirty-revision 集成测试在同一 workspace 中先建立两个同名定义，再重命名旧实现并增加
新模块：新 snapshot 必须生成不同 revision/index key 并把新模块排到 rank 1；旧 manifest 在
新索引创建后仍逐对象重放原 EvidencePack。FTS5 与显式 lexical-scan fallback 都覆盖同名路径
规则。该夹具由实现者有意构造，不是外部盲测；查询缺少模块限定词时仍无法可靠消歧。

## 7. 三项目外部定位盲测

[`external-blind-v1/selection.yaml`](../benchmarks/retrieval/external-blind-v1/selection.yaml)
在查看上游补丁与 gold path 前冻结了 Cookiecutter 2、HTTPie 2、The Fuck 1 的错误/修复 commit
及查询，并单独提交为 `74ddaaec2cd1ebdf79056777e5369102b59fa9c3`。查询只来自上游 Issue
标题或修复 commit 标题；PySnooper 1 因 Issue 正文直接给出目标文件和行而排除。冻结后只读取
`git diff --name-status` 生成 production gold path，没有读取补丁正文、执行仓库代码或调用模型。

v6 的首次结果是：

| 指标 | 盲测基线 |
|---|---:|
| Hit@1 | 0/3 |
| Hit@5 | 3/3 |
| Micro path recall@5 | 3/4 = 0.75 |
| MRR | 0.416667 |
| degraded | 0 |

Cookiecutter 的目标为 rank 4；HTTPie 只返回两个修复 production path 中的一个；The Fuck 为
rank 2。完整 returned path 和内容寻址 report ref 保存在
[`baseline-v6.yaml`](../benchmarks/retrieval/external-blind-v1/baseline-v6.yaml)，负结果没有删除。

三个案例共同暴露前 5 个 chunk 被同一文件重复占位。v7 只增加确定性路径多样化：原排序中每个
文件的首个 chunk 先占槽，仍有空位时再按原顺序补同文件 chunk；候选仍最多 256，没有新服务、
依赖或 gold path 特例。处理后 HTTPie 召回 2/2，整体 Micro path recall@5 为 4/4=1.0，MRR
为 0.444444；但 **Hit@1 仍为 0/3**。完整结果见
[`treatment-v7.yaml`](../benchmarks/retrieval/external-blind-v1/treatment-v7.yaml)。因为 v7 使用了该
基线的失败信息，它是自适应 treatment，不是新的独立 holdout；不能据此声称跨任务泛化或真实
Agent 成功率提升。

## 8. 第二批独立冻结 holdout

v7 完成后、读取任何新修复文件名之前，Luigi 1、Sanic 1、Tornado 1 的错误/修复 commit、
BugsInPy failing-test 名和自然语言查询被单独冻结在 commit
`83ca2c1f0098720ada888fbfb7186f269fd3172f`。选择协议见
[`external-holdout-v2/selection.yaml`](../benchmarks/retrieval/external-holdout-v2/selection.yaml)。
随后仍只用 `git diff --name-status` 揭示 production gold path，未读取补丁正文、调用模型或执行
外部代码。

当前 v7 结果为：

| 指标 | 新 holdout |
|---|---:|
| Hit@1 | 3/3 |
| Hit@5 | 3/3 |
| Micro path recall@5 | 3/3 = 1.0 |
| MRR | 1.0 |
| degraded cases | 2/3 |

三个目标分别为 `luigi/server.py`、`sanic/app.py`、`tornado/websocket.py`，均为 rank 1。291 个
scoped 文件中实际索引 252 个、跳过 39 个：Luigi 的 36 个是字体、图片和单行压缩前端资源；
Tornado 的 3 个是二进制测试 fixture。目标 production 文件均已索引并在返回前通过 immutable
Artifact 复核，因此命中保留，同时两个案例继续标记 `degraded`。完整路径、计数和 report ref
见 [`result-v7.yaml`](../benchmarks/retrieval/external-holdout-v2/result-v7.yaml)。

这批任务没有参与 v7 规则设计，可以作为当前实现的独立顺序 holdout；但只有 3 个作者选择的
案例，查询含有 `metrics handler`、`blueprint middleware`、`websocket nodelay` 等明确词项，
不是随机样本或官方 BugsInPy 成绩。它降低了立即引入 symbol/vector 的必要性，却不能证明
Agent 会生成同样的查询，更不能证明能够完成计划、修改和验证。

## 9. 其他外部执行证据

补充证据不并入上述固定 5 案例分数：[双项目完整 checkout suite](full-checkout-pilot.md)用作者
冻结的查询在 82/872 文件上游仓库中都返回目标生产文件 rank 1；分别有 73/870 个文件可索引、
9/2 个因非 UTF-8 或超限被跳过，因此 EvidencePack 正确保留为 `degraded`。它验证外部仓库
执行路径，不是盲测或真实模型查询质量。
