# ADR-0004: File Write Boundary

LLM: Runtime writes belong to owned, ignored storage paths. All writes go through repository or write_boundary module.

给人看的解释：
agent 框架不能让模型输出随意写文件。写操作必须经过显式的边界检查。

## Status

Accepted

## Context

The agent processes untrusted model output and must execute file operations on behalf of users. Without a write boundary, model-generated code could write to arbitrary paths, overwrite source code or credentials, create files that get committed to version control, or modify agent internals. Current patch apply logic has boundary enforcement but it lives in a mixin, not a dedicated security module.

## Decision

All file writes must go through one of two mechanisms:

**Repository Pattern** -- for structured data (memory, state, config):
```python
class MemoryRepository:
    def save(self, entry: MemoryEntry) -> Path:
        self._boundary.validate_write(self._resolve_path(entry.id))
        ...
```

**WriteBoundary module** -- for ad-hoc writes (patch apply, file generation):
```python
class WriteBoundary:
    def validate_write(self, target: Path) -> None:
        """Raise WriteBoundaryViolation if target is outside allowed paths."""
```

Allowed write paths: `data/` (runtime), `data/memory/` (archives), `data/sessions/` (state), `data/local.db` (store), `data/reports/` (generated), workspace root (user-requested).

Forbidden: source code, `.git/`, config files (`.env`, `settings.json`), agent internals outside `data/`, paths outside workspace root.

## Consequences

- Security boundary enforced in code, not just prompts. Single place to audit write policies.
- PatchApply service, tool registry, gateway renderer, and memory repository all validate through WriteBoundary.
- Tests must use `tempfile.mkdtemp()` or explicit sandboxes.
- `.gitignore` blocks memory archives, runtime data, caches, and generated reports.
- Migration cost: existing write calls must be updated; some legitimate writes may initially need boundary configuration.

### References

- ADR-0001 (infrastructure layer); ADR-0002 (PatchApply as primary consumer); ARCHITECTURE_EXEMPTIONS.md E-004
