# Channel Delivery Design

## 目标

普通用户只通过飞书等 IM 与自己的 Agent 交流。无论回复来自普通聊天、长任务回送、后台主动汇报，
还是模型显式调用 `send_message` 发送文件，都必须经过同一个外部投递出口。

这层解决四个问题：

1. 模型不能决定收件人，避免把消息发给任意用户。
2. 内部完成协议不能原样出现在用户聊天中。
3. 新增 IM 时不复制 Agent 业务逻辑，只增加薄 adapter 和注册事实。
4. 文本、引用回复、图片、文件和运行状态使用同一套结构化合同；普通用户可见措辞仍由模型生成。

## 四个核心对象

### `DeliveryContext`

可信投递上下文，由入站 adapter、owner 配置或会话绑定构造。它包含：

- `channel`：使用哪个 IM；
- `target`：当前 owner 在该 IM 的真实地址；
- `mode`：`reply` 或 `proactive`；
- `conversation_id`、`reply_to`、`progress_handle`：回复原消息和结束处理中状态所需上下文；
- `request_id`、`thread_id`、`task_id`：审计和回执关联字段；
- `idempotency_key`：程序从可信请求事实生成的稳定投递身份，模型和回复信封无权覆盖。

模型工具 schema 不接收这些字段，模型正文也不能覆盖它们。

### `ReplyEnvelope`

平台无关的回复信封，只包含：

- 用户可见正文；
- 已通过上游 owner/registry/path/hash 校验的 typed attachments；
- 展示格式。

信封没有 `target`。即使模型能影响正文和附件选择，也不能借此改变收件人。

### `ChannelAdapterRegistry`

注册表保存 `channel -> adapter/factory/capabilities/target validator`：

- 已启动的入站 adapter 可直接注册实例；
- 只做主动出站时可注册懒工厂；
- capabilities 明确声明 text、reply、proactive、files、images；
- target validator 声明该平台地址类型，例如 Feishu `open_id` 必须以 `ou_` 开头。

未知通道没有隐含能力，也不会回落到 Feishu 或其他默认平台。懒工厂失败会缓存 `None`，避免缺凭据时
反复构建和重复打日志。

### `DeliveryService`

唯一外部投递 chokepoint。固定顺序为：

1. 校验 `mode`；
2. 主动消息拦截内部运行协议；
3. 最终正文直接采用主模型基于当前对话、工具结果和任务事实写出的自然答复，不再经过独立目录验收器、
   完成 marker 或第二次摘要重写。派工或 wait 的短回执只获得结构化 lifecycle facts，并清空旧工具 IR、
   工具正文、运行注入和交付合同，避免一句回执重放整段工具历史。所有正文仍经过
   `project_user_reply`，内部 token 被拒绝，宿主绝对路径降成 basename；任务状态只保存在 typed
   ledger/event，不从自然语言反推，也不投影成“正在处理”模板；
4. 检查注册能力和目标地址合同；
5. 从 registry 解析 adapter；
6. `reply` 调 `finalize_response`，`proactive` 调 `send_message`；
7. typed attachment 调原生 `send_image` / `send_file`，并为每个附件派生独立稳定去重键；
8. 返回 `DeliveryReceipt`，异常转稳定错误码且不向调度器抛出。

## 两条现有路径如何收敛

### 普通最终回复

`ChannelManager` 保存入站消息的 channel、user、message、conversation 和 typing handle。Gateway 完成后，
它构造可信 `DeliveryContext(mode="reply")` 和无目标 `ReplyEnvelope`，交给 `DeliveryService`。

Feishu adapter 的 `finalize_response` 仍会先撤掉 reaction，再引用回复用户原消息；其他 adapter 的默认
实现继续调用自己的 `send_message`。统一服务没有抹掉 provider 原生体验。

### 长任务中的模型原话与工具进度

Gateway 将三类内容分栏：真实 model delta、typed tool progress 和 durable final reply。第一次工具开始
前已经形成的模型正文可投影成一条 `assistant_commentary`，让用户尽早看到 Agent 自己说出的下一步；
runtime/provider notice 仍是内部事件，不能冒充模型文字。工具过程继续服从 per-thread
`/verbose off|on|full`，commentary 不携带工具名、命令或输出。

commentary 只影响展示，不结束 request，也不抑制最终回复。它投递失败时前移 progress cursor，避免坏
通道被重复轰炸；最终回复仍使用独立 pending/sent receipt，按原耐久链恢复。新增 IM 只需按 adapter 的
普通 text/reply 能力接收这类平台无关正文，不需要实现 my-agent 专用 commentary API。

### `send_message` 工具和后台主动消息

