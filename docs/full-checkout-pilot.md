# 完整上游 Checkout Suite：BugsInPy 二十二案例、九项目

更新：2026-10-29。v11 原样保留已由公开禁网 Docker 统一通过的 v10 十九个完整 checkout，再加入
Sanic bug 3、TheFuck bug 5 与 Luigi bug 7。当前共二十二个案例、九个不同项目、6,062 个
tracked 文件；同一项目的不同 bug 分别计为不同案例，不冒充二十二个独立项目。三个新增案例已通过
本地完整 Harness 与公开二十二案例统一门。v7 首次公开 run 的 Cookiecutter 符号链接
拒绝与替换版 12/12 成功均继续保留。每个案例先执行
初始失败门，再运行 Baseline 与单次 replan Treatment。执行使用冻结脚本模型，验证的是 Harness、
Code RAG、预算、可恢复等待和 Trace 合同，不是模型能力跑分。

## 1. 来源门与案例

- v1：[`bugsinpy-full-checkout-pilot-v1.yaml`](../benchmarks/run_ab/bugsinpy-full-checkout-pilot-v1.yaml)，
  tqdm 与 youtube-dl 两例，digest
  `e6856e2e974cd855b535c9bae46bdd2795c3e5cb0bbdafcbd481ef54d649d69e`。
- v2：[`bugsinpy-full-checkout-pilot-v2.yaml`](../benchmarks/run_ab/bugsinpy-full-checkout-pilot-v2.yaml)，
  原样保留 v1 两个 case，再追加 Luigi，digest
  `be359684b9e3adfd676ed06c457197f7aff42bdcd1dbb3913321cebf5ef857b4`。
- v3：[`bugsinpy-full-checkout-pilot-v3.yaml`](../benchmarks/run_ab/bugsinpy-full-checkout-pilot-v3.yaml)，
  原样保留 v2 三个 case，再追加 FastAPI 与 Tornado，digest
  `d878964e246f8fabb6cf162f657791e959ae6c529d61eb1143cc98ea7d4edd8a`。
- v4：[`bugsinpy-full-checkout-pilot-v4.yaml`](../benchmarks/run_ab/bugsinpy-full-checkout-pilot-v4.yaml)，
  原样保留 v3 五个 case，再追加 HTTPie，digest
  `17d12ba657bde272251783d3364b4f809691fa71a5ae55c8d6dce98947b1d99c`。
- v5：[`bugsinpy-full-checkout-pilot-v5.yaml`](../benchmarks/run_ab/bugsinpy-full-checkout-pilot-v5.yaml)，
  原样保留 v4 六个 case，再追加 TheFuck 与 Sanic，digest
  `2dca6b3bcffcb88456ae0f05e833f11fa78a3ee1e0ca6dd081c5d0087c7ab12b`。
- v6：[`bugsinpy-full-checkout-pilot-v6.yaml`](../benchmarks/run_ab/bugsinpy-full-checkout-pilot-v6.yaml)，
  原样保留 v5 八个 case，再追加 PySnooper，digest
  `52afa08518568e0c4e2ab19df6bf4cd267dcf260804152888b301e16f2940428`。
- v7：[`bugsinpy-full-checkout-pilot-v7.yaml`](../benchmarks/run_ab/bugsinpy-full-checkout-pilot-v7.yaml)，
  原样保留 v6 九个 case，再追加 Sanic-2、HTTPie-4 与 Luigi-3，digest
  `825d359204ddf66081c1faf8d6f3b60d4869ae3e2a82b16e6f151039a7dc2047`。
- v8：[`bugsinpy-full-checkout-pilot-v8.yaml`](../benchmarks/run_ab/bugsinpy-full-checkout-pilot-v8.yaml)，
  原样保留 v7 十二个 case，再追加 Sanic-5、TheFuck-2 与 Luigi-2，digest
  `1985508c6a8c807d4231102d78fdfb34d599d8a81de04954b245614e6271a229`。
- v9：[`bugsinpy-full-checkout-pilot-v9.yaml`](../benchmarks/run_ab/bugsinpy-full-checkout-pilot-v9.yaml)，
  原样保留 v8 十五个 case，再追加 HTTPie-5 与 Tornado-2，digest
  `b35d10759f7104f3db57ed773d537fe323bc19a47a465cfb1faa94d49aa94611`。
- v10：[`bugsinpy-full-checkout-pilot-v10.yaml`](../benchmarks/run_ab/bugsinpy-full-checkout-pilot-v10.yaml)，
  原样保留 v9 十七个 case，再追加 HTTPie-1 与 Tornado-4，digest
  `328007b101620c79ff79faba16da7b7ed7398c29f5543c3015303c05a3d9168f`。
- v11：[`bugsinpy-full-checkout-pilot-v11.yaml`](../benchmarks/run_ab/bugsinpy-full-checkout-pilot-v11.yaml)，
  原样保留 v10 十九个 case，再追加 Sanic-3、TheFuck-5 与 Luigi-7，digest
  `4056bd9cf49bf3373685acd0fd0732ea64f24eee4a39e88640d1728dbcf3fc98`。

