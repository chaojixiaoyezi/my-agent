# Testing Rules / 测试规则

Tests prove that code works **and** that the architecture does not continue to
degrade.  This project enforces both functional correctness and structural hygiene.

测试不仅证明功能正确，也证明架构没有继续恶化。本项目同时强制功能正确性和结构卫生。

---

## 1. Test Categories / 测试类别

| Category          | Purpose                                        | Speed   | When to Run          |
|-------------------|------------------------------------------------|---------|----------------------|
| **Unit**          | Pure functions, data models, helpers           | Fast    | Every commit         |
| **Integration**   | Multi-module workflows, real file I/O          | Medium  | Every PR             |
| **Contract**      | CLI argument parsing, output format stability  | Fast    | Every commit         |
| **Regression**    | Bug fix proof -- test that reproduces the bug  | Fast    | Every commit         |
| **Architecture**  | Structural guardrails: imports, naming, sizes  | Fast    | Every commit         |
| **E2E / Stress**  | Full dispatch flow, concurrency, memory push   | Slow    | Before release       |

---

## 2. Test File Naming and Location / 测试文件命名与位置

- All tests live under `agent_py_agent/tests/`.
- File naming: `test_<module_or_feature>.py`.
- Class naming: `Test<ClassName>` or `Test<FeatureDescription>`.
- Function naming: `test_<behavior_description>`.
- Architecture guardrails: `test_architecture_guardrails.py` (single file, do not split).

---

## 3. Filesystem Safety in Tests / 测试中的文件系统安全

- **Never write to the project directory in tests.**
- Use the pytest `tmp_path` fixture for all filesystem operations.

```python
# CORRECT
def test_write_something(tmp_path):
    target = tmp_path / "output.json"
    safe_write(target, '{"key": "value"}')
    assert target.read_text() == '{"key": "value"}'

# WRONG -- writes to project directory
def test_write_something():
    Path("output.json").write_text("data")  # FORBIDDEN
```

If a test needs to exercise real paths, use `monkeypatch` to redirect the base
directory to `tmp_path`.

---

## 4. Architecture Guardrails Tests / 架构护栏测试

`agent_py_agent/tests/test_architecture_guardrails.py` enforces:

- **Zero star-import baseline** -- no new `import *` may be introduced.
- **Entrypoint line limits** -- entry files must not exceed their frozen baselines.
- **Junk filename baseline** -- no new `utils.py`, `helpers.py`, `common.py`, etc.
- **Runtime artifact detection** -- `.pyc`, `__pycache__`, `.coverage` must not be committed.
- **Size limit enforcement** -- new files/functions/classes must be within limits.

These tests **must pass** on every commit.  If they fail, fix the violation or
update the baseline with a documented reason and a task record.

---

## 5. Writing Good Tests / 编写好的测试

**Test Isolation** -- each test must be independent.  Use fixtures (`conftest.py`)
for shared setup and `monkeypatch` for environment variables.

**Naming:** `test_<what>_<when>_<expected>()`.  Example:
`test_dispatch_loop_when_no_tasks_returns_empty()`.

**Assertions** -- use specific assertions:

```python
# BAD:  assert result
# GOOD: assert result.status == "ok" and result.count == 3
```

**Fixtures** -- shared in `conftest.py`, test-specific in the test file.
Keep fixtures minimal; prefer factory functions for complex objects.

---

## 6. Verification Commands / 验证命令

Run these commands before committing:

```bash
python -m compileall -q agent_py_agent scripts          # Syntax check
python -m pytest agent_py_agent/tests/ -q                # Full test suite
python -m pytest agent_py_agent/tests/test_architecture_guardrails.py -q  # Guardrails
ruff check agent_py_agent                                # Lint
python scripts/check_code_size.py                        # Size limits
git diff --check                                         # Whitespace errors
```

For focused development: `python -m pytest agent_py_agent/tests/test_memory_routing_matcher.py -q -v`

---

## 7. Coverage / 覆盖率

```bash
python -m pytest agent_py_agent/tests/ --cov=agent_py_agent --cov-report=term-missing
```

Target: 80%+ for new code.  Coverage is a minimum bar, not a goal.
Focus on state transitions, error paths, and boundary conditions.

---

## 8. Test Categories by Marker / 按标记分类测试

```python
@pytest.mark.slow
def test_full_dispatch_flow(): ...

@pytest.mark.integration
def test_memory_push_with_real_archive(tmp_path): ...
```

Run: `python -m pytest agent_py_agent/tests/ -q -m "not slow"`

---

## 9. Regression Tests / 回归测试

When fixing a bug:

1. Write a test that **reproduces the bug** before fixing it.
2. Verify the test **fails** without the fix.
3. Apply the fix, verify the test **passes**.
4. Name the test: `test_<module>_<bug_description>`.

This ensures the bug cannot silently return.

---

## 10. Pre-Release Testing / 发布前测试

Before tagging a release:

1. Full test suite: `python -m pytest agent_py_agent/tests/ -q`
2. Architecture guardrails: `test_architecture_guardrails.py -q`
3. E2E flows: `test_e2e_*.py -q`
4. Compile + lint: `compileall -q` + `ruff check`
5. No uncommitted changes: `git status`
