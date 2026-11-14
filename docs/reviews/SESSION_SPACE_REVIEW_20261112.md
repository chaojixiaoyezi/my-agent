# Session space selection and task attribution

This local candidate follows continuity v2 at `922510c099d9f72d8f92aba5dfc0641a8c3dbadd`. The two patches can be reviewed independently. This change uses the existing business-space feature switch.

## What a user can do

2026-11-13 补充：`/space clear` 沿原 `sessions.space.set` 发送显式 `space_id:null`，清除当前发送者在本会话的选择。返回 `source=default` 并保留递增的当前 revision；模型可用同一工具，先 get 原版本再带幂等键和 expected_revision 调用 set。空字符串无效，旧版本不能恢复旧选择；已受理任务继续使用原冻结空间。

In a conversation, `/space use <space_id>` writes the sender's selection to the canonical thread metadata. The main Gateway model turn can discover the same get and set actions. A newly accepted chat request captures the selection at admission. Switching later applies to the next new request. A replay retains the original request's selection.

For the model's `tasks.assign` tool, an omitted space uses the accepted selection. An explicit space takes precedence and an explicit null keeps the task unassigned. A native or plugin assignment still uses its existing explicit schema. The IM task dispatcher freezes the admitted selection into the original SessionTask business context. An archived space can block dispatch into it. A normal IM chat can still proceed.

## Review boundaries

- Two senders sharing a thread and one sender using two threads must keep independent selections. Concurrent writes use the sender's revision. Repeating the last accepted operation with its original key must leave the revision stable.
- A selection describes where to work. The business domain must still authorize the target at creation and revalidate it on replay. Old display metadata and model-written child attributes must never issue a selection.
- Model tools can change the selection only from the verified, active foreground Gateway session. A child may receive delegated work with a frozen business context. A child cannot change its parent's selection.
- The binding cache keeps exact lookups under a bounded LRU. The canonical bindings file and live thread remain authoritative. A changed fingerprint, replacement or deletion invalidates the previous lookup. A cold lookup can still read the entire binding file.

## Evidence

The real chat client used its normal session identity in both business requests and chat admission. It selected A, admitted a request, selected B, replayed the first message and sent a new one. The accepted requests retained A and B respectively. Targeted checks also exercised concurrent writers, actor forgery, child isolation, archived selections, IM replay and file replacement. A final six-node integration run passed. The import-boundary checker returned an empty list. Ruff and diff checks passed.

A separate live DeepSeek Flash run used one synthetic prompt which explicitly named two spaces and workers. The parent registered two assignments. Both original workers finished and produced separately attributed text results of 4200 and 330. The test advanced the scheduler explicitly and captured outgoing delivery with a test adapter. Automatic polling, live Feishu delivery and file-artifact attribution remain open.

The earlier catalog/mount incident has a reproducible dependency distinction. A package can be present in the catalog while mount preview lacks a reference resolver. The old incident's runtime registration is unavailable. The earlier cancellation and retirement scenarios passed in isolation, including quota release. The recorded foreground timings under a blocked model were 4.99 ms for listing spaces and 16.18 ms for looking up a background agent. These two observations measure business HTTP reads only.

## Independent review prompt

Review the patch after continuity v2. Trace one real chat input from session selection through admission, idempotent replay and durable task creation. Check whether any caller can select a different actor's thread, whether a child can forge a host selection, and whether service discovery advertises an unavailable operation. Check compare-and-set races, authorization changes, archived spaces, old IM policies, original request retries and file replacement during a cached binding lookup.

Give each confirmed finding a severity, exact file and function, a minimal failing sequence and the smallest repair. Identify missing evidence separately. Use focused behavioral checks where needed. Preserve the v2 review scope and the user's live workspace. The full repository suite and another paid model run are unnecessary for this review.
