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

Run focused tests while developing, then choose the final gate based on whether
the change will be pushed to the remote repository.

开发中先跑 focused tests；收尾时按是否提交远端选择验收强度。

### 6.1 Local-only development / 只做本地开发

If the work stays local and will not be pushed yet, run the checks that match
the risk of the change:

如果这轮只是本地探索、草稿、小切片开发，暂时不推远端，按改动风险运行：

```bash
python -m compileall -q agent_py_agent scripts          # Syntax check
python -m pytest agent_py_agent/tests/test_memory_routing_matcher.py -q -v  # Focused example
python scripts/check_doc_sync.py                         # Docs sync when docs/modules changed
python scripts/check_code_size.py --mode warn            # Size warnings for code changes
git diff --check                                         # Whitespace errors
```

### 6.2 Remote push / PR / main merge strict gate

Before pushing a branch, updating a remote PR, or merging into `main`, run the
strict local gate below. This rule still applies when GitHub Actions is disabled,
blocked by billing, or intentionally deferred until the next quota cycle.

推送远端分支、更新远端 PR、或合并到 `main` 前，必须跑下面的本地严格 gate。GitHub Actions 被关闭、被账单阻塞、或计划下个月再开时，也不能跳过这一步。

```bash
python3 -m pytest -q --tb=short                          # Full test suite
ruff check agent_py_agent scripts                        # Lint
python3 scripts/check_doc_sync.py                        # Docs sync
python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json  # Hard/high-risk/soft gate
git diff --check                                         # Whitespace errors
python3 scripts/check_clean_package.py .                 # Package cleanliness
```

Local ruff note: on the desktop machine, ruff is also available at
`/Users/example/ai_claw/bin/ruff`. If `python3 -m ruff` is missing in the
current interpreter, use `ruff check ...` from PATH or that explicit binary.

If any strict-gate command fails, do not push or merge by default. Only bypass
with an explicit user instruction, and record the failing command plus risk in
the final report.

如果严格 gate 任一命令失败，默认不得推送或合并。只有用户明确要求绕过时才允许，并且最终汇报必须写清失败命令和风险。

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

## 9.1 Natural Prompt E2E Discipline / 自然语言 E2E 约束

- Real user-style E2E prompts should avoid internal implementation terms such as
  `dispatch`, `workflow_template_id`, `run_id`, or `structured contract` unless
  the test is explicitly a protocol test.
- For subagent delegation E2E, the prompt may say in plain language:
  `你自己不要做，你要陪我聊天，你只派小傻妞/子代理做。` This is a test/user
  instruction for the root model, not a product-code rule.
- Do not hardcode that sentence, or any similar Chinese/English prose, into
  runtime guards. If the behavior must become deterministic, first introduce a
  machine field such as `delegate_only=true` or `refs_only=true`, then test that
  the natural phrase alone does not trigger the hard guard.
- Business E2E tasks should be realistic enough to expose orchestration issues.
  Example baseline: `用单文件html做一个高端现代家具品牌的网站首页，风格高级、简洁、有设计感，适合真实商业品牌使用。只输出完整html，不要注释。`

---

## 10. Pre-Release Testing / 发布前测试

Before tagging a release:

1. Full test suite: `python -m pytest agent_py_agent/tests/ -q`
2. Architecture guardrails: `test_architecture_guardrails.py -q`
3. E2E flows: `test_e2e_*.py -q`
4. Compile + lint: `compileall -q` + `ruff check`
5. No uncommitted changes: `git status`
