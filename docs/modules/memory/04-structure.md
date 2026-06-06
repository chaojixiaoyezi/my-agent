# Memory Structure

本文只描述当前 owner-home 主链路。

## Owner Home

```text
~/.my-agent/owners/<provider>/<owner>/
|-- memory-hot.md                         # 很短的高频偏好/规则
|-- memory.md                             # 入口说明，可引用下方文件
|-- memory/
|   |-- long_term/memory.jsonl            # 主代理长期记忆，显式 remember/export 才写
|   |-- daily/YYYY-MM-DD.jsonl            # 每日工作记忆
|   |-- lessons/*.md                      # 较长经验/教训
|   `-- routing/INDEX.md                  # 人类可读路由索引
|-- audit/YYYY-MM-DD.jsonl                # raw turn/tool/gateway 黑盒流水
|-- blobs/tool_outputs/                   # 大工具输出正文和索引
|-- tasks/<date>/<task-slug>/
|   |-- output/                           # 最终交付物或索引/验收记录
|   `-- work/                             # 过程状态、compact、子代理账本、草稿
|-- agents/<run_id>/                      # refs-only projection
|-- compact/                              # owner 级轻量 compact 指针
|-- global_index/                         # 可重建 owner/task/run/agent 索引
`-- workspace/runtime/workspaces/<scope>/ # LocalStore、gateway、conversation 等
```

## 主代理记忆

- `memory/long_term/memory.jsonl` 是正式长期记忆机器库，适合 grep、RAG、向量索引和审计。
- `memory/daily/YYYY-MM-DD.jsonl` 是每日流水摘要，记录当天的工作片段、引用、决定和下一步。
- `memory-hot.md` 只放极短规则，避免长期 JSONL 或 lessons 被整个塞进 prompt。
- `memory.md` 可以作为入口和索引，引用更具体的 lesson 或 routing 条目。

## 子代理记忆

子代理不写长期记忆。它只在自己的任务周期内写：

- `work/agents/<run_id>/canonical_state.json`
- `work/agents/<run_id>/events.jsonl`
- `work/agents/<run_id>/artifacts.jsonl`
- `work/agents/<run_id>/compact/`
- `memory_gate/` 候选经验，等待父级或 root 显式导出

## Compact

- 主代理 compact 读取当前 task workspace、owner memory、tool-output refs 和当前 run 状态。
- 子代理 compact 读取自己的 task-local canonical state、events、artifact refs 和父级可见 guidance。
- `rollup_ledger.jsonl` 只在子代理状态签名变化时追加；心跳式保存不应制造新的 compact 包。
- `read_file` / `read_artifact` 的恢复游标来自结构化工具记录；旧状态词、summary 和人工描述不能证明某段已经读过。
- compact handoff 的 final/running 判断只读当前协议状态，不能用 `succeeded/completed` 这类别名补齐。

## Artifact And Raw Output

- raw turn/tool/gateway 流水写 `audit/YYYY-MM-DD.jsonl`。
- 大工具输出写 `blobs/tool_outputs/`，prompt 中只放摘要和 refs。
- 用户最终交付写当前 task `output/`，或用户显式指定目录；task `output/work` 仍记录索引和验收。

## Design Rules

- 普通运行不从 repo `data/*` 读取事实源。
- 新字段优先建明确业务字段，不使用通用保留槽承载业务语义。
- 索引可以重建，不能替代正文事实。
- memory 只提供事实和检索，不做任务质量硬门。
- 状态别名必须 fail closed；需要迁移旧数据时写显式迁移记录，不在 compact 读取链路里临时猜。
