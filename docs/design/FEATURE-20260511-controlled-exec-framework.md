# FEATURE-20260511-controlled-exec-framework

Status: Implemented through tool dry-run/apply/trash v1

## Background / 背景

子代理后续需要使用本机 CLI、网络工具、日志分析命令、MCP 工具和 skill 脚本。但用户明确要求：子代理不能拿裸 `exec`，也不能直接 `rm`；只要用户给了任务目录权限，常见读写命令应当低摩擦，但必须受目录、命令、网络和输出预算限制。

## Goal / 目标

先做受控 exec 的功能框架：把父级 `CapabilityGrant` 编译成 shell gateway 请求，保证执行权限来自上级授权，而不是模型在工具参数里自填 allowlist；在显式 `apply=true` 且检查通过时，才进入 bounded shell execution 或 task trash。

## Non-Goals / 非目标

- 本片不新增全局裸 shell 工具。
- 本片不自动给所有子代理暴露真实执行入口；只有 runner context/allowed_tools 里出现 `controlled_exec` 且父级写入边界携带 grant 时才可用。
- 本片不实现 MCP/tool/skill registry 的完整安装和发现。
- 本片不执行 `rm`；删除类动作在 `apply=true` 时只走 task trash move。

## Scenarios / 场景

- 父级 grant 允许 `pwd`，路径范围是任务目录：受控 exec plan 允许进入 shell gateway。
- 子代理请求 `python --version`，但 grant 没有 `python`：plan 阻断。
- 父级 grant 漏了路径范围：plan 阻断，避免隐式扩大到整个 workspace。
- 子代理请求 `rm stale.txt`：plan 返回 `use_task_trash` 和 source path 提示，不执行 shell。
- 父级 grant 允许 `curl` 且只允许一个域名：其他域名继续阻断。
- runner 调用 `controlled_exec` 且 `apply=false`：只返回 dry-run plan。
- runner 调用 `controlled_exec` 且 `apply=true`：复用 `shell_gateway_execution.py`，写 bounded stdout/stderr refs 和 audit JSONL。
- runner 缺少 `controlled_exec` / shell / MCP / skill / network 能力时，先调用正式 `capability_request` 工具记录 OPEN request；不得靠写 `capability_request.json` 或改 `execution_context.json` 伪造申请。

## Requirements / 需求

| ID | Description | Priority |
|----|-------------|----------|
| FR-001 | Controlled exec shall require a parent `CapabilityGrant`. | Must |
| FR-002 | Command allowlist, path scope, network scope, and output budget shall come from the grant. | Must |
| FR-003 | Missing parent path scope shall block subagent exec planning. | Must |
| FR-004 | Delete-like commands shall route to task trash, not shell execution. | Must |
| FR-005 | Dry-run shall return refs-only planning data and not execute. | Must |
| FR-006 | Explicit apply shall execute only after the same grant/path/network/output checks pass. | Must |
| FR-007 | Tool params shall not be allowed to self-authorize command/path/network scope. | Must |

## Constraints / 约束

- 不引入新依赖。
- 接口必须是 bundle/dataclass。
- 继续复用已有 `shell_gateway.py` 和 `task_trash.py` 策略，不复制安全逻辑。
- 输出预算仍由 shell gateway 归一化和执行层截断；工具结果只回传预览、字节数、截断标记和 refs。

## Impact / 影响

- 新增 `agent/subagents/controlled_exec_gateway.py`。
- 新增 `agent/tooling/controlled_exec.py`，并在 `ToolRegistry` 注册 `controlled_exec`。
- 新增 `agent/agent_core/capability_request_tool.py`，并在 orchestration tools 中注册 `capability_request`。
- 新增 `test_subagent_controlled_exec_gateway.py`。
- 新增 `test_subagent_controlled_exec_tool.py`。
- 新增 `test_subagent_capability_request_tool.py`。
- 更新 subagent 结构文档、开发规范、文件树和进度文档。

## Architecture / 架构

`ControlledExecRequest` 接收命令、workspace、cwd、task_dir 和父级 grant。`plan_controlled_exec()` 先识别删除类命令，如果是 `rm` / `rmdir` / `unlink`，直接返回 `use_task_trash`。其他命令必须是 `grant_type="shell"` 且有 `path_scope`，然后由 grant 字段构造 `ShellGatewayRequest`，交给 `plan_shell_command()` 继续做命令、cwd、网络和输出预算检查。`tooling/controlled_exec.py` 是 registry-aware 工具包装层：从 `write_boundary.controlled_exec_grants` 选 grant，忽略模型参数里的 allowlist/path_scope；`apply=true` 时才调用 `execute_shell_command()` 或 `move_to_task_trash()`。

## Data Model / 数据模型

- `ControlledExecRequest`: grant-backed exec planning 输入包。
- `ControlledExecPlan`: refs-only 输出，包含 `allowed`、`action`、`blockers`、可选 `shell_decision` 和 `trash_hint`。
- `ControlledExecToolRequest`: registry 注入 workspace/write_boundary 后的工具执行包。
- `CapabilityGrant`: 已有父级授权事实源。
- `CapabilityRequest`: runner 通过 `capability_request` 工具写入的正式父级可路由申请；状态从 `OPEN` 开始，route 后变为 `GRANTED` 或 gap。

## State Transitions / 状态转换

- request + grant + `apply=false` -> `execute_shell` dry-run plan.
- request + grant + `apply=true` -> `execute_shell` execution metadata + stdout/stderr/audit refs.
- request + delete command + `apply=true` -> `move_to_task_trash` result + manifest ref.
- request + missing/invalid grant scope -> `blocked` plan.

## File Writes / 文件写入

Dry-run 不新增运行时写入。`apply=true` 的 shell 执行由 `shell_gateway_execution.py` 写 bounded stdout/stderr refs 和 audit JSONL；删除替代由 `task_trash.py` 写 manifest。

## Test Plan / 测试计划

- grant scope 编译测试。
- 未授权命令阻断测试。
- 缺路径 scope 阻断测试。
- 删除命令转 trash hint 测试。
- 网络 scope 传递和阻断测试。
- registry catalog / write_boundary grant 选择测试。
- `apply=true` shell 执行和大 stdout 外置截断测试。
- `rm` -> task trash move 测试。
- runner prompt capability request scope 模板测试。

## Acceptance Criteria / 验收标准

- [x] 子代理不能通过工具参数自授权命令。
- [x] grant 的 path/network/output scope 会进入 shell gateway。
- [x] 缺 path scope 会阻断。
- [x] `rm` 会转 task trash 提示。
- [x] 测试覆盖核心规划边界。
- [x] `controlled_exec` 工具不能通过模型参数自授权。
- [x] 显式 apply 会写 bounded refs/audit，不回传大输出正文。
- [x] 删除类动作只移动到 task trash。
- [x] 缺能力时有正式 `capability_request` 工具入口，OPEN request 不会被 `AWAITING_ACCEPTANCE` 收口误清理。

## Risks / 风险

- 删除命令只提取第一个非 option 参数作为 trash source，复杂批量删除后续应拆成显式多条 trash move。
- 多进程和长任务累计输出预算还没统一到 gateway control-plane。
- MCP/tool/skill 安装发现仍未接入；当前只是 capability request 字段和 prompt 预留口。

## Rollback / 回滚方案

移除 `controlled_exec_gateway.py` 和调用方即可；已有 shell gateway、trash 和 capability grant 数据模型不受影响。
