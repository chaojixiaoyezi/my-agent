# TEST_CHECKLIST

## 每轮开发最低检查

- [ ] `python3 -m py_compile agent_py_agent/agent/*.py agent_py_agent/__main__.py`
- [ ] `python3 -m agent_py_agent --help`
- [ ] `git diff --check`
- [ ] `python scripts/check_doc_sync.py`，确认代码、模块文档和同文件注释同步更新
- [ ] 收口前运行 `python3 agent_py_agent/tests/run_tests.py`，并确认它使用真实 API
- [ ] 确认完整冒烟打印 `FULL_SMOKE_TEST_WORKSPACE=...`，测试数据没有污染默认 `agent_py_agent/data/`
- [ ] 确认完整冒烟脚本自动发现并运行了所有 `test_*.py` / `test_` 函数

## 修改普通 run / chat 时

- [ ] `python3 -m agent_py_agent run "测试" --no-save`
- [ ] chat 手动检查 `/help`、`/status`、`/btw`、`/exit`
- [ ] 确认不会意外打印 API key
- [ ] 不使用 echo/fake backend 作为最终通过依据

## 修改本地事实源 / 记忆时

- [ ] JSONL 记忆仍然能直接写入和读取
- [ ] 新记忆会索引到 SQLite/FTS5 LocalStore
- [ ] FTS5 不可用或关闭时，LIKE fallback 仍能搜到内容
- [ ] 正文文件写入 `local_store_files_dir`
- [ ] 审计事件追加到 `local_store_events_path`
- [ ] `my-agent local-store-status` 输出路径、记录数、事件数和 FTS5 状态
- [ ] `my-agent local-doctor` 能发现 memory/LocalStore、gateway processing、subagent 工单不一致
- [ ] `my-agent local-rebuild` 能从 memory、gateway、subagent 文件事实源重建索引
- [ ] `my-agent status` 能汇总 gateway、LocalStore、subagent 和 timeline
- [ ] `my-agent status` 能输出 suggested actions
- [ ] `my-agent timeline --limit 5` 能显示最近事件
- [ ] `my-agent timeline --source-type gateway_request` 和 `--event-type ...` 能过滤
- [ ] `my-agent local-index-memory` 能补建旧 `memory.jsonl`
- [ ] `my-agent local-search "关键词" --source-type memory` 能返回命中
- [ ] 完整冒烟配置会隔离 `local_store_path`、`local_store_files_dir` 和 `local_store_events_path`
- [ ] gateway ask 能写入 `gateway_request` 记录和 gateway 事件
- [ ] subagent run 保存后能写入 `subagent_run` 记录
- [ ] runner result 能写入 `subagent_runner_result` 记录
- [ ] acceptance / patch / dispatch / watch / planner / capability route / action apply / channel probe 都有 LocalStore 记录或事件

## 修改工具系统时

- [ ] 工具目录仍能渲染
- [ ] 推荐工具仍能按任务筛选
- [ ] `[TOOL_CALL]` 解析正常
- [ ] 未授权工具会被 allowlist 拦住
- [ ] 文件工具仍限制在 workspace 内
- [ ] HTTP 工具有超时和长度限制

推荐命令见 [TESTS.md](TESTS.md) 的 Tools 部分。

## 修改 capability / skill 时

- [ ] `SKILL.md` frontmatter 解析正常
- [ ] Skill Registry 能扫描目录
- [ ] ToolSpec 能映射成 CapabilityCard
- [ ] Capability Router 能同时检索 skill/tool
- [ ] `capability_candidate_limit=0` 仍表示不限制
- [ ] grant 不展开完整 skill 正文

推荐命令见 [TESTS.md](TESTS.md) 的 Capability Router 部分。

## 修改 subagent 工单时

- [ ] 创建 run 后有标准目录和关键文件
- [ ] `task.json` / `run.json` 可读
- [ ] `QualityContract`、`ContextManifest` 和 `context_packs` 能写入 `task.json`，旧 task 缺字段时仍能读取
- [ ] 高质量/主观交付任务必须有 `QualityContract` 或等价验收说明，不能只写动作目标
- [ ] 质量契约写清 `user_visible_goal`、对标样本、坏版条件、禁止交付条件、必须检查项和最终裁决人
- [ ] 子代理角色明确为 producer / critic / repairer / reviewer 之一，不能默认让一个子代理既生产又自证质量
- [ ] `STATUS.md` / `WORK_LOG.md` / `ACCEPTANCE.md` / `DEBRIEF.md` 存在
- [ ] `ACTION_RECEIPTS.md` / `TEST_CHECKLIST.md` / `BUGS.md` / `SKILL_USAGE.md` / `HANDOFF.md` 存在
- [ ] `output.json` / `dependencies.json` 存在
- [ ] `validate_work_order()` 能发现缺失文件
- [ ] `record_takeover()` 会写 `TAKEOVER.md`
- [ ] DONE 缺 evidence 时会被拦截或 due-check 标红

