# 完整显示原文分页

状态：后端与薄客户端已实现，完整 TUI 接入和真实验收由显示工作线与主线负责。

## 解决问题

旧界面“全部展开”仍可能只有已裁剪的预览。把完整 diff 和工具输出直接塞进历史事件，又会让长会话重放卡顿。
新方案保存当时的公开显示快照，事件只携带不含路径的引用与小预览；点击完整浏览后才取一页。
不会重新读取当前业务文件来“还原”旧 diff，也不会把展示归档注入模型、Compact 或记忆。

## 外部参考与适配

已阅读 会话运行时 `会话运行时-rs/tui/src/pager_overlay.rs`：完整 transcript 使用独立 overlay 与显示缓存，
避免每帧重新构造全部内容。本项目是 Gateway/薄 TUI 分离，因此需要额外的服务端原文分页；
owner/thread 认证复用本项目 history 和 agent thread 的 canonical 身份，不照搬本机直读路径。

## 合同

- 生产者：`archive_display_rows(agent, thread_id=..., rows=...)`，只接收已经按当前通道处理的公开行。
- `thread_id` 来自 `task_attributes.agent_thread_id`，主代理才回退到 `conversation_thread_id`。
- 行输入：`{kind, text}`；空行、中文、组合字符和换行均按原字符串保存。
- 引用：`{schema: display_archive_ref.v1, archive_id, thread_id, page_count, row_count, char_count}`。
- 页面每页最多 256 行片、12000 字符；超长逻辑行按 2000 字符分片，每片携带 `row_index/part_index/part_count`。
- 同一个 `row_index` 的文本按 `part_index` 拼接，即为原逻辑行，UI 不应把分片边界误当源码换行。
- 客户端：`request_display_page(session_id=..., reference=..., page_index=...)`，须在线程池执行，不阻塞 TUI。
- HTTP：`POST /client/display-page`，载荷字段是 `conversation_id/reference/page_index`。
- 返回：`ok/rows/reference/page_index/page_count/has_previous/has_next`。页数只信服务端 manifest，不信客户端值。
- 目录：canonical `ConversationStore.root/display_archives/<随机ID>/`，0600 页面和 0700 新目录；manifest 最后发布。
- 写入失败或进程中断可能留下未发布的孤立目录，不作为成功引用返回，也不从该目录恢复模型上下文。

## 权限与缺失数据

HTTP 必须通过现有 trusted source/owner 解析。先确认当前绑定线程，再允许该线程或结构化 lineage 下的后代。
不同 owner、同 owner 其它会话、伪造路径、越界页码、软链接、损坏页面均不能读取。
历史子代理任务结束后仍可查看其旧显示，不靠当前 workspace task 锁限制只读历史。
旧版本只有预览而无归档时必须提示“完整原文缺失”，不能宣称已全部显示，也不能从改过的文件重算。

## 前台长思考的入口

Gateway 的 `BufferedChunkStreamWriter.write_thinking` 曾在通知 foreground sink 之前裁到 12K 字符，
所以只在下游保存原文仍会存到残缺版本。现在 writer 先完整执行公开投影，再调用同一个
`_thinking_archive_payload` 保存快照，chunk 仅包含有界预览和 `display_archive_ref`。
`GatewayForegroundTranscriptSink` 将引用透传给 `BackgroundTranscriptSink.write_thinking`，不重复归档。
存档失败必须透传 `history_incomplete=true`，否则下游会把已裁预览误存成“完整原文”。
没有绑定 owner/thread sink 的独立 writer 不能猜目录，超长内容只显示缺失事实；非 rich 客户端仍不显示思考。

普通模型正文没有同样的前置字符裁剪：model delta 原样累计，工具边界 commentary 使用 `max_chars=0`，
后台 commentary/final 使用 `limit=0`。本切片不改变这些正文的保存或模型上下文，只修显示思考的提前裁剪。

## 命令采集原文与模型预览

追加只读审计发现 `shell._command_display` 在富展示进入统一归档前也裁到 12K；通用工具输出归档保存的
`outcome.output` 同样已是模型预览，不能从它恢复丢失的中段。现已取消这一次源头显示裁剪：当次
`CompletedProcess.stdout/stderr` 作为 handler display 原文，经统一公开净化、owner/thread 原文归档之后，
Gateway 事件仍各只携带 8K 预览和 opaque ref。模型正文 `_format_process_result` 的既有限额保持不变。
不从当前业务文件或客户端传入路径读取内容，不重跑命令来伪造原输出。

参考依据：会话运行时 `core/src/tools/events.rs::emit_exec_stage/emit_exec_end` 将真实捕获的 stdout/stderr
与模型 `formatted_output` 分开；终端交互 `src/tools/BashTool/BashTool.tsx` 在短预览以外保留本次
输出到 tool-results。本项目复用已实现的 owner-scoped display archive，不新增另一种路径引用格式。

`ProcessOutputCapture` 每流 4 MiB 的保留上限及持续排空行为未改。真正超过采集上限、管道读取失败或
超时未确认排空时，`capture_complete=false`、`history_incomplete=true` 和归档首页缺失提示一并保留；
仍可浏览已经采集到的部分，但不能宣称进程输出全部已保存。旧 command display 的 typed
`stdout_truncated/stderr_truncated` 同样声明缺口，不解析正文里的“省略”字样决定完整性。
未知终止码展示“未知”，不把超时或采集缺失当作成功，也不改变操作账本的真实终态。

## 验证与交接

定向测试覆盖长中文行精确重建、空行、多页、原数据变化后的快照、跨 owner/跨会话拒绝、后代读取、
路径引用拒绝、页码/页大小上限、软链接、损坏归档和客户端载荷。真实 TUI 由主线统一验证并记录 tmux。
本分支运行 `test_display_archive.py`、`test_gateway_client_service.py`、`test_chat_client_context.py`，
共 36 项通过；包含真实本机 HTTP route，不包含真实 LLM 或 TUI。相关 Ruff、diff 检查及严格尺寸门通过。
前台思考入口追加 `test_gateway_thinking_archive.py` 联合 foreground 与 archive 测试共 38 项通过：
确认完整原文重建、只归档一次、现场/检查点/最终快照的引用一致、失败不把预览重存成全文，以及普通长正文未裁剪。
显示生产者与事件白名单由 TUI 工作线接入；共享台账、目录树、发布文档由主线合并，避免并行覆盖。
命令源头修复追加 `test_tool_display_archive.py`：主/子长 stdout 与 stderr 精确分页重建、事件预算、
归档快照不随源对象变化、模型预览不变、真实小容量管道采集缺口、未知 timeout 和旧预览缺失提示。
联合工具进度、工具 Gateway、归档、思考、进程退出回收和 TUI 浏览/runtime 共 161 项通过；
Ruff、diff 与严格尺寸门通过。没有在本分支部署、调用真实模型或操作用户 TUI，真机验收由主线负责。

建议下一步：主线先合并 API 与显示生产者，确认 ref 从现场事件到历史回放不丢，再通过同 Gateway 的真实
TUI 检查 Ctrl+O/Ctrl+E、长 diff 与主子历史。不要用后端定向通过代替完整 TUI 验收。
