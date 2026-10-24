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

补充证据不并入上述固定 5 案例分数：[三项目完整 checkout suite](full-checkout-pilot.md)用作者
冻结的查询在 82/872/382 文件上游仓库中都返回目标生产文件 rank 1；分别有 73/870/327 个文件
可索引、9/2/55 个因非 UTF-8 或超限被跳过，因此 EvidencePack 正确保留为 `degraded`。它验证外部仓库
执行路径，不是盲测或真实模型查询质量。

## 10. 24 案例外部定位集与 v0.3.0 development 基线

为避免继续用 3 个外部案例支撑过强结论，新增
[`external-development-v3/selection.yaml`](../benchmarks/retrieval/external-development-v3/selection.yaml)。
它在查看新案例的生产 gold path 前单独提交为
`57776b89d1f7a99723f61c5a8b15db5c5ff579ae`，包含 Cookiecutter、HTTPie、Luigi、PySnooper、
Sanic、The Fuck 和 Tornado 共 7 个项目、24 个 BugsInPy bug。查询只把 failing-test 的类名与
测试名规范化为词组，不读取 Issue 补丁内容，也不调用模型。16 个案例划入 development，另 8 个
holdout 继续保持 sealed；本轮没有读取 holdout 的变更路径或物化其源码。

新增 `horizon eval retrieval-suite`：每个案例仍调用原 `RetrievalEvaluator` 和 revision-bound
EvidencePack，只在外层聚合不同 source snapshot。报告按去重后的 path ranking 给出
Hit/Recall/MAP/nDCG@1/3/5/8、MRR、empty/degraded/leakage，以及首次建立索引与同进程缓存复用的
P50/P95。命令要求全新的 `--state-dir`，避免把已有磁盘缓存误报为 cold run；同一案例的 warm
报告必须与首次报告逐字段相等。评测不执行仓库源码、不加载 provider，也不发起网络请求。

v0.3.0 词法检索在 16 个 development 案例、20 个 production gold path 上的冻结结果为：

| K | Hit | Micro path recall | MAP | nDCG |
|---:|---:|---:|---:|---:|
| 1 | 11/16 = 0.6875 | 11/20 = 0.55 | 0.6375 | 0.6875 |
| 3 | 15/16 = 0.9375 | 17/20 = 0.85 | 0.777083 | 0.837049 |
| 5 | 16/16 = 1.0 | 18/20 = 0.90 | 0.792708 | 0.846637 |
| 8 | 16/16 = 1.0 | 18/20 = 0.90 | 0.792708 | 0.846637 |

MRR 为 `0.817708`，leakage/empty 均为 0，3 个 Luigi 案例因跳过非 UTF-8 或超长行保持
`degraded`。LF 规范化源码上，全新状态目录的首次评测 P50/P95 为 `414.989 / 3809.351 ms`，
同进程缓存复用为 `338.235 / 2135.362 ms`；这只是本机 16 案例的冷索引+查询与 warm 查询观察，不是跨机器性能
承诺。内容寻址 suite report ref 为
`8726f5d1ea513269542eab17b1b788cd1ebb7a4d30f02f3f323fa2f315b482a3`，完整案例、返回路径和
子报告 ref 保存在
[`baseline-v0.3.0.yaml`](../benchmarks/retrieval/external-development-v3/baseline-v0.3.0.yaml)。

负结果继续保留：Luigi-7 的目标 `luigi/scheduler.py` 到 rank 4 才出现；PySnooper-2 的修复
跨 5 个 production path，前 8 个 chunk 只覆盖 `utils.py`、`tracer.py`、`variables.py`，
`__init__.py` 与 `pycompat.py` 未召回，因此 K=8 总 recall 停在 0.90。这个 development 集已经
揭示 gold，后续检索改动可以在它上面诊断，但不能把调参后的分数称为独立泛化证据。只有候选
规则在 development 上冻结后，才允许一次性揭示并运行剩余 8 个 holdout。

