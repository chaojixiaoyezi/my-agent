# Feature Spec Rules

LLM: Require a short spec before adding user-visible behavior.

给人看的解释：
功能规格不用长，但要防止“边写边发明需求”。

## Required Sections

- Problem.
- User-visible behavior.
- Non-goals.
- Data format changes.
- CLI/API compatibility.
- Tests.
- Rollback plan.

## When Not Required

- Pure documentation updates.
- Mechanical refactors with no behavior change.
- Test-only guardrails.

