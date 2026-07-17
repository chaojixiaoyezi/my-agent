# Compact Plan

## 唯一语义

compact 只压缩当前 agent 的同一条 session history（会话历史）。执行 compact 的内部对象即使叫
`task`，也只是一次会话操作；不会产生另一条 task history（任务历史）。主代理、普通聊天和工作回合
持续使用同一条 thread transcript；每个子代理是独立 agent，因此只压缩自己的 session。

根任务工作区不再生成 `work/compact/`、`task_rollup.json`、task continue packet 或 owner
`compact/by_task` 索引。`work/state.json`、task progress、子代理 canonical state 和 artifact refs
是独立的结构化运行事实，不是第二套上下文。

## 主代理会话

- 原始 transcript 只追加，不因 compact 改写或删除。
- thread JSON 的 `summary + cursor + generation` 是主代理唯一 compact 状态。
- 达到上下文阈值时，把较旧消息归入 summary，保留最近尾部，再继续在同一 thread 累计。
- `/goal`、普通任务、聊天和后续“继续”都读取同一条 thread history；精确 task id 只用于选择运行状态，
  不选择另一份历史。

## 当前回合与子代理会话

- 当前回合因 context pressure（上下文压力）续跑时，typed compact carrier 保留本回合 UserTurn、工具
  事实、待执行结构化工具调用和 refs；续跑仍是同一个 turn。
- 子代理的 session compact 只写该子代理自己的摘要、work state、continue packet 和 refs，用于它自身
  恢复；父代理通过 canonical state、task progress 和 agent tree 获取结构化状态。
- 大正文留在 artifact/blob/audit，compact 只保留必要摘要、游标和 refs。

## 规则

- compact 不验收任务，不制造新硬门，不从自然语言猜状态。
- compact 触发压力以 `max(provider_input_tokens, local_prompt_estimate)` 为准，避免 provider usage
  低报绕过阈值。
- 长文本读取必须保留 `offset/total_chars` 或 `start_line/total_lines` 等机器游标，不能只靠摘要。
- 已达到阈值而尚未执行的正文工具调用登记为 `CONTEXT_COMPACT_DEFERRED`，compact 后按原结构化调用
  续跑，不能让模型凭一句提示重新构造。
- 已存在的结构化 coverage、delivery contract、desired outputs 和 run identity 在 compact 后仍保留；
  普通自然语言不得被加工成新的硬合同。
- 搜索命中可以帮助定位，但 required full-source coverage 的完成证明只能来自读取窗口、artifact refs
  或 coverage ledger。
