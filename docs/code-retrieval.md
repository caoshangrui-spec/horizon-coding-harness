# Revision-aware 词法 Code RAG：实现与证据合同

更新：2026-10-29。当前实现是本地、无模型费用的首个 Code RAG 垂直切片：SQLite FTS5
召回 + immutable snapshot 复核 + Typed Tool 输出。它不是向量检索、语义搜索或完整代码图。

## 1. 为什么现在做这一层

原 `search_repo` 只支持精确子串，适合模型已经知道标识符时定位；长程任务还需要在不知道
完整字符串时，用多个词找到有限、可追溯的代码片段。首版选择词法检索，因为它：

- 不调用 embedding/LLM，不消耗 SiliconFlow 预算；
- SQLite FTS5 已随当前运行时提供，不增加服务和依赖；
- 排序、版本和来源可以离线重放；
- 能先用真实诊断任务验证召回缺口，再决定是否引入向量库。

## 2. 数据流与权威关系

```text
当前 workspace
  → SnapshotManager.capture
  → immutable FileSnapshot manifest + file Artifacts
  → TaskSpec allowed/denied scope
  → scope_hash + revision-bound index_key
  → SQLite FTS5 derived cache
  → sanitized lexical query / identifier variants / definition-first bounded rank
  → 对每个命中重新读取 immutable file Artifact 并校验 path/range/hash/content
  → EvidencePack
  → retrieve_code ToolOutcome Artifact + Run tool receipt
```

FTS 数据库只是可删除、可重建的派生缓存，绝不是证据权威。每个返回 chunk 都必须重新通过
manifest 中的文件 Artifact 验证；缓存被修改、返回越权路径、行号越界、content/hash 不一致时
抛出 `IntegrityError`，不会把索引内容交给模型。

## 3. Revision 与权限绑定

索引身份为：

```text
scope_hash = sha256(allowed_paths, denied_paths, index_algorithm_version, chunker limits)
index_key  = sha256(source_manifest_ref, workspace_revision, scope_hash)
```

因此即使 Git HEAD 不变，只要 staging 文件内容变化，新的 workspace revision 就生成独立索引。
相同 revision 在不同路径权限下也不会共享结果。Gateway 每次调用都先生成当前 manifest，检索
只读该 manifest 的 Artifact；返回前再次捕获 workspace，任何并发漂移都会拒绝该次结果。

Snapshot 固定排除 `.git`、`.horizon`、`.venv`、`__pycache__`、`.pytest_cache` 及 TaskSpec
denied paths；Retriever 再应用 allowed/denied 交集。离线评测进一步只捕获 manifest allowed
scope，范围外缓存不进入快照预算。EvidencePack 只可能包含当前 WorkItem 已授权范围内的相对
POSIX 路径。

## 4. 索引和查询边界

当前固定边界：

| 项目 | 上限 |
|---|---:|
| 单 revision 索引文件 | 2,000 |
| 单 revision 输入字节 | 16 MiB |
| 单文件 | 512 KiB |
| 单行 | 24,000 字符 |
| chunk | 40 行，重叠 5 行 |
| 返回 chunk | 1～8，默认 6 |
| 返回 snippet | 每 chunk 1,200 字符 |
| 查询词 | 最多 20 个，每个最多 80 字符 |
| 组合标识符候选 | 最多 256 个 chunk |
| 本地缓存 revision | 最近 16 个 index key |

非 UTF-8、超大文件/行或总预算后的文件不进入索引，并把 EvidencePack 标为 `degraded`，而不是
静默当作完整搜索。超过 16 个缓存 revision 后删除最旧派生行；immutable manifest/file
Artifacts 不删除，所以旧 revision 再次查询时可确定性重建。

查询先用 Unicode word 规则提取并去重词项；保留原词的同时拆分 snake_case、camelCase 和
PascalCase。每个包含多个片段的原始 token 会单独生成标识符候选，整句的全 ASCII 片段也保留
一个组合候选。例如 `orders renderInvoice` 同时保留 `renderinvoice` 和
`ordersrenderinvoice`，不会再把模块语境误当成符号本身；`unescape HTML` 仍会增加
`unescapehtml`。所有词项再生成只含转义 phrase 的 FTS 表达式，用户文本不能直接注入 FTS
操作符。