源码准备现在也进入可复现合同，而不再依赖人工描述。`source-materialization.yaml` 为每个
development 案例固定公开 GitHub URL、buggy commit、production package 根、文件数、字节数和
跨平台内容树 SHA-256。`horizon eval materialize-retrieval-suite` 按仓库合并 shallow fetch，使用
bare Git cache，并为所有 Git 命令强制 `core.autocrlf=false` 与 `core.eol=lf` 后再由 `git archive`
生成全新目录；它拒绝 credential URL、路径穿越、link、内容哈希
漂移和已有目标目录，默认也拒绝 holdout manifest。该阶段需要 GitHub 网络，但不 checkout、import
或执行上游代码。Windows 默认 `core.autocrlf` 曾使首版字节证据与 Linux CI 不一致；该失败及修复
保留为跨平台负证据。规范化后的 source manifest digest 为
`74a87b4dbf6c54f8f9fd003473dcc0a7995f5b7be9953e080a1f5d672ec16431`，16/16 案例已在本机重新物化。

`retrieval-suite --baseline baseline-v0.3.0.yaml` 进一步逐项核对稳定证据：全部 path ranking、
gold 覆盖、状态、degradation reason、子报告 ref 和聚合指标；机器相关的 cold/warm 时间及 suite
report ref 不进入相等判定。本机重建后的稳定投影 digest 为
`ea72dcf20fe111527b9058df388a94feee7557b567631325385357a885fbb934`。独立
`retrieval-evidence.yml` 只在 main 的相关路径变更或手工触发时重新下载 development 源码、执行
冻结比较并上传 source receipt 与报告；第三方源码、Git cache 和检索索引不上传。这个 CI 闭环
验证数据准备和排名可复现，仍不是 holdout、真实 Agent query 或端到端修复证据。

## 11. 已冻结查询扰动诊断

在已揭示的 development 结果中，选择原基线 rank 2、rank 3、rank 4 和多文件 Recall 不足的
4 个困难案例；先以 commit `03261ed2f68a99f9a48e82bf9781590232e959d0` 冻结查询，再运行
`raw_identifier`、`english_paraphrase`、`failure_description`、`chinese_description` 四种形式。
所有查询只来自 failing-test 标识符和自然语言改写，不复制实现路径、源码符号或 patch 内容。

| 查询形式 | Hit@8 | Path Recall@8 | MRR |
|---|---:|---:|---:|
| raw identifier | 4/4 | 6/8 = 0.75 | 0.460714 |
| English paraphrase | 4/4 | 8/8 = 1.0 | 0.520833 |
| failure description | 4/4 | 8/8 = 1.0 | 0.40625 |
| 中文描述 | 1/4 | 1/8 = 0.125 | 0.0625 |
| 合计 | 13/16 | 23/32 = 0.71875 | 0.362574 |

完整 16 查询在 K=1/3/5/8 的 Hit、Recall、MAP、nDCG、每条返回路径与四个内容寻址报告见
[`query-robustness-v1/result-v1.yaml`](../benchmarks/retrieval/external-development-v3/query-robustness-v1/result-v1.yaml)。
英文改写使 PySnooper 的 5 个 production path 全部进入前 8；原始 identifier 仍只有 3 个。
反面证据更重要：三个纯中文描述未命中；Tornado 的中文描述之所以命中，是查询仍保留 `HTTP`
和 `PUT` 两个拉丁词项。当前实现是英文代码上的词法检索，不应包装成多语言 semantic RAG。
该切片按已知弱点选择且 gold 已揭示，只用于确定下一步优先级，不能称为泛化或独立 holdout。

## 12. 确定性查询规范化 development A/B

在查看 treatment 结果前，commit `109b268b1515a2f1e6b2f5ee5dfd37b7daaaeb89` 先冻结了
`query_normalization.py`、16 条 treatment 查询和 A/B 协议。规范化器只接收 query：拆分
snake/camel identifier，删除测试脚手架与少量低信息词，扩展一个小型英文别名表，并用固定中文
软件术语表生成英文 lexical terms。它不接收 case ID、仓库名、源码、路径、gold 或 patch，也不调用
模型和网络。完整结果见
[`query-normalization-v1/result-v1.yaml`](../benchmarks/retrieval/external-development-v3/query-normalization-v1/result-v1.yaml)。

| K | Baseline Hit | Treatment Hit | Baseline path recall | Treatment path recall |
|---:|---:|---:|---:|---:|
| 1 | 3/16 | 7/16 | 3/32 = 0.09375 | 7/32 = 0.21875 |
| 3 | 6/16 | 13/16 | 12/32 = 0.375 | 21/32 = 0.65625 |
| 5 | 11/16 | 15/16 | 21/32 = 0.65625 | 27/32 = 0.84375 |
| 8 | 13/16 | 16/16 | 23/32 = 0.71875 | 28/32 = 0.875 |

