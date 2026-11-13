# Continuity and reply delivery review — 2026-11-12

## Review target

Repository `my-agent`, branch `codex/continuity-repair-20261112`.
Base `91ba2950c67d2479f4b83002fee7377976207ce4` is the existing business candidate. Review only the new continuity patch against this base. The business candidate already contains 15 commits after `1b70870c7`; those commits are inherited, not newly verified wholesale here. Production still runs `1b70870c7`.

## Observed failures and intended behavior

### A. User reply suppressed after a background wake

On November 12 at 17:39 and 17:43 PST, the user requested before/after tables while a child-completion background turn was running. Canonical native messages retained completed assistant replies of 811 and 599 characters. No corresponding public final was appended. `_background_delivery_decision` suppressed an active-Goal child lifecycle turn based on its original wake reason, even though that turn had consumed a direct user input.

The patch marks an existing active-turn input packet only after its exact guidance entry is acknowledged as consumed, and only when its structured origin denotes a direct user. The mark survives existing compact carrier normalization and monotonic merging. `collect_background_execution_result` projects this fact into the delivery decision. A consumed user reply can pass the lifecycle silence rule and the external idle-Goal suppression. Cancellation and narrow-audit guards retain precedence. Existing delivery ownership, message-tool receipts, frozen retries and canonical append remain the delivery mechanism.

Review the source distinction, compact carry lifetime, cancellation races, final/partial behavior, duplicates, and cross-owner delivery. In particular, a received user input must not grant independent permission to publish arbitrary internal cross-session events. The flag does not prove response quality or that a requested table is correct.

### B. An interrupted final hides a successful retry

One request completed on its second attempt with a 2,060-character answer, but its conversation history kept a 528-character failed first final. Dedupe previously keyed the assistant only by request and part.

The patch uses the existing execution attempt ID where available and permits an explicitly failed/interrupted final to coexist with a subsequent completed final. Legacy records without an attempt ID remain supported. Unknown legacy terminal reasons retain conservative old dedupe behavior. Repair filenames distinguish attempts/terminal states so queued repairs cannot overwrite one another.

Review replay ordering carefully. A follow-up identified that an older failed repair from another attempt could be appended after a completed final. The revised patch makes an existing completed final absorbing for that logical request, before comparing attempts. Four additional cases cover late failed, interrupted, completed and unclassified repairs and direct appends. Hash ordering of multiple uncommitted successful attempts still does not establish chronological order. Old missing replies are not automatically replayed by this patch.

### C. Up arrow intercepted by background agent navigation

An empty input buffer with background agents enabled or background processes present allowed Up to enter agent navigation before restoring queued input or history. Six deterministic regression cases failed on the old handler. The handler now uses agent navigation only when a run is explicitly selected. Existing queue locking, restoration, wrapped-line navigation and explicit selected-run navigation are retained.

## Validation policy

Run narrow regression files and relevant neighbor tests only. Do not run the whole repository suite. Do not deploy, push, stop user workloads, transmit messages, or change credentials during review. Findings should identify the exact source path and a concrete failing scenario. Proposed tests should exercise runtime behavior rather than code-shape assertions.

The supplied validation evidence identifies individual runs and must not be summed into unique coverage when runs overlap. No live LLM, production upgrade or Feishu acceptance is claimed for this patch.

## External reference

The inspected upstream Pi agent loop processes structured steering messages before the next assistant turn and emits assistant turn completion separately from explicit continuation. The applicable pattern is keeping input consumption, visible reply, and scheduling decisions distinct. No upstream source was copied. The formerly configured local reference directory is unavailable. [Pi agent loop](https://raw.githubusercontent.com/badlogic/pi-mono/main/packages/agent/src/agent-loop.ts)

## Long-task follow-up requiring design review

Current Goal publication and admission both wait for active children. Task checklist entries do not establish that parent work is independent. Simply removing the child guard risks repeated idle model calls. The existing tool loop already supports doing independent work before a final reply.

Please recommend a narrow structured continuation contract for a genuine technical cutoff with unfinished parent work, preserving exact task identity, cancellation, deduplication, event-driven child waiting and no-progress bounds. For ordinary no-Goal tasks, explain how the contract should continue unfinished authorized work without inventing a Goal or interpreting prose promises as machine state. This scheduling redesign is not implemented by this patch.

## Inherited work still outstanding

The 128-item business ledger currently has 27 PASS, 94 PARTIAL, 1 BLOCKED and 6 NOT_TESTED. Focused checks here independently passed 16 ROUTE cases and 5 Goal fuse cases; these remove those particular reproduced test failures but do not promote entire requirements to PASS.

`/space use` still reports `SPACES_SWITCH_UNSUPPORTED` rather than selecting a space. A real per-thread selection must use the canonical thread/dispatch state, not change a shared interface binding's default. Real provider execution, six write/composition scenarios, packaging, live TUI sequences, Feishu behavior and resource recovery remain separate acceptance work. Nine old slice worktrees contain differences that need reconciliation before cleanup.
