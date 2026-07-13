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

## Evidence required before calling this production-proven

- Two real Feishu users on the same deployment, one running sequential long tasks and one issuing the same work
  in natural-language steps.
- Automatic compact at a temporary 50% threshold, continued work after compact, old-chat recall via search,
  per-user/per-chat isolation, `/verbose` on/full/off behavior, and durable final delivery.
- Restore the deployment threshold to 90% after the experiment and confirm Gateway/Feishu health.
