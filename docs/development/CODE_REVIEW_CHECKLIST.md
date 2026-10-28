# Code Review Checklist / 代码审查清单

Use this checklist for code reviews and self-review before handoff.  It prioritises
risk reduction over formalism.

本清单用于代码审查和提交前自查。优先看风险，不追求形式主义。

---

## 1. Architecture / 架构

- [ ] **Single responsibility** — does the module/class/function do exactly one thing?
- [ ] **Thin entry points** — do CLI entry files delegate to services, not inline logic?
- [ ] **类职责清楚** — 状态所有权与生命周期是否完整，而非承担无关领域？
- [ ] **函数职责清楚** — 正常、失败与取消路径是否直接可追踪？
- [ ] **文件边界清楚** — 拆分或合并是否改善实际调用路径，而非满足行数？
- [ ] **Module boundary** — does the change stay inside the owning module's directory?
- [ ] **Explicit dependencies** — are dependencies injected or passed, not imported
      from global singletons?
- [ ] **Naming** — no `utils.py`, `helpers.py`, `common.py` in new files?

---

## 2. Safety / 安全性

- [ ] **No `import *`** — does the codebase still have zero star-import usage?
      (Enforced by `test_architecture_guardrails.py`.)
- [ ] **No junk filenames** — no new `temp.py`, `misc.py`, `new.py`, `final.py`, etc.
- [ ] **No runtime artifacts** — no `.pyc`, `__pycache__`, `.coverage`, `.db` files
      in the diff.
- [ ] **No secrets** — no tokens, passwords, or API keys in the code or config.
- [ ] **No bare except** — all `except` clauses catch specific exception types.
- [ ] **No silent failures** — errors are raised, logged, or explicitly handled.

---

## 3. Responsibility / 职责与合同

代码尺寸数值门已于 2026-10-28 退役，不再要求 checker、报告或 baseline。

- [ ] 拆分/合并依据真实职责、状态所有权与接口合同，不以行数、参数数或 Mixin 数量决策。
- [ ] 类型化 bundle 承担真实语义、快照或生命周期责任，没有为计数新增包装与空转发。
- [ ] 导入边界、Ruff、文档同步、clean-package 与相关回归仍有对应证据。

---

## 4. File Writing / 文件写入

- [ ] **Write boundary** — durable writes go through a repository, common JSON/text writer,
      or current write-boundary tool, not ad hoc `Path.write_text()`.
- [ ] **No path traversal** — user-supplied paths are validated against workspace root.
- [ ] **No system path writes** — no writes to `/etc`, `/usr`, `/var`, `/tmp`.
- [ ] **Atomic writes** — durable writes use the temp + fsync + `os.replace` pattern.
- [ ] **Correct storage class** — runtime data goes to `data/` or `memory/`,
      not next to source files.
- [ ] **`.gitignore` updated** — new runtime paths are excluded from version control.

---

## 5. Security / 安全

- [ ] **Subagent sandboxing** — subagents cannot escape their allocated scope.
- [ ] **Input validation** — external input (CLI args, gateway messages) is validated
      before use.
- [ ] **No arbitrary code execution** — no `eval()`, `exec()`, or `__import__` with
      user-controlled input.
- [ ] **Path safety** — file paths from external sources are sanitized and confined.
- [ ] **Adapter isolation** — channel adapters (QQ, Feishu, Telegram) cannot affect
      each other's state.

---

## 6. Testing / 测试

- [ ] **Tests exist** — new behavior has corresponding test coverage.
- [ ] **Tests pass** — `python -m pytest agent_py_agent/tests/ -q` succeeds.
- [ ] **Architecture tests pass** — `test_architecture_guardrails.py` succeeds.
- [ ] **Regression test** — bug fixes include a test that reproduces the bug.
- [ ] **Filesystem safety** — tests use `tmp_path`, not the project directory.
- [ ] **Edge cases** — boundary conditions and error paths are tested.
- [ ] **No flaky tests** — tests do not depend on timing, network, or external state.

---

## 7. Documentation / 文档

- [ ] **API changes** — if public interfaces changed, docs are updated.
- [ ] **Config changes** — if config format changed, docs are updated.
- [ ] **CLI changes** — if commands or arguments changed, docs are updated.
- [ ] **Task record** — multi-file work has a task record in `docs/tasks/`.
- [ ] **Feature spec** — user-visible behavior changes have a feature spec.
- [ ] **Comments** — non-obvious decisions have inline comments explaining WHY.

---

## 8. Upgrade Safety / 升级安全

- [ ] **Main chain** — changes remove obsolete paths instead of adding bypasses.
- [ ] **CLI surface** — changed command names or arguments are documented.
- [ ] **Data format** — changed JSONL/JSON schemas have current readers and tests.
- [ ] **Python version** — code is compatible with Python 3.10+.
      No backslashes in f-strings, guarded `tomllib` import.
- [ ] **Dependency changes** — new runtime dependencies are documented and justified.

---

## 9. State Management / 状态管理

- [ ] **Audit trail** — state changes produce audit records.
- [ ] **No ghost state** — no undocumented files created at runtime.
- [ ] **Lifecycle clarity** — every new file/format has a documented lifecycle.
- [ ] **Reversibility** — the change can be rolled back if needed.

---

## 10. Review Priority / 审查优先级

When time is limited, review in this order:

1. **Security** — path traversal, input validation, sandboxing.
2. **File writing** — write boundary, storage class, `.gitignore`.
3. **Architecture** — single responsibility, state ownership, import boundaries, naming.
4. **Testing** — coverage for new behavior, architecture tests pass.
5. **Upgrade safety** — CLI, data format, Python version.
6. **Documentation** — task record, feature spec, comments.

Security and file-writing issues are the highest risk because they can cause data
loss, corruption, or privilege escalation.  Architecture issues are lower risk but
compound over time.
