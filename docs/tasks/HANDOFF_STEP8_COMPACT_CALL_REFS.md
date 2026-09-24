# 第8步逐调用 Compact 来源交接

## 基本信息

- workstream：step8-compact-call-refs
- branch：`codex/step8-compact-call-refs`
- worktree：`../my-agent-worktrees/step8-compact-call-refs`
- owner：Codex 第8步模型边界子代理
- date：2026-09-23
- 基线：`d939f6cf3`；本线只提交本地，不推送、不操作真实模型或 Gateway。

## 本线目标

同 thread 不同 request/attempt 可以复用 provider call id。旧 checkpoint 汇总裸 ID 后跨域过滤，实际会隐藏当前调用；本片用原有 canonical 调用身份修复来源、选择、提交及恢复链，不移植新的 scope/摘要链。

## 实际完成

- 新 `tooling/call_ref.py` 只表达已有 `(run_id, attempt_id, call_id)`；持久引用 schema 为 `tool_call_ref.v1`，逐字段严格检查。无当前身份默认值，不解析 scoped 字符串或 operation 哈希。
- `conversation_compact_checkpoint.v2` 显式追加 `source_tool_call_refs` 与 `retained_tool_call_refs`。数组与裸 ID 逐位对应；来源必须完整，未知尾部项为 `null`。裸 ID 允许重复/交叉，精确 refs 不可重复或交叉，数量按记录计。
- 原生 Compact 在摘要 I/O 前冻结实际 IR ToolCall 来源，提交时用保留 IR 的精确 refs 计算来源；不把 params 的提交者身份套给调用。
- carried Compact 按记录位置选择来源/尾部；未知来源只保留。模型恢复仅凭已提交精确 refs 隐藏，旧无 refs 或畸形 refs 不获删除权，并通过 `compact_source_resolution.status=uncertain` 暴露。
- 真实归档入口向 externalizer 传入 canonical attempt/turn；大小输出索引和 carried 恢复保持原身份，去重只按精确三元组。原 current-subagent fallback 测试改为断言实际 call 来源不被当前 runner 覆盖。
- 发现并修复内容地址竞争：旧 ID 输入相同但新 refs 不同的迟到候选，会在 CAS 失败后以同 ID 遮蔽已提交行。新 writer 显式将规范 source/retained refs 加入候选 ID；无 refs 的旧函数输入仍输出原 ID，旧行/旧指针不重算。顶层 request/attempt 提交者语义、写候选 → 停止检查 → 锁内 generation CAS 顺序保持。

## 改动文件

- 生产：`tooling/call_ref.py`、`conversation/compact_checkpoint.py`、`live_tool_compact.py`、`active_turn_compact.py`、`agent_core/_tool_loop_service.py`、`tool_call_archive_record.py`、`memory_archive/tool_output_externalizer.py`、`compact_tool_output_refs.py`。
- 新测试：`tests/test_compact_tool_call_refs.py`；更新原生 IR、externalizer、carried、Gateway 和 subagent 的直接夹具/断言。
- 文档：CODEBASE_TREE、本交接、memory/verification 的 progress/structure；中央设计台账及 Goal 由主代理维护。

## 测试命令和结果

```bash
python3 -m pytest agent_py_agent/tests/test_compact_tool_call_refs.py agent_py_agent/tests/test_native_tool_ir_compact_and_orphan_sweep.py agent_py_agent/tests/test_tool_output_externalizer.py agent_py_agent/tests/test_memory_compact_tool_output_refs.py agent_py_agent/tests/test_compact_tool_refs.py agent_py_agent/tests/test_gateway_chat_conversation_context.py agent_py_agent/tests/test_gateway_conversation_compact.py agent_py_agent/tests/test_subagent_runtime_compact.py -q --tb=short -o addopts=''
ruff check agent_py_agent scripts
python3 scripts/check_doc_sync.py
python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json
git diff --check
python3 scripts/check_clean_package.py .
```

- 最终 focused：**336 passed，23.12 秒**；其中新文件 **25 passed**，含原生冻结和真实 executor → archive → index → carried 身份断言。
- 初始核心红灯 7 failed / 1 passed，重复 request/attempt 与 unknown/orphan 同号均实际误隐藏；归档链另有 3 项红灯；同 ID 迟到候选测试实际改写了已提交来源，修复后全部转绿。
- 受影响组合中的旧 fake carried 记录缺身份导致 4 项失败，已明确补原调用身份；生产没有增加默认身份旁路。最初 pytest 参数使用保留字属于测试准备错误，不计产品红灯。
- 本地严格 gate 已通过：全目录 Ruff、doc sync、strict code-size（hard=0、blocked=False）、diff 和 clean-package。过程中一次测试 import 排序、未暂存新文件的 clean-package 项已修正；尺寸基线不改，生成的尺寸报告不随本片提交。

## 影响范围与兼容边界

- 仅 live-tool IR / carried 来源过滤；不改 transcript 来源、估算、消息遍历、执行器、轮执行及工具结果 schema。新增字段属于现有 checkpoint/index 的显式增量。
- 新 reader 能读取旧 v2 行，但旧裸来源不再具有跨域隐藏权。缺 attempt 的旧 read-only durable index 通常无 operation；不透明 operation_id 也不能反推 attempt。本片不伪造或静默迁移身份。
- 全未知旧大历史实测 `compacted=false`、`source_resolution=uncertain`，完整记录和 generation 不变。其 provider overflow 可能无法恢复，属于已证实的兼容限制，不能称为该场景已通过或自动修复。
- 旧 reader 虽可解析新 JSON，却仍用裸 ID 过滤，不能安全读取新混源 checkpoint。不得只回退旧 wheel 后继续读新账；发布回滚须匹配运行时和数据快照、保留新增数据，并明确不可直接降级的边界。

## 需要主线重点复查

- `_tool_loop_service.py` 与主线 closeout 片只有同文件/import 交错；本片仅改原生计划冻结、提交边界和精确引用提取。
- 新 source/retained refs 数量、非重叠校验与 ID 内容地址必须一并集成，不能只 cherry-pick 过滤函数。
- 原锁和 CAS 未改；候选落盘后的 orphan 仍无权影响已提交 chain。

## 需要其他线协调 / 剩余风险

- 主代理另行认领 process 事实经 durable index 恢复丢失的问题；本片不扩大 runtime_facts 或 process 元数据范围。
- 没有真实模型、Gateway/TUI、部署或线上 CI 证据。线上 CI 不作为本片验收来源。

## 建议下一步

主代理先组合审阅并跑相关 focused 与发布 gate，再完成独立 process-index 缺口和既定真实 TUI 验收。可由另一代理只读核对 refs/ID 与旧 reader 回滚边界；不要并行改写本片来源/CAS，同步保留新增 checkpoint 数据和存量 uncertain 限制。
