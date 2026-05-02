# ShellTool Workstream — HANDOFF

## 做了什么

添加了 `run_command` Shell 执行工具，让 agent 能在工作区执行终端命令。

## 新增文件

- `agent_py_agent/agent/tooling/shell.py` — ShellTool 实现

## 修改文件

| 文件 | 改动 |
|------|------|
| `agent_py_agent/agent/tooling/registry.py` | 添加 `shell_tool_timeout` 参数；实例化并注册 `ShellTool` |
| `agent_py_agent/agent/settings/config.py` | 添加 `tool_shell_timeout: int = 30` 字段和 coerce 规则 |
| `agent_py_agent/config/agent_config.yaml` | 添加 `tool_shell_timeout: 30` |
| `agent_py_agent/tests/test_tools/test_shell_tool.py` | 新建，22 个测试用例 |
| `agent_py_agent/tests/test_tools/backends.py` | `make_tool_registry()` 添加 `shell_tool_timeout=30` |
| `agent_py_agent/tests/test_capabilities.py` | 两处 `ToolRegistry()` 初始化添加 `shell_tool_timeout=30` |
| `agent_py_agent/tests/test_tools/test_tool_loop.py` | 4 处 `ToolRegistry()` 初始化添加 `shell_tool_timeout=30` |

## ShellTool 功能说明

- **工具名**: `run_command`，分类 `shell`
- **参数**: `command`（必填）、`timeout`（默认 30 秒）、`working_dir`（默认工作区根目录）
- **安全控制**:
  - 危险命令黑名单（`rm -rf /`、`mkfs`、`dd`、`shutdown`、`reboot`、`halt` 等）
  - 命令长度上限 2000 字符
  - 空命令/纯空白命令拒绝
  - subprocess timeout 强制终止
- **输出格式**: `return_code=<n>\nstdout=<>\nstderr=<>`

## 测试结果

```
22 passed in 6.11s
```

所有测试用例通过：正常执行、working_dir、timeout、超时降级、危险命令拒绝、空/空白/超长命令校验、stderr 捕获、非零返回码、spec 验证。

## 依赖变更

无新增外部依赖，使用标准库 `subprocess`。

## 待注意事项

- ShellTool 继承 `BaseTool`（而非 `FileSystemTool`），不强制路径限制在 workspace_root 内，因为 shell 命令本身可能需要访问系统命令
- `working_dir` 参数只是设置 subprocess 的 cwd，不做路径安全校验（与 FileSystemTool 的 resolve_path 逻辑不同）