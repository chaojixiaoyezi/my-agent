# Acceptance Real Execution 设计文档

## 一句话结论

验收不能只看子代理"填表"，必须有系统级真实执行验证。当前实现新增 `TestExecutionRecord` 记录测试命令的真实退出码/stdout/stderr，并通过 `subagents-tests --re-run`、`subagents-acceptance --execute-tests`、配置 `acceptance_execute_tests: true` 或 `AcceptanceReviewOptions(execute_tests=True)` 显式执行 allowlist 内的测试命令；默认验收路径暂不自动执行。

## 需求背景

### 当前问题

现有验收流程：

```
子代理输出 → tests: [{name: "xxx", ok: true, summary: "测试通过"}]
           → 验收时只检查 tests 字段是否存在、ok 是否为 true
           → 但这个 ok 是子代理自己填的，没有系统验证
```

风险：
- 子代理可能写了"测试通过"但没真的跑测试
- 子代理可能跑了一个空命令就标记 ok
- 父代理只看证据文字，无法区分"真完成"和"假完成"

### 目标

```
子代理输出 → tests: [{name: "xxx", command: "python3 -m pytest -q", ...}]
           → 验收时系统自动执行测试命令
           → 记录真实退出码、stdout、stderr
           → 保存为 TestExecutionRecord
           → 验收结论基于真实执行结果
```

## 核心设计

### 1. TestExecutionRecord 数据模型

```python
@dataclass
class TestExecutionRecord:
    """测试执行的真实记录。"""
    test_name: str                          # 测试名称
    command: str                            # 执行的命令
    executed: bool = False                  # 是否真的执行了
    exit_code: int = -1                     # 退出码（-1 表示未执行）
    stdout: str = ""                        # 标准输出（截断到 4000 字符）
    stderr: str = ""                        # 标准错误（截断到 4000 字符）
    duration_seconds: float = 0.0           # 执行耗时
    executed_at: str = ""                   # 执行时间 ISO
    error: str = ""                         # 执行异常信息
    validation_method: str = ""             # 验证方式：command/file_check/content_check
    validation_result: dict[str, Any] = {}  # 验证结果详情
```

### 2. 测试验证方式

支持三种验证方式：

#### 2.1 命令执行验证（command）

```python
{
    "name": "pytest_pass",
    "command": "python3 -m pytest -q",
    "validation_method": "command"
}
```

系统会真的执行命令，记录退出码/stdout/stderr。

#### 2.2 文件存在验证（file_check）

```python
{
    "name": "output_file_exists",
    "file_path": "output/result.json",
    "validation_method": "file_check"
}
```

系统检查文件是否存在，记录文件大小/修改时间。

#### 2.3 内容检查验证（content_check）

```python
{
    "name": "output_contains_success",
    "file_path": "output/result.json",
    "content_pattern": '"status": "success"',
    "validation_method": "content_check"
}
```

系统检查文件内容是否包含指定模式。

### 3. 测试执行器

新增 `TestExecutor` 类，负责执行测试并生成 `TestExecutionRecord`：

