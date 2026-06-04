# Memory Progress

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
