# Task Record Rules

LLM: Create task records for architecture or feature work that spans multiple files.

给人看的解释：
任务记录让后来者知道为什么改，而不是只看到 diff。

## Location

- Active task records: `docs/tasks/active/`.
- Completed task records: `docs/tasks/completed/`.
- Audit-style summaries: `docs/audits/`.

## Required Fields

- Problem solved.
- Scope changed.
- Files or modules touched.
- Tests run.
- Risks and follow-up work.

## Rule

- Move or create the task record under `completed/` when the work is finished.

