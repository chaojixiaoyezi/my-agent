# Memory Progress

## 2026-06-06 Compact 事实源收敛

- compact/resume 的 read cursor 只认当前工具记录的结构化成功事实：`ok: true`，或无错误且
  `status` 为空/`ok`。`succeeded`、`completed`、`done` 等旧字符串不再算已读。
- runtime fact source 的 run phase 只认 `DONE` 为 final；`succeeded` 等旧状态别名保持 running，
  避免 compact 后把未收口任务误恢复成已完成。
- raw archive 工具布尔只接受真正 bool 或机器布尔字面量 `true/false/1/0`；`success/error/ok`
  这类状态词不再变成工具成功/失败事实。

## 2026-06-04 收敛

- 主代理长期记忆只写当前 owner `memory/long_term/memory.jsonl`。
- 每日工作记忆写当前 owner `memory/daily/YYYY-MM-DD.jsonl`。
- raw archive 写当前 owner `audit/YYYY-MM-DD.jsonl`。
- 大工具输出写当前 owner `blobs/tool_outputs/`。
- 保存型任务写 `tasks/<date>/<task-slug>/{output,work}/`；任务名来自短标题，不直接截取整段 prompt。
- 子代理只保留 task-local 状态、事件、artifact refs 和 compact，不拥有长期记忆。
- 普通恢复、compact、tree 和 doctor 走当前 owner/task/run/agent refs，不再扫描 repo `data/*` 作为事实源。
- owner projection/global index 是查找地图，可重建；canonical state 和 task workspace 才是权威。

## 下一步

- 把 remaining docs 和 tests 里只服务历史路径/历史字段的描述继续删除。
- RAG/向量层接入时优先索引 `memory/long_term/memory.jsonl`、`memory/daily/`、`lessons/` 和 artifact refs。
- 精确 grep 查询保留 JSONL 明文字段，避免只剩二进制索引。