FTS5 只负责在精确 `index_key` 中匹配候选；最终至多保留 256 个 chunk。排序先让规范化同名的
`def`、`async def` 或 `class` 定义优先，只在这些精确标识符定义之间比较模块路径词；普通查询
随后使用 FTS5 兼容的 BM25，再按 path、start line 稳定破同分。BM25 的文档数、文档频率、平均
长度和每行词频都从当前 immutable `index_key` 的 corpus 重新计算。普通自然语言查询不单独使用
路径加权，避免 `tool_recovery.py` 一类文件名偶然压过正文证据。索引算法版本为 8，不会复用旧
派生缓存。

v7 及更早版本曾用 `bm25(code_chunks)` 排序，但 FTS5 的 corpus statistics 会覆盖同一虚拟表中的
全部 cache key；即使 `WHERE index_key=?` 限制返回行，相同 revision/query 仍可能随其他缓存历史
改变排名。v8 因此不再使用 FTS 虚拟表的全库 BM25 分数，而是按同一公式从 exact-key corpus
本地复算。该规则仍不是 AST、符号表、调用图或语义相关性证明。

## 5. EvidencePack 合同

EvidencePack 保存：

- 原查询与规范词项；
- workspace revision、source manifest ref、scope hash、index key；
- backend：`sqlite_fts5` 或 `lexical_scan`；
- status：`ok / empty / degraded`；
- 明确 degradation reasons、已索引/跳过文件数；
- 最多 8 个 chunk 的 rank、路径、起止行、完整 chunk hash、有界 snippet 和 truncated 标记。

状态语义不混淆：

| 状态 | 含义 |
|---|---|
| `ok` | 完整索引范围内至少一个命中，且没有跳过项 |
| `empty` | 完整索引范围内无命中，不能解释成“仓库不存在相关实现”之外的更强结论 |
| `degraded` | FTS 不可用或有文件因边界被跳过；可有命中，也可无命中 |

若运行时没有 FTS5，Retriever 使用有界 lexical scan；输出强制为 `degraded` 并记录
`fts5_unavailable`；它使用原有有界词频排序，不会假装与 FTS 候选或 BM25 等价。

## 6. Agent Tool 集成

WorkItem 可显式授权：

```yaml
allowed_tools:
  - search_repo
  - read_file
  - retrieve_code
  - replace_text
  - apply_patch
  - run_check
```

模型调用示例：

```json
{"query":"parser empty value","max_chunks":4}
```

模型只收到 EvidencePack JSON，不能传 SQL、索引路径或 workspace 路径。`retrieve_code` 与
`search_repo/read_file` 一样被视为 workspace 只读工具；若 intent 后、receipt 前退出，可在
workspace revision 未变且用户显式选择 `--retry-readonly` 时保守取消旧尝试并重派。派生索引
写入不等于代码工作区副作用。

## 7. 固定离线诊断

`horizon eval retrieval` 读取严格 manifest，对 scoped immutable snapshot 逐案例执行当前
Retriever，并生成内容寻址 `RetrievalEvalReport`。报告给出 Hit@case-K、micro path recall、MRR、
forbidden-path leakage、empty 和 degraded 计数，并显式记录零模型/零网络。首个 5 案例内部诊断
在 v6 上仍是 Hit=5/5、micro recall=1.0、MRR=0.9、leakage/empty/degraded 均为 0；promotion
adapter 为 rank 2，其余 rank 1。新增 2 案例同名符号诊断在 v5 基线上 Hit@1=0/2、leakage=2，
v6 为 Hit@1=2/2、MRR=1.0、leakage=0。它们都是实现者编写的小样例，不是 benchmark 或泛化
证据。协议、公式、命令、负结果和限制见[词法检索离线诊断](retrieval-evaluation.md)。

## 8. 已执行验证

离线集成测试覆盖：

- FTS5 多词召回、rank、路径/行号/content hash 和相同 revision 缓存复用；
- 多 chunk 结果先覆盖不同文件，再按原排序补同文件 chunk；FTS5 与 scan fallback 共用规则；
- 自然语言到 camelCase、camelCase 到 snake_case 的双向词项扩展，并在高频文档/调用片段前
  优先同名定义；
- 两个模块定义同名函数且交叉说明文本误导 BM25 时，模块限定词只在定义候选之间消歧；FTS5
  与 scan fallback 结果一致；
- 两条 HTTPie 真实模型查询形态下，目标定义即使面对重复 prose 仍保留在 top-2，FTS5 与 scan fallback 都通过；
- 另一 cache key 写入大量重叠词项后，原 revision/query 的完整排名逐字段不变；
- 单一 index 中本地复算的每个 BM25 分数与 SQLite FTS5 内置分数逐项近似相等；
- allowed/denied path scope，不召回未授权测试文件或 `.env`；
- dirty workspace 产生新 index key；同名目标移动后新 revision 排到新路径，旧 manifest 仍能
  逐对象重放原排序；
