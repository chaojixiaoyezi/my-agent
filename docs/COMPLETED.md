# COMPLETED

已落地功能清单。每条记录包含：解决问题、落地内容、验证方式。

更新时机：从 ROADMAP.md 移入时填写完整记录。

---

## 基础设施

### 大文件按职责拆分完成

解决问题：`subagent.py`(4.8k行)、`__main__.py`(2.7k行)、`core.py`(1.6k行) 等文件职责混杂，难以维护。

落地内容：
- `agent_py_agent/cli/`：`__main__.py` 拆成 common、local、subagents、daemon、gateway、adapter、scenario、chat、parser
- `agent_py_agent/agent/agent_core/`：`core.py` 拆成 runtime、subagent、dispatch、planner、runner prompt、runner dispatch、orchestration tools
- `agent_py_agent/agent/tooling/`：`tools.py` 拆成 models、filesystem、web、parser、registry、write_boundary
- `agent_py_agent/agent/gateway_parts/`：`gateway.py` 拆成 paths、io、process_control、logging、recovery、runtime、adapter
- `agent_py_agent/agent/local_storage/`：`local_store.py` 拆成 models、schema、records、search、events、maintenance
- `agent_py_agent/agent/subagents/`：拆成 models/reports/rendering/parsing/policies/probe 和多组 manager mixin

验证：py_compile 全量通过，生产文件无超过 500 行。

### 第二轮目录归位

解决问题：根目录仍然平铺大量文件，真实实现和兼容入口混在一起。

落地内容：
- 根目录只保留兼容门面，真实实现进职责目录
- 新增 `agent_py_agent/agent/DIRECTORY_GUIDE.md` 作为目录地图
- 新增未来目录：`clients/`、`repositories/`、`observability/`、`security/`、`validators/`

验证：旧导入路径全部兼容，测试全量通过。

### 文档基线更新

解决问题：README 停留在早期简版，与当前 capability/subagent/runner 体系不匹配。

落地内容：
- 重写根目录 `README.md` 和 `agent_py_agent/README.md`
- 新增 `SUBAGENT_RUNBOOK.md`
- 更新 `TESTS.md`、`TEST_CHECKLIST.md`、`CODEBASE_TREE.md`

验证：`check_doc_sync.py` 通过。

### 安装后命令入口

解决问题：用户需要 `python3 -m agent_py_agent` 才能运行，不够直接。

落地内容：
- `pyproject.toml` + console script `my-agent`
- `agent_py_agent/__init__.py`

验证：安装后直接 `my-agent` 可用。

### 能力路由配置

解决问题：子代理/skill/tool 授权参数不放 `agent_config.yaml`，避免主配置变成杂物间。

落地内容：
- `agent_py_agent/config/capability_config.yaml`
- `agent_py_agent/agent/capability_config.py`

验证：配置文件独立，数字限制项 `0` 表示不限制。

### Qwen XML-ish 工具调用兼容

解决问题：真实模型可能输出 XML-ish 方言而非标准 `[TOOL_CALL]` JSON，导致解析失败。

落地内容：
- `ToolRegistry.parse_tool_calls()` 新增 XML-ish `<tool_call>` 方言解析
- 工具名/参数名别名自动归一化
- 半截 XML-ish 返回 `__parse_error__` 而非崩溃

验证：新增工具解析回归测试 4 组。

### 大文件拆分第一步

解决问题：`__main__.py` 不应该长期承载 gateway 文件协议细节。

落地内容：
- 新增 `agent_py_agent/agent/gateway.py`
- 新增 `ARCHITECTURE_GUIDE.md`
- 关键函数补了 LLM contract + Human version 两层注释

验证：旧导入兼容，测试通过。

---

## Gateway

### 第一版本地 Gateway 控制面

解决问题：需要本地后台外壳接受用户消息并触发完整 LLM turn。

落地内容：
- `gateway` 命令族：start/status/stop/restart/logs
- 控制面文件：pid、state、heartbeat、stop request、log
- 配置项：workspace、heartbeat_interval、stale_seconds、stop_timeout

验证：`my-agent gateway start/status/stop` 可用。

### Gateway 本地消息入口

解决问题：gateway 不能只是后台调度壳子，需要接受用户消息。

落地内容：
- `my-agent gateway ask "<prompt>"` / `my-agent gateway result <request_id>`
- 请求目录：pending/processing/done/responses + JSONL
- gateway 后台 request worker

验证：投递请求 → worker 处理 → 读取结果完整链路。

### Chat 接入 Gateway

解决问题：chat 应能成为 gateway 客户端，退出 chat 不关闭 gateway。