## 修改 due-check / action apply 时

- [ ] due-check 能发现 fake done
- [ ] due-check 能发现 open capability request
- [ ] due-check 能发现 open gap
- [ ] due-check 能发现 heartbeat stale / run timeout
- [ ] action plan 默认 dry-run
- [ ] apply 动作必须显式 `--apply`
- [ ] takeover 需要 `--take-over-by`
- [ ] takeover/rescue 只把 `takeover_readiness.json`、failure handoff、checkpoint、artifact manifest 等 refs 放入计划或审计记录，不自动读取大 artifact 正文
- [ ] rescue packet 记录 dedupe、repeat count、retry limit、escalation target、manual confirmation 和 recovery refs，且 `auto_retry=false`
- [ ] apply 后有审计日志

## 修改 runner 时

- [ ] `subagent-run <run_id>` 默认 dry-run
- [ ] `subagent-run <run_id> --execute` 才调用模型
- [ ] 执行前默认 channel probe
- [ ] BROKEN 通道不会继续模型调用
- [ ] runner prompt 只包含 allowed tools
- [ ] runner prompt 包含必要的 core context、task context、role context，不无脑注入全量长上下文
- [ ] runner prompt 明确子代理不能定义完成标准，最终只能提交待验收材料
- [ ] runner prompt 要求不确定性进入 risks / needs_parent_decision，不能吞掉模糊边界
- [ ] 如果有 context manifest，必须记录本轮给了哪些上下文包、必读文件和质量标准
- [ ] `execution_context.json` 和 `EXECUTION_CONTEXT.md` 包含 `QualityContract`、`ContextManifest`、context packs 和 parent final gate 规则
- [ ] 未授权工具调用会失败
- [ ] `[SUBAGENT_RESULT]` 缺失或 JSON 错误会被记录
- [ ] `[SUBAGENT_RESULT]` 前文提到协议标记或 JSON 带 Markdown fence 时仍能解析最终结果块
- [ ] evidence 会写入 `task.evidence`
- [ ] 输出必须包含 checks_performed / evidence / failures / risks，不能只写“看起来可以”
- [ ] capability_requests 会写成 open request
- [ ] artifacts / tests / patches 会写入 `output.json`
- [ ] lessons / next_actions 会写入 `output.json` 和 `DEBRIEF.md`
- [ ] runner 不会直接标记 DONE
- [ ] 完整冒烟覆盖真实 API `subagent-run --execute`，并确认 backend 不是 echo
- [ ] 真实 API runner 至少完成一次授权工具调用、结构化输出和 evidence 写回

## 修改 subagent 验收时

- [ ] `subagents-acceptance` 默认 dry-run
- [ ] `subagents-acceptance --apply` 只验收等待验收的任务
- [ ] 缺 evidence 时不会标记 DONE
- [ ] 有质量契约时，验收必须检查 evidence 是否覆盖 `must_check` 和 `sampling_plan`
- [ ] 子代理自称 PASS 不能作为通过依据，必须检查产物、路径、截图、日志或抽查结果
- [ ] critic / reviewer 缺失时，高质量任务不能自动标记最终交付完成
- [ ] 对 PDF/文档类交付，至少抽查第一页、中间正文、后半段、appendix、图表密集页和参考文献页
- [ ] 有 blocker、失败 tests、未处理 patches 或未审核 applied patch 时不会标记 DONE
- [ ] 验收通过时状态变为 `DONE` 且 verification 为 `VERIFIED`
- [ ] 每次验收都会写全局报告和单任务 `ACCEPTANCE_REVIEW.md`

## 修改 subagent patch 审核时

- [ ] `subagents-patches` 默认 dry-run
- [ ] `subagents-patches --apply --run-id <run_id>` 会写回 `review_status`
- [ ] `applied` patch 只有审核为 `APPROVED` 后才允许验收通过
- [ ] `planned` / `blocked` patch 会写成 `NEEDS_ACTION` 并阻断验收
- [ ] 未知 patch 状态会写成 `NEEDS_ACTION` 并阻断验收
- [ ] 每次 apply 都会写全局 patch 审核报告、单任务 `PATCH_REVIEW.md` 和审计日志

## 修改 subagent 调度器时

