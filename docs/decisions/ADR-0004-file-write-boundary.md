# ADR-0004: File Write Boundary

LLM: Runtime writes belong to owned, ignored storage paths.

给人看的解释：
这个决定解决 data、memory、local DB、缓存和报告混在源码里的问题。

## Status

Accepted

## Decision

Runtime data must be written to ignored storage directories. Permanent project knowledge must be written as docs. Tests must use temp paths or explicit sandboxes.

## Consequences

- `.gitignore` blocks memory archives, runtime data, caches, local reports, and generated experiment directories.
- Guardrail tests block tracked runtime artifacts that still exist in the repository tree.
- New durable write formats require documentation before implementation.

