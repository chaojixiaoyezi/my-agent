# N02 飞书入站受理与反馈修复交接

## 基本信息

- workstream：N02，2026-10-14；父任务统一集成、提交、打包。
- 基线：`5142efb0a823191304742078946f118a799897e5`，共享工作树；本线逐文件独占，不操作生产或真实飞书。
- 目标：暂时失败不能提前完成 WS 去重；换行不影响可信提及；没表情与没受理可分辨。

## 参考与现场边界

- 先查 `openclaw_contract_code_files.xlsx`（Feishu policy/通道合同）、`hermes_contract_code_files.xlsx`（无对应精确行）。
- 读 OpenClaw `extensions/feishu/src/dedup.ts`、`monitor.message-handler.ts`：claim/commit/release 与 retryable 分开；采用这个边界，未复制其第二持久去重状态。
- 读 Hermes `plugins/platforms/feishu/adapter.py`：结构化 mention ID 归一；其现有前置 seen 模式不能解决本次暂时失败，不照搬。
- 核对官方 Python SDK `lark_oapi/ws/client.py` 的 `_handle_data_frame`：消费者异常会生成 HTTP 500 形状的 WS ACK；因此本线 retryable 必须向 SDK 抛安全异常，不能吞掉。
- 官方接收事件明确按 `message_id` 去重：[接收消息](https://open.feishu.cn/document/server-docs/im-v1/message/events/receive.md)。
- 官方表情表列出 `Typing`、`OnIt`、`OneSecond`；原随机 26 项中 `Thinking` 未列出：[表情文案](https://open.feishu.cn/document/server-docs/im-v1/message-reaction/emojis-introduce.md)。固定 `Typing` 不代表应用权限或实际请求已成功。
- [添加表情接口](https://open.feishu.cn/document/server-docs/im-v1/message-reaction/create.md)列出 HTTP 400 下不同数值原因；不能把用户此前 `Typing HTTP400` 直接解释成表情名称错误。
- 仓库外 66 条合成对照：同行/独占行 @ × 有/无“你看看”，正常富文本均受理并保留清单。没有现场 message_id/payload，不能确认用户首条消息的根因。

## 实际完成

1. 原飞书入站返回 `FeishuEventDisposition`：`accepted / ignored / retryable`，不是模型完成或对外投递状态。
2. WS 复用原 bounded seen，加锁领取 inflight；仅 accepted 或稳定 ignored 提交，异常/暂时失败释放。并发未完成返回失败 ACK，不能当作 done。
3. Webhook 仍先签名核验；暂时失败返回 503，稳定受理/不触发返回 200。没有新入站账、队列或重放业务入口。
4. 主消费者的 False 原样传回运输；实际 CLI `adapter.on_message(lambda msg: manager.route_message(msg))` 保留这个返回值。base 原 void 消费者接口继续正常消费，但 WS `on_payload` 必须返回 typed 回执，None 不算受理。
5. 顶层 mention 与 canonical post at 同用可信 open_id/key；post 内精确 bot ID 不因顶层数组非空而丢失。未知/缺失 identity 需要重投，@all、昵称和普通正文不授予触发或角色。
6. 富文本 bot at 的占位 key 也按已核验 mentions 移除，保留普通用户、代码块和原分行。
7. 原私聊密码门区分服务暂不可读与正常锁定：后者仍用原 unlock queue；服务异常不 commit seen，不允许绕过密码。
8. 表情固定为官方 `Typing`；HTTPError 只提取有限数值 code/http_status，关闭响应。失败仅返回空旧句柄，原 Gateway 请求和 watcher 不变。
9. 原 logger 投影消息/群短引用、类型、bot_known/提及门、ingress/submission、reaction emoji/http/code。未输出正文、显示名、秘密、API msg 或异常 str。

## 回执与诊断解释

| 状态 | 典型原因 | 去重行为 |
| --- | --- | --- |
| accepted | 原 ingress 已登记，或原 unlock queue 已保留消息 | commit 原 seen |
| ignored | GROUP_NOT_ADDRESSED、EVENT_INVALID、MESSAGE_EMPTY、UNLOCK_QUEUE_FULL | 稳定不触发，commit |
| retryable | BOT_IDENTITY_UNAVAILABLE、MENTION_UNVERIFIED、INGRESS_UNAVAILABLE、SESSION_LOCK_UNAVAILABLE、EVENT_INFLIGHT | release，供应商重投 |

原因均使用 `FEISHU_` 前缀协议标识；不是 ToolResult/ErrorContract，稳定 ignored/accepted 不能登记成工具错误。
`ingress_state=completed` 仅代表入口移交结束，不代表模型执行完成。`reaction failed` 仅代表反馈失败。
日志的 `message_ref/chat_ref/request_ref` 是诊断摘要，不能作为路由、权限或原消息编号使用。

## 改动文件

- `adapter/base.py`：原消费者返回值。
- `adapter/protocol.py`：typed 运输回执、安全原因标识与短引用。
- `adapter/feishu_mentions.py`：唯一 mention 裁决与首尾 key 规范化。
- `adapter/feishu.py`：共享受理、原认证门和 webhook ACK。
- `adapter/feishu_ws.py`：原 seen 的并发 claim/commit/release。
- `adapter/feishu_typing.py`：固定官方表情、有限 HTTP/code 诊断。
- `adapter/manager.py`：原 ingress 登记/提交日志，不修改模型等待或 progress draft。
- `tests/test_feishu_ingress_admission.py`、`test_adapter_feishu_ws.py`。

## 验证

- 旧因果先在仓库外真实 adapter/WS/manager 合成复现：bot 身份暂不可用后，同 message_id 重投仍被早 seen 丢弃；reaction 非 0 仍有原入站和 watcher。
- 新合同测试首先暴露缺少 typed 裁决接口；修复后新切片 23 项通过，随后补 `post at.user_id` 缺失不得稳定 ignored 的一项重投合同（合计 24）。
- 13 个 focused 文件合计 **279 项通过**，0 fail/error/skip，XML：`/tmp/my-agent-N02-focused.xml`。
- 最后把原登记/日志提到同文件小函数以守尺寸，随后直接入站/manager 的 3 文件 98 项回归通过，XML：`/tmp/my-agent-N02-last-focused.xml`；缺失 at 目标的 3 文件增量复验 **90 项通过**，另记 `/tmp/my-agent-N02-at-target.xml`。以上是不同范围的复验，不累计成一次测试数。
- 本线 Ruff、diff check 通过；尺寸报告只写 `/tmp/my-agent-N02-code-size.md`，未改 tracked 报告。整片最终严格 gate 由父任务冻结后统一运行。
- 覆盖：WS 暂时失败同 ID 恢复、并发只派发一次、callback 落盘后抛异常重投只保留一个原 ingress/一次提交、webhook 503→200、@all/不完整身份、私聊锁异常、错误正文不进日志、表情失败 watcher 保留。

## 剩余风险与建议下一步

- 未发送真实 Feishu、未读取生产正文/密钥、未验证用户客户端的实际富文本节点或当前应用表情权限；合成测试不是现场根因确认。
- SDK/供应商的实际重投次数和持续时间属于运输外部边界；本线允许安全重投，不另加第二队列。重启后真实已受理消息仍由原持久 ingress 幂等保护。
- 原 bot 身份探针失败节流 30 秒未改；诊断显示剩余秒数，默认 120 秒进度未改。
- 建议父任务先做非作者复核和最终 combined gate，再统一合并打包；N01 actor 身份可并行只读审查，禁止跨线修改或把同群会话误当成同一个操作者。

## 非作者复核附录

- N01 原两文件最终独立复验 **56 项通过**，XML：`/tmp/my-agent-N01-independent-final.xml`。最初 42 项复验发现缺身份拒绝仍是普通 ValueError；原作者改为 typed `CHANNEL_ACTOR_IDENTITY_REQUIRED` 后，原断言没有放宽。
- 两条独立 P1 探针已关闭：旧未知主根借 raw attributes 指向另一人的工作线程时，resolve/list/get/pause/approval 全部 `CHANNEL_ACTOR_SCOPE_MISMATCH`，调度原账字节不变；同群 child A/scope B 与 current B/root A 审批全部 `CHANNEL_ACTOR_IDENTITY_CONFLICT`，原根记录不变。证据：`/tmp/my-agent-N01-independent-probes-final.json`。
- 另一人的工作线程仅通过其本人合法新入口准备；负向探针仍只改旧根的模型属性或冲突 actor，没有降低拒绝断言。新增首轮委派按精确 SessionTask 恢复，普通非身份派活保持 None；isolated/new run 只从 creator、冻结 execution_context 和原 TaskLink 取身份，旧根不升级。
- A 两新文件非作者原断言 **39 项通过**，XML：`/tmp/my-agent-A-independent.xml`。只读审查来源签发、显式 schema、默认无工具和冻结冷重投未确认新的阻断；额外使用真实 declared output target 与原 archive ready artifact ref，验证附件不能被 internal_only 取消，publish 空正文加真实附件仍允许。
- 所有上述复核均使用隔离合成记录，没有实际生产利用、生产泄漏或真实 Feishu 应用权限验证结论；A 的 39 项与 N01 的 56 项不是最终整片联合验收数。
- 建议下一步：主线程冻结全部切片后统一严格 gate、安装包及少量真实模型验收；现场首条消息未受理原因仍需受限元数据，不能凭蓝色 @ 或缺少表情断言根因。可并行只读审查，不并发修改产品或生产数据。

## 2026-10-14 WS 代理依赖与真实连接健康追加

- 本线在根集成工作树逐文件独占 `feishu_ws.py`、`test_feishu_ws_reconnect.py`、`test_packaging.py`、`pyproject.toml` 和快速核对指南；不提交、不推送、不读私人配置、不启动连接。根负责全局台账、测试入口和准确安装包，健康投影线负责 adapter/registry。
- 问题证据由根保留：`.7` 的 lark-oapi 1.7.1 持续报告缺少 SOCKS 运行依赖。已读准确安装 SDK 的 `_ws_connect_kwargs` 与 `_conn` 赋值位置，确认空配置原合同是系统/环境发现，首连成功前 `_conn=None`。
- 保留原代理合同，声明 `websockets>=15,<16` 的正式代理接口，并补 `python-socks[asyncio]>=2.7.2,<3`；显式 HTTP/SOCKS 不回退直连，依赖随包安装，不新增运行外网服务。接口、许可、替代方案见 [快速核对指南](../guides/FEISHU_QUICK_CHECK.md)。
- `transport_health()` 只读原 SDK `_conn.state`（`websockets.protocol.State`）；旧接口只认明确布尔 `closed`。返回 `status=healthy/degraded/stopped` 与固定 `reason_code=FEISHU_WS_CONNECTED/CONNECTING/DISCONNECTED/STATE_UNKNOWN/STOPPED`。启动线程和首连失败不算健康，没有第二份健康账或回调状态。
- SDK 原日志过滤器复用 `common.log_redaction` 净化格式化后的消息与参数，防 SDK 自带 handler 暴露代理密码/WS URL 令牌。外层重连只记异常类型，不记异常正文。
- 4 文件 focused 74 项通过：`test_feishu_ws_reconnect.py`、`test_adapter_feishu_ws.py`、`test_packaging.py`、`test_feishu_transport_security.py`。相邻 Responses WS 与共享脱敏 2 文件 109 项通过；本线 Ruff、doc sync、diff-check 通过。
- 使用原 `.7` Python 3.12、准确 lark-oapi 1.7.1 / websockets 15.0.1 做进程内无连接探针：真实 SDK 参数包装、HTTP/SOCKS/继承三组、实时 OPEN→CLOSED 健康和日志脱敏均通过。没有修改安装内容或运行网络握手。
- strict code-size 在并行健康线施工中曾报告 `feishu.py` 类长/嵌套超限，已交原负责人处理；根须按最终同版源码重跑完整严格 gate，不能把本线定向通过当全局发布通过。
- 建议下一步：根先审依赖闭包并构建准确新包，再在已授权 `open.feishu.cn:443`、`msg-frontier.feishu.cn:443` 范围验首连与断线投影；其它 agent 可并行只读复核，真实安装/重启/发消息由根统一，禁止旁路直连或扩大放行。
