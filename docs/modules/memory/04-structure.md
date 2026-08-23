# Memory Structure

本文只描述当前记忆主链路（单 owner 视角，local/main 下的持久化事实源）。

## 事实源

- 正式长期记忆：`owners/local/main/memory/long_term/memory.jsonl`（晋升后的正式事实）。
- 日常记忆：`memory/daily/`（按日），`memory/lessons/`（经验教训）。
- 候选：`memory/candidates.jsonl`（待审核，晋升前不具事实权威）。
- recovery snapshot：`memory/hooks/YYYY-MM-DD.jsonl`（运行经历归档的轻量恢复线索）。
- 运行事实（runtime facts）与 raw archive：由 `run --save` 写，`memory-resume`
  据此恢复上下文；与 `ConversationStore`（transcript/guidance/goal）分开。
- 任务级事实源（task.yaml / run_workspace.json / runtime.db）是任务生命周期权威，
  记忆层只消费不覆盖。

## 权威与投影

- 记忆晋升、候选审核、HOT/lesson 写入走 `AgentConfig` 开关与统一 Candidate/Curator
  链，入口收敛在 memory 模块；CLI 普通 run 不直接把对话正文写进正式长期记忆
  （`--no-save` 关闭本次运行归档，ConversationStore 与审计仍照常）。
- `memory_path` 等路径由 home 解析统一给出（显式配置 > MY_AGENT_HOME 环境变量 >
  ~/.my-agent 兜底），记忆模块不自行猜测 owner home。

## 运行中 native 工具历史摘要

- `agent/memory_archive/compact_semantic_summary.py` 是 carried archive 续跑摘要与运行中
  native IR 摘要共用的语义摘要入口，但不拥有 Compact 状态、工具执行或完成判定。
- 运行中真实 turn 的摘要输入由“上一代 ConversationThread 完整摘要 + 本次 native 工具
  历史 + 当前任务”组成，输出是可独立替代上一代的完整摘要，不是只描述本次增量的片段。
- provider 消息顺序固定为“真实任务 user → native history → synthetic Compact user 指令”。这是 会话运行时
  compaction turn 的适配；普通 backend 的 `prompt + messages` 会把 prompt 放最前，不能直接复用该顺序。
- `agent/conversation/live_tool_compact.py` 才负责把这份摘要连同精确移除/保留的 tool-call
  ID 写入 checkpoint，并通过 ConversationStore 的同一 CAS 推进 generation；TUI 只投影
  已提交的代次和 token 前后值。
- 辅助、不保存的展示回合可以继续使用临时摘要并静默退回机械窗口；持久且正文权威的
  main/child/grandchild 不能在摘要失败后先删除历史，必须整体失败并恢复原 native IR。
- 摘要不是执行事实源。精确副作用和交付仍以 raw archive、operation ledger、artifact
  registry、任务工作区和真实文件为准。

## lifecycle wake 的 carried tool archive

- 每个 root task 的 `work/blobs/tool_outputs/index.jsonl` 同时索引 bounded `tool_call` 与外置
  `tool_output`。child lifecycle 后续工作片按 exact root run/task 读取，保留 append 顺序，并以
  `scoped_call_id` 去重；其它 task 和 child workspace 不扫描。
- 恢复记录沿既有 `carried_archive_tool_calls` 进入工具循环，重建已执行工具、one-shot key、工具轮基线、
  参数和 artifact refs。大输出正文仍留在 owner 私有 artifact，按需读取；索引损坏时 fail-soft 回到已有
  task/transcript 上下文，但不得编造已执行事实。
- 这条链只解决同一 active turn 跨后台工作片的连续性，不新建 Compact 账本，不推进 generation，也不让
  自然语言计划获得机器权威。

## 2026-08-17 测试适配记录

- 记忆相关测试的 `tool_protocol="text"` 配置移除、假后端 native 化、协议快照默认
  native（EXEC-31b 适配，详见 02-progress.md）。
- compact 自动续接链的 native 工具轮差异已记录 xfail，待按 native 语义适配。