```python
class TestExecutor:
    """测试执行器。"""
    
    # 命令 allowlist（复用 patch apply 的 allowlist）
    ALLOWED_PREFIXES = {
        "python", "python3", "pytest", "pip",
        "node", "npm", "npx",
        "go", "cargo", "make",
        "test", "bash", "sh",
    }
    
    # 阻止的 shell 字符
    BLOCKED_CHARS = {"|", "&", ";", ">", "<", "`", "$"}
    
    # 超时限制
    DEFAULT_TIMEOUT_SECONDS = 120
    MAX_TIMEOUT_SECONDS = 300
    
    def __init__(self, workspace_root: Path, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS):
        self.workspace_root = workspace_root
        self.timeout_seconds = min(timeout_seconds, self.MAX_TIMEOUT_SECONDS)
    
    def execute(self, test: dict[str, Any]) -> TestExecutionRecord:
        """执行单个测试。"""
        validation_method = test.get("validation_method", "command")
        
        if validation_method == "command":
            return self._execute_command(test)
        elif validation_method == "file_check":
            return self._check_file(test)
        elif validation_method == "content_check":
            return self._check_content(test)
        else:
            return TestExecutionRecord(
                test_name=test.get("name", "unknown"),
                command="",
                executed=False,
                error=f"未知验证方式: {validation_method}",
            )
    
    def _execute_command(self, test: dict[str, Any]) -> TestExecutionRecord:
        """执行命令测试。"""
        command = str(test.get("command", "")).strip()
        name = test.get("name", command[:50])
        
        # 验证命令安全性
        error = self._validate_command(command)
        if error:
            return TestExecutionRecord(
                test_name=name,
                command=command,
                executed=False,
                error=error,
            )
        
        # 执行命令
        start_time = time.time()
        try:
            argv = shlex.split(command)
            completed = subprocess.run(
                argv,
                cwd=self.workspace_root,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
            duration = time.time() - start_time
            
            return TestExecutionRecord(
                test_name=name,
                command=command,
                executed=True,
                exit_code=completed.returncode,
                stdout=completed.stdout[-4000:],
                stderr=completed.stderr[-4000:],
                duration_seconds=duration,
                executed_at=utc_now_iso(),
                validation_method="command",
                validation_result={"ok": completed.returncode == 0},
            )
        except subprocess.TimeoutExpired:
            return TestExecutionRecord(
                test_name=name,
                command=command,
                executed=False,
                error=f"命令超时 ({self.timeout_seconds}s)",
                validation_method="command",
            )
        except Exception as exc:
            return TestExecutionRecord(
                test_name=name,
                command=command,
                executed=False,
                error=str(exc),
                validation_method="command",
            )
    
    def _validate_command(self, command: str) -> str:
        """验证命令安全性。"""
        if not command:
            return "空测试命令"
        if any(char in command for char in self.BLOCKED_CHARS):
            return f"测试命令包含高风险 shell 字符"
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return f"测试命令解析失败: {exc}"
        if not argv:
            return "空测试命令"
        if argv[0] not in self.ALLOWED_PREFIXES:
            return f"测试命令不在 allowlist 内: {argv[0]}"
        return ""
```

### 4. 集成到验收流程

修改 `acceptance_helpers.py` 的 `_build_output_and_capability_findings()`：

```python
def _build_output_and_capability_findings(
    task: SubAgentTask,
    output: dict[str, object],
    created_at: float,
    artifact_exists_fn: Callable[[str], bool],
    test_executor: TestExecutor | None = None,  # 新增参数
) -> list[AcceptanceReviewFinding]:
    # ... 现有代码 ...
    
    tests = _dict_list(output.get("tests", []))
    
    # 如果有 test_executor，执行测试
    test_records: list[TestExecutionRecord] = []
    if test_executor and tests:
        for test in tests:
            record = test_executor.execute(test)
            test_records.append(record)
    
    # 检查测试结果
    if test_records:
        # 使用真实执行结果
        failed_tests = [r for r in test_records if not r.validation_result.get("ok", False)]
        not_executed = [r for r in test_records if not r.executed]
        
        findings.append(
            AcceptanceReviewFinding(
                name="tests_executed",
                ok=not not_executed,
                severity="P1",
                message=(
                    f"所有 {len(test_records)} 条测试已执行。"
                    if not not_executed
                    else f"有 {len(not_executed)} 条测试未执行: {[r.test_name for r in not_executed]}"
                ),
                evidence_path=task.test_execution_json,  # 新增文件
                created_at=created_at,
            )
        )
        findings.append(
            AcceptanceReviewFinding(
                name="tests_passed",
                ok=not failed_tests,
                severity="P0",  # 从 P1 升级到 P0
                message=(
                    f"所有 {len(test_records)} 条测试通过。"
                    if not failed_tests
                    else f"有 {len(failed_tests)} 条测试失败: {[r.test_name for r in failed_tests]}"
                ),
                evidence_path=task.test_execution_json,
                created_at=created_at,
            )
        )
    else:
        # 兼容旧逻辑：只检查 tests 字段
        failed_tests = [item for item in tests if not bool(item.get("ok", False))]
        findings.append(
            AcceptanceReviewFinding(
                name="tests_passed",
                ok=not failed_tests,
                severity="P1",
                message=(
                    f"runner 记录的 {len(tests)} 条测试均通过（未真实执行）。"
                    if tests and not failed_tests
                    else "runner 未记录测试，允许仅凭证据进入人工验收。"
                    if not tests
                    else f"存在 {len(failed_tests)} 条失败测试。"
                ),
                evidence_path=task.output_json,
                created_at=created_at,
            )
        )
    
    # ... 现有代码 ...
```

### 5. 测试执行记录存储

测试执行结果保存到任务目录：

```
task_dir/
  ├── output.json              # runner 输出
  ├── test_execution.json      # 新增：测试执行记录
  ├── test_execution.md        # 新增：测试执行报告（人类可读）
  └── ...
```

`test_execution.json` 格式：

```json
{
  "executed_at": "2026-05-02T10:00:00Z",
  "workspace_root": "/path/to/workspace",
  "timeout_seconds": 120,
  "total_tests": 3,
  "executed": 3,
  "passed": 2,
  "failed": 1,
  "records": [
    {
      "test_name": "pytest_pass",
      "command": "python3 -m pytest -q",
      "executed": true,
      "exit_code": 0,
      "stdout": "...",
      "stderr": "",
      "duration_seconds": 5.2,
      "validation_result": {"ok": true}
    },
    {
      "test_name": "syntax_check",
      "command": "python3 -m py_compile main.py",
      "executed": true,
      "exit_code": 1,
      "stdout": "",
      "stderr": "SyntaxError: ...",
      "duration_seconds": 0.1,
      "validation_result": {"ok": false}
    }
  ]
}
```

### 6. 配置项

```yaml
# agent_config.yaml

# 验收时是否默认执行测试命令
# - true: subagents-acceptance 默认执行 tests 里的 allowlist 验证
# - false: 只检查 tests 字段，不真实执行（兼容旧模式）
acceptance_execute_tests: false

# 测试执行超时（秒）
# - 单个测试命令的最大执行时间
# - 当前允许 1 到 300 秒，超出范围会回退默认值
acceptance_test_timeout_seconds: 120

# 测试命令 allowlist
# - 只有这些前缀的命令允许执行
# - 当前先使用 TestExecutor 内置 allowlist，配置项后续接入
# acceptance_test_allowed_prefixes: []

# 测试执行结果保留天数
# - 超过这个天数的 test_execution.json 会自动清理
# - 0 表示不清理
acceptance_test_retention_days: 30
```

### 7. CLI 命令

```bash
# 查看某个任务的测试执行记录
my-agent subagents-tests <run_id>

# 显式重新执行某个任务的测试
my-agent subagents-tests <run_id> --re-run --timeout 120

# 本次验收显式执行真实 tests
my-agent subagents-acceptance --execute-tests --test-timeout 120

# 查看测试执行统计（待做）
# my-agent subagents-tests --stats
```

## 与现有系统的关系

| 现有组件 | 改动 |
|----------|------|
| `acceptance_helpers.py` | 集成 TestExecutor，使用真实执行结果 |
| `manager_patch.py` | 复用 `_run_patch_apply_tests` 的逻辑到 TestExecutor |
| `models.py` | 新增 `test_execution_json` 字段 |
| `agent_config.yaml` | 新增 `acceptance_execute_tests` 和 `acceptance_test_timeout_seconds` |
| `cli/subagents.py` | 新增 `subagents-tests` 命令；`subagents-acceptance` 支持 `--execute-tests` / `--no-execute-tests` / `--test-timeout` |

## 安全边界

- 测试命令必须在 allowlist 内，防止命令注入
- 测试执行有超时限制，防止卡死
- stdout/stderr 截断到 4000 字符，防止内存溢出
- 高风险 shell 字符被阻止（`|`, `&`, `;`, `>`, `<`, `` ` ``, `$`）

## 向后兼容

- `acceptance_execute_tests: false` 时使用旧逻辑，只检查 tests 字段
- 旧的 runner 输出（没有 command 字段的 tests）走人工验收
- test_execution.json 是新增文件，不影响现有流程

## 实现计划

### 第一阶段：核心执行器

1. 新增 `TestExecutionRecord` 数据模型（已落地第一片：`agent_py_agent/agent/subagents/execution_records.py`，当前只做记录模型、序列化、stdout/stderr 截断和 `passed` 派生结果）
2. 实现 `TestExecutor` 类（已落地第一片：`agent_py_agent/agent/subagents/execution_executor.py`，当前不会自动影响 acceptance 状态）
3. 实现命令安全性验证（已落地第一片：shell=False、基础 allowlist、高风险 shell 字符拦截、超时记录）
4. 实现三种验证方式（command/file_check/content_check）（已落地第一片：command 真实执行、file_check 元数据检查、content_check 字面量包含检查）

### 第二阶段：集成验收

1. 修改验收流程集成 TestExecutor（已落地第一片：`AcceptanceReviewOptions(execute_tests=True)` 显式开启；dry-run 生成执行报告和 P0 阻断 finding，默认不自动执行）
2. 新增 `test_execution.json` 存储逻辑（已落地第一片：`write_test_execution_report()` 写 JSON 事实源和 Markdown 展示报告，当前由调用方显式触发）
3. 新增配置项（已落地：`acceptance_execute_tests` 默认关闭，`acceptance_test_timeout_seconds` 默认 120；`subagents-acceptance` 的 CLI 参数可覆盖配置）

### 第三阶段：CLI 和报告

1. 新增 `subagents-tests` CLI 命令（已落地：默认查看 `test_execution.json`，显式 `--re-run` 才读取 `output.json.tests` 并重跑）
2. 生成人类可读的 `test_execution.md` 报告（已落地）
3. 新增测试执行统计（待做）

### 第四阶段：测试和文档

1. 单元测试覆盖 TestExecutor
2. 集成测试：模拟各种测试场景
3. 更新 CLI_REFERENCE.md
4. 更新 CODEBASE_TREE.md
