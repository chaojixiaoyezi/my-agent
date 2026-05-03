# Code Review Checklist

LLM: Use this checklist for reviews and self-review before handoff.

给人看的解释：
这份清单优先看风险，不追求形式主义。

## Architecture

- Does the change stay inside the owning module boundary?
- Does the codebase still have zero `import *` usage?
- Did it avoid vague module names?
- Did large entrypoint files stay within guardrail limits?

## Runtime State

- Are generated files ignored or written to temp directories?
- Is persistent format compatibility preserved?
- Are write failures handled explicitly?

## Compatibility

- Are CLI command names and arguments preserved?
- Are existing JSONL formats unchanged unless explicitly approved?
- Do tests cover the changed behavior or boundary?
