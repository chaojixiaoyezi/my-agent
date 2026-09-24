# 第 7 步子代理结果链工作线交接

## 基本信息

- workstream：第 7 步子代理生命周期与交接。
- branch：`codex/step7-subagent`，位于 Codex 管理的独立工作树。
- worktree：任务所附的隔离工作树；具体本机路径不写入仓库。
- owner：子代理结果链工作线。
- date：2026-09-23。
- 核心提交：`2ca573193`、`ccb1d6cff`、`e7bce0e6a`、`1267e81ef`，起点 `f04ec3a42`；交接文档另随本次提交。

## 本线目标

降低子代理结果服务的职责混杂：把 exact attempt 准入、展示与完成信封投影、
已保存结果的持久收口和直属父级通知按真实边界分开，同时保留原 WAL、运行账和通知顺序。

## 实际完成

- 展示标签与有界完成信封各归只读投影；根父通知、递归父快照和直属父等待共用交接内容。
- 迟到、接管、废弃和换代结果的准入独立，旧私有判据与旧导出已删除。
- 结果服务在业务结果及 task 投影保存后调用唯一初次提交模块；
  该模块复用原 `runtime_closeout` WAL／运行账／恢复原语及父级 wake。
- 终态冲突诊断改用 manager 的原 RuntimeDB，恢复遗漏的结构化事件。
- 首次父级通知后若 `delivered` 标记写盘失败，保留 pending WAL；
  恢复链凭原 wake 回执补清，不重发给父级。
- 对应设计、结构、进度和测试文档已更新。尚未把这组提交集成到主工作区或安装运行。

## 改动文件

- 生产代码：`agent_py_agent/agent/subagents/runner_display_projection.py`、
  `runner_completion_payload.py`、`runner_result_admission.py`、
  `services/runner_result_commit.py`，以及这些模块的原调用方。
- 回归：`agent_py_agent/tests/test_subagent_runner_result_state.py`、
  `test_dispatch_liveness_and_revive.py`。
- 文档：`CODEBASE_TREE.md`、`TESTS.md`、`LLM_GUIDE.md`、`docs/ROADMAP.md`、
  `docs/COMPLETED.md`、`docs/design/SUBAGENT_PARALLEL_EXECUTION.md`、
  `docs/modules/subagent/02-progress.md`、`04-structure.md`、
  `docs/tasks/REFACTOR_PLUGIN_GOAL.md`。

## 测试命令和结果

- 10 个直接受影响测试文件：229 passed、4 个既有 xfail、1 个既有 skip。
- 7 个多层恢复、停止、产物和离线工作流文件：68 passed、1 个既有 xfail。
- 5 个 worker pool、作用域、创建幂等及层级合同文件：47 passed。
- `ruff check agent_py_agent scripts`、`python3 scripts/check_doc_sync.py`、
  `python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json`、
  `git diff HEAD --check`、`python3 scripts/check_clean_package.py .`：通过。
- 候选 wheel 仅在仓库外构建；四个新模块均在包内，包内 `agent_py_agent/` 文件共 1,271 个。

## 需要主线重点复查

- 主工作区另有 TUI／历史工作线的未提交修改；集成前重新核对双方 HEAD、脏文件和文档归属，
  不直接覆盖其暂存内容。特别核对 Goal、TESTS、ROADMAP、COMPLETED 的并行更新。
- 保持“结果文件与 task → WAL → RuntimeDB → 父级 wake → delivered → 清账”的顺序；
  恢复只补账和通知，不重跑业务。同一 attempt 的重复 wake 与新 attempt 的有效结果须分开。
- 新故障注入仅证明本地首次 `delivered` 标记失败后的恢复；发布包的真实 TUI 仍须取证。

## 需要其他线协调

- 共享 Gateway 的部署窗口与 TUI／历史工作线及其它活动测试协调；每台机器仍只用一个 Gateway。
- 本线只拥有子代理结果链，不改 TUI／历史、插件装配、决策模型或 Jev 文件。

## 剩余风险

- 本地回归不等于官方 MiniMax-M2.7 的原生 TUI 验收；单孩子、三个并行孩子、主子孙、
  插话、失败取消、恢复和独立主任务仍要按 Goal 的第 7.6 项实际运行并读回原账与产物。
- 代码尚未推送远端 main，也没有发包、部署或切换 Gateway。

## 后续建议

先由主线在安全窗口集成此工作树提交并复跑相关测试与严格 gate，再构建同一 wheel、保留回滚、
协调双机部署；最后用多路原生 TUI 完成第 7.6 项。TUI／历史线可以继续独立并行，
但不要同时写子代理结果链或切换本线要用的共享 Gateway。
