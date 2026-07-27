# Compact Plan

## 唯一语义

compact 只压缩当前 agent 的同一条 session history（会话历史）。执行 compact 的内部对象即使叫
`task`，也只是一次会话操作；不会产生另一条 task history（任务历史）。主代理、普通聊天和工作回合
持续使用同一条 thread transcript；每个子代理是独立 agent，但调用的是同一个通用 Compact 引擎，
只是把归档写入自己的 agent run workspace。

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
- 子代理以 `context_scope=task_local` 调用通用 Compact；摘要、work state、continue packet 和 refs
  写在该子代理自己的 `memory_archive/`。父代理通过 canonical state、task progress 和 agent tree
  获取结构化状态，不直接继承子代理上下文。
- 子代理从既有 `checkpoint.json`、`state.json`、`timeline.jsonl` 恢复任务，不另建恢复目录或恢复包链。
- 大正文留在 artifact/blob/audit，compact 只保留必要摘要、游标和 refs。

## 恢复权威顺序

- 当前 task locator / `task.json` 中的结构化目标优先于旧归档摘要；通用 runner 包装文字不能覆盖业务目标。
- 当前 runtime guidance、task progress 和当前 task 的 `next_actions` 依次决定下一步。工具调用进度、
  read coverage 和 cursor 仍作为可核对事实保留，但不能自行升级为任务目标或下一步。
- 只有任务已显式声明 `full_source_read` 结构化合同，未完成的读取游标才可成为恢复后的优先动作；普通读取
  不因一次 Compact 被强制继续，也不从文件名或自然语言猜“必须读完”。
- 父代理只读取 child 的 canonical state、task progress、agent tree 和 artifact refs；不会把 child
  的摘要或工具游标复制成父任务的新目标。

## 规则

- compact 不验收任务，不制造新硬门，不从自然语言猜状态。
- compact 触发压力以 `max(provider_input_tokens, local_prompt_estimate)` 为准，避免 provider usage
  低报绕过阈值。
- 配置百分比只定义 active context 的单一触发边界；不把未来最大输出额度当成已经占用，也不允许工具
  digest 以另一个硬编码比例越过该边界。
- 长文本读取必须保留 `offset/total_chars` 或 `start_line/total_lines` 等机器游标，不能只靠摘要。
- 已达到阈值而尚未执行的正文工具调用登记为 `CONTEXT_COMPACT_DEFERRED`，compact 后按原结构化调用
  续跑，不能让模型凭一句提示重新构造。
- 已存在的结构化 coverage、delivery contract、desired outputs 和 run identity 在 compact 后仍保留；
  普通自然语言不得被加工成新的硬合同。
- 搜索命中可以帮助定位，但 required full-source coverage 的完成证明只能来自读取窗口、artifact refs
  或 coverage ledger。