- [ ] `subagents-dispatch` 默认 dry-run
- [ ] `subagents-dispatch --apply` 会写 `SUBAGENT_DISPATCH.md` 和审计日志
- [ ] dispatch 不把子代理当独立负责人；父代理保留质量标准、综合判断和最终收口
- [ ] 高质量任务优先生成 producer + critic 两类任务，critic 不能和 producer 共享“证明完成”的目标
- [ ] dispatch 能把用户/父会话给出的质量契约传入 execution context
- [ ] dispatch 不默认给所有子代理塞完整长上下文；按角色注入相关 context pack
- [ ] `--execute-runners` 不传时，不会调用模型 runner
- [ ] `--apply --execute-runners` 才会推进真实 runner，并可能消耗 API
- [ ] dispatch 顺序保持为 due-check / action apply / capability route / runner / patch review / acceptance
- [ ] dispatch apply 后能把可验收 run 收口到 `DONE/VERIFIED`
- [ ] acceptance dispatch record 可展示 parent acceptance auto-policy 的 ref、decision、action、would_execute、executed，且 executed 仍为 false
- [ ] parent acceptance auto-policy 半自动计划必须保持 `execution_mode=manual_only`、`automatic_execution_allowed=false`，`recommended_command` 只能作为人工或后续受控调度参考；dispatch JSON/Markdown 只能透传这些字段，不能执行命令
- [ ] parent acceptance auto-policy preflight 可以显示 manual_ready，但 `ready_for_automatic_execution` 必须为 false，且自动执行禁用原因必须进入 blockers
- [ ] `subagents-dispatch --watch --max-cycles 1 --interval 0` 能安全退出
- [ ] watch 模式会写 heartbeat、watch 报告和 watch 日志
- [ ] watch 只引用本轮 dispatch report/Markdown，不在 watch 层重新执行 auto-policy、tests、apply 或 rescue
- [ ] watch lock 会阻止第二个父代理同时运行
- [ ] `--planner` 在有 active/pending/stalled/needs-intervention 时会调用父代理 LLM
- [ ] planner gate 不允许有待处理事项时只返回 `HEARTBEAT_OK`
- [ ] `my-agent daemon` 会读取 `daemon_*` 配置，能解析 `daemon_max_runners: "auto"`，并能用 `--max-cycles 1` 安全退出
- [ ] `my-agent gateway start/status/stop/restart/logs` 命令存在
- [ ] `my-agent gateway run --max-cycles 1 --interval 0 --max-runners 0 --no-planner` 能写 gateway state/heartbeat 并安全退出
- [ ] `my-agent gateway stop` 通过 stop request 正常停止后台进程
- [ ] `my-agent gateway ask` 能通过后台 gateway 处理真实 API 请求并写入 `responses/<request_id>.json`
- [ ] gateway 超时 `processing` 请求会按 attempts 退回 pending 或归档 failed
- [ ] `gateway_request_workers` 大于 1 时多个 request worker 能并发抢 pending 请求且不重复覆盖响应
- [ ] `my-agent gateway result <request_id>` 能读取异步请求结果
- [ ] `my-agent chat --gateway` 会把普通聊天消息投递给后台 gateway，并且 `/status` 会显示 gateway 队列状态
- [ ] 无子命令 `my-agent` 会自动启动 gateway 并进入 gateway chat；退出 chat 不会关闭 gateway
- [ ] `my-agent adapter file --help` 可用，文件 adapter 能把 inbox JSON 转成 gateway 请求并把响应写到 outbox

## 修改 subagent workflow 模板时

- [ ] `subagent_workflow_mode` 支持 `auto/manual/off`，非法值会回退并记录 warning
- [ ] `subagent_builtin_workflows` 和 `subagent_user_workflow_dirs` 配置能被安全解析
- [ ] 内置模板能被加载，用户模板同 id 能覆盖内置模板
- [ ] 每个 workflow 模板必须有 `solves` 字段，说明解决什么问题
- [ ] 坏模板必须生成 validation issue，不能静默进入模板库
- [ ] 新增模板资源后，`pyproject.toml` package data 保证安装包能带上模板文件

## 修改文档时

- [ ] 新增重要文件后更新 `CODEBASE_TREE.md`
- [ ] 新增架构想法后更新 `DESIGN_LEDGER.md`
- [ ] 调整 gateway / daemon 形态后更新 `GATEWAY_DESIGN.md` 和 `GATEWAY_RESEARCH.md`
- [ ] 新增命令后更新 README 或 runbook
- [ ] CLI 命令或参数变化时更新 `CLI_REFERENCE.md`
- [ ] 新增测试策略后更新 `TESTS.md`
- [ ] 文档里的验收级测试说明真实 API 要求和必要环境变量

## LOG tool registry / security prompt profile
- [ ] Default ToolRegistry catalog and recommendations do not expose `security_query`, `security_hunt_ip`, or `security_trace_case`.
- [ ] Logs/security capability or explicit tool grants expose LOG security tools.
- [ ] Unauthorized LOG security tool calls fail before execution.
- [ ] LOG security tool prompt output contains bounded `summary`, `preview_rows`, and `evidence_refs`, not full `rows`.
- [ ] Analyst security prompt names match registered LOG security tools and do not mention legacy `traffic_*` names.