落地内容：
- `my-agent chat --gateway`
- 复用 `submit_gateway_ask()` 写入 gateway inbox
- `--gateway-timeout` 覆盖等待时间

验证：chat --gateway 可发送消息并收到响应。

### 默认入口自动进入 Gateway Chat

解决问题：用户安装后应直接敲 `my-agent` 使用，不需要先学 gateway start。

落地内容：
- argparse 子命令改为可选
- `cmd_default()` 自动 `ensure_gateway_started()` 后进入 gateway chat

验证：直接 `my-agent` 可用。

### Gateway 说明白话化

解决问题：`gateway ask/result` 的定位和队列目录含义不清晰。

落地内容：
- `GATEWAY_DESIGN.md` 增加大白话解释
- `CLI_REFERENCE.md` 增加示例
- 代码注释完善

验证：文档同步。

### Gateway 参数分层

解决问题：普通用户不应理解 interval/max-runners/limit 等底层调度参数。

落地内容：
- 用户层：`task_max_subagents`、`task_max_grandchildren`
- 未来层：`scheduler_mode`、`runner_concurrency`、`runner_start_rate` 等
- `daemon_max_runners` 默认 `auto`

验证：配置文件注释和默认值。

### Gateway 独立主体与委托模型

解决问题：多 gateway 不应是"主完整、副低配"关系，每个 gateway 都是完整独立 agent。

落地内容：
- `GATEWAY_DESIGN.md` 增加多 gateway 组织模型
- 明确 identity/grant/delegation 三权分立
- schema 预留 gateway_id/agent_identity_id 等字段

验证：设计文档。

### Organization Gateway Model

解决问题：my-agent 可以组成组织，支持多级 gateway 邀请和授权。

落地内容：
- `GATEWAY_DESIGN.md` 增加组织模型
- invite key 字段设计
- 组织事件日志设计

验证：设计文档。

---

## Subagent

### Subagent 框架

解决问题：subagent 不只是"拆出 thought/plan"，而是可追踪的运行节点。

落地内容：
- `SubAgentCard`、`SubAgentTask`、`task.json`/`run.json` 双写
- `thought.md` 增加 Capability Boundary 区块
- `SubAgentBoardItem`/`SubAgentBoard`、`subagent_board.json`
- `/subagents` 和 `/subagent <id>` 查看运行详情
- hot list 浮出风险旗标

验证：`test_agent.py` 覆盖。

### Subagent 验收器

解决问题：runner 完成后不能自己标记 DONE，需要独立验收入口。

落地内容：
- `AcceptanceReviewFinding`/`AcceptanceReviewRecord`/`AcceptanceReviewReport`
- `subagents-acceptance` 命令，默认 dry-run
- 验收检查：工单完整、通道非 BROKEN、有 ok evidence、无失败 evidence、无 open capability request/gap、tests 不失败、patches 已审核

验证：`test_agent.py` 覆盖验收链路。

### Subagent Runner Entry

解决问题：子代理 runner 需要一条可审计链路：execution context → runner prompt → 模型执行 → 工单回写。

落地内容：
- `SimpleAgent.run_subagent()`：读取执行上下文，生成 runner prompt
- `SubAgentManager.record_runner_result()`：结果写回工单
- `parse_subagent_runner_output()`：解析 `[SUBAGENT_RESULT]` 结构化 JSON
- `subagent-run <run_id>` 默认 dry-run，`--execute` 调用模型
- runner 输出文件：RUNNER_RESULT.md、runner_result.json、runner_prompt.md、runner_response.md、output.json

验证：冒烟测试覆盖 dry-run 和真实 API 执行。

### Runner actual_tools 系统证据

解决问题：模型自称的 `used_tools` 不可靠，需要系统记录真实工具调用。

落地内容：
- `record_runner_result(..., actual_tools=[...])` 落成系统证据
- 验收优先信任系统真实工具记录

验证：新增回归测试。

### Subagent Due-check

解决问题：父代理不能 spawn 后放养，必须周期性查看子代理真实产出。

落地内容：
- `SubAgentManager.due_check()`/`write_due_check()`
- `subagents-due-check` CLI
- P0/P1/P2 问题分级

验证：CLI 可运行，报告输出正常。

### Due-check Action Plan

解决问题：due-check 负责发现问题，action plan 负责把问题转成下一步动作。

落地内容：
- `ActionPlanItem`/`ActionPlanReport`
- `subagents-plan-actions` CLI
- 10 种动作类型

验证：CLI 可运行，dry-run 输出正常。

### Action Apply v1

解决问题：action plan 需要受控执行层，必须默认 dry-run。

