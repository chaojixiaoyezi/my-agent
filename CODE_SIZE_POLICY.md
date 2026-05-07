# CODE SIZE POLICY

LLM: Enforce this policy with `scripts/check_code_size.py --mode warn` (CI) or `--mode strict` (pre-release).

给人看的解释：
这份规范控制文件、函数、类和嵌套的规模上限。目的是防止项目继续长成大文件和万能 Manager。所有数值来自 check_code_size.py 的实际检测结果。

---

## 1. File Size Limits

Limits are expressed as **ideal / warn / hard** line counts. "Warn" triggers a CI annotation; "hard" blocks merge.

| Category | Ideal | Warn | Hard | Examples |
|----------|------:|-----:|-----:|----------|
| **Business logic** | 250 | 400 | 600 | `agent/agent_core/*.py`, `agent/subagents/*.py`, `agent/memory_archive/*.py` |
| **CLI registration** | 200 | 300 | 400 | `cli/parser.py`, `cli/commands/*.py`, `cli/subcommands_*.py` |
| **CLI interactive / TUI** | 250 | 400 | 500 | `cli/chat.py`, `cli/gateway_loops.py`, `cli/scenario.py` |
| **Service** | 250 | 350 | 500 | `agent/gateway_parts/runtime.py`, `agent/log_analysis/tools.py` |
| **Repository / Store** | 200 | 300 | 400 | `agent/local_store.py`, `agent/memory_store/*.py` |
| **Manager / Facade** | 80 | 120 | 200 | `agent/subagents/manager_base.py`, any `*_facade.py` |
| **Test** | 400 | 700 | 900 | `tests/test_*.py` |
| **Config / Settings** | 200 | 350 | 500 | `agent/settings/config.py`, `agent/config.py` |

### Exceeding Limits

- **80% of Warn -> Warn**: surfaced as `high-risk` / near-soft in `CODE_SIZE_REPORT.md`.
  These findings are visible in `--mode strict`, but they do not block unless they also exceed an existing hard-blocking rule.
- **Ideal -> Warn**: allowed, but reviewer should ask "can this be split?"
- **Warn -> Hard**: file must have an entry in `ARCHITECTURE_EXEMPTIONS.md` or be actively refactoring.
- **Above Hard**: merge is blocked in `--mode strict`. In `--mode warn` it produces a CI warning that must be acknowledged.

---

## 2. Function Size Limits

| Category | Ideal | Warn | Hard |
|----------|------:|-----:|-----:|
| **Normal function / method** | 40 | 60 | 100 |
| **CLI handler** | 60 | 100 | 150 |
| **Parser / builder** | 80 | 120 | 180 |
| **Test function** | 60 | 120 | 200 |

A function over the hard limit must either be split or registered in the exemption table with a concrete extraction plan.

---

## 3. Class Size Limits

| Category | Ideal | Warn | Hard |
|----------|------:|-----:|-----:|
| **Normal class** | 150 | 250 | 350 |
| **Mixin** | 120 | 200 | 250 |
| **Manager / Facade** | 120 | 200 | 300 |
| **Service** | 150 | 250 | 350 |
| **Test class** | 200 | 400 | 600 |

A class over the hard limit is a signal that it owns too many responsibilities. Extract a service or split via composition.

---

## 4. Complexity Limits

| Metric | Ideal | Warn | Severe | Forbidden |
|--------|------:|-----:|-------:|----------:|
| **Cyclomatic complexity** (per function) | <= 8 | 9 - 12 | 13 - 15 | > 15 |
| **Parameter count** | <= 4 | 5 - 6 | 7 - 8 | > 8 |
| **Nesting depth** | <= 2 | 3 | 4 | > 4 |

### Parameter Count

- Instance/class method receivers (`self` / `cls`) do not count toward the parameter total.
- 5-6 parameters: consider an options dataclass or a context object.
- 7-8 parameters: must refactor before merge.
- \> 8 parameters: hard block; no exemption.

### Nesting Depth

- Depth 3: acceptable for parsers and tree walkers only.
- Depth 4: allowed only with an exemption entry.
- Depth > 4: forbidden; extract inner logic into a helper.

---

## 5. Exemption Process

Every exemption must be registered in `ARCHITECTURE_EXEMPTIONS.md` with all of the following fields:

| Field | Required | Description |
|-------|----------|-------------|
| **File / Function** | Yes | Exact path or qualified name |
| **Current Size** | Yes | Lines (file) or cyclomatic (function) |
| **Why Can't Split Now** | Yes | Concrete blocker (test gap, dependency, etc.) |
| **Risk** | Yes | What breaks if size grows further |
| **Split Plan** | Yes | Target structure after refactoring |
| **Owner** | Yes | Person responsible |
| **Expiry Date** | Yes | Date by which split must be completed |

No permanent exemptions are allowed. An exemption that passes its expiry date without action becomes a merge blocker.

---

## 6. CI Check Mode

### `--mode warn` (default, always on)

- Produces annotations for all violations.
- Does not block merge.
- Must run on every PR.

### `--mode strict` (pre-release gate)

- Blocks merge for any hard-limit violation without an active exemption.
- Blocks merge for any function/class over hard limit.
- Enabled during release candidate cycles.

### Recommended CI Pipeline

```yaml
code-size-check:
  script: python scripts/check_code_size.py --mode warn
  allow_failure: false
```

For release branches, switch to `--mode strict`.

---

## 7. Forbidden Patterns

The following are always forbidden for new code:

- `import *` (star imports)
- Vague filenames: `utils.py`, `helpers.py`, `common.py`, `manager_extra.py`
- Adding new features to files already at hard limit without an exemption entry
- Creating new top-level runtime directories without an ADR

---

## 8. Metrics Reference

Run the checker:

```bash
# Warning mode (CI default)
python scripts/check_code_size.py --mode warn

# Strict mode (release gate)
python scripts/check_code_size.py --mode strict

# Current snapshot
python scripts/check_code_size.py --mode warn 2>&1 | tail -5
```

Current strict snapshot (2026-05-07): **277 findings** (0 hard, 277 high-risk, 0 soft), with `CODE_SIZE_BASELINE.json` loaded. Target: keep hard and soft findings at 0 and reduce high-risk findings before they become soft violations.