工具的收件人只从 scoped owner 取得。附件仍必须命中 task artifact registry，并重新验证 owner 边界、
ready 状态和 SHA-256。校验完成后构造 `DeliveryContext(mode="proactive")` 与 `ReplyEnvelope`。

后台主代理使用会话的结构化 channel binding 构造同样的上下文和信封。纯 internal 路由没有注册的
proactive 能力，因此只回 `not_applicable`，不会误发。

## 幂等和重试边界

`DeliveryService` 负责一次发送的统一动作和回执，不自行重跑模型：

- 普通长任务由 `GatewayReplyDeliveryWorker` 的 pending/sent receipt 保证重启后继续同一 request；
- 显式 `send_message` 使用通用 tool operation 账本，业务键由
  `owner + provider + target + request + content + artifact refs` 的可信结构化事实生成；同一请求即使
  模型换了 call id，也只重放首份结果；
- 副作用超时或 adapter 已经开始发送后失去终态时，operation 进入 `unknown`，不会按 `retryable`
  标记盲目再发。只有目标系统的只读核对器带 `source_ref` 明确确认 succeeded、failed 或
  not_started，才可收口或原子重开同一 operation；
- Feishu 文本、引用回复、长消息分片和附件消息使用官方 `uuid` 字段。同一分片的传输重试复用同一个
  UUID，不同分片使用不同 UUID；没有 UUID 的请求和 4xx 明确错误不自动重试；
- commentary 和工具进度按 cursor 至多投递一次；它们失败不得拖住或重跑最终回复。

这个分工避免在通用发送层再造第二套任务队列，也避免“回送失败”被误处理成“重新做一遍任务”。如果
目标平台既没有原生去重键，也没有查询操作结果的接口，超时后的正确状态就是保持 unknown 并交给用户或
管理员决定，而不是冒险重复副作用。

## 新增一个 IM 需要做什么

不需要改 Agent 对话、任务、记忆或工具主链，但仍需要一层平台 adapter，因为各 IM 的鉴权、消息格式、
上传接口和引用回复 API 不同。

接入步骤：

1. 实现 `BaseChannelAdapter.send_message`，内部调用该 IM 的官方接口；
2. 如果支持引用/更新回复，实现 `finalize_response`；
3. 如果支持媒体，实现 `send_image` / `send_file`，接收 DeliveryService 传入的稳定
   `idempotency_key`；
4. 注册 capabilities 和 target validator；
5. 入站 adapter 将平台事件转换成统一 `IncomingMessage`；
6. 运行 registry 契约、普通 reply、proactive、媒体、幂等和隔离测试。

平台 API 改动只落在 adapter 内。`DeliveryService` 不新增 `if channel == ...`，`send_message` 工具也不新增
平台专用变体。

## 参考取舍

- 通道运行时：复用 per-run 当前 channel/target 的可信上下文、typed reply/media、provider plugin 和
  dispatcher，以及“保留最终文本、清洗内部脚手架”的出口边界；没有复制它的大量 action 枚举。
- 长期助手：复用一个通用 `send_message` 路由多 adapter 的入口、Feishu SDK 的 `uuid` 能力，以及
  “执行输出与最终答复分离”的边界；没有照搬其每次底层重试重新生成 UUID 的实现，也没有采用
  `MEDIA:path` 正文标记或放宽任意 target。

本轮再次核对了 `通道运行时_contract_code_files.xlsx`、`src/auto-reply/reply/agent-runner-payloads.ts`、
`src/auto-reply/reply/completion-delivery-policy.ts`、`src/agents/subagent-announce-delivery.ts`，以及
`长期助手_contract_code_files.xlsx`、长期助手 `gateway/stream_dispatch.py`、`gateway/stream_consumer.py`、
`gateway/delivery.py`。可复用的共同点是：模型正文、工具/typing 进度和投递结果分别承载；缺少可见模型
回复应成为结构化失败或静默，不由通道层编造一句“仍在运行”。

当前实现选择 通道运行时 式的“可信上下文与模型回复分离”，再保留 长期助手 式的“一个通用消息工具”。

## 当前证据和边界

- 普通 `ChannelManager` 最终回复、后台主动消息和显式 `send_message` 已接入同一服务。
- Feishu 引用回复、typing 收口、文本、图片和文件接口保持原生调用。
- 契约测试证明第二个 fake IM 只注册 adapter/capabilities 即可发送，未修改投递服务。
- 当前内置主动出站工厂仍只有 Feishu；第二个 fake IM 不是生产平台可用性证明。
- 1.10 真实 Feishu 主动文本 API 已返回成功；真实客户端新入站、引用回复和附件仍需继续复验。
- typed `assistant_commentary` 候选已通过 sanitization、runtime notice 隔离、顺序与失败不阻塞最终回复的
  回归；尚未部署，因此还不是 1.10 的生产证明。
