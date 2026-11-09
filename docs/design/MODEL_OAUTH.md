# 模型账号登录

## 2026-11-07：官方 Sign in with ChatGPT（SIWC）

新增账号使用 **Continue with ChatGPT · my-agent**，按 OpenAI 的 OSS 动态登记协议接入本人套餐。
应用名始终是 `my-agent`，不冒充 OpenAI、Codex 或其它项目。旧 Codex 订阅档案保留原协议，读取时不会被偷偷迁移，
也不会把已有账号的令牌发往另一条 API 路径。

### 两条协议各守原账号

| 档案 | 初始 Client ID | API / 目录 | 传输 |
|---|---|---|---|
| 新 SIWC | `dynamic_agent_client`，仅用于首次授权页 | `https://api.openai.com/v1`；`GET /models` | Responses HTTP/SSE；`store=false`、`stream=true` |
| 已保存的 Codex 档案 | 保留原 `app_EMoamEEZ73f0CkXaXp7hrann` | `https://chatgpt.com/backend-api/codex`；`GET /models?client_version=...` | 保留原账号头与 WebSocket |

SIWC 固定授权端点为 `https://auth.openai.com/api/accounts/authorize`，令牌与撤销端点分别为
`https://auth.openai.com/api/accounts/oauth/token`、`https://auth.openai.com/api/accounts/oauth/revoke`。
`resource` 固定为 `https://api.openai.com/v1`，申请范围为
`openid profile email offline_access resource.invoke chatgpt.tokens.use.direct`；不需要应用密钥或其它项目的客户端身份。

### 登记、回调与已验证身份

1. 首次登录前，在当前 owner/provider 的唯一认证记录内保存 `ext_agent_host_id=urn:uuid:<uuid4>`。
   同一记录后续登录、取消和退出继续使用这个值；读取到损坏值时明确拒绝，不自动换一个身份。
2. 每次开始生成新的 `state`、OIDC `nonce` 与 PKCE S256 校验码。首次动态登记只在 `127.0.0.1:1455` 或 `1457`
   的 `/auth/callback` 回调，并携带 `agent_name_hint=my-agent`；已签发 Client ID 的再授权可使用其它可用端口，
   但主机必须是 `127.0.0.1`，不能替换为 `localhost`、其它路径或带额外查询的回调地址。
3. 回调与 Gateway 均用常量时间比较核对 `state`。首次登记必须带已签发的 `client_id`，不能缺失或仍为
   `dynamic_agent_client`；再授权可以省略它，但返回不同值时必须在兑换前拒绝。
4. 用回调中的已签发 Client ID、原 `redirect_uri`、授权码、PKCE 校验码及 `resource` 进行一次 form 兑换。
   从固定 issuer 的 OIDC 发现文档获取同源 JWKS，验证 RS256 签名及 `iss/aud/exp/nonce/sub`；再授权还须匹配
   原已验证 `subject`。失败不覆盖原账号；取消、退出或配置变化后的迟到回调仍受原 CAS 拒绝。
   首次兑换若明确返回 `invalid_grant`，用同一 CAS 只保留已签发的 Client ID 并丢弃该次 pending，供重新授权，
   不采用任何未经验证的身份或令牌；取消后的旧响应不能把登记写回来。
5. 通过原子保存，把已签发 Client ID、验证后的身份、ID/访问/刷新令牌、有效期和实际授予的 `scopes` 放回同一记录。
   未授予 `chatgpt.tokens.use.direct` 时返回 `scope_not_granted` 和明确提示，保留身份供再次授权，但不能推理。
6. 再授权复用已签发 Client ID 与 host ID，不再携带 `agent_name_hint`。同一账号可携带先前 ID token 作为
   `id_token_hint`、已验证邮箱作为 `login_hint`；不自动要求重新同意、不以邮箱或展示名称识别账号。

验签复用项目已有 `secrets` extra 中的 `cryptography`（Apache-2.0/BSD），不自行实现密码学算法，
不为本片新增另一套令牌文件、守护线程、密钥缓存或依赖声明。缺少该组件时，在生成 pending 或打开授权页前明确提示
安装 `my-agent[secrets]`，不消费授权码、不自动安装或降级验签。服务部署应包含该 extra；本轮验收使用已安装的项目环境。

