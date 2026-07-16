# Memory Structure

本文只描述当前 owner-home 主链路。

## Compact threshold authority

- 正式默认 90% 由 settings/runtime/standalone compact options 与 `config/agent_config.yaml` 对齐；部署级
  压力值必须显式配置，不能写成另一套代码默认。
- 触发判据使用 active turn，不使用累计账本；provider usage 低于本地完整 prompt 估算时保守取较大值。
- context window（上下文窗口）容量的唯一优先级是：provider 模型 metadata API 的显式字段 →
  `model_context_window_tokens` 本地配置 → 200K 通用兜底。provider 值一旦存在，无论比本地配置大或小都
  直接采用；本地配置不得伪装成 backend/provider metadata。provider 探测按 backend 实例缓存，失败只
  回退配置，不改变 90% 阈值、token 估算、compact summary/cursor/generation 或原始 transcript。
- MiniMax 的 `/v1/models` 与 `/v1/models/MiniMax-M2.7` 当前只返回模型身份和创建时间，没有返回
  context window 字段；因此运行时按上述合同使用本地配置。MiniMax 官方文档另声明 M2.7 总上下文为
  204,800 tokens，但文档声明不冒充本次 API 响应字段。

2026-07-09 P0 维护只调整 `compact_semantic_summary.py` 的类型导入，不改变本页结构或事实源。

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

### MemoryRecord 行结构（P5-2 扩展）

- 基础键：`role / content / kind / tags / created_at`。
- 可选 `attributes`：开放结构化扩展位——教训记忆的 `trigger_conditions`
  （结构化触发条件）挂在这里。旧行没有该键，读取按空处理；空 attributes 不写键，
  旧行格式不变。
- 写入口：`JsonlMemory.add`（便捷封装，不带 attributes）与 `add_record`
  （底层唯一落盘口，带扩展字段的记忆构造 MemoryRecord 走这里）。
- 消费：`memory_push.trigger_conditions_match` 按结构化事实匹配
  （列表=任一命中 / `min_` 前缀=数值阈值 / 标量=相等），匹配的教训在推送时
  排到最前（软提权，不淘汰未声明条件的记忆，绝不解析正文）。
- 生产端：失败自省调参后自动写一条带条件教训（failure_type + min_attempts），
  同型失败再现时自动提权注入。

## 工具输出归档

- 当前 task 的工具输出写入 `work/blobs/tool_outputs/`；较大正文进入独立 artifact，索引只保留可检索摘要、
  hash、大小与路径，短输出也写一条 tool-call index。
- 失败记录的 `error_code` 是统一错误 taxonomy 的控制码，`reported_error_code` 是工具/provider 的原始报码；
  record、artifact 与 index 三层都保留这两个字段，compact/恢复可以使用控制码，诊断不会丢失真实原因。

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
- task workspace 查询只认当前 owner 下的 `tasks/<date>/<task-slug>/work/state.json` 和
  `work/run_workspace.json`；根目录旧 `tasks/*/state.json`、旧 daily memory 和旧 root route index
  不再作为当前 owner 的事实源。
- `rollup_ledger.jsonl` 只在子代理状态签名变化时追加；心跳式保存不应制造新的 compact 包。
- `read_file` / `read_artifact` 的恢复游标来自结构化工具记录；旧状态词、summary 和人工描述不能证明某段已经读过。
- compact work-state 的 `read_coverage` 同时保留 `primary` 主游标、`sources` 多源覆盖摘要和
  `incomplete_sources` 未完成来源队列；多文件/多项目任务恢复时先从 `incomplete_sources`
  续接下一段，再用 `sources` 审计每个 source_path 的覆盖范围，不能只按一个最大文件游标续接。
- `list_files`、`find_files`、`search_text` 的分页续接来自结构化 `page_window`；
  `next_offset` 可以继续展示在工具正文里给人看，但 compact/resume 只能从 `page_window` 恢复下一页位置。
- compact handoff 的 final/running/terminal 判断只读当前协议状态：`DONE` 是 final，
  `FAILED`、`TIMEOUT`、`CHANNEL_ERROR`、`CANCELLED`、`ABANDONED` 是 terminal；
  不能用 `succeeded/completed` 这类别名补齐。
- compact rollup/handoff 的机器统计桶只使用当前协议状态或 `unknown`；旧状态原文只留在 child row / refs
  里做证据展示，不能扩散成新的机器状态。
- task compact rollup 的 `pending_run_ids` / continue packet `pending_work` 只放还需要父代理行动的状态；
  `CANCELLED`、`ABANDONED`、`TAKEN_OVER` 这类已处理终端状态只留在 status_counts 和 child rows。
- compact apply 的 id、metadata、restore refs、bundle、ledger、self-check 和 context markdown 属于同一条
  apply 链路，集中在 `compact_apply/__init__.py`；`compact_apply/work_state.py` 只负责构建续接所需的
  work-state snapshot。
- compact 连续失败熔断在 `compact_circuit_breaker.py`：状态持久化在
  `workspace/compact/circuit_breaker.json`，连续失败达阈值即 open，冷却期内 `run_memory_compact_auto_cycle`
  跳过 apply 返回 `blocked_circuit_open`，避免 thrash loop 空烧；一次成功清零回 closed。
- compact resume 的入口集中组装 consistency、recommended paths、handoff、completion prompt 和 continue
  packet；`completion.py`、`handoff.py`、`focus.py`、`failsafe.py`、`blocked.py`、`io.py` 分别保留为真实职责边界。
- `compact_resume/focus.py`、`compact_state.py` 和 `compact_work_state/archive.py` 只消费结构化工具记录、
  coverage/cursor/refs；普通 summary、next_action 或多语言状态词不能证明读取范围完成。
- task-local fact 文件只读当前模板名：`ACCEPTANCE.md`、`CONSTRAINTS.md`、`TEST_CHECKLIST.md`、`failing_tests.json`。文件名必须精确匹配，不能因为 mac/Windows 大小写行为把旧小写文件当成当前事实源。

## Artifact And Raw Output

- raw turn/tool/gateway 流水写 `audit/YYYY-MM-DD.jsonl`。
- 大工具输出写当前 task `work/blobs/tool_outputs/`，prompt 中只放摘要和 refs。
- 用户最终交付写当前 task `output/`，或用户显式指定目录；task `output/work` 仍记录索引和验收。

## Design Rules

- 普通运行不从 repo `data/*` 读取事实源。
- 新字段优先建明确业务字段，不使用通用保留槽承载业务语义。
- 索引可以重建，不能替代正文事实。
- memory 只提供事实和检索，不做任务质量硬门。
- 状态别名必须 fail closed；需要迁移旧数据时写显式迁移记录，不在 compact 读取链路里临时猜。
- main context bundle 的结构化验收字段只认 `acceptance`、`constraints`、`latest_tests`；中文字段名和旧别名只作为普通用户文本保留，不进入机器验收合同。

## 2026-06-10 compact_context_bundle 合并

- `memory_archive/compact_context_bundle/`（match.py / refs.py / `__init__` 转发）合并为单模块
  `memory_archive/compact_context_bundle.py`；对外导入路径不变（`from ..compact_context_bundle import ...`），
  匹配判定与 refs 读取在同一权威文件内。
