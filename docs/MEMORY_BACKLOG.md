# MEMORY BACKLOG

当前记忆设计以这些文档为准：

- `docs/modules/memory/04-structure.md`
- `docs/modules/memory/05-memory-v2-layout.md`
- `docs/modules/memory/06-runtime-memory-requirements.md`

原则：

- 主代理长期记忆归 owner home。
- 子代理只保留任务周期内可审计状态和候选摘要。
- 日流水、raw audit、artifact refs 要方便 grep、RAG 和恢复。
- 不再保留多套入口并存的说明。