MRR 从 `0.362574` 提至 `0.653646`；empty 从 1 降至 0，degraded 仍为 4。按查询形式看：

| 查询形式 | Baseline Recall@8 | Treatment Recall@8 | Baseline MRR | Treatment MRR |
|---|---:|---:|---:|---:|
| raw identifier | 6/8 | 7/8 | 0.460714 | 0.520833 |
| English paraphrase | 8/8 | 7/8 | 0.520833 | 0.750000 |
| failure description | 8/8 | 7/8 | 0.406250 | 0.593750 |
| 中文描述 | 1/8 | 7/8 | 0.062500 | 0.750000 |

这里不能只看总分：English paraphrase 与 failure description 都因 PySnooper 的 `__init__.py` 掉出
前 8 而从 8/8 回退到 7/8；Tornado failure description 仍到 rank 8 才命中；4 个 Luigi 查询继续
因跳过不支持文件而标记 degraded。中文提升也只覆盖这个已知的四案例固定词表，不能外推成通用
多语言 RAG。因此本轮**不改变默认 runtime policy**，规范化器只作为实验组件保留。下一步应冻结
并测试一个更窄的 gate：仅对含中文或明确测试标识符脚手架的 query 启用，普通英文自然语言保持原样。

## 13. 选择性查询规范化门控

commit `2626daabac7e303904c864119563fe4c04a9e046` 在运行结果前冻结了更窄的
`normalize_retrieval_query_selectively`：query 含 CJK 字符，或含带下划线／camel boundary 且
包含 `test`、`tests`、`TestCase` 脚手架的结构化 identifier 时才调用规范化器；其余普通英文
query 字节级原样旁路。门控只读取 query，不读取仓库、源码、路径、gold 或 patch。完整排名和
三方比较见
[`query-normalization-gate-v1/result-v1.yaml`](../benchmarks/retrieval/external-development-v3/query-normalization-gate-v1/result-v1.yaml)。

| K | Baseline Hit / Recall | Always-on Hit / Recall | Selective Hit / Recall |
|---:|---:|---:|---:|
| 1 | 3/16 / 3/32 | 7/16 / 7/32 | 5/16 / 5/32 |
| 3 | 6/16 / 12/32 | 13/16 / 21/32 | 11/16 / 19/32 |
| 5 | 11/16 / 21/32 | 15/16 / 27/32 | 15/16 / 29/32 |
| 8 | 13/16 / 23/32 | 16/16 / 28/32 | 16/16 / 30/32 |

门控对 8 条 raw identifier／中文 query 生效，对 8 条英文改写／故障描述原样旁路。它保留中文
`7/8` 和 raw identifier `7/8` 的 Recall@8，同时把 always-on 在两类英文 query 上各丢失的
PySnooper `__init__.py` 恢复，因此总路径 Recall@8 达到 `30/32 = 0.9375`。代价也明确：MRR
为 `0.549479`，高于 baseline 的 `0.362574`，但低于 always-on 的 `0.653646`；旁路英文 query
同时放弃了别名扩展带来的前排提升。Tornado failure description 仍到 rank 8 才命中，PySnooper
raw／中文仍各缺 1 个路径，4 条 Luigi 结果仍 degraded。

这证明窄门控能在当前开发切片上避免已观察到的英文 Recall 回退，不证明门控规则可泛化。规则本身
是在看到这四个 development 案例后设计，8 个 holdout 仍密封且未物化。因此默认 runtime policy
继续保持不变；该门控只作为显式 opt-in development component，等待更宽的查询集或独立 holdout。

## 14. 未参与门控设计的 12 案例扩展集

为了避免只在 4 个门控设计案例上循环优化，commit
`7bb44b6bce647d1d6b080b4c6a6e2705fa85aa79` 在执行前冻结了其余 12 个 development 案例。
每个案例包含 raw test identifier、原冻结英文 query、含普通单词 `test` 的英文句子和中文描述。
raw／中文是门控正例，另外两类是旁路负例；baseline/selective 共形成每 arm 48 个逻辑观察，实际
以 12 份六查询 paired manifest 执行 72 次检索。完整结果见
[`query-normalization-gate-broad-v1/result-v1.yaml`](../benchmarks/retrieval/external-development-v3/query-normalization-gate-broad-v1/result-v1.yaml)。

