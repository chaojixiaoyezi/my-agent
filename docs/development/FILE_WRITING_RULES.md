# File Writing Rules / 文件写入规则

File writes are the most common source of ghost state, orphaned data, and security
holes.  Every write in my-agent must have a clear owner, a clear storage class,
and a clear lifecycle.

文件写入是"幽灵状态"最常见的来源。本规则要求每次写入都有明确的归属、存储类别和生命周期。

---

## 1. Storage Class Categories / 存储类别

Every write in the system falls into exactly one of these categories:

| Category             | Target Path Pattern                         | Committed? | Lifecycle          |
|----------------------|---------------------------------------------|------------|--------------------|
| **SourceCode**       | `agent_py_agent/**/*.py`                    | Yes        | Permanent          |
| **Test**             | `agent_py_agent/tests/**/*.py`              | Yes        | Permanent          |
| **Documentation**    | `docs/**/*.md`, `*.md`                      | Yes        | Permanent          |
| **TaskRecord**       | `docs/tasks/active/`, `docs/tasks/completed/` | Yes     | Until archived     |
| **RuntimeData**      | `agent_py_agent/data/`, `agent_py_agent/memory/`, `agent_py_agent/memory_archive/` | **No** | Session-bound |
| **Audit**            | `agent_py_agent/data/audit/`                | **No**     | Rotated/pruned     |
| **GeneratedArtifact**| `tmp/`, `build/`, pytest `tmp_path`         | **No**     | Ephemeral          |

- **RuntimeData** includes: memory JSONL files, task state DBs, token profiles,
  adapter session state, cached indices, lock files.
- **Audit** includes: dispatch audit logs, state-change event logs, gateway I/O logs.
- **GeneratedArtifact** includes: build outputs, coverage reports, generated docs.

---

## 2. The Write Boundary / 写入边界

All writes from business logic must go through the file tools or write-boundary
utility.  The current runtime policy is:

1. **Shared path policy** — main agents and subagents use the same
   `path_access_mode` and `path_dangerous_roots`.
2. **Normal mode** — `path_access_mode=normal` allows ordinary absolute output
   paths, including user-specified directories outside `workspace_root`, but
   blocks configured dangerous roots such as system and credential directories.
3. **Full mode** — `path_access_mode=full` disables the dangerous-root path
   block.  Catastrophic shell commands are still protected by command policy.
4. **No hidden path whitelist** — `workspace_root` is only the relative path base
   and default cwd. Ordinary output authority comes from the current path policy,
   the task workspace, and any explicit user-requested output directory.
   Relative source/input reads use the real workspace/cwd; task workspace paths
   are for task outputs, work files, audits, and child-agent process files.
5. **Resolved path check** — symlinks and `..` are resolved before applying the
   dangerous-directory policy.
6. **No runtime ledger pollution in source workspaces** — subagent runner guard
   state, recovery snapshots, and artifact registries must be rooted in the
   current task workspace or agent work dir. User/source workspaces may contain
   requested product files, but not `.agent_delivery`, `memory/hooks`, or
   `data/artifacts` runtime ledgers created by the framework.
7. **Large tool outputs are task work data** — complete large tool outputs are
   archived under the current task `work/blobs/tool_outputs/`; they must not be
   written into hidden directories inside the user/source workspace.

```python
# CORRECT — runtime code uses the shared atomic writer
from agent_py_agent.agent.common.json_io import write_json_file_atomic

write_json_file_atomic(target_path, payload)

# WRONG — direct durable writes from business code
target_path.write_text(content)  # FORBIDDEN in business code
```

Model-visible file changes should use the `write_file` / `apply_patch` tools so
the tool gateway can record provenance, path policy, and audit events.

---

## 3. Atomic Write Pattern / 原子写入模式

For any durable write (RuntimeData, Audit), use the atomic pattern:

```
1. Write to a temporary file in the same directory (same filesystem)
2. fsync the temporary file
3. os.replace(tmp_path, target_path)  — atomic on POSIX
4. fsync the parent directory (optional, for crash safety)
```

Why: a crash mid-write should never leave a half-written file.  `os.replace` is
atomic on POSIX and will not corrupt the target if the process is killed.

Use existing repository writers, `agent_py_agent.agent.common.json_io` helpers,
or the current filesystem write tool implementation in
`agent.tooling._filesystem_write`. Do not add a facade just to wrap writes.

---

## 4. Business Code Must Not Write Directly / 业务代码禁止直接写入

Business code (anything under `agent/`, `cli/`, or command handlers) must **not**
use any of the following directly:

- `Path.write_text()` / `Path.write_bytes()`
- `open(path, "w")` / `open(path, "wb")`
- `shutil.copy()` / `shutil.move()` for state files
- `os.makedirs()` for state directories

Instead, use the appropriate repository or service:

| Need                        | Use                            |
|-----------------------------|--------------------------------|
| Write memory entry          | `MemoryArchive` or `LocalStore`|
| Write task record           | `TaskRepository`               |
| Write audit event           | `Audit` class                  |
| Write config change         | `ConfigManager`                |
| Write temp/test file        | pytest `tmp_path` fixture      |
| Write generated artifact    | model-facing `write_file` / `apply_patch`, or the owning service writer |

---

## 5. Runtime Data Must Not Be Committed / 运行时数据禁止提交

The `.gitignore` must exclude:

```
agent_py_agent/data/
agent_py_agent/memory/
agent_py_agent/memory_archive/
*.db
*.sqlite
.coverage
.pytest_cache/
__pycache__/
```

If you introduce a new durable format, add the path to `.gitignore` **and** document
the format in `docs/design/` before implementation.

The architecture guardrails test (`test_architecture_guardrails.py`) scans for
accidental commits of runtime artifacts (`.pyc`, `.coverage`, `__pycache__`, etc.).

---

## 6. Test File Writes / 测试中的文件写入

- Tests must **never** write to the project directory.
- Use the pytest `tmp_path` fixture for all filesystem tests.
- The `tmp_path` fixture provides a unique temporary directory per test function,
  automatically cleaned up after the test.

```python
def test_something(tmp_path):
    target = tmp_path / "output.json"
    write_json_object(target, {"key": "value"})
    assert target.exists()
```

If a test needs to exercise the real data directory, use a monkeypatch to redirect
the base path to `tmp_path`.

---

## 7. Write Failure Handling / 写入失败处理

- If a write can fail and the caller does not explicitly accept best-effort, the
  failure must be raised (not swallowed).
- Log the target path, the error, and enough context for diagnosis.
- For atomic writes: clean up the temporary file on failure.
- The caller is responsible for retry or user notification.

---

## 8. Adding a New Write Path / 新增写入路径

Before adding a new write path:

1. Determine the storage class (see table above).
2. Document the path, format, and lifecycle in a design note.
3. Add the path to `.gitignore` if it is RuntimeData, Audit, or GeneratedArtifact.
4. Implement through an existing repository, common JSON/text writer, or current write-boundary tool.
5. Add a test that exercises the write using `tmp_path`.
6. Update the architecture guardrails baseline if needed.