| Case | Buggy → fixed | 完整 checkout | 保护验收 |
|---|---|---:|---|
| [`tqdm-1-tenumerate-start-full`](../benchmarks/run_ab/full/tqdm-1-tenumerate-start/manifest.yaml) | [`8cc777f`](https://github.com/tqdm/tqdm/commit/8cc777fe8401a05d07f2c97e65d15e4460feab88) → [`c0dcf39`](https://github.com/tqdm/tqdm/commit/c0dcf39b046d1b4ff6de14ac99ad9a1b10487512) | 82 files | `tenumerate(..., start=42)` 与 `enumerate(..., 42)` 一致 |
| [`youtube-dl-3-unescape-html-full`](../benchmarks/run_ab/full/youtube-dl-3-unescape-html/manifest.yaml) | [`f5469da`](https://github.com/ytdl-org/youtube-dl/commit/f5469da9e6e259c1690c7ef54f1da1c19f65036f) → [`95f3f7c`](https://github.com/ytdl-org/youtube-dl/commit/95f3f7c20a05e7ac490e768b8470b20538ef8581) | 872 files | `unescapeHTML('&a&quot;') == '&a"'` |
| [`luigi-1-metrics-handler-full`](../benchmarks/run_ab/full/luigi-1-metrics-handler/manifest.yaml) | [`1164eb6`](https://github.com/spotify/luigi/commit/1164eb6b85b8a70f596dbb99452bec513e72c12e) → [`aec5dc2`](https://github.com/spotify/luigi/commit/aec5dc2ed8db53fc282a0bd24aabe59031b6d1ba) | 382 files | payload 由 collector 生成，HTTP handler 也必须由同一 collector 配置 |
| [`fastapi-5-nested-field-clone-full`](../benchmarks/run_ab/full/fastapi-5-nested-field-clone/manifest.yaml) | [`7cea84b`](https://github.com/fastapi/fastapi/commit/7cea84b74ca3106a7f861b774e9d215e5228728f) → [`75a07f2`](https://github.com/fastapi/fastapi/commit/75a07f24bf01a31225ee687f3e2b3fc1981b67ab) | 629 files | cloned response model 的嵌套字段不能继续别名指向原字段对象 |
| [`tornado-1-websocket-nodelay-full`](../benchmarks/run_ab/full/tornado-1-websocket-nodelay/manifest.yaml) | [`6a5a0bf`](https://github.com/tornadoweb/tornado/commit/6a5a0bfa370b6c0d3dbbf9589a560a98202d2baa) → [`4677c54`](https://github.com/tornadoweb/tornado/commit/4677c54cc18bbfbdf0f4dadf11610fab6203fd63) | 301 files | handler → protocol → stream 的 nodelay 委托及抽象合同 |
| [`httpie-2-max-redirects-full`](../benchmarks/run_ab/full/httpie-2-max-redirects/manifest.yaml) | [`356e043`](https://github.com/httpie/cli/commit/356e0436510fee70b4071fac58be81c0a0a7db59) → [`e18b609`](https://github.com/httpie/cli/commit/e18b609ef7d867d6efa0efe42c832be5e0d09338) | 70 files | CLI `max_redirects` 必须写入实际发请求的 Session |
| [`thefuck-1-pip-unknown-command-full`](../benchmarks/run_ab/full/thefuck-1-pip-unknown-command/manifest.yaml) | [`2ced7a7`](https://github.com/nvbn/thefuck/commit/2ced7a7f33ae0bec3ffc7a43ce95330bdf6cfcb9) → [`444908c`](https://github.com/nvbn/thefuck/commit/444908ce1c17767ef4aaf9e0b4950497914f7f63) | 401 files | 带 `+` 的 pip 错误子命令也能被完整提取并替换 |
| [`sanic-1-blueprint-middleware-order-full`](../benchmarks/run_ab/full/sanic-1-blueprint-middleware-order/manifest.yaml) | [`e7001b0`](https://github.com/sanic-org/sanic/commit/e7001b00747b659f7042b0534802b936ee8a53e0) → [`4497312`](https://github.com/sanic-org/sanic/commit/44973125c15304b4262c51c78b5a86bd1daafa86) | 189 files | named response middleware 必须按注册逆序执行 |
| [`pysnooper-2-custom-repr-full`](../benchmarks/run_ab/full/pysnooper-2-custom-repr/manifest.yaml) | [`e21a311`](https://github.com/cool-RR/PySnooper/commit/e21a31162f4c54be693d8ca8260e42393b39abd3) → [`814abc3`](https://github.com/cool-RR/PySnooper/commit/814abc34a098c1b98cb327105ac396f985d2413e) | 25 files | 单条 custom repr 规则同时作用于局部变量和返回值 |
| [`sanic-2-asyncio-start-serving-full`](../benchmarks/run_ab/full/sanic-2-asyncio-start-serving/manifest.yaml) | [`ba9b432`](https://github.com/sanic-org/sanic/commit/ba9b432993019b0af0c4827a5ed42aaa091bd17d) → [`801595e`](https://github.com/sanic-org/sanic/commit/801595e24acdf8050b8d3ffa512d424147848d32) | 188 files | `AsyncioServer` 同时代理 `start_serving` 与 `serve_forever`，并保留不支持版本的明确错误 |
| [`httpie-4-host-header-overwrite-full`](../benchmarks/run_ab/full/httpie-4-host-header-overwrite/manifest.yaml) | [`8c892ed`](https://github.com/httpie/cli/commit/8c892edd4fe700a7ca5cc733dcb4817831d253e2) → [`040d981`](https://github.com/httpie/cli/commit/040d981f00c3f6830b2d0db3daf3c64c080e96e3) | 59 files | 大小写不敏感的自定义 Host header 只输出一次，不被默认值覆盖 |
| [`luigi-3-serialize-tuple-parameter-full`](../benchmarks/run_ab/full/luigi-3-serialize-tuple-parameter/manifest.yaml) | [`a0f1db0`](https://github.com/spotify/luigi/commit/a0f1db01ddab5b4b2bda3fbe58bad09a6d94a7b4) → [`3a0bfbf`](https://github.com/spotify/luigi/commit/3a0bfbff69addfb3be1107adab3d4914bcae3e4b) | 375 files | JSON 中为标量元素的 flat tuple 能恢复为不可变 tuple |
| [`sanic-5-root-logger-config-full`](../benchmarks/run_ab/full/sanic-5-root-logger-config/manifest.yaml) | [`e3a27c2`](https://github.com/sanic-org/sanic/commit/e3a27c2cc485d57aa1ff87d9f69098e4ab12727e) → [`b63c06c`](https://github.com/sanic-org/sanic/commit/b63c06c75a54752d7f3115d3c635580db44b8399) | 156 files | 默认 console handler 与 INFO level 只配置到 `sanic.root`，不占用通用 `root` logger key |
| [`thefuck-2-executable-path-separator-full`](../benchmarks/run_ab/full/thefuck-2-executable-path-separator/manifest.yaml) | [`40ab4eb`](https://github.com/nvbn/thefuck/commit/40ab4eb62db57627bff10cf029d29c94704086a2) → [`78ef9ee`](https://github.com/nvbn/thefuck/commit/78ef9eec88f43d5727986be2237f6e0e250cbbbc) | 380 files | `PATH` 使用 `os.pathsep` 切分，Windows-style 分号路径不再被冒号切碎 |
| [`luigi-2-beam-target-path-full`](../benchmarks/run_ab/full/luigi-2-beam-target-path/manifest.yaml) | [`baa54c9`](https://github.com/spotify/luigi/commit/baa54c9f4f809692d62d4c3e4497161717c76550) → [`24e8594`](https://github.com/spotify/luigi/commit/24e85945ae39e5975491527e00c3f0f64b42ea6e) | 382 files | Beam BigQuery output 从 `target.table` 读取 project/dataset/table 并格式化为 `p:d.t` |
| [`httpie-5-escape-long-separator-full`](../benchmarks/run_ab/full/httpie-5-escape-long-separator/manifest.yaml) | [`16df884`](https://github.com/httpie/cli/commit/16df8848e81eefac830f407e4b985f42b52970da) → [`90af1f7`](https://github.com/httpie/cli/commit/90af1f742230831792d74d303d1e7ce56c96d4bd) | 15 files | `bob\:==foo` 保留 escaped `:=`，并在后续真实 `=` 处分割为 key `bob:=` 与 value `foo` |
| [`tornado-2-redirect-put-without-body-full`](../benchmarks/run_ab/full/tornado-2-redirect-put-without-body/manifest.yaml) | [`2ca8821`](https://github.com/tornadoweb/tornado/commit/2ca8821d006f6693f920a4b183a3a7c985a5c8ad) → [`4f486a4`](https://github.com/tornadoweb/tornado/commit/4f486a4aec746e9d66441600ee3b0743228b061c) | 300 files | redirected bodyless PUT 已带 `Transfer-Encoding: chunked` 时保持 client chunking，且不改变 server 分支语义 |
| [`httpie-1-unique-download-filename-full`](../benchmarks/run_ab/full/httpie-1-unique-download-filename/manifest.yaml) | [`001bda1`](https://github.com/httpie/cli/commit/001bda19450ad85c91345eea3cfa3991e1d492ba) → [`5300b0b`](https://github.com/httpie/cli/commit/5300b0b490b8db48fac30b5e32164be93dc574b7) | 73 files | 超长下载文件名在追加碰撞后缀前保留扩展名并预留后缀空间，最终组件不超过文件系统上限 |
| [`tornado-4-static-range-bounds-full`](../benchmarks/run_ab/full/tornado-4-static-range-bounds/manifest.yaml) | [`a8420fc`](https://github.com/tornadoweb/tornado/commit/a8420fc681c5423d072978f00eab4d0645057d16) → [`db52903`](https://github.com/tornadoweb/tornado/commit/db529031a1e1a6e951826aba0b7d0b18f05cd4c7) | 298 files | 超出文件头部的 suffix range 归一到 0，显式 end 不大于 start 的 range 被拒绝 |
| [`sanic-3-routes-with-host-full`](../benchmarks/run_ab/full/sanic-3-routes-with-host/manifest.yaml) | [`91f6aba`](https://github.com/sanic-org/sanic/commit/91f6abaa81248189fbcbdf685e8bdcbb7846609f) → [`861e873`](https://github.com/sanic-org/sanic/commit/861e87347a2d373d6ffa387965a6887c83af632c) | 188 files | 内部 URL 只保留 path，外部 URL 默认复用 route host 作为 netloc |
| [`thefuck-5-git-push-bitbucket-full`](../benchmarks/run_ab/full/thefuck-5-git-push-bitbucket/manifest.yaml) | [`7c858fa`](https://github.com/nvbn/thefuck/commit/7c858fadb3458be829d3d43666ccb46c3ed5b8a0) → [`c205683`](https://github.com/nvbn/thefuck/commit/c205683a8df8a57e2db1e9816a5a7ce3255b08fc) | 359 files | Bitbucket 文本中仅出现 `set-upstream` 时不误触发；真实 `git push --set-upstream` 建议仍匹配 |
| [`luigi-7-status-wont-override-full`](../benchmarks/run_ab/full/luigi-7-status-wont-override/manifest.yaml) | [`dd1f2ce`](https://github.com/spotify/luigi/commit/dd1f2ce0061e7787166522a3c75339ba4755dd2c) → [`daf9ce9`](https://github.com/spotify/luigi/commit/daf9ce99a3a7ed4227d1564570c5fce8848357e5) | 338 files | 运行中任务只接受 owning worker 的终态更新，其他 worker 不能覆盖状态 |

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

$fastapi = "benchmarks/run_ab/full/fastapi-5-nested-field-clone/fixture"
New-Item -ItemType Directory -Path $fastapi
git -C $fastapi init
git -C $fastapi remote add origin https://github.com/fastapi/fastapi.git
git -C $fastapi fetch --depth 1 origin 7cea84b74ca3106a7f861b774e9d215e5228728f
git -C $fastapi checkout --detach FETCH_HEAD

$tornado = "benchmarks/run_ab/full/tornado-1-websocket-nodelay/fixture"
New-Item -ItemType Directory -Path $tornado
git -C $tornado init
git -C $tornado remote add origin https://github.com/tornadoweb/tornado.git
git -C $tornado fetch --depth 1 origin 6a5a0bfa370b6c0d3dbbf9589a560a98202d2baa
git -C $tornado checkout --detach FETCH_HEAD

$httpie = "benchmarks/run_ab/full/httpie-2-max-redirects/fixture"
New-Item -ItemType Directory -Path $httpie
git -C $httpie init
git -C $httpie remote add origin https://github.com/httpie/cli.git
git -C $httpie fetch --depth 1 origin 356e0436510fee70b4071fac58be81c0a0a7db59
git -C $httpie checkout --detach FETCH_HEAD

$thefuck = "benchmarks/run_ab/full/thefuck-1-pip-unknown-command/fixture"
New-Item -ItemType Directory -Path $thefuck
git -C $thefuck init
git -C $thefuck remote add origin https://github.com/nvbn/thefuck.git
git -C $thefuck fetch --depth 1 origin 2ced7a7f33ae0bec3ffc7a43ce95330bdf6cfcb9
git -C $thefuck checkout --detach FETCH_HEAD

$sanic = "benchmarks/run_ab/full/sanic-1-blueprint-middleware-order/fixture"
New-Item -ItemType Directory -Path $sanic
git -C $sanic init
git -C $sanic remote add origin https://github.com/sanic-org/sanic.git
git -C $sanic fetch --depth 1 origin e7001b00747b659f7042b0534802b936ee8a53e0
git -C $sanic checkout --detach FETCH_HEAD

$pysnooper = "benchmarks/run_ab/full/pysnooper-2-custom-repr/fixture"
New-Item -ItemType Directory -Path $pysnooper
git -C $pysnooper init
git -C $pysnooper remote add origin https://github.com/cool-RR/PySnooper.git
git -C $pysnooper fetch --depth 1 origin e21a31162f4c54be693d8ca8260e42393b39abd3
git -C $pysnooper checkout --detach FETCH_HEAD

$sanicAsyncio = "benchmarks/run_ab/full/sanic-2-asyncio-start-serving/fixture"
New-Item -ItemType Directory -Path $sanicAsyncio
git -C $sanicAsyncio init
git -C $sanicAsyncio remote add origin https://github.com/sanic-org/sanic.git
git -C $sanicAsyncio fetch --depth 1 origin ba9b432993019b0af0c4827a5ed42aaa091bd17d
git -C $sanicAsyncio checkout --detach FETCH_HEAD

$httpieHost = "benchmarks/run_ab/full/httpie-4-host-header-overwrite/fixture"
New-Item -ItemType Directory -Path $httpieHost
git -C $httpieHost init
git -C $httpieHost remote add origin https://github.com/httpie/cli.git
git -C $httpieHost fetch --depth 1 origin 8c892edd4fe700a7ca5cc733dcb4817831d253e2
git -C $httpieHost checkout --detach FETCH_HEAD

$luigiTuple = "benchmarks/run_ab/full/luigi-3-serialize-tuple-parameter/fixture"
New-Item -ItemType Directory -Path $luigiTuple
git -C $luigiTuple init
git -C $luigiTuple remote add origin https://github.com/spotify/luigi.git
git -C $luigiTuple fetch --depth 1 origin a0f1db01ddab5b4b2bda3fbe58bad09a6d94a7b4
git -C $luigiTuple checkout --detach FETCH_HEAD

$sanicLogger = "benchmarks/run_ab/full/sanic-5-root-logger-config/fixture"
New-Item -ItemType Directory -Path $sanicLogger
git -C $sanicLogger init
git -C $sanicLogger remote add origin https://github.com/sanic-org/sanic.git
git -C $sanicLogger fetch --depth 1 origin e3a27c2cc485d57aa1ff87d9f69098e4ab12727e
git -C $sanicLogger checkout --detach FETCH_HEAD

$thefuckPath = "benchmarks/run_ab/full/thefuck-2-executable-path-separator/fixture"
New-Item -ItemType Directory -Path $thefuckPath
git -C $thefuckPath init
git -C $thefuckPath remote add origin https://github.com/nvbn/thefuck.git
git -C $thefuckPath fetch --depth 1 origin 40ab4eb62db57627bff10cf029d29c94704086a2
git -C $thefuckPath checkout --detach FETCH_HEAD

$luigiBeam = "benchmarks/run_ab/full/luigi-2-beam-target-path/fixture"
New-Item -ItemType Directory -Path $luigiBeam
git -C $luigiBeam init
git -C $luigiBeam remote add origin https://github.com/spotify/luigi.git
git -C $luigiBeam fetch --depth 1 origin baa54c9f4f809692d62d4c3e4497161717c76550
git -C $luigiBeam checkout --detach FETCH_HEAD

# 这两个历史 SHA 可从当前历史到达，但 GitHub 拒绝直接抓取未公告 SHA；使用 filtered history clone。
$httpieLongSep = "benchmarks/run_ab/full/httpie-5-escape-long-separator/fixture"
git clone --filter=blob:none --no-checkout https://github.com/httpie/cli.git $httpieLongSep
git -C $httpieLongSep checkout --detach 16df8848e81eefac830f407e4b985f42b52970da

$tornadoRedirect = "benchmarks/run_ab/full/tornado-2-redirect-put-without-body/fixture"
git clone --filter=blob:none --no-checkout https://github.com/tornadoweb/tornado.git $tornadoRedirect
git -C $tornadoRedirect checkout --detach 2ca8821d006f6693f920a4b183a3a7c985a5c8ad

$httpieFilename = "benchmarks/run_ab/full/httpie-1-unique-download-filename/fixture"
git clone --filter=blob:none --no-checkout https://github.com/httpie/cli.git $httpieFilename
git -C $httpieFilename checkout --detach 001bda19450ad85c91345eea3cfa3991e1d492ba

$tornadoRange = "benchmarks/run_ab/full/tornado-4-static-range-bounds/fixture"
git clone --filter=blob:none --no-checkout https://github.com/tornadoweb/tornado.git $tornadoRange
git -C $tornadoRange checkout --detach a8420fc681c5423d072978f00eab4d0645057d16

$sanicHost = "benchmarks/run_ab/full/sanic-3-routes-with-host/fixture"
git clone --filter=blob:none --no-checkout https://github.com/sanic-org/sanic.git $sanicHost
git -C $sanicHost checkout --detach 91f6abaa81248189fbcbdf685e8bdcbb7846609f

$thefuckBitbucket = "benchmarks/run_ab/full/thefuck-5-git-push-bitbucket/fixture"
git clone --filter=blob:none --no-checkout https://github.com/nvbn/thefuck.git $thefuckBitbucket
git -C $thefuckBitbucket checkout --detach 7c858fadb3458be829d3d43666ccb46c3ed5b8a0

$luigiStatus = "benchmarks/run_ab/full/luigi-7-status-wont-override/fixture"
git clone --filter=blob:none --no-checkout https://github.com/spotify/luigi.git $luigiStatus
git -C $luigiStatus checkout --detach dd1f2ce0061e7787166522a3c75339ba4755dd2c

uv run --locked --cache-dir .uv-cache horizon eval run-ab-suite `
  benchmarks/run_ab/bugsinpy-full-checkout-pilot-v11.yaml `
  --image python:3.12-alpine `
  --state-dir .horizon/run-ab-suite-full-twenty-two-v11
```

前三个直接导入类案例（tqdm、youtube-dl、PySnooper）运行完整 checkout 中的真实生产代码；
PySnooper 只为 Python 3.12 注入历史 `collections` 标准库别名，不修改被测逻辑。另外十九个案例为
避免安装历史依赖栈，使用标准库 AST 从真实生产文件提取目标函数或类后执行：Luigi 验证 `MetricsHandler` 的 collector
所有权；FastAPI 以最小 stdlib 假对象执行真实 `create_cloned_field`，验证嵌套字段不再复用原对象；
Tornado 执行真实 handler/protocol 方法，验证抽象合同以及 handler → protocol → stream 的 nodelay
委托；HTTPie 执行真实 `get_response` 并验证 Session 上的 redirect cap；TheFuck 执行真实
`get_new_command`，Sanic-1 执行真实 `register_named_middleware`；v7 新增三例分别执行真实
`AsyncioServer` lifecycle methods、`HTTPRequest` 和 `TupleParameter.parse`；v8 新增三例执行真实
logging config assignment、`get_all_executables` 与 `BeamDataflowJobTask.get_target_path`；v9 新增两例执行真实
`KeyValueType` 与 client/server chunking predicate；v10 新增两例执行真实 filename helpers 与
static range predicate/normalizer；v11 新增三例执行真实 `Sanic.url_for`、`git_push.match` 与
`Scheduler.add_task` 状态谓词。它们不是文本 marker 检查，但也
不运行各项目原 Python 版本和完整依赖矩阵，不能替代官方 BugsInPy runner。

## 3. 当前 A/B 证据

### 3.1 v11 三种失效模式的本地与公开证据

Sanic-3、TheFuck-5 与 Luigi-7 均来自 2026-10-24 在补丁揭示前冻结的 development 选择；holdout
保持密封。三个 exact buggy commit 的保护检查均失败，对应 fixed commit 均通过。本地完整 Harness
中三个 Baseline 都进入 `WAITING_FOR_USER`，三个 Treatment 都单次 replan 后 `SUCCEEDED`，Trace
可重放且 source checkout 保持 clean；没有模型网络或付费调用。Sanic 首版 manifest 的 YAML block
scalar 丢失候选补丁前导缩进，Treatment 因 `IndentationError` 失败；改用显式 indentation indicator
后才通过，该负结果不被覆盖。

公开禁网 Docker run
[`40804131201`](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40804131201)
进一步通过 exact checkout 准备、22/22 完整 checkout、3/3 三阶段、1/1 写入硬崩溃和最终证据门；
完整 suite 的初始失败与 Treatment 恢复也都为 22。统一 artifact ID `12866973813`，
234,783,960 bytes，digest
`sha256:f49be3f5a38d75524fbf74ec0fd761081c42320a14b1b7ef4de49e7d54bf06b3`，保留至 2026-11-28。
该结果证明三条 Scripted Treatment 能在受保护验证下恢复，不是任何真实模型成功率。

### 3.2 v10 两种失效模式的本地与公开证据

HTTPie-1 与 Tornado-4 均来自 2026-10-24 在补丁揭示前冻结的 development 选择；holdout 保持密封。
本地完整 Harness 先确认两个 exact buggy commit 的保护检查失败；两个 Baseline 均因精确重复检索进入
`WAITING_FOR_USER`，两个 Treatment 均单次 replan 后 `SUCCEEDED`。四条 Trace 可重放、source checkout
保持 clean，且没有模型网络或付费调用。Tornado 初版保护探针曾把负数分支 body 无条件执行，导致合法
正数 range 被错误归一；该负结果保留并改为执行真实 `if` 节点后通过，没有放宽验收。

公开禁网 Docker run
[`40799174668`](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40799174668)
进一步通过 exact checkout 准备、19/19 完整 checkout、3/3 三阶段、1/1 写入硬崩溃和最终证据门。
统一 artifact ID `12865471959`，214,329,764 bytes，digest
`sha256:d33a1f98990849b6a0f1510b2d7728cf055f1dae0d0b6f9eef0f643fcddc3110`，保留至 2026-11-28。
该结果证明两条 Scripted Treatment 能在受保护验证下恢复，不是任何真实模型成功率。

### 3.3 v9 两种失效模式的本地与公开证据

HTTPie-5 与 Tornado-2 均来自 2026-10-24 在补丁揭示前冻结的 development 选择；holdout 保持密封。
本地只执行受限 AST 保护探针：两个 exact buggy commit 都返回非零，应用候选修复后都返回 0；没有
调用模型。公开禁网 Docker run
[`40794561712`](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40794561712)
进一步通过 exact checkout 准备、17/17 完整 checkout、3/3 三阶段、1/1 写入硬崩溃和最终证据门。
该结果证明两条 Scripted Treatment 能在受保护验证下恢复，不是任何真实模型成功率。

### 3.4 v8 三种失效模式的本地与公开证据

Sanic-5、TheFuck-2 与 Luigi-2 均来自 2026-10-24 在补丁揭示前冻结的 development 选择；不占用
仍密封的八案例 holdout。buggy 保护检查分别以错误 logger key、Windows-style PATH 被冒号切碎，
以及 `BigQueryTarget` 顶层属性不存在而失败；fixed commit 均通过。

test-only 本地完整 Harness 中，三个 Baseline 均以 48 events 进入 `WAITING_FOR_USER`，三个
Treatment 均以 6/7/7 model/tool/step、74 events `SUCCEEDED`。六条 Trace 均可重放、三个 source
checkout 均未改变，且 `paid_model_called=false`、`network_called=false`：

| Case | Baseline / Treatment | report ref | Trace refs B / T |
|---|---|---|---|
| Sanic-5 | `WAITING_FOR_USER` / `SUCCEEDED` | `bfbd34874a742bc5bad9f63485babfda8f077a676684b54c3c3a0b6c1eae1699` | `e58569f064b7e2cba0d852652af826b7bad3cf3ebbb15a237c34ad975b48d3cd` / `fc61e0a336318495d8f74126e331eee1f61905dadc25908bdd81133aaa90b4ff` |
| TheFuck-2 | `WAITING_FOR_USER` / `SUCCEEDED` | `17322d1482168d854d7a4c71ee55a2dca1d36b4b33dd2ef53a687fa823c42cbc` | `dd48f1b50f4beff6f869e8d7fd7e679c5098b129d2a02710072da0d1f95292f2` / `943e4a00ed6ffb99255d87811bc9bdc3fda3a19966a4245dd14b1ba6b4bd524b` |
| Luigi-2 | `WAITING_FOR_USER` / `SUCCEEDED` | `71e426d585e78a406a699d02b36f935a5ad2086f9d89b6cb9ea83ac2eb34a6f0` | `7c0d07246c735b87fe76be55a9470a2b08963a1e3a7eb171387376e60242298c` / `f03ffa4a6c29e4d75a744a2fa2312ef6d008e5113c5f9c15b2752ba400c8a629` |

三条 Treatment 的 Code RAG 最终都把生产目标文件排 rank 1。Sanic/TheFuck/Luigi 分别索引
154/378/327 个文件，跳过 2/2/55 个文件；对应 EvidencePack ref 为
`0d09843bc3145acca92f0d139fc2b0febb63d0255b1bd3d1bf08d4fae18ae225`、
`7089973e6b567c9765124aa120f638d6fa3bd172a9e2cfa894246d0056b717d4` 与
`e34ed925255119bff4422fbcab5da71782fc840e72aaa5fdb8be99fca303dec3`。初始较泛的 Sanic/Luigi
query 曾把目标排 rank 3/4，随后用 failing-test 已知的配置常量或类/方法标识符收窄到 rank 1；
这是 development 调整，不是独立 RAG 泛化证据。

Run A/B 新生成的 arm/case/suite report 现在会直接消费 Trace 中的检索 degradation：上述跳过文件
不再只存在于单次 EvidencePack，而会进入 `retrieval_degraded_arm_count`、
`degradation_record_count` 和逐 flag arm 数。required check 通过仍可保持 `SUCCEEDED`，但该 arm
会归入 quality-flagged success 而不是 clean success。此变更没有修改 v8 manifest；采用新代码的
公开 run [`40682108470`](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40682108470)
已给出精确聚合：30 条 arm 中 unknown acceptance 15、known check failure 0、clean success 1、
quality-flagged success 14、retrieval-degraded arm 28、degradation record 56。

公开 run [`40311947427`](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40311947427)
随后在生产 Docker adapter 与禁网容器中通过完整 checkout 15/15、三阶段 3/3 与硬崩溃 1/1。
统一 artifact ID `12645122879`、201,843,271 bytes，digest
`sha256:764e8ee2af63a050bb8465542adf69e9fc521e24aebc502eff312bc91b457b84`，创建于
`2026-10-26T11:07:03Z`、到期于 `2026-11-25T11:06:35Z`。当前 doctor 的来源案例数与公开
Docker verified count 均为 15。

上述质量聚合复跑的统一 artifact ID 为 `12813023460`，201,900,712 bytes，digest
`sha256:e1a37b2308e7992244d096cde9f629ceea5df3c2290d6cb1ea2e474bf6dd7735`；同一 job 也通过
dependency-reduced 5/5、三阶段 3/3、硬崩溃 1/1 和全部 Docker 合同。旧 run `40311947427`
继续作为 v8 初次公开 15/15 的历史证据，不回填当时不存在的质量字段。

### 3.5 v7 三种失效模式的本地证据

Sanic-2、HTTPie-4 与 Luigi-3 均来自 2026-10-24 在补丁揭示前冻结的 development
选择；它们不占用仍密封的八案例 holdout，也不把随后依据 gold 制作的端到端 manifest 称为盲测。
三个 buggy commit 的保护检查分别以 lifecycle proxy 缺失、重复 Host header 和 flat tuple 的
`TypeError` 失败，对应 fixed commit 均通过。

test-only 本地完整 Harness 的最终结果如下；六条 Trace 均可重放，三个 source checkout 均未改变，
且 `paid_model_called=false`、`network_called=false`：

| Case | Baseline | Treatment | 事件数 B/T | report / Trace refs |
|---|---|---|---:|---|
| Sanic-2 | `WAITING_FOR_USER` | `SUCCEEDED`，一次 replan、一次唯一锚点替换 | 48 / 74 | report `9002c6ed23a68dbdc2834a74cf72a717241e77e100c3acc960b6e3baa208124e`；Trace `155b3b8b0d7c82c821fc7f5fefd2e78b6cf870eb41af4227c1bb439d47260c39` / `d99497306ff6bd2a2b83d3641428f34238203a70a53722861eee74361b2d8a58` |
| HTTPie-4 | `WAITING_FOR_USER` | `SUCCEEDED`，一次 replan、一次逐行精确替换 | 48 / 74 | report `14d018f0228695f20f329e8bd289fab44d8f2c66f3c217535dce3b44c56d8c81`；Trace `7365c55773a5b4b51dfd12997b4923179afad07cdb9e17bcd289fff85bc03053` / `fa5a001bd094ed089fee02752948fb582ee943ae750b6c6424a4e1ed76c4c4c8` |
| Luigi-3 | `WAITING_FOR_USER` | `SUCCEEDED`，一次 replan、两次逐行精确替换 | 48 / 83 | report `27c5ca113926f80e18cabedd0da99445b2dbbb0f5b4ed58b6ed29c659404bc83`；Trace `574a22f6ad104619a227a85d1673c2f34b8800ebc71e23e2ef304723b94392fa` / `7df130c10b4c90ed39a2e3c98d859fe8f1e68778ddefc210aa9e6e5d0a575865` |

三个 Treatment 的 revision-aware Code RAG 都把生产目标文件排在 rank 1：Sanic 索引
186、跳过 2 个非文本文件；HTTPie 索引 57、跳过 2 个；Luigi 索引 320、跳过 55 个。状态均为
`degraded` 而不是伪装成全量索引；对应 EvidencePack ref 为
`688e6eef7535cb89d367d6e72c4abcad82dff858b84e3af63bb36e7492912a5a`、
`71160034f4818b9ae333760e6256bb7ec68d957d0edf82d08cbb390eb37c6b86` 与
`cb41436a54e2d4b69211e6f8a40a7bc3821e9e949fb99b23c5ad7ba288259054`。

本批保留失败链路。第一次 Luigi Treatment 使用文件内出现三次的
`except ValueError:` 作为“精确”锚点，写工具按 occurrence 合同拒绝修改；失败 report ref 为
`0dde813853c9fa96c58224b0fff8633fcf4c6149f26c6c04c1feb35029ab8ba9`。最终改成两个唯一、逐行的
等价修复锚点后才通过。另一次 Windows 十二案例联合尝试中，既有 Tornado 多行 LF 锚点没有命中
CRLF checkout，随后 Cookiecutter 又在长 state 路径上触发 Win32 path-length 限制，因此该次不能
记为 12/12。第一次公开 v7 run
[`40303452323`](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40303452323)
又确认 Cookiecutter-1 的 Linux checkout 含文档符号链接，安全快照门以
`Links and junctions are not accepted: AUTHORS.md` 拒绝；失败 artifact ID `12642035153`、
72,062,582 bytes、digest `sha256:9e159c8ec37e2751b6ad3f4ebb74e0506a057fc22b24ff029c771b9030b77a86`。
该候选已从 v7 移除，没有排除链接、修改上游内容或放宽安全门；替换后的 Sanic-2 本地 Harness 已
通过。随后替换版公开 run
[`40305739627`](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40305739627)
完成十二案例 12/12、三阶段 3/3 和硬崩溃 1/1。统一 artifact ID `12642577416`，
183,527,015 bytes，digest
`sha256:dade5bfc81fd600cfd85e504e5415e8fa890e8de3794dfc1d8388953f7acaf63`，创建于
`2026-10-26T10:19:12Z`、到期于 `2026-11-25T10:18:53Z`。该 v7 提交时 doctor 的来源案例数与
公开 Docker verified count 均为 12；v8 的最新计数见上节。

### 3.6 v6 PySnooper 两文件案例的本地证据

PySnooper-2 来自 2026-10-24 在补丁揭示前冻结的 development 选择，不占用仍密封的八案例
holdout，也不把随后依据 gold 制作的端到端 manifest 称为盲测。buggy commit 的真实包导入保护
检查先以 `Tracer.__init__()` 不接受 `custom_repr` 失败，对应 fixed commit 通过。

test-only 本地完整 Harness 中，Baseline 产生 48 个事件并进入 `WAITING_FOR_USER`；Treatment 在
一次 replan 后对 `pysnooper/utils.py` 与 `pysnooper/tracer.py` 做八次单行精确替换，以 13 次模型、
14 次工具、14 步和 137 个事件进入 `SUCCEEDED`。report ref 为
`9ab791353fd1e6172cc5d974bcb897ee8210be60285f08bce47989c4208dd3fa`，Baseline/Treatment Trace ref
分别为 `0444a8c7c7b8f3f62b98fed414f17caa7a32c027900d913421d1b37c3cbe6ba4` 与
`d2905ac211b93b66e757a7fcb4b0f8d779e92b4c9cdadadd499950428b9d3171`；两条 Trace 可重放，source
未改变，且 `paid_model_called=false`、`network_called=false`。

同一 Treatment 的 revision-aware Code RAG 索引 25/25 文件，无降级；查询
`custom repr single get_shortish_repr Tracer custom_repr` 将 `pysnooper/utils.py` 排第 1、
`pysnooper/tracer.py` 排第 3，EvidencePack ref 为
`be1c6b0d8e2999bbc1edf0649c8af905afe115da41a048de205dbf82f04baeeb`。重复相同检索随后按既有
NoProgress 合同触发 replan，不把重复结果冒充新证据。

第一次本地 Treatment 因 YAML block scalar 丢失补丁前导缩进而得到 `IndentationError`，失败 report
ref `78f888cf2444d27b0c7628d3c43fcdf4796990a8cb5314848a866542c37ef269`、Treatment Trace ref
`9d955973c33f6ec9c5b9b5d70aa8d85c3dead61e8e334a0105dd1ca642494830`；改为显式 `\n` 后才获得上述
成功。这一负结果保留。

随后，2026-10-25 的[公开 Source-bound Docker evidence](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40206304099)
重新获取并核对九个固定 buggy commit 的 HEAD、tracked 文件数和 clean status，在生产
`DockerAcceptanceExecutor` 的禁网、非 root、只读根、drop capabilities 与资源上限下通过 v6。
工作流硬门确认 case/passed/initial-failure/treatment-recovered 均为 9，九例全部 Trace replay 且
source isolation 成立；没有读取 API Key 或调用付费模型。统一 artifact ID `12604040662`、
167,434,042 bytes、digest
`sha256:8c991b53d88d2934459e940ca7ba20fcba8dd13f8fd14faf4361cb01c331a224`，计划保留至
2026-11-24。doctor 的来源案例与公开 Docker verified count 现均为 9。

### 3.7 v5 两个预冻结来源案例的本地与公开证据

新增任务没有从当前失败日志临时挑选：TheFuck-1 来自 2026-10-04 在补丁揭示前提交的
[三项目外部定位盲测](retrieval-evaluation.md#7-三项目外部定位盲测)，Sanic-1 来自随后独立冻结的
[第二批 holdout](retrieval-evaluation.md#8-第二批独立冻结-holdout)。这只约束任务来源，不把已经
揭示 gold 后制作的端到端 manifest 重新包装成新 holdout。

两个 buggy checkout 的保护检查均先失败，对应 fixed commit 均通过。随后 test-only 本地可信
Harness A/B 得到：

| Case | Baseline | Treatment | 事件数 B/T | report / Trace refs |
|---|---|---|---:|---|
| TheFuck-1 | `WAITING_FOR_USER` | `SUCCEEDED`，一次 replan、同文件两次精确替换 | 48 / 83 | report `3e25e0f75121b91c632eacc26f21b897a40e78a313ca7f0f520cad6da0189c89`；Trace `9bf9e77c69509bf77b35d8511922bffab084e5e7d64c8491cbae7799a6e837aa` / `622de977b92ca7fc9d8af90d8c5b16dc3fbbc2badc142996e1c0d9451bd8e8b6` |
| Sanic-1 | `WAITING_FOR_USER` | `SUCCEEDED`，一次 replan、一次精确替换 | 48 / 74 | report `6b6bbce8f1df762504516c43cb27b2126b58ebb6d3cc2adf70311252ea664331`；Trace `90ff8db0620c193ba2ea84c20be6d606635fa09db74722cfde21f368267f8fc5` / `bab42affbb3b6152a06d1d9246eb6936ecd1dda7002baf383379c2602c92c3fe` |

四条 Trace 均可重放，两个 source checkout 未改变，unknown/open reservation 为 0，且
`paid_model_called=false`、`network_called=false`。

随后，2026-10-25 的[公开 Source-bound Docker evidence](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40203191203)
重新获取并核对八个固定 buggy commit 的 HEAD、tracked 文件数和 clean status，在生产
`DockerAcceptanceExecutor` 的禁网、非 root、只读根、drop capabilities 与资源上限下完成 v5。
工作流硬门确认 case/passed/initial-failure/treatment-recovered 均为 8，八例全部 Trace replay 且
source isolation 成立；没有读取 API Key 或调用付费模型。统一 artifact ID `12602543425`，大小
166,866,560 bytes，digest
`sha256:319ad06ae83d92fb1f921cf49dab71f019987d1bab75533306d334c89ef4c330`，计划保留至
2026-11-24。doctor 的来源绑定 case count 与公开 Docker verified count 现均为 8。

### 3.8 v4 第六案例本地证据与统一验收门

HTTPie 单案在完整 70-file checkout 上先确认保护检查失败；Baseline 产生 48 个事件并按预期进入
`WAITING_FOR_USER`，Treatment 产生 74 个事件、执行一次 replan 后进入 `SUCCEEDED`。report ref 为
`dbe16c62fcb95a9b730279961199b7271092dcd0edf2df84534c750f14ae19e9`，Baseline/Treatment Trace ref
分别为 `3b20bc548eda5b8433269f2dadda1d8a9dc78f09125f184fdfb049e500b76f58` 与
`cb9e27327ef0accef969b3cd3c4c96f19bd6b8172fc51b93e1bb1a262252cce6`；两条 Trace 均可重放，
source checkout 未改变，`paid_model_called=false`、`network_called=false`。这是 test-only 本地可信
执行，不是 Docker 隔离证据。

第一次本地六案例联合执行保留为负结果：结果为 4/6。新 clone 的 Tornado checkout 使用 CRLF，
冻结 v3 manifest 的 LF 多行 exact replacement 无法匹配；HTTPie 初稿也使用了同类多行锚点。
HTTPie 随后缩为唯一单行锚点，并在 LF/CRLF 合同测试及上述单案中通过；历史 v3 manifest 没有为
迎合这次 Windows clone 而改写，且未把 4/6 冒充统一成功。

2026-10-25 的[公开 Source-bound Docker evidence](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40200117233)
重新获取并核对六个固定 buggy commit 的 HEAD、tracked 文件数和 clean status，在生产
`DockerAcceptanceExecutor` 的禁网、非 root、只读根、drop capabilities 与资源上限下完成 v4。
工作流硬门确认 case/passed/initial-failure/treatment-recovered 均为 6，六例全部 Trace replay 且
source isolation 成立；没有读取 API Key 或调用付费模型。统一 artifact ID `12602031276`，大小
157,416,541 bytes，digest
`sha256:a70612aab310c6d6c09c9ea0796242da75877fe025b10a9c72d76a525bd3c0e8`，计划保留至
2026-11-24。该公开结果把 doctor 的 full-checkout Docker verified count 从 5 提升到 6。

### 3.9 v3 五项目公开 Docker 执行

2026-10-24 的专用 [Source-bound Docker evidence](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40080364921)
在 GitHub-hosted Linux runner 上重新 shallow-fetch 五个固定 buggy commit，并逐一检查 HEAD、tracked
文件数和 clean status。工作流随后在生产 `DockerAcceptanceExecutor` 中运行冻结 v3：5/5 初始检查
按预期失败，5/5 Baseline 进入 `WAITING_FOR_USER`，5/5 Treatment 进入 `SUCCEEDED`；验收容器使用
`network=none`、只读根文件系统、非 root 用户、drop all capabilities 与资源上限。工作流不读取
API Key、不调用付费模型，动作仍来自冻结 Scripted Model。统一 artifact
`source-bound-docker-evidence` 为 131,547,546 bytes，digest
`sha256:f3698c74ef95dede7b2f51109c08b006d6e242640947045d1c49bee37faae80c`，计划保留至
2026-11-23；其中同时包含依赖裁剪、五项目完整 checkout、多阶段和硬崩溃 suite 的报告与 Trace。

同一批本地 test-only 可信执行先独立跑过新增两例：FastAPI 与 Tornado 的 buggy checkout 均未通过
保护检查，对应 fixed checkout 均通过；两例 Baseline 均产生 48 个事件并进入 `WAITING_FOR_USER`，
Treatment 分别产生 74 / 92 个事件、生成 Plan v2 并进入 `SUCCEEDED`，Trace 可重放且两个源 checkout
保持干净。该证据验证的是 Harness 与冻结修复动作，不是模型自主定位能力。

### 3.10 v2 三项目历史公开 Docker 执行

2026-10-06 的专用 [Source-bound Docker evidence](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/37436568589)
工作流在 GitHub-hosted Linux runner 上重新 shallow-fetch 三个固定 buggy commit，并逐一检查 HEAD、
tracked 文件数和 clean status。随后它拉取 `python:3.12-alpine`，通过生产
`DockerAcceptanceExecutor` 在 `network=none`、只读根文件系统、非 root 用户、drop all capabilities
和资源上限下运行 v2 suite：

- 工作流、依赖裁剪 5-case suite、三项目完整 checkout suite 和证据上传均成功；总 job 用时 56 秒；
- 3/3 初始保护检查失败，3/3 Baseline 进入 `WAITING_FOR_USER`，3/3 Treatment 进入 `SUCCEEDED`；
- CLI 只在 suite 全部期望满足时以 0 退出；每个 report/Trace、具体 Docker image ID 和状态库存入
  `.horizon/full-checkout-docker-v2`；
- 32.3 MB 统一公开 artifact 的 digest 为
  `sha256:5bb438df11526a43476272f5622af3446c013bdc0c3ae1614b56272c8bcbc4b2`，保留 30 天；
- 工作流不读取 API Key，不调用付费模型；Agent 模型仍是冻结 Scripted Model，验收容器自身禁网。

这关闭了 Luigi “仅本地可信、尚无 Docker”的缺口，但仍不是原 Python 版本、完整依赖环境或
BugsInPy 官方 runner 成绩。工作流只在自身、v2 manifests 或直接的 runner/sandbox 合同变化时
自动执行，也可手动触发；不拖慢每个普通文档提交。

### 3.11 v2 三项目历史本地可信执行

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

### 3.12 v1 双项目历史 Docker 结果

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
随后显式 replan、精确修改真实生产文件并通过对应保护验收。v3 五例同时有 test-only 本地执行
和专用公开禁网 Docker 执行，两条路径均保存 revision-bound EvidencePack。下表只列已从历史 v2
报告复核过具体检索排名的三例；新增两例不在缺少 artifact 明细时追写排名。

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

候选选择保留两个负结果。Cookiecutter bug 3 的历史 checkout 有七个 tracked 文档符号链接：
`docs/AUTHORS.md`、`docs/BACKERS.md`、`docs/CODE_OF_CONDUCT.md`、`docs/CONTRIBUTING.md`、
`docs/HISTORY.md`、`docs/README.md` 与 `docs/case_studies.md`。Windows checkout 把它们物化为普通
文件，因此本地候选曾通过；公开 Linux runner 保留符号链接后，快照安全门正确拒绝
`Links and junctions are not accepted: AUTHORS.md`。两次诊断运行分别是
[首次失败](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40079729293)与
[带公开错误注解的复现](https://github.com/caoshangrui-spec/horizon-coding-harness/actions/runs/40080091517)。
最终 v3 不计入该候选，也没有放宽链接安全门或把七个文件排除后冒充完整 checkout。

PySnooper bug 3 的 BugsInPy `requirements.txt` 为空，但真实源码导入
`decorator`，官方 `setup.sh` 也会安装它。为避免为一个案例维护专用依赖镜像，本批没有把它包装成
“依赖无关成功”；该候选也被明确放弃，不计入任何 suite。

## 6. 结论边界

可以声称：v5 的八个不同项目、共 2,926 个 tracked 文件的精确完整 checkout 已通过来源门、初始
负例、真实 Code RAG 路径、可恢复等待、单次 replan、公开禁网 Docker 验收和 Trace replay；v6
新增的 25-file PySnooper 两文件修复已通过相同本地 Harness 合同，九项目合计 2,951 files，并已
通过公开九案例统一门。v7 又增加三个预冻结 development 案例，十二案例、九项目合计 3,573 files；
三个新增案例的初始负例、完整本地 Harness、rank-1 生产路径检索、Trace replay 与 source isolation
均成立；Cookiecutter 候选在首次公开 run 被符号链接安全门拒绝后已由 Sanic-2 替换，替换版公开
十二案例门已通过 12/12。v8 再增加 Sanic-5、TheFuck-2 与 Luigi-2，十五案例、九项目合计
4,491 files；三个新增案例的 buggy/fixed 探针、本地完整 Harness、rank-1 生产路径检索、Trace replay
与 source isolation 均成立，公开十五案例门也已通过 15/15。v9 继续加入 HTTPie-5 与 Tornado-2，
十七案例、九项目合计 4,806 files；两个 buggy commit 的保护探针失败、候选修复通过，公开生产路径
已通过 17/17、Trace replay、source isolation 与零付费模型调用门。v10 再加入 HTTPie-1 与
Tornado-4，十九案例、九项目合计 5,177 files；两个新增案例的本地完整 Harness 与公开 19/19
生产路径均通过。v11 再加入 Sanic-3、TheFuck-5 与 Luigi-7，二十二案例、九项目合计 6,062 files；
三个新增案例的本地完整 Harness 与公开 22/22 生产路径均通过。TheFuck、Sanic、PySnooper 及
v7/v8/v9/v10/v11 新增案例来自预冻结检索集合，
但端到端 manifest 是揭示 gold 后制作，不能称为新 holdout。第一次 Windows 六案例联合运行的 4/6、
PySnooper YAML 缩进失败、v7 Luigi 非唯一锚点失败、Windows 十二案例联合运行的 CRLF/长路径失败，
以及公开 Cookiecutter 符号链接拒绝均保留，不用后续成功覆盖负结果。
不能声称：

- 这是 BugsInPy 官方分数、原 Python 版本或完整依赖环境的测试结果；
- 脚本模型证明真实模型能自主生成查询、定位缺陷或选择 replan；
- 22 个作者选择案例足以估计泛化成功率、误触发率或成本收益。

后续增量已把 youtube-dl、Luigi 与 FastAPI 扩展为[三阶段完整 checkout](multi-stage-full-checkout-pilot.md)，
每条 arm 均跨两个持久边界到 epoch 3，联合观察上下文、Memory、检索、replan 与恢复；v4 本地可信
与公开禁网 Docker 均为 3/3。
真实模型 arm 仍只在用户明确授权费用后执行；当前不增加向量库、无界 replan、分布式队列或
多 Agent 编排。
