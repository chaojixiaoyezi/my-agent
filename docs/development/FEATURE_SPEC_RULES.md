# Feature Spec Rules / 功能规格规则

A feature spec is a short, structured document that captures the **what** and **why**
of a user-visible behavior change before any code is written.  It prevents
"requirements invented during coding."

功能规格是一份简短的结构化文档，在写代码之前捕获用户可见行为变更的"做什么"和"为什么"。
它防止"边写边发明需求"。

---

## 1. When a Feature Spec Is Required / 何时需要功能规格

A feature spec is required when the change:

- Adds or modifies a **CLI command** (new subcommand, new argument, changed output).
- Changes **user-visible behavior** of the agent (response format, routing, workflow).
- Introduces a **new file format** or storage schema.
- Modifies the **adapter protocol** (gateway, channel adapter, subagent protocol).
- Changes **authentication or authorization** logic.

### Exemptions / 豁免

- Pure documentation updates.
- Mechanical refactors with no behavior change.
- Test-only guardrails (architecture tests, property tests).
- Bug fixes that restore previously documented behavior.

---

## 2. File Naming Convention / 文件命名规范

```
FEATURE-YYYYMMDD-short-name.md
```

Examples:
- `FEATURE-20260503-memory-push-mode.md`
- `FEATURE-20260428-failure-introspection.md`

- `short-name` uses lowercase, hyphens only, max 40 characters.
- Store in `docs/design/`.

---

## 3. Required Sections / 必需章节

Every feature spec must include all of the following sections:

```markdown
# FEATURE-YYYYMMDD-short-name

## Background / 背景
Why this feature is needed.  Link to issues, user feedback, or architectural gaps.

## Goal / 目标
One-paragraph statement of what the feature achieves from the user's perspective.

## Non-Goals / 非目标
What this feature explicitly does NOT do.  Prevents scope creep.

## Scenarios / 场景
Concrete user scenarios: who, what, when, expected outcome.

## Requirements / 需求

| ID      | Description                           | Priority |
|---------|---------------------------------------|----------|
| FR-001  | The system shall...                   | Must     |
| FR-002  | The system shall...                   | Should   |

Priority: Must / Should / Nice-to-have.

## Constraints / 约束
Technical constraints: Python version, dependencies, performance, and any external protocol promises.

## Impact / 影响
Which modules, commands, and data formats are affected.

## Architecture / 架构
High-level design: new classes, modified classes, data flow.

## Data Model / 数据模型
New or modified data structures, schemas, file formats.

## State Transitions / 状态转换
All states and transitions if the feature involves state machines.

## File Writes / 文件写入
New write paths introduced.  Must comply with FILE_WRITING_RULES.md.

## Test Plan / 测试计划
Categories of tests and specific scenarios requiring coverage.

## Acceptance Criteria / 验收标准
- [ ] Criterion 1 (verifiable)
- [ ] Criterion 2 (verifiable)

## Risks / 风险
Known risks and mitigations.

## Rollback / 回滚方案
How to revert the feature if it causes problems in production.
```

---

## 4. Requirement Traceability / 需求可追溯性

The requirements table (FR-xxx) creates a traceability chain:

```
Feature Spec (FR-xxx)  -->  Task Record (TASK-*.md)  -->  Tests (test_*.py)  -->  Code
```

- Each FR requirement should map to at least one acceptance criterion.
- Each acceptance criterion should map to at least one test.

---

## 5. Status Flow / 状态流转

```
Proposed  -->  Accepted  -->  In Progress  -->  Implemented  -->  Verified
                  |
                  v
              Rejected
```

- **Proposed**: spec written, under review.
- **Accepted**: reviewed and approved, ready for implementation.
- **In Progress**: implementation has started (linked task record exists).
- **Implemented**: code complete, tests pass.
- **Verified**: feature confirmed working in integration/staging.
- **Rejected**: spec reviewed and declined (reason documented).

Update the status in the spec file header as the feature progresses.

---

## 6. Relationship to Task Records / 与任务记录的关系

- A feature spec may be implemented by **one or more** task records.
- Each task record should reference the feature spec in its Input section.
- The feature spec's acceptance criteria are the **source of truth**; task records
  break them down into implementation steps.

---

## 7. Review Process / 审查流程

Before accepting a feature spec:

- [ ] All required sections are present and substantive.
- [ ] Requirements are specific and testable (no "should be fast" or "user-friendly").
- [ ] Non-goals are clearly stated.
- [ ] Impact assessment covers all affected modules.
- [ ] Rollback plan is realistic.
- [ ] File writes section complies with FILE_WRITING_RULES.md.
