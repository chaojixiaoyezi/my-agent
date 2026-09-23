# 子代理完整 Compact 恢复交接

## 基本信息

- workstream：决策模型第 12.4 项，child overflow 接入。
- branch：`codex/decision-model-integration`；独立决策工作区，基线 `5aa08c7e4`。
- owner：决策线主代理；独立测试由 sol high 执行，边界审查由 Astra max 执行。
- date：2026-09-23。

## 本线目标

子代理选定模型后若遇上下文溢出，原 Compact 必须按真实下一请求检查容量，不能只量摘要或重复执行宿主准备。

## 实际完成

Gateway 与 child 共用 `PreparedCompactRecovery` 的一次准备、完整候选计量、原 checkpoint/CAS 和同次发送。`tool_request_capture.py` 共用正常选模及恢复的实际请求捕获；原 Gateway 重复实现已移除。

child 在原 overflow 分支只读加载来源，下一次真实 `agent.run` 的 render/select 执行 Compact。候选只投影独立线程历史与第 0 注入，其余身份、权限、系统、上下文包和工具保持。无 transcript 来源时仍先执行原 active-turn 归档提交。

## 改动文件

- `agent_core/compact_request_recovery.py`、`tool_request_capture.py`：公共恢复与捕获。
- `agent_core/subagent/compact_recovery.py`、`run_flow.py`、`model_selection.py`：子代理接线与首请求捕获复用。
- `conversation/agent_thread.py`：延迟来源与纯历史投影。
- `gateway_compact_recovery.py`、`gateway_model_adoption.py`、`gateway_parts/request_execution.py`、`model_request_selection.py`：消费公共实现，原发送事务保持。
- 子代理新增 HTTP 测试、Gateway 测试定位及子代理展示失效时点断言；测试结果见 `TESTS.md`。

## 测试命令和结果

仓库虚拟环境运行 `python -m pytest -o addopts='' <相关文件> -q --tb=short`；相关文件和最终联合数量记录在 `TESTS.md`，不以各轮重跑累加覆盖数。

新增测试走真实 SimpleAgent、runner、store/CAS、provider builder，仅最终 HTTP 替身。覆盖两协议 × 工具开关、取消 token、代次冲突、摘要瞬时错误和来源读取失败。19 文件联合408项通过，尺寸拆分后的child三文件31项通过；独立审查的工具接续和活动回合恢复已保存为 `test_subagent_compact_recovery_continuation.py`，另4项通过。

## 影响范围

只改内部恢复与参数准备，无新增配置、模型请求重试、持久 schema 或授权方式。默认关闭的决策设置保持；Compact 自身继续原有行为。

## 需要主线重点复查

公共恢复 scope 只覆盖当前实际运行；源码捕获必须跟随已准备的真实参数。延期清除旧展示发生在真正恢复准备时；摘要保留原 builder 的无候选占位段，不等于旧推荐复活。

## 需要其他线协调

已向模块重构负责人同步精确文件；不改 TUI、HTTP 连接或 owner 池，不部署/重启。媒体线认领 `_native_initial_tool_ir_history`、UserTurn/media 和 provider 编码，本片不修改；后续跨模型模态容量须与其正式交接对齐。

## 剩余风险

后台 detached task 的局部历史与全局摘要适用性尚未接入；初次加载/手动入口仍未使用完整恢复输入。真实供应商 tokenizer、缓存命中、跨窗口和媒体组合不在本片证明范围。前轮全仓短期限失败保持未收口。

## 后续建议

建议下一步：先为后台准备一次冻结的结构化上下文与历史范围，再接公共恢复器；后台进度可能写账，不能对候选重跑 `context_markdown`。共享 core 串行实施，独立 HTTP/失败测试与只读审查可并行；第 12.4 整项及总 Goal 继续保持未完成。
