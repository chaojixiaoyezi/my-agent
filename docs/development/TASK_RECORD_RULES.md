# Task Record Rules / 任务记录规则

Task records are the audit trail for multi-file architecture or feature work.
They answer: what was done, why, what changed, and how to verify.

任务记录是多文件架构或功能工作的审计线索。它们回答：做了什么、为什么、改了什么、如何验证。

---

## 1. When to Create a Task Record / 何时创建任务记录

Create a task record when the work:

- Spans **3 or more files** across different modules.
- Changes public CLI commands, arguments, or output format.
- Introduces a new file write path or storage format.
- Modifies the adapter, gateway, or subagent infrastructure.
- Requires a feature spec (see `FEATURE_SPEC_RULES.md`).

Do **not** create a task record for:

- Single-file bug fixes with no architectural impact.
- Documentation-only changes.
- Mechanical refactors that change no behavior.

---

## 2. File Naming Convention / 文件命名规范

```
TASK-YYYYMMDD-HHMM-short-name.md
```

Examples:
- `TASK-20260503-1430-memory-push-mode.md`
- `TASK-20260501-0900-failure-introspection.md`

- `short-name` uses lowercase, hyphens only, max 40 characters.
- The timestamp is the **creation** time, not the completion time.

---

## 3. File Location / 文件位置

| Status       | Path                                 |
|--------------|--------------------------------------|
| Active       | `docs/tasks/active/TASK-*.md`        |
| Completed    | `docs/tasks/completed/TASK-*.md`     |
| Paused       | `docs/tasks/paused/TASK-*.md`        |
| Cancelled    | `docs/tasks/cancelled/TASK-*.md`     |

Move the file to the appropriate directory when the status changes.
Do not delete task records; they are part of the project audit trail.

---

## 4. Required Sections / 必需章节

Every task record must include all of the following sections:

```markdown
# TASK-YYYYMMDD-HHMM-short-name

## Goal / 目标
One-paragraph description of what this task aims to achieve and why.

## Input / 输入
- Trigger: what initiated this task (issue, bug report, design decision, etc.)
- Related docs: links to feature specs, ADRs, or prior task records.

## Status / 状态
- Current: [NOT_STARTED | IN_PROGRESS | BLOCKED | REVIEWING | DONE | FAILED]
- Created: YYYY-MM-DD HH:MM
- Updated: YYYY-MM-DD HH:MM

## Scope / 范围
- Modules touched: list of top-level modules affected.
- Files changed: list of specific files (updated as work progresses).
- Out of scope: what this task explicitly does NOT cover.

## Steps / 步骤
- [ ] Step 1: description
- [ ] Step 2: description
- [ ] Step 3: description

## Changes / 变更记录
(Detailed changelog entries as work progresses.)

## Tests / 测试
- New tests added: list.
- Existing tests modified: list.
- Verification commands run and their results.

## Acceptance Criteria / 验收标准
- [ ] Criterion 1
- [ ] Criterion 2

## Risks / 风险
- Risk 1: description + mitigation.
- Risk 2: description + mitigation.

## Closeout / 收尾
- Final status: [DONE | FAILED | CANCELLED]
- Summary: one-paragraph summary of what was actually delivered.
- Follow-up: any deferred work or known issues.
```

---

## 5. Status Flow / 状态流转

```
NOT_STARTED  -->  IN_PROGRESS  -->  REVIEWING  -->  DONE
                      |                  |
                      v                  v
                   BLOCKED            FAILED
                      |
                      v
                IN_PROGRESS (retry)
```

- **NOT_STARTED**: record created, work not yet begun.
- **IN_PROGRESS**: actively being worked on.
- **BLOCKED**: waiting on an external dependency or decision.
  The blocker must be documented in the task record.
- **REVIEWING**: implementation complete, under review or verification.
- **DONE**: all acceptance criteria met, tests pass, record moved to `completed/`.
- **FAILED**: work abandoned due to insurmountable issues.
  The reason must be documented in the Closeout section.

---

## 6. Linking / 关联关系

- If the task is based on a feature spec, link to `docs/design/FEATURE-*.md`.
- If the task produces an ADR, link to `docs/decisions/ADR-*.md`.
- Cross-reference related task records in the Input section.
- Commit messages should reference the task ID:
  ```
  feat: implement memory push mode (TASK-20260503-1430)
  ```

---

## 7. Review Checklist for Task Records / 任务记录审查清单

Before marking a task as DONE:

- [ ] All acceptance criteria are checked off.
- [ ] All tests listed pass.
- [ ] The Changes section reflects actual code changes.
- [ ] The Risks section is honest about remaining risks.
- [ ] The file has been moved to `docs/tasks/completed/`.
- [ ] Related docs (if any) have been updated.
