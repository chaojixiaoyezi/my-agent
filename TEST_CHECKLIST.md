# TEST_CHECKLIST

## 每轮开发最低检查

- [ ] `python3 -m py_compile agent_py_agent/agent/*.py agent_py_agent/__main__.py`
- [ ] `python3 -m agent_py_agent --help`
- [ ] `git diff --check`
- [ ] 收口前运行 `python3 agent_py_agent/tests/run_tests.py`，并确认它使用真实 API
- [ ] 确认完整冒烟打印 `FULL_SMOKE_TEST_WORKSPACE=...`，测试数据没有污染默认 `agent_py_agent/data/`
- [ ] 确认完整冒烟脚本自动发现并运行了所有 `test_*.py` / `test_` 函数

## 修改普通 run / chat 时

- [ ] `python3 -m agent_py_agent run "测试" --no-save`
- [ ] chat 手动检查 `/help`、`/status`、`/btw`、`/exit`
- [ ] 确认不会意外打印 API key
- [ ] 不使用 echo/fake backend 作为最终通过依据

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
- [ ] `STATUS.md` / `WORK_LOG.md` / `ACCEPTANCE.md` / `DEBRIEF.md` 存在
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
- [ ] apply 后有审计日志

## 修改 runner 时

- [ ] `subagent-run <run_id>` 默认 dry-run
- [ ] `subagent-run <run_id> --execute` 才调用模型
- [ ] 执行前默认 channel probe
- [ ] BROKEN 通道不会继续模型调用
- [ ] runner prompt 只包含 allowed tools
- [ ] 未授权工具调用会失败
- [ ] `[SUBAGENT_RESULT]` 缺失或 JSON 错误会被记录
- [ ] `[SUBAGENT_RESULT]` 前文提到协议标记或 JSON 带 Markdown fence 时仍能解析最终结果块
- [ ] evidence 会写入 `task.evidence`
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
- [ ] `--execute-runners` 不传时，不会调用模型 runner
- [ ] `--apply --execute-runners` 才会推进真实 runner，并可能消耗 API
- [ ] dispatch 顺序保持为 due-check / action apply / capability route / runner / patch review / acceptance
- [ ] dispatch apply 后能把可验收 run 收口到 `DONE/VERIFIED`
- [ ] `subagents-dispatch --watch --max-cycles 1 --interval 0` 能安全退出
- [ ] watch 模式会写 heartbeat、watch 报告和 watch 日志
- [ ] watch lock 会阻止第二个父代理同时运行

## 修改文档时

- [ ] 新增重要文件后更新 `CODEBASE_TREE.md`
- [ ] 新增架构想法后更新 `DESIGN_LEDGER.md`
- [ ] 新增命令后更新 README 或 runbook
- [ ] 新增测试策略后更新 `TESTS.md`
- [ ] 文档里的验收级测试说明真实 API 要求和必要环境变量
