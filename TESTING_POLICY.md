# TESTING POLICY

## 测试分层

| 层级 | marker | 说明 | 何时写 |
| --- | --- | --- | --- |
| unit | (无) | 单个模块/函数的纯逻辑测试 | 所有新功能必须写 |
| integration | `integration` | 跨模块协作测试 | 修改模块接口时写 |
| e2e | `e2e` | 端到端工作流测试 | 修改核心流程时写 |
| slow | `slow` | 压力测试、长时间场景测试 | 性能相关改动时写 |
| security | `security` | 权限、认证、边界测试 | 修改权限/auth 时写 |
| contract | `contract` | Schema 和接口契约测试 | 修改对外接口时写 |
| regression | `regression` | 已修复 bug 的回归测试 | 修 bug 时必须写 |

## pytest marker 使用规范

在 `pyproject.toml` 中注册所有 marker：

```toml
[tool.pytest.ini_options]
markers = [
  "slow: stress or long-running scenario tests",
  "e2e: end-to-end workflow tests",
  "integration: integration tests across modules",
  "security: permission, auth, and boundary tests",
  "contract: schema and interface contract tests",
  "regression: tests for previously fixed bugs",
]
```

使用方式：

```python
import pytest

@pytest.mark.slow
def test_stress_something():
    ...

@pytest.mark.e2e
def test_full_workflow():
    ...
```

## 测试命令

### PR 默认测试（快速）

```bash
python3 -m pytest -q -m "not slow and not e2e" --tb=short --timeout=60
```

### 全量测试

```bash
python3 -m pytest -q --tb=short
```

### 慢测试

```bash
python3 -m pytest -q -m slow --tb=short
```

### 安全测试

```bash
python3 -m pytest -q -m security --tb=short
```

### E2E 测试

```bash
python3 -m pytest -q -m e2e --tb=short
```

### 架构护栏测试

```bash
python3 -m pytest agent_py_agent/tests/test_architecture_guardrails.py -q --tb=short
```

## 测试命名规范

- 测试文件：`test_<module>.py` 或 `test_<feature>.py`
- 测试类：`Test<Feature>`
- 测试函数：`test_<behavior>_<condition>`
- 示例：`test_dispatch_returns_error_when_locked`

## fixture 规范

- 公共 fixture 放在 `conftest.py`
- 模块级 fixture 放在模块顶部
- 使用 `tmp_path` 而非手动创建临时目录
- fixture 命名用 snake_case，不带 `test_` 前缀

## golden fixture 规范

- 存放在 `agent_py_agent/tests/fixtures/` 或 `tests/golden/`
- 使用 `.json` 或 `.yaml` 格式
- 命名：`golden_<feature>_<scenario>.json`
- 版本控制中必须包含 golden fixture
- 修改 golden fixture 需要在 PR 中说明原因

## contract test 规范

- 验证模块对外接口的输入/输出 schema
- 使用 `@pytest.mark.contract` 标记
- 修改公共接口时必须更新 contract test

## 新功能必须补测试的要求

1. 新增模块必须有对应的 `test_<module>.py`
2. 新增公共函数必须有 unit test
3. 新增 CLI 命令必须有 parser test
4. 修 bug 必须写 regression test（用 `@pytest.mark.regression`）
5. 新增对外接口必须有 contract test

## CI 集成

- PR 默认运行快速测试（排除 slow 和 e2e）
- 合并到 main 后运行全量测试
- nightly 运行 slow 和 security 测试
