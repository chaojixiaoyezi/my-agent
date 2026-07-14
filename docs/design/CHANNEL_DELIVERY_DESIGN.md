# Channel Delivery Design

## 目标

普通用户只通过飞书等 IM 与自己的 Agent 交流。无论回复来自普通聊天、长任务回送、后台主动汇报，
还是模型显式调用 `send_message` 发送文件，都必须经过同一个外部投递出口。

这层解决四个问题：

1. 模型不能决定收件人，避免把消息发给任意用户。
2. 内部完成协议不能原样出现在用户聊天中。
3. 新增 IM 时不复制 Agent 业务逻辑，只增加薄 adapter 和注册事实。
4. 文本、引用回复、图片、文件、处理中状态和回执使用同一套结构化合同。

## 四个核心对象

### `DeliveryContext`

可信投递上下文，由入站 adapter、owner 配置或会话绑定构造。它包含：

- `channel`：使用哪个 IM；
- `target`：当前 owner 在该 IM 的真实地址；
- `mode`：`reply` 或 `proactive`；
- `conversation_id`、`reply_to`、`progress_handle`：回复原消息和结束处理中状态所需上下文；
- `request_id`、`thread_id`、`task_id`：审计和回执关联字段。

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
3. closeout 在替换模型最终答复前先把自然最终说明保存为非权威 `user_summary`；显式验收工具轮可用其
   `summary/note` 覆盖。所有用户正文经过 `project_user_reply`，完成协议只取该结构化摘要和已验产物名，
   拒绝内部 token，并把宿主绝对路径降成 basename；
4. 检查注册能力和目标地址合同；
5. 从 registry 解析 adapter；
6. `reply` 调 `finalize_response`，`proactive` 调 `send_message`；
7. typed attachment 调原生 `send_image` / `send_file`；
8. 返回 `DeliveryReceipt`，异常转稳定错误码且不向调度器抛出。

## 两条现有路径如何收敛

### 普通最终回复

`ChannelManager` 保存入站消息的 channel、user、message、conversation 和 typing handle。Gateway 完成后，
它构造可信 `DeliveryContext(mode="reply")` 和无目标 `ReplyEnvelope`，交给 `DeliveryService`。

Feishu adapter 的 `finalize_response` 仍会先撤掉 reaction，再引用回复用户原消息；其他 adapter 的默认
实现继续调用自己的 `send_message`。统一服务没有抹掉 provider 原生体验。

### `send_message` 工具和后台主动消息

工具的收件人只从 scoped owner 取得。附件仍必须命中 task artifact registry，并重新验证 owner 边界、
ready 状态和 SHA-256。校验完成后构造 `DeliveryContext(mode="proactive")` 与 `ReplyEnvelope`。

后台主代理使用会话的结构化 channel binding 构造同样的上下文和信封。纯 internal 路由没有注册的
proactive 能力，因此只回 `not_applicable`，不会误发。

## 幂等和重试边界

`DeliveryService` 负责一次发送的统一动作和回执，不自行重跑模型：

- 普通长任务由 `GatewayReplyDeliveryWorker` 的 pending/sent receipt 保证重启后继续同一 request；
- 显式 `send_message` 由 owner 内的持久化 receipt 绑定 request/run/tool call/content/artifact hash；
- adapter 明确失败或异常返回结构化错误，外层按既有退避合同处理。

这个分工避免在通用发送层再造第二套任务队列，也避免“回送失败”被误处理成“重新做一遍任务”。

## 新增一个 IM 需要做什么

不需要改 Agent 对话、任务、记忆或工具主链，但仍需要一层平台 adapter，因为各 IM 的鉴权、消息格式、
上传接口和引用回复 API 不同。

接入步骤：

1. 实现 `BaseChannelAdapter.send_message`，内部调用该 IM 的官方接口；
2. 如果支持引用/更新回复，实现 `finalize_response`；
3. 如果支持媒体，实现 `send_image` / `send_file`；
4. 注册 capabilities 和 target validator；
5. 入站 adapter 将平台事件转换成统一 `IncomingMessage`；
6. 运行 registry 契约、普通 reply、proactive、媒体、幂等和隔离测试。

平台 API 改动只落在 adapter 内。`DeliveryService` 不新增 `if channel == ...`，`send_message` 工具也不新增
平台专用变体。

## 参考取舍

- 通道运行时：复用 per-run 当前 channel/target 的可信上下文、typed reply/media、provider plugin 和
  dispatcher，以及“保留最终文本、清洗内部脚手架”的出口边界；没有复制它的大量 action 枚举。
- 长期助手：复用一个通用 `send_message` 路由多 adapter 的入口，以及“执行输出与最终答复分离”的边界；
  没有采用 `MEDIA:path` 正文标记，也没有放宽任意 target。

当前实现选择 通道运行时 式的“可信上下文与模型回复分离”，再保留 长期助手 式的“一个通用消息工具”。

## 当前证据和边界

- 普通 `ChannelManager` 最终回复、后台主动消息和显式 `send_message` 已接入同一服务。
- Feishu 引用回复、typing 收口、文本、图片和文件接口保持原生调用。
- 契约测试证明第二个 fake IM 只注册 adapter/capabilities 即可发送，未修改投递服务。
- 当前内置主动出站工厂仍只有 Feishu；第二个 fake IM 不是生产平台可用性证明。
- 1.10 真实 Feishu 主动文本 API 已返回成功；真实客户端新入站、引用回复和附件仍需继续复验。