### 刷新、退出与使用边界

- 刷新携带保存的已签发 Client ID、刷新令牌和相同 `resource`，成功后一次保存轮换后的令牌、有效期及 scopes。
  暂时网络失败不清有效凭据；明确失效的刷新凭据沿同一账号 CAS 清理，不能改用其它账号、API Key 或模型。
  清理只影响旧会话令牌，保留正在进行的授权尝试，不让迟到刷新删除用户后来发起的登录。
- 退出先在模型档案锁外尝试 form 撤销刷新令牌，再核对原 Client/host 绑定、会话代次及 pending 编号，
  清除访问/刷新/ID令牌和 pending，同时保留已签发 Client ID、host ID 和已验证身份。远端撤销未确认也清本地令牌，
  并提示用户可在 ChatGPT 设置中断开；迟到退出不能删除后来发起的新登录。
  HTTP 响应断流也按远端未确认处理，不阻断本地退出；签名资料断流同样归一为无秘密的明确错误。
- 凭据只用于本人发起或明确授权的后台、定时任务，不与其它用户分享。新 SIWC 不借用其它项目的设备码登记；
  本人的 API Key 仍可作为另一个明确配置的账号使用，不构成自动回退。
- UI 明示本机存储和本人授权边界，并提供 `https://chatgpt.com/settings/usage` 管理应用用量；
  此地址仅由用户选择打开，登录或打开菜单不自动访问用量页面。

### 模型与 Responses 预览限制

SIWC 目录接受 `models` 或 `data` 数组、保留服务器排序，不列 `visibility=hide` 的模型。
目录没有报告容量时明确返回未报告，不猜上下文窗口；可在账号管理中手动填写型号与容量。
旧 Codex 目录继续携带原 `client_version`，不混用两条目录。

SIWC 推理只使用公开 `/responses` 的 HTTP/SSE，每次携带完整 `input` 数组；system 指令转为 `instructions`。
省略 `temperature`、`top_p`、`max_output_tokens`、`truncation`、`user`、`metadata`、`max_tool_calls`、
`background`、`conversation`、`prompt`、`prompt_cache_retention`、`safety_identifier`、`top_logprobs`、
`moderation`、`multi_agent` 和 HTTP 不支持的 `previous_response_id`。函数工具归入 `my_agent` 命名空间；
不把本地执行工具误报成托管工具，不对 SIWC 声明旧 Codex 专用远端压缩能力。

只在收到 `response.completed` 后认定推理成功。`subscription_sharing_usage_limit_exceeded` 按用量限制提示，
`subscription_sharing_usage_unavailable` / `subscription_sharing_user_unavailable` 按暂时不可用处理；
其它 `subscription_sharing_*`、`chatpass_v2_*` 保留结构化错误码，不靠消息里的 quota/token 等单词误分类。

### 本轮证据及适用边界

`test_gap_w11_openai_siwc_compliance.py` 使用真实回环 HTTP/JWKS、RSA 签名与私有档案覆盖登记、身份校验、
授权范围、轮换刷新、撤销、迟到 CAS、目录和回调监听器释放，并检查 Responses 最终请求信封。
回调连接读写期限为 2 秒，同一监听最多登记 16 条活动连接；取消时先终止这些连接，再等待请求线程退出，
正常请求与半开请求均核对 FD 释放。
它与原七个 OAuth/TUI/失败分类文件共同验收；回环协议结果不代表已通过真实 OpenAI 网页、真实账号套餐或外部模型调用。
下文带日期的 Codex 实测属于旧协议历史，不是新 SIWC 真实账号验证。

