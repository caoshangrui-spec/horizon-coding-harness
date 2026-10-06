# 完整 Checkout 多阶段 Pilot：youtube-dl-3 与 Luigi-1

更新：2026-10-06。本记录保留 v1 两阶段任务的失败与成功证据、v2 的 youtube-dl 三阶段任务，
并在 v3 增加独立的 Luigi 三阶段任务。两个案例都使用固定的干净历史 checkout，验证两个持久化
Worker 边界、跨 revision Code RAG/Run Memory、只替换最后未完成项的受限 replan、最终全量验收
和 Trace replay；仍使用冻结脚本模型，不是实际模型或 BugsInPy 官方跑分。youtube-dl 已有公开
禁网 Docker 证据，Luigi 当前只有本地可信执行，公开 Docker 结果必须等工作流实际完成后再记录。

## 1. 冻结输入与 v1 两阶段合同

- Suite：[`bugsinpy-multi-stage-pilot-v1.yaml`](../benchmarks/run_ab/bugsinpy-multi-stage-pilot-v1.yaml)
- Case：[`multi-stage.yaml`](../benchmarks/run_ab/full/youtube-dl-3-unescape-html/multi-stage.yaml)
- 来源：[BugsInPy](https://github.com/soarsmu/BugsInPy) / youtube-dl bug 3
- Buggy commit：[`f5469da`](https://github.com/ytdl-org/youtube-dl/commit/f5469da9e6e259c1690c7ef54f1da1c19f65036f)
- Fixed commit：[`95f3f7c`](https://github.com/ytdl-org/youtube-dl/commit/95f3f7c20a05e7ac490e768b8470b20538ef8581)
- Fixture revision：`fb97a849f99bbdd13922ac4a4a471c36b4b463b313493bf03479c464fed94b10`

初始 Plan v1：

1. `repair-unescape-html-production`：检索并修复 `youtube_dl/utils.py`，只以真实行为检查验收；
2. `add-unescape-html-regression`：依赖第一项，在 `test/test_utils.py` 加入 fixed commit 的回归断言。

两个 required check 在原始 buggy checkout 上都真实失败。第一项提交只运行生产行为检查；第二项
作为最后一项提交时重跑生产行为与回归源代码两项检查。Baseline 完成第一项后在第二项重复检索，
最终进入 `WAITING_FOR_USER`；Treatment 收到相同软阻断证据后，只替换未完成第二项并成功提交。

## 2. 首次负结果：换行合同不匹配

首次 Treatment 没有成功。历史 checkout 中 `test/test_utils.py` 使用 CRLF，而 manifest 的多行
精确替换模式使用 LF。Typed Gateway 返回
`ValueError: Exact replacement occurrence count did not match`，没有写入测试文件；随后生产行为仍
通过、回归源检查失败，Run 最终为 `FAILED`。这份负结果保留为：

- 失败 manifest digest：`035f2628f485277e78f91a4ac696bc5a826efb0cdffde625d477048bebd61c85`
- 失败 suite / case report：
  `d94f2be4b66bf64a3bd8ed96b97de3de41cf1948d2496c04219d84bda81f3775` /
  `765e81d524a59ec44bec48ddf5e8c0d26c50cc70e7ba634dd12629d2119d53f6`
- 失败 Treatment trace：`d549048ec18c794a3bbfe59182007bbec8d17acf3ebf5e01f5cb0c5f719d3250`
- 被拒编辑 evidence：`af3bafecb225ff72e9ecfeb1be0381bc36b5e8aec25e4f801c53502f09a18805`
- 回归检查失败 evidence：`13047accd8a29884c9add980c9805ed3bde98f78a788d922850038c73d51436f`

修正只发生在 benchmark manifest：把冻结 `old/new` 字节模式改成 CRLF，并在运行前确认旧模式
occurrence 恰好为 1。没有放宽 Gateway、模糊匹配或修改上游 fixture。

## 3. 重启前的成功 A/B 基线

修正后的 manifest digest 为
`9ad695eb6d64a2bc2dd3f9e42b85142d35c5e301f40641312f5d4c3748bdd941`，使用本机已有禁网镜像
`sha256:0687a6bc9716edc2a6ee0fbfb0f87e7ee358b262b67c9215de91bc9b2d38ba71`。

| 指标 | Baseline | Single replan |
|---|---:|---:|
| 最终状态 | `WAITING_FOR_USER` | `SUCCEEDED` |
| 已通过 WorkItem | production | production + regression |
| Plan version / replans | 1 / 0 | 2 / 1 |
| Model calls | 7 | 9 |
| Tool calls / steps | 8 / 8 | 12 / 12 |
| Event count | 84 | 114 |
| 合成模型成本（CNY） | 0.00336 | 0.00432 |
| Trace duration | 135.72 s | 183.71 s |
| Trace replay / source unchanged | 通过 / 是 | 通过 / 是 |

汇总 `success_delta=+1`、model/tool/step delta 为 `+2/+4/+4`、成本增量 CNY 0.00096；unknown
model/tool calls 与开放预留均为 0，`paid_model_called=false`、`network_called=false`。完整 suite
墙钟为 445.20 秒。

内容寻址证据：

- Suite report：`49deb8c6a65c92eef5d3646e0f4a3376e59f93dc8c188ceac82cbdc55c5c8a68`
- Case report：`665929bc86579c2b19ec0e50bb3a7bed314da7733362e3845f9fb104cfed612a`
- Baseline trace：`a26ec5eb18acd928cec3bdd49fcaceb77ec59708c08aa210fef1a822468be538`
- Treatment trace：`5fd1ed57b62883c8b4befe047984c1a210c7ab95cd39c4069c59869e34afd678`
- Production-only revision：`2485324969af85fb8dceb432da08ab477a8a274c248bc50dcea5227bbd1a2a34`
- Final two-file revision / manifest：
  `a99a7669de9261b6a67fad4aaa334b9ffc25f5c4595642e521afcfc220975019` /
  `2aebc525d57f437b40e570a4e726941e8f9b291fa1e6a6535a0cbcbb98c76963`
- Final validation evidence：`373cf5910a6f3062f3eb1ea9dac8d51453d34e4468a3e50b79c1533e335d525e`

## 4. 跨 revision 检索、Memory 与 replan

两次检索没有混用索引：

| 阶段 | Workspace revision | EvidencePack | rank 1 | 索引状态 |
|---|---|---|---|---|
| 修生产代码 | `fb97a849…` | `ef1c855b529fd8a4ec65b67ffd6932cba99bf7d69540a24e9a09b473a60f988f` | `youtube_dl/utils.py` | 870 indexed / 2 skipped / degraded |
| 补回归测试 | `24853249…` | `3436323a43f75d282ce8101fc65f0c3b7fea2655057f34a2edf853d226105483` | `test/test_utils.py` | 870 indexed / 2 skipped / degraded |

进入第二 WorkItem 的首个模型请求绑定 Memory ref
`aa44f68fbc20793366b1e350399fb13aea9579ddc9d5ec5b001972794eca31ff` 与 MandatoryFactLedger ref
`fd71e4604870c1073ff34b07006e1d86c91743fbc9e2d8b536f3cc4e774e670c`：ledger 明确列出
`repair-unescape-html-production` 已完成；Memory 中旧 revision 的首次检索为 stale，生产编辑和
验收 receipt 为 active。

Plan v1 → v2 的 replan event 固定：

- old / new plan hash：
  `be7b0d6cfc5f02e83b528e9f71269523b4ca2d373a188c87fb751a661d85e049` /
  `acc56c5e5451755dc30698c58f8ddc9f7bb99842d1984634f00a4ace37e7e294`；
- `preserved_work_item_ids=[repair-unescape-html-production]`；
- proposal hash：`dce7abea066db2c574c1229a2a3be988ba1e01961f5c6d6685a2815fdb215c0a`；
- replan 后首个请求绑定 Memory `b9d09c91aed3ec38267222af5ea1d2c0190621aaaae6ac693b073037760ceaac`，
  completed item 仍在 ledger 中；
- 测试编辑后 revision 变为 `a99a7669…`，下一请求的 Memory
  `010162cd60cbdf3878efa010f796ea4d7997e76f6fda46c46caccd6c1c33aef2` 正确保留 1 条 active
  新编辑并把此前 6 条 observation 标为 stale。

最终 `VALIDATION_RECORDED` 同时保存
`youtube-dl-unescape-html-behavior` 与 `youtube-dl-unescape-html-regression-source`，然后第二项
`WORK_ITEM_PASSED`、Run `SUCCEEDED`。这给出了跨阶段数据流证据，而不只是检查最终文件内容。

## 5. WorkItem 边界 Worker 重启

在相同 Task、Plan、actions 和 checks 上，两个 arm 都增加
`restart_after_model_calls: 3`。第三次模型调用提交并验收 production WorkItem 后，evaluator 要求
Run 必须处于以下持久安全边界，否则直接拒绝评测：

- 状态仍为 `RUNNING`，production 已在 `passed_items`；
- AgentSession 已指向依赖满足但尚未通过的 regression WorkItem；
- 当前没有开放 reservation 或 unknown 调用。

满足后，第一任 Worker 释放 Lease。评测重新构造 `SQLiteEventStore`、`HarnessService`、
`ArtifactStore`、`SnapshotManager`、`SQLiteCodeRetriever`、`CampaignBudgetLedger`、Tool Gateway、
Scripted Model adapter 和 `CodingAgentRunner`，再由第二任 Worker 取得 epoch 2 Lease。Docker 验收
后端视为 Worker 外部仍存活的 sandbox 服务，没有被伪装成可恢复的进程内状态。

### 5.1 首次重启负结果：默认 Lease 太短

第一次完整运行在 Baseline 第二 WorkItem 的冷缓存复核期间失败：重启后的 `acquire_lease` 沿用
默认 60 秒 TTL；872 文件在新 `ArtifactStore` 中必须重新校验已有 CAS，随后还要重建检索请求，
耗时超过 60 秒。epoch 2 Worker 的下一次持久写入被 fencing 正确拒绝为
`LeaseConflict: Worker lease is missing, expired or fenced`，没有绕过租约或伪造完成。

- Run：`run_a3b5adc9aa25436298074732f2fad090`
- partial Trace：`5ca26f5ae9f775d607cbc544aad79068a6bbf82f4404ab121d503c2248bcdd10`
- 失败点：64 events，production 已通过，regression 会话停在 iteration 5；
- 已结算用量：5 model calls / 6 tool calls，无 unknown/open reservation；
- workspace revision：`2485324969af85fb8dceb432da08ab477a8a274c248bc50dcea5227bbd1a2a34`。

修复没有延长全局预算或放宽 `check_worker`：只让重启 Worker 与初始 Worker 一样显式申请 600 秒
Lease，并加入测试核对 epoch 2 的 `expires_at - created_at == 600s`。失败状态目录继续保留。

### 5.2 600 秒 Lease 后的真实结果

重跑使用 manifest digest
`d3a5bad7eb1c93842678bb584f1e507154a86d6d9d996e85c017ba2bc781341e`，suite manifest digest 为
`e4afeee636e571010c65557757770b8c1b489227e3c3809314371e35527b7569`。镜像、fixture revision、
动作、检查和价格卡均未改变。

| 指标 | Baseline | Single replan |
|---|---:|---:|
| 最终状态 | `WAITING_FOR_USER` | `SUCCEEDED` |
| Worker restarts / final lease epoch | 1 / 2 | 1 / 2 |
| 已通过 WorkItem | production | production + regression |
| Plan version / replans | 1 / 0 | 2 / 1 |
| Model calls | 7 | 9 |
| Tool calls / steps | 8 / 8 | 12 / 12 |
| Event count | 86 | 116 |
| 合成模型成本（CNY） | 0.00336 | 0.00432 |
| Trace duration | 153.10 s | 204.22 s |
| Trace replay / source unchanged | 通过 / 是 | 通过 / 是 |

两个 arm 的 production 验收后均在 event 43 记录 `WORK_ITEM_PASSED`，event 46 释放 epoch 1
Lease，event 47 由 `worker-*-restart-1` 获取 epoch 2、600 秒 Lease。Treatment 最终 event 114
记录两个 checks 同时通过，event 115 标记 regression WorkItem 通过，随后 Run 成功。

内容寻址证据：

- Suite report：`d812ecb5a9dfe23a28cbf079820ec6ee4862a37d614e3c98a3a90e2abe583e02`
- Case report：`760cea79f8e306be372dda139db4d084fb46d58f1b902dfa008a7d9b4bf07356`
- Baseline trace：`0d50243ce79f6e24bba00da462bdc940ff7b4d02648beb5d1696d124f59c34b9`
- Treatment trace：`3c3494b9416acdd2bfdd45e0c86db50e7e94ac9d618dee1504342436fafbb058`
- 最终 validation evidence、revision 和 manifest 仍分别是
  `373cf5910a6f3062f3eb1ea9dac8d51453d34e4468a3e50b79c1533e335d525e`、
  `a99a7669de9261b6a67fad4aaa334b9ffc25f5c4595642e521afcfc220975019`、
  `2aebc525d57f437b40e570a4e726941e8f9b291fa1e6a6535a0cbcbb98c76963`。

汇总仍为 `success_delta=+1`、model/tool/step delta `+2/+4/+4`、成本增量 CNY 0.00096；
unknown model/tool calls 与开放预留均为 0，`paid_model_called=false`、`network_called=false`。

## 6. v2 三阶段、两次 Worker 重启

v2 没有覆盖 v1 文件或重写历史结果：

- Suite：[`bugsinpy-multi-stage-pilot-v2.yaml`](../benchmarks/run_ab/bugsinpy-multi-stage-pilot-v2.yaml)，
  digest `c57ae62f6a4bbed7a7ba22e4169822887564c41272f9e440ca0f1c2bd4d458d3`；
- Case：[`multi-stage-v2.yaml`](../benchmarks/run_ab/full/youtube-dl-3-unescape-html/multi-stage-v2.yaml)，
  digest `ae87c1d42c3a4464e6167ee72f885bac45997806a06f67c98cd9942404448da4`；
- v1 suite / case digest 仍分别为 `e4afeee636e571010c65557757770b8c1b489227e3c3809314371e35527b7569` /
  `d3a5bad7eb1c93842678bb584f1e507154a86d6d9d996e85c017ba2bc781341e`；
- 回归断言改用单行精确替换，同一 manifest 在 LF 与 CRLF 输入上都由回归测试证明 occurrence 恰好
  为 1；没有引入模糊匹配。

Plan v1 有三个严格依赖的 WorkItem：`production → regression → compatibility`。两个 arm 都在第
3、6 次模型调用后停在已持久化的 WorkItem 边界，释放旧 Lease，重新构造 Worker 所有持久适配器，
再由 epoch 2、epoch 3 Worker 继续。每个重启 Lease 都是 600 秒；旧 epoch 不能继续写入。

### 6.1 本地可信完整运行

本地使用严格 test-only Python 执行器实际运行三个验收命令；它复用生产 Harness 主链路，但不是
Docker 证据。结果如下：

| 指标 | Baseline | Single replan |
|---|---:|---:|
| 最终状态 | `WAITING_FOR_USER` | `SUCCEEDED` |
| Worker restarts / final lease epoch | 2 / 3 | 2 / 3 |
| 已通过 WorkItem | production + regression | production + regression + compatibility |
| Plan version / replans | 1 / 0 | 2 / 1 |
| Model calls | 10 | 12 |
| Tool calls / steps | 12 / 12 | 17 / 17 |
| Event count | 124 | 158 |
| 合成模型成本（CNY） | 0.00480 | 0.00576 |
| Trace duration | 270.42 s | 323.25 s |
| Trace replay / source unchanged | 通过 / 是 | 通过 / 是 |

汇总 `success_delta=+1`、model/tool/step delta 为 `+2/+5/+5`、成本增量 CNY 0.00096；unknown
model/tool calls 与开放预留均为 0，`paid_model_called=false`、`network_called=false`。

内容寻址证据：

- Suite report：`adbd34e860a171f42730a84baf886d403e14bb561966b00ae32138b4db9fedea`；
- Case report：`31c49fe98fc54c05c2876feea314064f6a8c082204c016dd9324f8358102bfc5`；
- Baseline / Treatment trace：
  `6cb1da3ecd715461c9ade9f7b955dc07f0c8539891369801e51e776f4717da6e` /
  `4dad1233acc58c0eaa416e692cc59673a411e59a67c739cccb07bcd76088ff56`；
- 最终 workspace revision / manifest：
  `d32ed3b4d6e7cc875578fe3fdcfa2c6d4597bba08e1ff11bf5f1ee828430fd5e` /
  `32d4ce0bf29ea08baf98eb6ac4e51d5f91a02b6caa9258b8c3993de7e353803a`；
- 最终三项 validation evidence：
  `7090babbe2956b41d83f394d16209ee7543841afaeba085bfc1084527243e0c9`。

### 6.2 跨两次重启的数据连续性

四次有效检索都绑定当时的 immutable workspace revision，且目标路径均为 rank 1：

| 阶段 | Workspace revision | EvidencePack | rank 1 |
|---|---|---|---|
| production | `fb97a849…` | `1de34743323676b1099704c3dc311f253d53d65722c36ad48d1d4ff0add77ffc` | `youtube_dl/utils.py` |
| regression | `24853249…` | `9546ec2faaedcac98e9a30dd2b3de0b7b73c0f1374fab195f1ade494f288b63e` | `test/test_utils.py` |
| compatibility 停滞前 | `d32ed3b4…` | `75f3fe9e56158b4f648d102a1e5cf83c69795cbcbe9871232d963f7bc45ab66d` | `youtube_dl/utils.py` |
| replan 后新查询 | `d32ed3b4…` | `a750942da766a570d834cb1c5d242f933f20a2a806b5a8362ece168ce9bd3c84` | `youtube_dl/utils.py` |

Treatment 在第二次重启后仍从权威事件重建 Run Memory。replan 后首个请求绑定 Memory
`d370213601aaaf6ed444fc1d4df84046cd9327b3483484899d4d34e0a57f59dc`，其中 5 条 active、4 条
stale；新查询后绑定 `7b06e9b6d5258511dc20553a63faf3ad3e0cba42341b51868b4a79398bfb311d`，为 6 条 active、
4 条 stale。旧 revision 片段没有被重新标成当前事实。

NoProgress 证据出现后只发生一次 `PLAN_REVISED`：old/new plan hash 分别为
`5e54d8cd7cebdc9455a8f1ff9c283bc72982313866dfc7adfa98fd687dc0dd16` /
`17ab7a42eee8e60a947a52c66ee49018dab6255f642ca9ee402049bc553bf317`，明确保存
`preserved_work_item_ids=[repair-unescape-html-production, add-unescape-html-regression]`。因此两个已
验收项未被重写，只把最后的 compatibility 项替换为有界闭环项。

### 6.3 公开禁网 Docker 证据

提交 `c369870d70330703f0adb795209bd66cc760f28b` 的
[公开工作流 #37442538827](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/37442538827)
在 `python:3.12-alpine`、`--network none` 的生产 Docker adapter 上通过：依赖裁剪 suite 5/5、
三项目完整 checkout suite 3/3、本 v2 三阶段 suite 1/1。三阶段 Baseline/Treatment 均记录 2 次
重启和 final epoch 3，初始失败 1/1、Treatment 恢复 1/1。上传证据包
`source-bound-docker-evidence` 的 digest 为
`sha256:cca5c0a2ac25f173c19de247d46631955b0154d621f812568a7f25ba58d684f3`；没有付费模型或模型网络调用。

## 7. v3 第二个独立任务：Luigi-1

v3 原样保留 v2 的 youtube-dl case 对象和 digest，再追加一个不同项目、不同缺陷形态的 Luigi
案例：

- Suite：[`bugsinpy-multi-stage-pilot-v3.yaml`](../benchmarks/run_ab/bugsinpy-multi-stage-pilot-v3.yaml)，
  digest `4f1d9c5596b5acb06986f082a42fa328f25e8954d44f56a9d02ddb50149314dd`；
- Case：[`multi-stage-v3.yaml`](../benchmarks/run_ab/full/luigi-1-metrics-handler/multi-stage-v3.yaml)，
  digest `fab149f13284d5428702d3f6ca72e618fe1004bc0dab7ae2409fc6161dd2e6c3`；
- 来源：[BugsInPy](https://github.com/soarsmu/BugsInPy) / Luigi bug 1；buggy commit
  [`1164eb6`](https://github.com/spotify/luigi/commit/1164eb6b85b8a70f596dbb99452bec513e72c12e)，
  fixed commit [`aec5dc2`](https://github.com/spotify/luigi/commit/aec5dc2ed8db53fc282a0bd24aabe59031b6d1ba)；
- v2 suite 与 youtube-dl case digest 仍分别为
  `c57ae62f6a4bbed7a7ba22e4169822887564c41272f9e440ca0f1c2bd4d458d3` /
  `ae87c1d42c3a4464e6167ee72f885bac45997806a06f67c98cd9942404448da4`。

上游修复被拆成严格依赖的 `collector binding → handler ownership → regression assertion` 三项。
前两项分别修改同一个生产文件，但各自有独立验收：第一项检查 collector 绑定和 payload 生成，
第二项用标准库 AST 提取并真实执行 `MetricsHandler`，确认配置调用落在 collector 而非生成结果上。
第三项精确更新上游回归断言。三个 required check 在原始 checkout 上都失败；最终检查不依赖安装
历史 Luigi/Tornado 依赖栈。

### 7.1 本地可信完整运行

本地执行复用生产 Harness、SQLite、Artifact、RAG、预算、Trace 和 Worker 重启路径，仅把最终
验收换成严格 test-only Python 执行器；因此这是本地可信运行，不是 Docker 证据。

| 指标 | Baseline | Single replan |
|---|---:|---:|
| Run | `run_92db7d9585ba4eb4ac5888e4e2c3ff71` | `run_18c8397b0dda4b1f8712fae7f0e7615d` |
| 最终状态 | `WAITING_FOR_USER` | `SUCCEEDED` |
| Worker restarts / final lease epoch | 2 / 3 | 2 / 3 |
| 已通过 WorkItem | collector + handler | collector + handler + regression |
| Plan version / replans | 1 / 0 | 2 / 1 |
| Model calls | 10 | 13 |
| Tool calls / steps | 12 / 12 | 18 / 18 |
| Event count | 124 | 167 |
| 合成模型成本（CNY） | 0.00480 | 0.00624 |
| Trace replay / source unchanged | 通过 / 是 | 通过 / 是 |

两条 arm 均无 unknown/open 调用。Baseline 在完成两个生产项后，因最后一项重复相同检索而安全
进入等待；Treatment 只替换最后的 regression WorkItem，保留前两项及其验收事实。Treatment 的
Trace ref 为 `2c1a989d3d1ba54e31a8003db66391597d8ae3df130fa313ae8dd584f7f7092d`，Baseline 为
`70750e108c0fe2172ba0f619ee07b9268ebfae9636afaf51a064a9bb8efabc00`；最终 validation evidence
为 `df422c8f30732b27ae6dc28a429f6476fe52f75ad43d22f5cc8fbb6f6c3ac956`，Treatment workspace
revision 为 `80ecd9871aa66e188367d6879211f5980d53e61111bd72cffd167286a35b4ab4`。

### 7.2 跨 revision 检索与当前证据边界

collector 阶段与 handler 阶段都把 `luigi/server.py` 排为 rank 1，且生产编辑后内容 hash 从
`622822…` 变为 `881536…`；regression 阶段把 `test/server_test.py` 排为 rank 1，replan 后的新
查询仍命中同一目标，但绑定新的 revision/content hash（`880c92…` → `7936d2…`）。这证明该次
运行没有复用旧 revision 片段；它不证明词法检索对任意任务的语义质量。

专用工作流已经改为在 `python:3.12-alpine`、禁网生产 Docker adapter 中顺序执行 youtube-dl 和
Luigi 两例，并上传统一内容寻址状态。但在对应公开 run 成功前，项目计数仍保持
`source_bound_run_ab_multi_stage_docker_verified_count=1`，不能把 v3 写成 Docker 2/2。

## 8. 结论边界与下一步

可以声称：两个完整上游 checkout 上的三阶段任务已在本地可信路径实际通过依赖调度、隔离会话、
跨 revision 检索、Run Memory、两个 WorkItem 边界 Worker 更替、完成项保留 replan、最终三项
全量检查和离线重放；其中 youtube-dl 另有公开禁网 Docker 证据。不能声称：

- 冻结脚本证明真实模型能自主分解、查询、修复或选择 replan；
- 源码断言等价于运行 youtube-dl 原 Python 版本的完整 pytest；
- 协作式释放/重取 Lease 等同于 OS 在任意指令处崩溃，或证明所有任意副作用都能恢复；
- 两个作者选择任务足以给出泛化成功率，或 Luigi 已有公开 Docker 通过证据。

后续增量已为悬空 `run_check` 加入 tool-call-ID 标签容器、显式丢弃合同，以及自然退出、清理前
停止容器的精确 success/error 结果恢复。控制器可查询状态，且只有显式授权才终止仍运行的精确
attempt；missing 仍需人工确认。workspace 漂移继续拒绝，也不重派原模型调用。真实子进程已
覆盖运行中退出；自然退出清理前案例已加入合同测试，但本批 Docker daemon 未运行，尚不能记为
真实通过。剩余窗口是容器创建前启动回执、missing 证明和信号/超时结果；当前不新增通用队列、
向量库、多 Agent 或第二次 replan。