门控合同本身为 24/24 正例启用、24/24 负例旁路，false activation 和 false bypass 都为 0；
12 条包含普通单词 `test` 的英文句子全部保持原文。但这是刻意构造的合同控制，不能解释为真实
Agent query 分布上的误触发率估计。

| 指标 | Baseline | Selective | 变化 |
|---|---:|---:|---:|
| Hit@1 | 27/48 | 27/48 | 0 |
| Hit@3 | 37/48 | 37/48 | 0 |
| Hit@8 / Path Recall@8 | 39/48 | 39/48 | 0 |
| MRR | 0.655208 | 0.662153 | +0.006944 |
| empty | 8 | 5 | -3 |
| non-empty miss | 1 | 4 | +3 |

raw identifier 的 12/12 Hit@8 保持不变，其中 HTTPie-4 和 Sanic-3 从 rank 3 提至 rank 2，
MRR 从 `0.833333` 小幅升至 `0.861111`，没有 rank 回退。两类英文旁路的结果逐项不变。

中文分支给出了关键负结果：Hit@8 仍是 `3/12`，MRR 仍是 `0.194444`。六条翻译被压缩成单个
`failure`，另一些只剩 `generate`、`override` 或 `beam`。三条原本 empty 的查询因此返回了候选，
却没有命中目标，使 non-empty miss 从 1 增至 4。也就是说，empty 下降并不是质量提升；四个设计
案例上的中文增益没有扩展到其余 12 个 development 案例。

这些 base case 未参与门控设计，但查询仍是在门控设计之后人工构造，并且 gold 属于已揭示的
development，因此不是独立 holdout。结论是：组合门控不接入 Agent runtime；test identifier
分支可继续作为独立候选，而 CJK 分支必须先测试“保留原 query，再按覆盖率追加映射词”的保守策略。
8 个 holdout 继续密封且未物化。

## 15. 保守 CJK 查询增强与低覆盖旁路

commit `e7eeca9501d60411cd1b68eeab4cd67d60bf8303` 在执行前冻结了
`normalize_retrieval_query_conservatively`、4 条处理查询和 12 条静态旁路控制。规则只读取 query：
当 CJK query 至少命中两个固定术语短语时，保留原 query 并追加确定性英文映射；不足两个时字节级
原样旁路。结构化 test identifier 继续复用上一轮分支，普通英文也保持原样。完整结果见
[`query-normalization-cjk-conservative-v1/result-v1.yaml`](../benchmarks/retrieval/external-development-v3/query-normalization-cjk-conservative-v1/result-v1.yaml)。

| 高覆盖中文指标 | Baseline | 替换式规范化 | 保留原文并追加 |
|---|---:|---:|---:|
| Hit@8 | 1/4 | 4/4 | 4/4 |
| Path Recall@8 | 1/8 = 0.125 | 7/8 = 0.875 | 7/8 = 0.875 |
| MRR | 0.0625 | 0.75 | 0.75 |
| empty / degraded | 1 / 1 | 0 / 1 | 0 / 1 |

追加式处理在四例上的 rank、相关路径数和完整返回路径都与替换式处理逐项相同：Cookiecutter、
Luigi、PySnooper、Tornado 的首个相关路径分别为 rank `2/1/1/2`。因此，保留中文原文在当前
SQLite FTS 切片上没有额外可测增益；它的价值是避免丢失原始意图，而不是把同分包装成提升。

其余 12 条扩展集中文 query 均只命中零或一个词表短语，保守函数为 `12/12` 精确旁路。因为处理
query 与已冻结 baseline 完全相同，本轮没有重复执行这 12 次检索，而是复用原证据：Hit@8 `3/12`、
MRR `0.194444`、empty 8、non-empty miss 1。这消除了上一轮把低覆盖中文压缩成 `failure`、
`override` 等泛词而制造额外错误候选的执行路径，但不是新的召回提升。

若把四条已执行的高覆盖 treatment 与十二条确定性旁路投影在一起，中文 Hit@8 为 `4/16→7/16`、
路径 Recall@8 为 `4/20→10/20`、MRR 为 `0.161458→0.333333`。这个数字是两组已冻结证据的
确定性组合，不是额外一次检索，更不是独立泛化成绩。四条高覆盖 query 正是用于设计词表的自适应
案例，Luigi 仍 degraded，PySnooper 仍缺 1/5 路径；因此保守分支继续作为实验候选，**不接入默认
Agent runtime，也不据此扩词表**。8 个 holdout 仍密封且未物化。
