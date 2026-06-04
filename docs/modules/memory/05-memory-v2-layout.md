# Memory V2 Layout

当前 layout 以 owner home 为唯一普通运行根。

```text
owner_home/
|-- memory-hot.md
|-- memory.md
|-- memory/long_term/memory.jsonl
|-- memory/daily/YYYY-MM-DD.jsonl
|-- memory/lessons/*.md
|-- memory/routing/INDEX.md
|-- audit/YYYY-MM-DD.jsonl
|-- blobs/tool_outputs/
|-- tasks/<date>/<task-slug>/{output,work}/
|-- agents/<run_id>/
|-- compact/
`-- global_index/
```

## Query Targets

- grep/RAG/vector：优先 `memory/long_term/memory.jsonl`、`memory/daily/`、`memory/lessons/`。
- 运行恢复：优先 task `work/compact/` 和 `work/agents/<run_id>/`。
- 审计：`audit/YYYY-MM-DD.jsonl`。
- 大文件正文：`blobs/tool_outputs/`。

## 不做

- 不给子代理长期记忆。
- 不让 global index 替代正文事实。
- 不用通用保留槽承载新业务字段。
