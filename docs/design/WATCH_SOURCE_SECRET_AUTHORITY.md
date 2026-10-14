# 监控来源 HTTP 凭据的宿主授权边界

状态：本地实现；不修改生产任务，不自动迁移已有来源。主线统一验收、提交和发行。

## 已确认问题

普通用户原 `watch_stream open` 的 `http_request.secret_bindings` 可提供 `env:` 或 `file:`。
原两个 `source_http` 渲染器直接调用宿主 `resolve_secret_ref`，不经过当前 owner 的文件权限。
隔离真实 Core/Registry/Executor 试验中，宿主合成环境变量和 owner 之外的临时文件均被作为
认证头发到模型选择的本机 HTTP 地址；同一文件在原 `read_file` 中被 owner 边界拒绝。
这是底层读取/出站权限缺口，不依赖模型提示词或飞书部署。

## 唯一主链

1. `source_http` 正常化并渲染普通请求；`SourceHttpRequest` 只带未解析的 `secret_bindings`
   和已有 TaskCredential 的 `credential_ref`，没有秘密、网络或文件副作用。
2. 原 Registry `execute_scoped` 传入 `ToolInvocationContext`，`SourceRequestAuthority` 冻结准确
   owner 根、原读取上下文、取消令牌、执行权回调及本次 RunParams 副本。
3. 唯一 `_fetch_json_pinned` 在首个 HTTP 请求前整体授权全部引用，任一失败均不发送部分请求。
   每次请求和重定向检查冻结父网络上界及原 owner 当前网络政策；带秘密才额外核对原活动执行与取消。
   原无秘密 harvester 保留持续生命周期，不能因为主回合已结束或换 attempt 而被新秘密门停掉。
4. 正式网络传输仍沿原 SSRF/DNS pin。带秘密请求禁止跨 origin；成功正文、响应头和错误回显
   在 JSON 解析、候选/spool 写盘和模型展示之前复用原 `web_fetch` 净化。

没有新凭据账、权限名单、后台调度器、来源缓存或兼容身份分支。结构化来源仍保存在原来源/WatchState，
宿主授权对象不序列化；source adapter、探针、inline pull 和原收割闭包共用这个出口。

## 引用类型与合法用法

- `env:`：来源工具不读取宿主环境，无论普通用户、群助手、管理员或 Full Access。已有 raw env
  来源不会自动获得特权；需要由原用户重新通过私人卡片绑定 TaskCredential。
- `file:`：只能读取准确 owner 根内、且原 `WorkspaceReadContext`/PathAccessPolicy 允许的文件。
  同时使用有界严格 no-follow FD，最多 8 KiB。Full Access、模型 root 参数或旧重放不扩大此读取。
- `credential_ref`：优先使用 `request_credentials` 的准确 `cred_...` 引用。解密注入完全沿原
  `credential_headers` 的 owner、私人 actor、活动 root/claim/child attempt、监控 job 和 origin
  检查；引用不是 bearer token。正式调用的 run_id 必须与冻结当前执行一致。

例如模型申请本人 API 的私人输入后，来源可使用：

```json
{"action":"open","url":"https://api.example/health","mode":"poll","http_request":{"credential_ref":"cred_0123456789abcdef0123456789abcdef"}}
```

群里仍是原请求者的私人卡片，不能把新成员、父消息发件人或管理员身份借给来源。子代理/孙代理
沿原 canonical 任务谱系使用同一 TaskCredential，不取得父级之外的文件或网络授权。

## 恢复边界

同监控的正式新执行，只有原 scheduler run/job/creator、TaskLink 和当前 claim/attempt 能证明
原授权时，才可沿已有 TaskCredential 使用。没有原执行证据的 detached/cold 来源明确返回
`SOURCE_SECRET_UNAVAILABLE`，不能拿该 owner 最新前台任务或群成员来补证明。
这可能使此前依赖宿主 raw env 或无活动任务的秘密来源停止成功抓取；应在原有任务中重新绑定私人凭据，
而不是复制 app secret、修改生产账或新增凭据状态来兜底。正常不带秘密的来源保留原行为。

所有引用在出站前核对；已经通过校验并发出的在途请求无法撤回。回显净化覆盖已知本次秘密及原文、
JSON/URL/表单编码（含实际 `urlencode` 产生的 `+`）等常见表示，不声称能够识别外部服务生成的任意变换或其它未知秘密。

## 验证范围

真实隔离 Core/Registry/Executor、本机 HTTP 接收器、原私人卡签名/加密提交、scheduler 预留与领取、
canonical child/孙级、原网络禁止和读取上界；不使用生产凭据、不发飞书，不调用真实模型。
回归包括 raw env/外 owner 文件零 HTTP、合法文件与私人凭据、同群他人/另 root/另 monitor/另 origin
拒绝、冷重放及旧收割上下文拒绝、重定向和回显。详见
[交接](../tasks/IM_SOURCE_SECRET_SECURITY_HANDOFF.md)。

## 建议下一步

主线与非作者先独立复核唯一出站 choke 及原 TaskCredential 的准确身份，再进行联合 gate。
其它 agent 可并行只读测试原网络权限损坏门；不要将 detached 缺证据改成借用当前前台，也不要手工迁移生产监控。
