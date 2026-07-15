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

An interrupted task remains a bounded user-selectable candidate. `/stop` transitions the exact active task and
its descendants to an interrupted state, suppresses late delivery, and leaves the transcript, compact state,
task workspace, artifacts, and owner memory intact. A later ordinary request such as “continue” is still an LLM
turn: the model may select the exact interrupted task ID through the structured task-promotion tool and reopen
the same workspace. The user does not have to reconstruct the old prompt, and the runtime does not guess the
target from the word “continue”.

## Explicit special overlays

`/goal` is a persistent overlay on the same conversation thread, not a second session or collaboration mode.
One thread may have at most one unfinished goal (`active`, `paused`, or `blocked`). The goal owns one durable root
task and advances through deduplicated wake signals. `/goal pause`, `/goal resume`, `/goal edit ...`, and
`/goal clear` mutate that exact record under a transition lock. Goal runs receive `get_goal` and `update_goal`;
the model may write only the terminal `complete` or `blocked` states. `/stop` pauses an active goal instead of
deleting it. Ordinary chat remains available on the same transcript while the goal task is between runs.

`/audit` is an explicit prefix-only task mode. The ingress parser stamps the audit guarantee and optional service
window into structured task attributes; descendants inherit those fields. Watch logic reads only the structured
attributes or its own explicit tool arguments. Mentioning `/audit` inside ordinary prose, a goal, a summary, or a
child prompt cannot activate monitoring authority.

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

The main agent is the sole user-facing aggregator. Child agents can publish typed progress and completion facts
to their parent, but cannot append their private commentary, command traces, tool protocol, or partial replies to
the user's transcript. The same `SimpleAgent` may serve a foreground chat and a background continuation on
different worker threads; transient prompt, run parameters, task workspace, and tool-loop state are stored per
thread with weak agent identity keys, so one run cannot overwrite another and destroyed agents cannot leave
object-ID state for a later agent.

Subagent count is a model planning decision constrained by structured capacity. The model supplies an explicit
count and independent work items after decomposition. Runtime validates per-call, per-task, per-owner, configured,
and currently active limits before creating anything. An over-capacity batch is rejected as a whole; it is never
silently shortened or partially created. The ordinary `/subagents <count>` chat command has been removed.

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

## Owner-visible files and shared capabilities

A remote owner can read or write only its own owner home. The sole cross-owner filesystem exception is the
administrator-published `~/.my-agent/shared/` tree, intended for shared skills, tools, and workflows. Other user
or group homes, root templates, and legacy top-level private directories are denied even in a broad tool mode.
Builtin tools and builtin skills shipped inside the wheel are common product code and do not require a shared
filesystem grant. One exact external directory may additionally enter the current run only through a structured
capability or delivery-contract workspace grant; model text and absolute paths cannot self-authorize it, and the
grant cannot override credential-file or cross-owner denials. `USER.md`, `SOUL.md`, memories, task workspaces,
and artifacts remain private to their owner.

## Completion versus deliverable files

Every promoted task still closes through structured progress, child aggregation, and closeout facts. That
internal closeout ledger does not imply that every task must create a file. Pure analysis or question answering
may finish with `delivery_mode=message` after all structured work is terminal. A missing file blocks completion
only when an explicit artifact contract or expected-output declaration requires one.

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
- 会话运行时 `会话运行时-rs/core/src/会话运行时.rs`, `会话运行时-rs/core/src/state/thread_settings.rs`, and protocol goal/steer/interrupt
  types at reference checkout `1bbdb327` were checked for thread-persistent goal overlay, one-turn steer, and
  interrupt-without-session-loss boundaries. We reused those semantics, not 会话运行时 process storage or UI state.
- 通道运行时 reference checkout `f2a46b06` was checked for stable session keys, background run delivery, lifecycle
  controls, and parent-only child aggregation. 模型助手 Code's public checkout `b7784f2` contains documentation and
  integration surface but not the proprietary runtime, so no unverified internal implementation claim is made.

## Background work versus user-visible conversation

- A successful child completion remains an internal orchestration event while sibling work under the same root is
  still active. The main agent may inspect artifacts or dispatch dependent work, but that intermediate model text
  is not appended to the ordinary transcript and is not proactively delivered to the IM user.
- Successful sibling completions arriving within the configured five-second window are consumed in one main-agent
  turn. Failures, blockers and decisions remain immediate. The final all-terminal turn is user-visible.
- Completion publication writes the durable wake before the linked observation. This closes the scheduler race in
  which an observation-only turn and its later wake could both run. Observation-only fallback retains the same
  lifecycle reason and delivery policy if the wake ledger is unavailable.
- Internal `wait`, automatic supervision and open-coverage continuation turns remain orchestration state while a
  related child is active; their placeholder text is not appended to the ordinary transcript.
- Every continuation uses the original `ThreadTaskLink` goal and workspace. Synthetic wake prompts are execution
  instructions only; they are never allowed to become a task title, directory name or durable parent goal.
- `bind_task` is create-or-fill, not a destructive upsert: an existing non-empty goal, workspace and creation time
  are immutable, a cross-thread rebind fails closed, and only an explicit status field may change lifecycle state.

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
