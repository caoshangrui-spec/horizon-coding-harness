# Revision-aware 词法 Code RAG：实现与证据合同

更新：2026-10-03。当前实现是本地、无模型费用的首个 Code RAG 垂直切片：SQLite FTS5
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
PascalCase，并对全 ASCII 标识符片段生成一个组合词。例如 `unescape HTML` 会增加
`unescapehtml`，`registerSocksProtocols` 会增加 `register/socks/protocols`。所有词项再生成只含
转义 phrase 的 FTS 表达式，用户文本不能直接注入 FTS 操作符。

组合词命中先进入至多 256 个 chunk 的候选集；若片段内有规范化后同名的 `def`、`async def`
或 `class` 声明，定义片段排在调用/导入片段前，其余仍按 SQLite BM25、path、start line 排序。
若组合词没有命中，普通词项候选也应用同一有界定义启发式，因此 camelCase 查询可以定位
snake_case 定义。该规则不是 AST、符号表或调用图；BM25 分数也不暴露为跨版本稳定指标。

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
`fts5_unavailable`，不会假装与 BM25 等价。

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

新增 `horizon eval retrieval`，读取严格 manifest，对 scoped immutable snapshot 逐案例执行当前
Retriever，并生成内容寻址 `RetrievalEvalReport`。报告给出 Hit@case-K、micro path recall、MRR、
forbidden-path leakage、empty 和 degraded 计数，并显式记录零模型/零网络。首个 5 案例内部诊断
结果是 Hit=5/5、micro recall=1.0、MRR=0.9、leakage/empty/degraded 均为 0；promotion adapter
为 rank 2，其余 rank 1。它是实现者编写的本仓小样例，不是 benchmark 或泛化证据。协议、公式、
命令和限制见[词法检索离线诊断](retrieval-evaluation.md)。

## 8. 已执行验证

离线集成测试覆盖：

- FTS5 多词召回、rank、路径/行号/content hash 和相同 revision 缓存复用；
- 自然语言到 camelCase、camelCase 到 snake_case 的双向词项扩展，并在高频文档/调用片段前
  优先同名定义；
- allowed/denied path scope，不召回未授权测试文件或 `.env`；
- dirty workspace 产生新 index key，旧 manifest 仍只能召回旧内容；
- 非 UTF-8 文件使状态变为 degraded，但不隐藏合法命中；
- FTS5 不可用时显式 lexical-scan 降级；
- manifest/revision 不一致与派生索引篡改拒绝；
- 缓存最多保留固定数量，已裁剪旧索引可由 Artifact 重建；
- Typed Gateway 输出内容寻址 EvidencePack，workspace revision 前后不变；
- scoped evaluation snapshot 不读取 allowed scope 外的大缓存文件；
- 固定 manifest 的 Hit/Recall/MRR/leakage/empty/degraded 汇总与 report Artifact；
- Scripted Agent 实际调用 `retrieve_code → replace_text → submit → protected validation`；
- 未知 `retrieve_code` intent 经显式 read-only retry 后恢复，不重放原模型调用。

这些验证没有网络调用或模型费用。真实 SiliconFlow fixture 是加入该工具之前的历史证据，不能
据此声称真实模型已经正确使用 RAG；固定内部诊断已测路径级 Recall/MRR。新增的
`youtube-dl-identifier-variants-v1` 在 764 个 `youtube_dl/**` 文件上将 `unescape HTML` 的
camelCase 定义和 `registerSocksProtocols` 的 snake_case 定义都排在 rank 1。最初仅加词项时
`unescape HTML` 未进前 5，随后只优先精确 token 仍因大量 import/call 命中失败；这两个负结果
均保留，最终才加入有界定义优先。它是作者选定的固定诊断，不是盲测。补充的
[双项目完整 checkout suite](full-checkout-pilot.md)在 82/872 文件历史仓库中分别索引 73/870、
跳过 9/2，两个目标生产文件均 rank 1，但查询由作者冻结、Agent 动作由脚本给定，也没有无 RAG
对照。因此仍没有真实模型生成查询、外部盲测、Agent 成功率或相对增益证据。

## 9. 已知限制与下一步

- 当前只有 camelCase/snake_case 子词与 `def/class` 文本启发式，没有 AST、符号表、import
  解析或调用图；超过 256 个组合词候选时，定义仍可能被截断。中文长串分词能力有限。
- 当前索引 path + 文本 chunk，不索引 Git history、互联网、Memory 或 benchmark gold。
- BM25 rank 依赖 SQLite/分词配置，不作为跨环境科学比较指标。
- Evidence snippet 进入 Agent transcript/Trace Artifact；生产级敏感信息分类与脱敏仍未完成，
  因此 TaskSpec path scope 必须继续最小化。
- 下一步应扩充 dirty-revision、同名符号歧义和更多外部小仓盲测，再评估真正的 symbol-aware 索引；
  根据内部 5 案例与两个完整 checkout 保留的降级结果决定是否加入 embedding，不能因两次 rank 1
  就声称 RAG 效果完成。
