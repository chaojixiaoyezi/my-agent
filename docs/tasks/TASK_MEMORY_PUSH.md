# Memory Push

历史任务说明已过期。当前记忆规则：

- 主代理长期记忆写 owner home。
- daily memory 记录每日流水。
- 子代理只保留任务周期内状态和候选摘要，不写长期记忆。
- RAG/vector/grep 都应以当前 memory store、daily audit 和 artifact refs 为输入。
