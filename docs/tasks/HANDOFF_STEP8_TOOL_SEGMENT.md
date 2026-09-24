# Step 8 工具并发段判定交接

## 基本信息

- workstream: 工具轮连续并发段的窄依赖切片。
- branch: `codex/step8-tool-segment`。
- worktree: `../my-agent-worktrees/step8-loop-entry`。
- baseline: `d939f6cf3`。
- owner: 并发段切片代理；中央文档与组合验收由主代理负责。
- date: 2026-09-23。

## 本线目标

让连续并发段判定只依赖 canonical 调用、批上限和两种按需查询，不再接收整个 Agent 或工具轮请求。工具轮继续独占审批、取消、线程桥与 provider 顺序记账，不引入第二个执行器、持久状态或转发 facade。

## 实际完成

- 新增 `segment_planning.py`，承接连续段扫描、批上限合并和既有整数解析规则。
- `round_execution.py` 在原位置装配有效批上限及 Compact、调度描述查询；每条候选仍先查询 Compact，再读取该调用的原快照、工作根和写边界。
- 任务属性优先于配置，只有缺省时才读取对应配置。并发负数或缺省仍采用 8，显式 0 不限制，每批数量非正数不限制，两个正上限取较小值。
- 屏障、冲突和批上限命中后不查询后续调用；Compact、调度描述和冲突异常原样传播。布尔值无效，整数转换仍只捕获 `TypeError` 与 `ValueError`。
- 取消、审批、并行执行和记账函数保持原实现；原工具轮测试函数保持原断言，只追加直接边界测试。

## 改动文件

- `agent_py_agent/agent/agent_core/tool_loop/round_execution.py`。
- `agent_py_agent/agent/agent_core/tool_loop/segment_planning.py`。
- `agent_py_agent/tests/test_tool_round_execution.py`。
- `agent_py_agent/tests/test_tool_segment_planning.py`。
- `docs/tasks/HANDOFF_STEP8_TOOL_SEGMENT.md`。

## 测试命令和结果

使用临时 `HOME`、`MY_AGENT_HOME` 和 `XDG_CONFIG_HOME` 的独立子进程，未调用真实模型或 Gateway。

```bash
python3 -m pytest agent_py_agent/tests/test_tool_segment_planning.py agent_py_agent/tests/test_tool_round_execution.py -o addopts= -q --tb=short
ruff check agent_py_agent/agent/agent_core/tool_loop/round_execution.py agent_py_agent/agent/agent_core/tool_loop/segment_planning.py agent_py_agent/tests/test_tool_round_execution.py agent_py_agent/tests/test_tool_segment_planning.py
python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json
git diff --check
```

- 新增合同与交错用例 21 项通过；两份直接测试共 65 项通过，前者包含在后者中。
- 覆盖顺序屏障、冲突停止扫描、动态 Compact 交错、异常发生前零执行/零记账、批上限、数值优先级和既有整轮执行合同。
- Ruff、严格尺寸检查与 diff 检查通过。尺寸报告是本地生成物，未纳入本切片提交。
- AST 对比确认既有取消、审批、并发执行、记账和工具轮测试函数均未改实现。
- 日志：`/tmp/my-agent-step8-tool-segment-contract.log`、`/tmp/my-agent-step8-tool-segment-focused.log`、`/tmp/my-agent-step8-tool-segment-review.log`。

## 影响范围与主线复查

- 新窄模块只决定可接纳的连续范围；被屏障或批上限留下的调用仍由原工具轮继续处理，不减少 provider turn 的调用数量。
- `ToolRoundExecutionRequest.agent` 其余真实消费者保持原样，未把整个请求换名传入新模块。
- 需重点保持原外层 Compact 预检与逐候选 Compact 查询的次数和顺序，不能把两次查询合并，也不能提前缓存整段事实。
- `_tool_loop_service` 的收口整理、Compact 实现和 no-action gate 常量归属不在本切片范围。

## 协调与剩余风险

- 主代理统一更新文件树、模块设计、测试索引及中央进展文档，并负责与独立收口片的组合验收。
- 本片没有发现未解决的行为变化；未使用真实模型验收，也未推送或部署。

## 建议下一步

主代理先将本片与已验收的收口片集成，再统一补中央文档和运行组合 gate。只读审查可并行；在集成完成前避免其他代理同时修改工具轮扫描与批上限装配，以守住 Compact 查询和审批记账的顺序边界。
