# 宿主提示（host notices）

状态：分支 `claude/be-host-notices`（2026-09-27，基于 `eb7c639a1`），待集成。第一个使用方是智能程度检测（[REASONING_EFFORT.md](REASONING_EFFORT.md) 第 8 节）。

## 1. 要解决什么

宿主有时需要告诉用户一件事，但当时没有可以直接回复的地方。例如：
- 后台检测结束后要说明结论；
- 以后也会有“参数已改，重启后生效”这类提示。

宿主提示有四个要求：
- 模型在历史里看不到它；
- 飞书排在正文前面；
- TUI 显示成灰色系统行；
- 同一条提示只显示一次。

## 2. 为什么新增字段（现有通道逐个对照，2026-09-27 查证）

- **`assistant_commentary`**：模型自己写的过程文字，属于助手消息，模型看得到。不能用。
- **turn-end 提示**（`turn_end.turn_end_notice`）：
  - 这是“六种轮次结束原因”的专用协议，模块写明不许加入别的状态；
  - 排在回复之后；
  - 飞书投递层不用它。
  - 不能用。
- **`/progress` 宿主事件**（`permission_requested` 那一类）：
  - 满足三条：模型看不到、飞书排在前面、TUI 能画；
  - 但它按设计“最多投一次”：发送失败时游标照样前进、不重试，多条合并成一条发出，没有送达确认。
  - 不能作为已读依据。
- **最终回复**：送达最可靠（认领、sent 回执、失败重试），但没有提示字段。

结论：新增通用字段 `host_notices`，挂在最终回复这一层。

## 3. 做法

- **形状**：`HostNotice = {notice_id, source, code, text}`（`conversation/host_notices.py`）。
  - text 是宿主生成的大白话（去掉控制字符，最长 500 字）；
  - source / code 是结构化事实，测试断言与同来源替换都按它们来。
- **规范存放位置**：会话线程记录上的 `pending_host_notices` 字段（`ConversationThread`），经 `threads.update_atomic` 原子读改写，不另建文件。
  - 同一来源只留最新一条；
  - 最多 5 条，超出丢最旧的。
- **模型看不到**：`models.MODEL_HIDDEN_THREAD_FIELDS` 统一列出不进模型上下文的线程字段（原有的两个显示遥测字段加上这个），`store.context_bundle_report` 和 `background_context._minimal_context_bundle` 都按它剔除。提示也不拼进最终消息正文。
- **发布**：Gateway 前台回合在用户消息落账后、模型执行前（`request_execution._publish_gateway_host_notices`），把此刻待送达的提示用 `stream_writer.write_host_notice_events` 发成 `host_notice` 流事件。这一步只读，不取走。
- **提交即已读**（集成者定的 B 口径）：
  - `request_history.persist_gateway_assistant_result` 在正常回复提交时，按本轮发布过的编号取走（`take_host_notices`）；本轮期间新到的提示留给下一轮。
  - 取走的提示同时写进最终消息元数据 `host_notices` 和 `channel_delivery.host_notices`。写入失败走补交时，元数据也一起带上。
  - 停止或失败的回合不取走，下一次回复还会附上。
  - 理由：最终回复本身会重试到送达；如果连回复都永久失败了，用户缺的是整条回复，结论仍可以从源头（如 `/effort`）查到。以后真有“必须确认送达”的提示，再在同一个字段上加确认。
- **各出口怎么显示**：
  - `/result`：白名单放行 `channel_delivery.host_notices`。
  - `/progress`：不转发 `host_notice`（飞书只从最终回复拿，不会重复）。
  - 飞书：适配层取结果时（`adapter/manager._reply_text_with_host_notices`），在同一条回复正文前加“【提示】……”并空一行。渲染后的整段正文进入适配层原有的持久回复记录，重试时一起重发。所以没有改 `ReplyEnvelope` 和 DeliveryService，也没有改持久记录的结构。
  - TUI 发起窗口：`tui_runtime._publish_host_notice` 把 `host_notice` 事件画成 `system_message` 块（灰色 `◇` 行）。块号带请求号和提示编号，重放不重复。
  - 同会话其他窗口：`foreground_transcript.write_host_notice` 同步成 system_message 显示事件，并进入最终快照。
  - 历史回放：`history_display._host_notice_events` 从最终消息元数据重建，挂在同片用户消息那一行，排在用户消息之后、回复之前。带快照的最终消息里已经有同一事件，不重复生成。
- **来源清理**：使用方可以用 `clear_host_notices(store, thread, source)` 清掉某个来源的待送达提示。智能程度检测在两种情况下清：`/effort` 查看结论时，以及开始新一轮检测时。

## 4. 边界

- 只附在同一会话的下一条**前台**回复上；后台续跑回合不附，提示留到下一条前台回复。
- 单独逐条推送的后台消息投影（`message_stream`，一条助手消息、没有用户消息）不按元数据重建提示。TUI 发起的回合靠快照里的事件显示，飞书回合在历史回放里由用户消息那一行重建。
- 没有 TUI 和飞书之外的专门渲染：CLI `chat --gateway` 只打印正文。
