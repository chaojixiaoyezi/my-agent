# Memory Progress

## 2026-06-07 恢复模式结构化边界

- 长 `write_file.content` 流式中断由 tool-stream 边界写结构化
  `source_tool`、`path`、`content_field_present`、`streaming_content_*`
  字段；后续 long content recovery 只读取这些字段。
- parse error 的 `raw` 和工具错误正文只保留为诊断/展示文本，不再用正则从里面猜
  `tool/path/content` 来触发恢复模式，避免恢复流转依赖未解析正文或自然语言提示。
- 普通 `write_file` 失败输出里提到 “inline content 过长” 也不会单独触发机器恢复；
  要触发恢复必须由上游工具/流边界提供结构化错误码和字段。
- compact/resume 不再根据 `memory-resume`、`LocalStore`、`restore_refs` 等文本片段过滤
  `next_action` / `next_actions`；机器续接优先级只由结构化 read coverage、cursor、
  `resume_focus` 和 work_state 字段决定。
- 主代理 task workspace 复用只认当前 `work/run_workspace.json`；`work/task.yaml` 是可读说明，
  不再作为旧目录身份兜底。
- compact work-state 的 read coverage 从单一 primary 游标扩展为
  `primary + sources + incomplete_sources`：`incomplete_sources` 用于多文件/多项目任务的
  下一段续接队列，`sources` 用于覆盖审计和 resume context 展示。
- `list_files`、`find_files`、`search_text` 统一写结构化 `page_window`，tool output 归档和
  compact work-state 从该字段恢复分页 offset；机器续接不再依赖输出正文里的 `next_offset` 文案。

## 2026-06-06 Compact 事实源收敛

- compact/resume 的 read cursor 只认当前工具记录的结构化成功事实：`ok: true`，或无错误且
  `status` 为空/`ok`。`succeeded`、`completed`、`done` 等旧字符串不再算已读。
- runtime fact source 的 run phase 只认当前结构化状态：`DONE` 是 final；
  `FAILED`、`TIMEOUT`、`CHANNEL_ERROR`、`CANCELLED`、`ABANDONED` 是 terminal；
  `succeeded` 等旧状态别名保持 running，避免 compact 后把旧残留误恢复成已完成。
- raw archive 工具布尔只接受真正 bool 或机器布尔字面量 `true/false/1/0`；`success/error/ok`
  这类状态词不再变成工具成功/失败事实。
- task progress 的 items、coverage 和 soft quality hints 已收回 `task_progress.py` 单入口。
  进度状态只认结构化 `pending/in_progress/done/skipped/blocked`；未知值保留为普通状态文本并归入
  `other`，不会被多语言自然词猜成已完成或阻塞。
- task compact rollup 复用当前 `TaskStatus` 协议分组；`CANCELLED`、`ABANDONED`、
  `TAKEN_OVER` 只计入状态统计和审计，不再进入 `pending_run_ids` 或 continue packet
  `pending_work`。

## 2026-06-04 收敛

- 主代理长期记忆只写当前 owner `memory/long_term/memory.jsonl`。
- 每日工作记忆写当前 owner `memory/daily/YYYY-MM-DD.jsonl`。
- raw archive 写当前 owner `audit/YYYY-MM-DD.jsonl`。
- 大工具输出写当前 task `work/blobs/tool_outputs/`。
- 保存型任务写 `tasks/<date>/<task-slug>/{output,work}/`；任务名来自短标题，不直接截取整段 prompt。
- 子代理只保留 task-local 状态、事件、artifact refs 和 compact，不拥有长期记忆。
- 普通恢复、compact、tree 和 doctor 走当前 owner/task/run/agent refs，不再扫描 repo `data/*` 作为事实源。
- owner projection/global index 是查找地图，可重建；canonical state 和 task workspace 才是权威。

## 下一步

- 把 remaining docs 和 tests 里只服务历史路径/历史字段的描述继续删除。
- RAG/向量层接入时优先索引 `memory/long_term/memory.jsonl`、`memory/daily/`、`lessons/` 和 artifact refs。
- 精确 grep 查询保留 JSONL 明文字段，避免只剩二进制索引。
