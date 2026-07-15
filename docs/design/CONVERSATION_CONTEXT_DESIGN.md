# Ordinary Conversation Context Design

Status: implemented in the current worktree; production status and real-host evidence remain governed by
`docs/PRODUCT_FACTS.md`.

## Boundary

An ordinary IM turn is scoped by structured identity, never by model text:

`owner + channel + channel_conversation_id + channel_user_id -> conversation thread`

The owner-scoped `ConversationStore` raw JSONL transcript is the only authoritative dialogue record. The
thread JSON stores the current compact summary, the exact raw-message ID and byte cursor covered by that summary,
its generation, and the per-thread `/verbose` setting. Compact never deletes or rewrites raw messages; after the
first compact, new turns read directly from the byte cursor instead of rescanning an ever-growing file prefix.

Task workspaces remain separately bound by structured task links. A background continuation carries the same
thread ID and task ID as its foreground run, so a successful structured closeout retires that exact active link;
a taskless background observation never invents a task binding. Recent completed links remain bounded, read-only
conversation candidates: when the user explicitly asks to continue or modify one, the model must select its exact
task ID before file work. Selection reopens that workspace and supersedes any newly promoted placeholder task.
The selected task root becomes the run's sole workspace immediately; structured arguments that still carry the
turn's generated placeholder path are rebased at the common tool-round boundary before any tool executes.
Subagent finalization may retire only a conversation link whose task ID exactly equals that subagent's own task ID;
an inherited parent link remains protected.
Internal `subagent-*` and `bg-main-*` links are never exposed as user-selectable active or completed work.

Ordinary chat is not itself a TaskRun. The request starts in the chat lane without creating a numbered task
workspace. A tool may promote the request only through registered structured metadata (`promotes_task`) or an
explicit task/orchestration action. Promotion materializes the workspace under the already resolved owner and
binds that exact path to the conversation task link; no natural-language classifier grants this authority.

## Foreground chat while background work runs

The transcript remains single-writer per conversation. A request that creates background workers ends as soon as
the scheduler has accepted the work, so the conversation slot is released without waiting for child completion.
Its public acknowledgement is rendered from the scheduling lifecycle (`recorded`, `accepted`, `running`,
`failed`); model tool syntax and child logs are never used as user-facing content. The next user message can then
be normal chat, clarification, `/btw`, or `/stop`, while children continue in their own TaskRuns.

Child command traces, intermediate tool output, and internal commentary stay in task-local ledgers. Only material
progress, a decision request, a blocker, or completion is projected back through the background main agent and
the channel-independent delivery envelope. Automatic dispatch supervision fingerprints those structured facts
and skips an unchanged LLM wake; explicit user timers and source-monitoring waits are not skipped.

## Context lifecycle

1. Resolve the owner-scoped Agent and stable conversation thread.
2. Rebuild the owner-local `conversation_message` search projection when needed.
3. Estimate the base prompt, current prompt, output reserve, prior summary, and uncompacted raw tail with the
   existing runtime compact policy and token estimator.
4. Below the threshold, inject the complete uncompacted tail. The old fixed 20-turn limit is now only the
   preferred recent tail retained after a compact, not a forgetting boundary.
5. At the threshold, summarize an older segment with the configured backend, atomically advance the thread
   summary/cursor generation, retain a recent raw tail, and repeat only if the projected context is still too
   large. Empty summaries, missing cursors, corrupt transcripts, or concurrent generation changes fail closed.
6. Inject the older summary and remaining raw tail as context; the current user message remains the only root
   task. Long-term owner memory stays separate and is changed only by its existing explicit tools/policies.

The LocalStore copy is a derived full-text index. Every record carries `thread_id`, `message_id`, role, channel,
and the authoritative transcript path. `session_search` can therefore recall old ordinary chat without loading
all old messages into every prompt. Because each scoped Agent owns a different LocalStore, search cannot cross
owners. Reindexing is idempotent and does not move unchanged records to the top of recent history.

## Verbose progress

`/verbose off|on|full` and `/v` are exact conversation directives. The setting lives on the same thread record:

- `off`: only the normal processing placeholder and final answer are delivered.
- `on`: typed tool start/finish/failure summaries are delivered.
- `full`: summaries plus bounded, credential-redacted tool output are delivered.

Gateway chunks distinguish model deltas from typed tool progress. The authenticated `/progress/<request_id>`
endpoint exposes only events enabled by that request's persisted thread setting. The existing durable reply
worker advances a progress cursor and sends progress through the same channel-independent `DeliveryService`;
it never resubmits the task. Final delivery remains authoritative and removes the pending record.

## References checked

- 通道运行时 `src/auto-reply/reply.ts`, directive parsing/tests, status help, session entry `verboseLevel`, embedded
  tool-event subscription, and compaction handlers: reused its stable session key and per-session verbose-state
  pattern, not its unsafe-for-this-product DM-main default.
- 长期助手 `gateway/session.py`, `gateway/run.py`, `agent/agent_init.py`, `agent/conversation_compression.py`,
  `agent/memory_provider.py`, and `tools/memory_tool.py`: reused its stable gateway conversation key passed into
  session/compact/memory providers and its separation between transcript search and curated memory. We did not
  copy its compression algorithm or profile-wide builtin memory layout.
- 通道运行时 gateway lifecycle/restart coordination, keyed wake coalescing, subagent acceptance receipts and
  completion outbox were also checked for this hardening round. We reused typed lifecycle separation and
  event-driven wake principles, not its product-specific session defaults or message schema.
- 长期助手 gateway shutdown forensics and first-terminal-completion handling were checked for explicit stop versus
  unexpected exit semantics. The my-agent implementation keeps its existing file queue and owner-scoped store.

## 1.10 MiniMax evidence

- Two Feishu-scoped synthetic users ran on the same 1.10 MiniMax M2.7 deployment. User A completed sequential
  long projects; user B completed one project in natural-language stages and later re-opened the completed work
  without creating a second workspace.
- At the temporary 50% threshold, A's thread reached compact generation 4. The cursor covered 38 messages at a
  valid JSONL byte boundary, ten raw-tail messages remained, and the authoritative 401,392-byte transcript was
  not rewritten or deleted.
- A recalled a fact from the compact summary and used one successful owner-local `session_search` for older task
  facts. B searched twice for A-only facts and correctly reported no record. The two transcript, thread and
  LocalStore roots were distinct.
- A retained `/verbose on` and B retained `/verbose full` on their respective thread records. Completed-task
  continuation and background wake handling kept all structured file operations on the selected original root.
- The experiment exposed a bounded MiniMax text-tool dialect after native downgrade. The common parser now
  accepts only unambiguous single-line JSON argument values and leaves all existing authorization/runtime gates
  in force; the exact recall query then succeeded in one tool round.
- The deployed source and active configuration were restored to 90%, both Gateway and Feishu services were
  active, and the Gateway health probe returned the expected 404 for a nonexistent result.

These requests exercised the real server-side Feishu-scoped Gateway identity and conversation path, but used
synthetic open IDs. They do not prove delivery to a real Feishu client, large-user concurrency, cross-node
migration, or long-duration disaster recovery. Search also remains best-effort: one query can return a correct
but incomplete slice, so broader autonomous multi-query recall is still a follow-up item.