落地内容：
- `SubAgentManager.apply_actions()`/`write_action_apply_report()`
- `subagents-apply-actions` 默认 dry-run
- 安全边界：不删文件、不覆盖已有内容、takeover 必须指定接管者

验证：`test_agent.py` 覆盖。

### Capability Request Routing

解决问题：子代理不直接看全局 skill/tool 宇宙，只提交 capability request，由父代理路由。

落地内容：
- `CapabilityRouteRecord`/`CapabilityRouteReport`
- `subagents-route-capabilities` CLI
- 命中 → capability grant，未命中 → capability gap

验证：CLI 可运行，apply 会写 grant/gap。

### 主代理自然语言派工工具

解决问题：chat 里的主代理不能稳定创建工单和触发调度。

落地内容：
- `create_subagents`、`subagent_board`、`dispatch_subagents` 工具
- `dispatch_subagents` 默认 dry-run

验证：工具级回归测试覆盖。

### 父代理一轮调度器

解决问题：需要可审计命令推进任务树。

落地内容：
- `SimpleAgent.dispatch_subagents()`
- `subagents-dispatch` 命令
- 按 due-check → action apply → capability route → runner → patch review → acceptance 顺序

验证：dispatch 单测覆盖。

### 父代理 watch 模式

解决问题：一轮 dispatch 退出后不会继续巡检。

落地内容：
- `SimpleAgent.watch_subagents()`
- `subagents-dispatch --watch`
- `--interval`、`--max-cycles`、`--force-lock`
- 运行锁、heartbeat、审计日志

验证：CLI 可运行，`--max-cycles` 可正常退出。

### 父代理 LLM planner

解决问题：规则调度器缺少父代理自己读状态并给出行动建议的一层。

落地内容：
- `ParentPlannerParsedOutput`/`ParentPlannerRecord`/`ParentPlannerReport`
- `SimpleAgent.run_parent_planner()`
- `subagents-dispatch --planner`
- gate：有 active/pending/stalled/needs-intervention 时不允许只回 HEARTBEAT_OK

验证：planner 单测覆盖。

### 配置驱动 daemon 入口

解决问题：`subagents-dispatch --watch --planner --apply --execute-runners` 太长。

落地内容：
- `daemon_*` 配置项
- `my-agent daemon` 前台入口
- 默认安全：不写回、不执行 runner

验证：`my-agent daemon` 可启动。

---

## 测试

### 测试默认真实 API

解决问题：echo/fake backend 不能作为最终通过依据。

落地内容：
- `run_tests.py` 改为自动发现所有 `test_*.py`/`test_` 函数
- 完整冒烟默认调用真实 API

验证：完整冒烟通过。

### 真实 API E2E、测试隔离和 patch 审核链

解决问题：完整冒烟需要覆盖真实 API 的 subagent-run --execute，且不能污染默认记忆。

落地内容：
- `run_tests.py` 创建临时配置隔离 memory_path 和 subagent_workspace
- 冒烟覆盖：创建工单 → subagent-run --execute → 验收 DONE/VERIFIED
- 新增 `PatchReviewRecord`/`PatchReviewReport`、`subagents-patches`
- 验收器新增 `patches_reviewed` 和 `patch_status_valid` 检查

验证：完整冒烟通过。

### 隔离全流程场景测试

解决问题：需要"看完整流程"而不是分散的单元测试。

落地内容：
- `workspace_root` 配置
- `my-agent scenario-test`
- 每次创建独立临时目录，隔离 gateway/主代理/runner

验证：`scenario-test --case happy` 通过。

### 坏天气场景测试第一版

解决问题：真正要长期可靠，需要把失败和恢复场景做成可重复测试。

落地内容：
- `--case verification`：伪造完成被验收拒绝
- `--case gateway-restart`：旧 gateway 崩溃恢复
- `--case structured-repair`：JSON 损坏修复
- `--case runner-retry`：临时错误重试

验证：各场景独立通过。

---

## 本地存储

### 本地事实源

解决问题：文件优先可读，但需要结构化账本。

落地内容：
- `LocalStore` 第一版：SQLite + FTS5 + JSONL
- `JsonlMemory` 双写
- gateway/subagent/run/patch 等已接入 LocalStore
- `status`/`timeline` 观察入口

验证：`test_local_store.py` 覆盖。

### 本地恢复、诊断、worker 和 adapter 第一版

解决问题：LocalStore 一致性诊断、gateway 请求崩溃恢复。

落地内容：
- `local-doctor`/`local-rebuild`
- gateway failed 归档、processing lease、超时重排
- `adapter file` 文件协议
- `runner_concurrency` 配置入口

验证：CLI 可运行，gateway 崩溃恢复场景通过。
