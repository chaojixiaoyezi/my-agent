# 来源 SecretRef 安全修复交接

## 基本信息与原红证据

- workstream：普通用户/群/管理员监控 HTTP 的秘密读取与外发边界，主线统一提交与打包。
- owner：scheduler/recovery 安全子代理，2026-10-13。
- baseline：本期候选 `aa150ba0b`，与其它安全线逐文件独占。
- 生产使用或正常 IM 能否修改宿主权限文件未作为本缺陷前提；公开来源参数本身已足够触发。

原完整探针 `/tmp/SOURCE_HTTP_OWNER_SECURITY_PROBE.py` 和 JSON 保留：真实普通 owner 原 Registry/Executor
对 `env:` 和外 owner `file:` 各发送合成秘密到 toy HTTP（各两次），`read_file` 同路径拒绝。
原合同攻击断言红证据 `/tmp/SOURCE_HTTP_SECRET_SCOPE_RED.xml`；不包含真实秘密或生产数据。

## 已实现

- 两个纯来源渲染器运输未解析引用，彻底取消默认宿主 `resolve_secret_ref`。
- 正式 `execute_scoped` 冻结原 owner/read/cancel/执行权和当前 RunParams，不序列化权威对象。
- 唯一 `_fetch_json_pinned` 请求前整体核对，逐请求/逐跳复核父网络、原执行与当前 owner 网络政策。
- 无秘密原收割闭包不要求已结束的主 attempt；仍每跳守冻结父网络与当前 owner。秘密闭包没有这个放行，不能借新 actor。
- raw env 全拒；file 只认原 owner 根和原 read scope，严格 FD/no-follow/8 KiB，Full Access 不扩大。
- 优先原 `request_credentials` / `credential_headers` 账本，保持本人 actor、root、claim/attempt、监控与 origin。
- 带秘密重定向不得跨 origin；成功/错误净化在 JSON/spool/model 之前；无原上下文的冷来源拒绝。
- 复用的 `web_fetch_tools._secret_echo_variants` 增加 `quote_plus`：实际表单/query 中空格变成 `+` 的可还原表示也必须清理。

改动：`ingestion/source_http.py`、`watch_tool.py`、`watch_tool_spec.py`，新增只含 DTO/helper 的
`ingestion/source_credentials.py`，共享 `tooling/web_fetch_tools.py` 仅补同一回显变体；未改来源/监控/凭据 schema 权威位置。测试新增
`test_ingestion_secret_scope.py`、`test_ingestion_task_credential_scope.py`，旧纯渲染器测试改为未解析引用合同。
详细规则见 [设计](../design/WATCH_SOURCE_SECRET_AUTHORITY.md)，根导航/TESTS 由主线登记。

## 验证

真实 private-card × 普通/群/admin × non-full/full、当前准确监控与另一监控拒绝、原父网络上界、
child/孙级、冷重放、全部引用预核、重定向及 POST 三位置成功/HTTP 错误回显，11 文件 **213/213**
通过，`/tmp/SOURCE_SECRET_ALL_FOCUSED_GREEN.xml`。更早两条 env/file 原入口探针也各 **0 HTTP**，
`/tmp/SOURCE_HTTP_OWNER_SECURITY_PROBE_GREEN.json`；原泄露红证据未覆盖。

随后两个窄收口（无秘密持续生命周期、query 表单 `+` 净化）在真实原闭包和私密卡/HTTP 路径中
**41/41** 通过，`/tmp/SOURCE_SECRET_LAST_NARROW_FOCUSED.xml`。这个计数包含来源私密作用域 31 项及原
TaskCredential 10 项，与前 213 有重叠，不相加。实际 HTTP choke 返回正文/错误由只读 observer 记录，
不改变请求、授权或响应；不能用本来不含原正文的 open 短回执证明净化。表单回显原红探针是
`/tmp/source_secret_plus_encoding_live_probe.json`，明确只确认原返回 JSON 留有可还原编码，不虚称最终回执已暴露。

夹具修正仅区分 env 合成变量名/值及保留原 HTTP 头 latin-1 合同；query/body 的 Unicode 与引号仍覆盖。
重定向 handler 已进入原 mutating watch 事务，原账主码保持 `TOOL_OPERATION_OUTCOME_UNKNOWN` 禁止盲重做，
准确 `SOURCE_SECRET_UNAVAILABLE` 保存在 reported 码；没有为测试把副作用结算成未开始。

本片 Ruff/diff 通过；strict code-size 无新增 hard，`/tmp/SOURCE_SECRET_CODE_SIZE.md`。
未新加配置或依赖，这是权限 bug fix；没有另一秘密名单、凭据镜像、兼容旁路或自动生产迁移。

## 需要主线/非作者复核

原来源公开参数既可写入持久 source，又会被 source adapter/puller 重放，所以必须保留唯一 HTTP choke，
不能只在 open schema 拒绝 env。旧 ref 和纯 helper 没有权威时不得直接读取。原 TaskCredential
正常同监控后续执行必须保持可用，模型 raw scope/root/actor 不能新造授权。

已另做认证配置非作者复核：原 factory/start/CLI 预检与 raw HTTP 头旧邻域四文件 **77/77** 通过；
最后 IM 声明必须 scoping、False-equivalent 取值加固后的准确新两文件 **40/40** 通过，
`/tmp/IM_AUTH_CONFIGURATION_INDEPENDENT_V3.xml`。计数不叠加；未改认证源或断言。
合法明确单机及 None-Agent utility 原边界保持。

## 剩余边界

旧依赖 raw env 或没有原活动任务证明的来源会失败，不自动读取最新前台或迁移生产监控。
正式恢复只有原 run/job/creator/TaskLink/claim/attempt 能证明授权时才复用，秘密引用自身不授予权。
已通过闸门的在途 HTTP 不能撤回；本片没有真实飞书、供应商 API 或真实模型验收。

## 建议下一步

非作者审查原 TaskCredential/owner/read scope、冷重放与重定向后，再由主线跑准确候选的联合严格 gate。
owner 网络损坏门可并行只读回归；不要在测试或生产补假任务身份、复制宿主 secret 或放宽原调用准入。