- 非 UTF-8 文件使状态变为 degraded，但不隐藏合法命中；
- FTS5 不可用时显式 lexical-scan 降级；
- manifest/revision 不一致与派生索引篡改拒绝；
- 缓存最多保留固定数量，已裁剪旧索引可由 Artifact 重建；
- Typed Gateway 输出内容寻址 EvidencePack，workspace revision 前后不变；
- scoped evaluation snapshot 不读取 allowed scope 外的大缓存文件；
- 固定 manifest 的 Hit/Recall/MRR/leakage/empty/degraded 汇总与 report Artifact；
- 16 个外部 development 案例按固定 URL/commit/package 和内容树 digest 从公开 Git 仓库重新
  物化，拒绝 holdout、路径/link 与内容漂移，再逐案例核对冻结排名和聚合指标；
- Scripted Agent 实际调用 `retrieve_code → replace_text → submit → protected validation`；
- 未知 `retrieve_code` intent 经显式 read-only retry 后恢复，不重放原模型调用。

这些验证没有网络调用或模型费用。真实 SiliconFlow fixture 是加入该工具之前的历史证据，不能
据此声称真实模型已经正确使用 RAG；固定内部诊断已测路径级 Recall/MRR。新增的
`youtube-dl-identifier-variants-v1` 在 764 个 `youtube_dl/**` 文件上将 `unescape HTML` 的
camelCase 定义和 `registerSocksProtocols` 的 snake_case 定义都排在 rank 1。最初仅加词项时
`unescape HTML` 未进前 5，随后只优先精确 token 仍因大量 import/call 命中失败；这两个负结果
均保留，最终才加入有界定义优先。它是作者选定的固定诊断，不是盲测。补充的
[三项目完整 checkout suite](full-checkout-pilot.md)在 82/872/382 文件历史仓库中分别索引
73/870/327、跳过 9/2/55，三个目标生产文件均 rank 1，但查询由作者冻结、Agent 动作由脚本给定，
也没有无 RAG
对照。另有三个未参与规则开发的外部任务形成顺序冻结盲测：v6 为 Hit@1 0/3、top-5 production
path recall 3/4；不同文件优先的 v7 为 Hit@1 0/3、recall 4/4。v7 已使用基线结果，不是独立
holdout。随后独立冻结的 Luigi/Sanic/Tornado 三案例在 v7 上均为 rank 1；其中两个因跳过
二进制/压缩资源保持 degraded。它仍不代表模型会生成同样查询或完成修改验证。

## 9. 已知限制与下一步

- 当前只有 camelCase/snake_case 子词与 `def/class` 文本启发式，没有 AST、符号表、import
  解析或调用图；超过 256 个组合词候选时，定义仍可能被截断。中文长串分词能力有限。
- 四个已知困难 development 案例的冻结扰动中，纯中文描述仅 Hit@8 1/4、path Recall 1/8；
  唯一命中仍依赖查询中的 `HTTP/PUT` 拉丁词项，因此当前没有跨语言语义召回能力。
- 同名定义消歧依赖查询中显式出现的模块/路径词；缺少限定词时仍按 exact-key BM25 和稳定 path
  顺序决定，也不会
  生成完整的“候选符号集合”或证明首项语义正确。
- 当前索引 path + 文本 chunk，不索引 Git history、互联网、Memory 或 benchmark gold。
- exact-key BM25 已消除其他 cache key 的语料统计漂移，但仍依赖分词、chunk 和规则版本，
  不作为跨版本科学比较指标。
- Evidence snippet 进入 Agent transcript/Trace Artifact；生产级敏感信息分类与脱敏仍未完成，
  因此 TaskSpec path scope 必须继续最小化。
- 第二批独立 holdout 没有复现 rank-1 缺口，当前不引入 symbol-aware/embedding/vector 索引。
  新增 16 个外部 development 案例后，Hit@1 为 11/16、MRR 为 0.817708，并保留一个多文件
  Recall@8=0.6 的失败案例；因此下一步先在 development 上诊断排序/多文件覆盖，再按预先冻结的
  8 个 holdout 做一次确认。是否引入 symbol-aware/embedding/vector 索引仍由冻结对照结果决定，
  不能仅凭功能完整感重开架构。现有结果也不能单独支持“RAG 效果完成”的结论。