官方依据（2026-11-04 生效条款及本轮保存的文档）：
[条款](https://openai.com/zh-Hans-CN/policies/sign-in-with-chatgpt-terms/)、
[OSS 总览](https://developers.openai.com/siwc/token-sharing-open-source.md)、
[登记与登录](https://developers.openai.com/siwc/token-sharing-open-source/sign-in.md)、
[账号与会话](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions.md)、
[模型与推理](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference.md)、
[预览限制](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations.md)、
[错误恢复](https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery.md)、
[UI/UX](https://developers.openai.com/siwc/ui-ux-guidelines.md)。

## 支持范围

新账号在 `/model → 新增模型 → 登录账号` 登录，已有账号在 `/model → 管理已有模型` 里重新登录、添加模型、退出或删除；模型目录仍是 provider/model v2。
本功能为显式启用：打开菜单不登录、不调用模型、不自动选择任何型号。

| 方式 | 配置和行为 | 边界 |
|---|---|---|
| ChatGPT 套餐登录 | 新账号使用上述官方 SIWC 浏览器登记；旧 Codex 档案保留原浏览器/设备码协议。登录后列出所选账号的模型，再由用户添加或切换 | 具体权益由服务商决定，不能把套餐凭据跨用户共享；不是普通 API 余额 |
| 通用 OAuth 2.0 | 填写模型 API 地址、Client ID、Device Authorization URL、Token URL，以及可选 Scope、Audience、Client Secret | 首版为 RFC 8628 设备码授权；客户端密钥用 form body；不支持仅授权码/PKCE、Basic client authentication、网页登录密码或任意 Cookie 导入 |

填写后明确保存。编辑 Client Secret 留空保留旧值，勾选清除才删除；取消不保存草稿。
等待页提供确认地址、短期用户验证码和取消按钮。Esc 关闭此页并撤销本次登录请求，不停止代理或 Goal。
已保存服务商不会因取消网页登录而删除；先前有效登录仍保留。退出账号另需确认，并删除访问与刷新令牌。
设备码启动失败会清理本次等待标记，不影响此前有效凭据；退出或改参数后立即刷新当前模型的可用性提示。
通用认证参数可再次编辑；改变端点、客户参数或 Scope 会撤销原登录，不能把原凭据转发到新地址。

## 浏览器登录（ChatGPT 订阅）

TUI 浏览器登录复用授权码 + PKCE(S256) 主链；新 SIWC 与旧 Codex 的端点和身份各按已保存档案选择，
不得混用。登录中不用复制网址或验证码：

1. TUI 优先在 `127.0.0.1:1455` 临时监听，被占用就用 `1457`，监听只在登录等待框存续期间开着；其它可用端口只由已登记的 SIWC 档案接受。监听采用内核实际绑定的地址与端口，不需要反向 DNS。
2. TUI 请 Gateway 执行 `auth_browser_start`：Gateway 生成 state 与 PKCE 校验码，校验码只写 owner 私有 pending，
   回执只含官方授权页地址与 state。回调地址逐字比对登记值。
3. TUI 用系统默认浏览器打开授权页。用户登录并同意后，浏览器跳回本机回调；回调只收 state 与本次一致的第一次请求，并保留 SIWC 已签发 Client ID，
   给浏览器回一个中文提示页，不写访问日志（请求行含授权码）。
4. TUI 把授权码与 state 交给 Gateway 执行 `auth_browser_complete`：锁内核对请求编号、期限（10 分钟）与 state（常量时间比较，
   不符不清等待状态，伪造回调打断不了真实登录），锁外兑换一次令牌（不重试），再按配置绑定与请求编号 CAS 提交。
5. 取消、Esc、超时或改用验证码都会关掉本机监听并撤销本次等待状态；迟到的回调不能恢复登录。
6. 登录成功（按结构化的 connected，不看提示文字）后直接弹出这个账号可用模型的勾选框，见下节「订阅账号的模型目录」。

SSH 会话、Linux 没有图形会话或用户选择验证码时，旧 Codex 档案仍可走其原设备码入口；新 SIWC 会明确要求本人浏览器授权，
不会借用旧应用的设备码身份。通用 OAuth 服务商仍只支持设备码；IM 没有登录入口。

## 唯一存储和权限

- 认证配置及令牌仅在宿主 `config/model-profiles/<owner 身份摘要>.json` 的 `providers[id].auth` 中。
  使用现有锁和原子写，目录 0700、文件 0600；没有第二份 token 文件、数据库或聊天记忆。
- 对外模型列表只返回认证类型与登录状态；编辑仅回显无秘密参数。网页用户码仅在授权页短期显示。
  access/refresh/device token、Client Secret、账号编号和存储路径不返回普通模型列表。
- 不读取其他应用的登录文件，不代替用户批准网页授权；退出不会退出其他应用或其他用户。
- OAuth 不得发布成跨用户共享模型。同 owner 的不同会话可以分别选择不同配置，子代理仍可按原模型引用继承或显式选择。
- 工作片只保存配置绑定、授权代次及私有位置引用，不复制 token。请求前核对 owner 文件、启用状态、端点与授权代次。
  退出、重新登录或变更端点使旧引用失效；已经发出的网络请求不可撤回，但后续请求不会再使用旧账号。

## 网络与并发

认证端点及令牌目的地必须 HTTPS，HTTP 仅允许本机回环测试。认证和带 token 的模型请求均不跟随重定向。
设备码按服务商 interval 轮询；`authorization_pending`、`slow_down`、到期和拒绝按结构化协议处理。
验证码只在当前模态页轮询，不常驻后台。登录回调以请求编号和配置绑定 CAS 写回，取消后的迟到结果不能恢复账号。

请求即将发送时，过期凭据用同 owner/provider 的原刷新互斥锁串行刷新；模型档案锁不跨网络等待，刷新互斥锁仍覆盖当次刷新。
refresh token 轮换原子保存，退出与刷新竞态再次核验授权代次。失败明确报错，不回退 API Key 或别的模型。
令牌刷新不是模型调用，不计入模型轮次或 token 消耗。

## 原模型链路复用

通用 OAuth Bearer 复用 Chat、Messages 或 Responses 后端；不另建 Agent、工具循环或 Compact。
新 SIWC 使用公开 Responses HTTP/SSE；旧 Codex 档案继续使用其专用端点和账号头。两者均流式请求及 `store=false`；system 指令转到 `instructions`，
不发送该接口不接受的 `max_output_tokens`。请求体在绑定宿主会话时带 `prompt_cache_key`（会话级提示缓存键，值与
`provider_session_scope` 的会话编号一致，参考官方 Codex 的做法，让服务商把同一会话路由到同一份提示缓存）；
未绑定会话时不带，且绝不生成随机键。订阅模式另带 `session-id` 请求头（ChatGPT 后端按它决定缓存亲和），值与
`prompt_cache_key` 相同，都是会话编号前 128 位排成的 UUID（官方 Codex 根代理两者同值）；WebSocket 握手沿用同一份请求头。通用服务商可按原显式目录发现操作读取 `/v1/models`。

### 订阅接口改走 WebSocket（2026-09-30）

- **现象**：切到 GPT 后 Compact 摘要十几分钟停在"生成摘要"；日志两次 `Compact 摘要响应不可用` 都是 `stop_reason=stream_eof`。
- **根因（实测）**：订阅接口的普通 SSE 流在长输出时服务端中途停止发送，约 60 秒后连接被干净关闭，没有终态事件。
  gpt-6-luna 长输出/长输入 4 次 SSE 全部在 33～98 秒处停住（只拿到 443～3153 字）；同一代理下 DeepSeek（OpenCode）长输出 9210 字 43.8 秒正常，
  所以不是本机代理问题。服务商目录对这些模型都声明 `prefer_websockets: true`，官方命令行（Codex）默认也走 Responses WebSocket。
  同一请求改走 WebSocket：长输出 9235 字、长输入（10.9 万 tokens）摘要 1.29 万字都完整收到 `response.completed`。
- **做法**：`backends/responses_websocket.py` 用同一个请求信封（地址、认证头、请求体）连 `wss://…/responses`，头加
  `OpenAI-Beta: responses_websockets=2026-02-06`，发 `{"type": "response.create", …}`，逐条产出与 SSE 相同的事件文本交原
  `collect_response`；超时（首事件/滚动空闲）、/stop、握手失败的错误分类与重试、观察事件口径都与 SSE 相同，回复完成前断开抛可恢复错误。
  只有登录模式 `chatgpt` 的 Responses 走它（`OAuthResponsesBackend.request_stream_iter`），其它服务商与登录模式仍走 SSE。不固定模型、不换模型。
- **心跳与断开诊断（09-30 加固）**：审计 49 次 WebSocket 请求有 2 次在第 23–24 秒中途断开（握手 101 之后），都紧跟客户端第 20 秒的
  心跳 ping；官方 Codex 不主动发 ping，只回应服务端 ping。现在同样关闭客户端 ping（`ping_interval=None`，服务端 ping 仍由库自动回 pong），
  死连接由首事件/滚动空闲超时收口。断开错误文本附阶段（first_event / stream_idle）、连接后秒数和关闭帧（服务端或本端关闭码与原因，
  无关闭帧时写明），只作排查，不参与判定。中途断开照旧是可恢复错误：主回合由模型回合级退避重试；Compact 摘要请求也按同一退避配置
  原地重发（见[对话上下文 · 摘要请求的瞬时错误重发](CONVERSATION_CONTEXT_DESIGN.md#摘要请求的瞬时错误重发2026-09-30)）。
- **失败事件按错误码分类（10-01）**：SSE 与 WebSocket 共用的 `collect_response` 遇到 `error`／`response.failed` 时，交
  `backends/responses_failure.failed_event_error` 读服务商错误对象（`response.error`，或 error 事件的 error／顶层 code、message、
  param，限长），与 HTTP 同一组判定：上下文超限 → `ProviderContextWindowError`（主循环压缩后重试），硬额度 → 额度用完，限流 →
  限流，服务端繁忙／内部错误 → 临时故障，其余 → `MODEL_RESPONSE_FAILED` 并在诊断 details 里保留服务商错误码与消息。此前一律报
  “返回失败事件”，原因整个丢掉，上下文超限也走不到压缩恢复。
- **依赖**：直接使用 `websockets`（BSD-3-Clause）。它原已随 `lark-oapi`（飞书长连接）安装，现在 pyproject 显式声明。
  收益是与服务商推荐的传输一致、长回复不断线；替代方案（标准库自写 RFC 6455 客户端）要自己处理握手、掩码、分片、ping 和代理 CONNECT，
  代码量与风险更大，不采用。
- **握手超时交回合层退避（2026-10-03）**：生产上出现过 8 次整轮失败，全是 chatgpt.com 的握手超时（TLS 握手 `_ssl.c` 超时、升级握手
  `timed out while waiting for handshake response`），其中 6 次挤在 25 秒内，是一次网络抖动；这些回合已经跑了十几到三十几轮工具，
  一次握手超时就整轮失败。原因是对照路径不一致：SSE 把连接阶段超时归 `first_event`，回合层会退避重试；WebSocket 少了这一步，
  落成 `provider_declared`，而回合层明确不放行这个 stage（它也可能表示“请求已发出后读超时”，重放会让服务商可能已处理的请求被重发）。
  现在 `_handshake_error` 里无状态码的失败先判超时，是超时就归 `first_event`，复用 `gateway_helpers._is_timeout_exception`；
  握手阶段连接还没打开、`response.create` 还没发出，所以不存在重放已发出的采样请求。`_is_transient_network_error`（所有 HTTP 共用）、
  回合层放行集合、连接打开后的 first_event / stream_idle 分类、非超时握手失败分类、握手中用户停止优先，都保持不变。
- **握手超时的诊断字段（ds2p，2026-10-03）**：归成 `first_event` 的代价是账本里“握手没连上”和“连上了但首个事件超时”不可区分（be 这次
  诊断就是靠 `timeout_stage` 才认出握手）。补一个**只用于诊断**的 `timeout_wait_phase`：握手超时标 `handshake`，其余路径留空串；
  值集合 `TIMEOUT_WAIT_PHASES` 与 `TIMEOUT_STAGES` 分开、独立封闭，异常构造与账本写入两侧都 fail-closed。
  `timeout_stage` 的封闭集合、回合层放行规则和修法 A 的行为一字未改；账本只多一列，`phase` 绝不回写 `stage`。

### 回合放弃时的 Responses 传输回收（2026-10-03）

- `tool_model_generation._wait_for_generation_result` 的 `wall_clock` 分支现在先按原合同结束模型调用账本，再对准确 provider worker 置取消标记并按已有 1 秒 drain 上限等待；不改一次性 wall-clock 重试或回合自动续跑规则。
- SSE 读流复用 `_GatewayResponseGuard.abort` shutdown 当前 socket；Responses WebSocket 的 `response.create` 发送复用本请求首事件预算，计时器到期直接 `close_socket()` 唤醒同步 `sendall`。用户停止、超时或异常直接 abort；只在收到终态事件后走既有 `close_timeout=5` 正常握手。
- 首事件与首事件后的滚动空闲沿用 SSE/WS 原期限与分类；在途调用结束后由原账本终态自然退出 `/status` 数字统计。`/status` 新增在途数和最老年龄，来自原账本只读快照，不返回模型名、请求/回合编号或正文。
- 连接/握手阶段使用既有 `connect_timeout` / `open_timeout`，SSE 响应头/首事件与 WS 首事件使用原首读预算，流内按 `request.timeout` 滚动；WebSocket 正常 close 最多 5 秒。标准库同步系统 DNS `getaddrinfo` 本身不能被 socket timeout 或取消回调强制终止，是已知边界；本地测试没有模拟系统解析器永久卡住。
- 只在随机 loopback 假 WS/SSE 与临时目录验证；没有连接真实模型、启动 Gateway 或验证真实客户端。具体等待点表与测试命令见 `DESIGN_LEDGER.md` 顶部及 `TESTS.md` 本条。

### 订阅接口的流式输出（2026-09-30 热修）

- 订阅接口的 `response.completed` 不回带 `output`（实测为 `[]`），函数调用和正文条目只在流里逐条的 `response.output_item.done` 给出。
  原先只读终态 output，工具能力检查判成"不支持原生工具"（`TOOL_PROTOCOL_CAPABILITY_UNAVAILABLE`），正文也是空的。
- `responses_wire.collect_response` 改为：终态 output 为空时用逐条 `output_item.done` 的完整条目；终态有 output 仍以它为准。
  这是 Responses 流协议层的通用处理，不按服务商地址分支。09-30 用真实订阅账号复验：gpt-6.1-sol、gpt-5.6-sol 能力检查通过，短问候有正文。

### 订阅账号的模型目录（2026-09-30）

- 本节为旧 Codex 档案的目录合同；新 SIWC 走上文公开 `/v1/models`。两者都不跟随重定向，一次 GET，不调用模型。
- 该接口必须带 `client_version`，并按它筛掉"需要更新客户端"的模型。本产品只用普通 Responses 调用、不用官方命令行的专用
  工具协议，声明版本 `1.0.0`（`CHATGPT_CATALOG_CLIENT_VERSION`）；它只决定列出哪些模型，不是身份凭据，也不影响计费。
- 只列服务商标为可见（`visibility` 不是 `hide`）且上下文容量合法的条目；给出模型名、显示名与容量，接口固定 `openai_responses`。
- TUI 勾选框只列这个账号还没添加的模型；勾中的经 `add_models`（`provider_id` + `openai_responses`）一次保存，全成或全不写
  （容量用目录值，其它字段默认），「返回」不写入；不预选、不自动切换当前会话。`/model → 管理已有模型 → 这个账号 → 添加模型`
  走同一个勾选框。09-30 改版前是逐个 `save_model`，一个失败不影响其它；改成整批后失败原因一次说清，重试由服务端按同名跳过。

## 验证与未覆盖

定向覆盖 owner 隔离、权限模式、过期刷新、并发刷新、取消/退出迟到回调、参数改变、禁止共享、
受保护头、真实回环 HTTP 重定向拒绝以及原 Responses 流映射。替身授权不等于真实订阅验收。
发布验收另需真实 TUI 的新增、取消、参数编辑、登录、模型选择及短调用；用户未在官方网页确认前，
只能报告入口和协议测试状态，不能报告其 Plus/Pro 账号已可用。上游权益或区域限制不能由本程序绕过。

此前真实 TUI 已完成回环测试服务的新增、取消、重新登录、参数回显编辑、短连接测试、自动刷新和退出。
测试服务不支持工具，进入普通任务被能力检查拒绝是预期结果，不算真实模型任务通过。
官方订阅设备码入口在测试环境返回 HTTP 403，未获得验证码、未进入账号确认；不能据此判断用户套餐或安全设置。

### 2026-09-19 复验与参考边界

本轮先核对 OpenClaw `9cf12734e6` 的 OpenAI 授权入口与 PKCE state/回调清理，以及 Hermes
`0a62610f1` 的设备码轮询、限流和刷新错误分类。它们提供流程对照；本项目仍保留设备码主链、owner 私有
存储、取消/退出 CAS 和禁止重定向，不移植其他应用凭据读取或另一套认证状态。

实际 TUI 验证了新增草稿取消、通用端点的非法 HTTP 拒绝、私密输入遮罩和取消后草稿未保存。
官方设备码启动在本机返回 HTTP 530、指定测试机返回 HTTP 403，均发生在产生验证码之前。
测试机使用其现有版本，只作网络条件对照，未部署候选代码，也未调用其他 LLM。
失败后主会话仍能使用原官方 MiniMax-M2.7 完成普通问答；认证表单没有替换会话模型。
本机私有配置文件权限为 0600，本次临时账号无 access/refresh token；原始记录留在仓库外。

新增与现有 Computer Use / OAuth / provider 定向共 103 项通过。真实账号确认、令牌刷新和已登录退出
仍未完成；须待设备码端点可达并由用户完成官方页面确认，不能用回环服务或旧日志替代这一步。

收尾再次从两处真实 TUI 新建独立临时服务商并发起官方设备码请求，仍分别返回 HTTP 530 / 403。
本机失败后没有 access/refresh token 或残留 pending；两处临时配置随后经 TUI 删除。
设备码地址及 JSON 请求形状与已核对的参考源码一致；当前证据不能进一步区分具体网络或上游拒绝原因。
当前重构批次与本机桌面验收已完成，完整认证验收受阻；需可达的测试环境和用户官方网页确认后才能继续。

### 2026-09-30 浏览器登录

定向测试 `test_model_oauth_browser.py`（开始/兑换/state 与编号不符/超时/兑换中取消或退出/回调地址与服务商限制/PKCE 与兑换表单/
等待状态校验）和 `test_tui_browser_login.py`（本机回调只收本次登录且不打印授权码、端口回退、SSH/无图形界面判断、
TUI 全流程自动完成、Esc 取消并关闭监听、端口被占退回设备码），15 个变异全部被杀。

网络事实（本机，经 127.0.0.1:7890 代理）：设备码接口 GET 回 405，已可达，不再是 09-19 的 530/403；官方授权页对脚本客户端是
Cloudflare 浏览器挑战（`cf-mitigated: challenge`），只能在真实浏览器里通过，无法用脚本预验参数；换令牌接口可达（无效授权码回 401）。
真实账号验收（09-30，step16n）：用户在 TUI 里用浏览器登录成功，令牌、刷新令牌与账号编号已保存，有效期约 10 天。
随后用该账号只读实测订阅目录：不带 `client_version` 回 400（字段必填）；`0.124.0` 只列 1 个模型，`0.155.0` 列 8 个，
`0.160.0` 与 `1.0.0` 列全部 9 个可见模型（上下文均为 272000），另有 2 个隐藏模型。勾选添加与短调用待下一版上线后验收。

参考：[设备码标准](https://www.rfc-editor.org/rfc/rfc8628.html)、[PKCE 标准](https://www.rfc-editor.org/rfc/rfc7636.html)、
[官方账号认证说明](https://learn.chatgpt.com/docs/auth)。
