# Independent review request for Opus 5.5 Max

Read `CONTINUITY_REVIEW_20261112.md`, `VALIDATION.md`, and `continuity.patch` in the supplied bundle. The source excerpts directory contains complete changed production files and tests. Base is `91ba2950c67d2479f4b83002fee7377976207ce4`.

Please independently review the three implemented fixes for user-visible correctness, loss/duplication of replies, authorization/source boundaries, legacy history, compact/retry behavior, and queue navigation. Challenge the tests rather than treating green results as proof. Prioritize reproducible product regressions over style or speculative refactors. Do not modify code or run a repository-wide suite.

Return numbered findings with severity, file/function, concrete failing sequence, expected versus actual behavior, and a minimal repair. Explicitly label confirmed defects, unverified risks, and optional suggestions. Give a separate verdict for A, B and C, with any deployment blockers. Check whether an unsuccessful legacy final can still shadow the successful retry, whether a claimed-but-unconsumed input can accidentally become public, and whether a cancellation can publish a late answer.

Then review the long-task follow-up proposal. Recommend the smallest structured change for technical-cutoff continuation and explain the no-Goal behavior. Preserve event-driven waiting and prevent unbounded polling. Do not request a broad rewrite or a natural-language keyword gate.

This is an interim candidate. Do not infer that the business project's 128 requirements, live providers, production upgrade, Feishu or resource-recovery tests have passed.
