# 2026-06-03 Runtime Hard Gates Audit

Baseline: `0c9bbc48` (`2026-06-01 09:56:51 +0800`). This audit lists hard gates or newly stricter runtime controls added after that baseline, plus the current decision for each one.

## Keep As Safety Gates

1. `agent_py_agent/agent/agent_core/orchestration/write_guard.py`
   - What it blocks: dangerous-root writes and obvious workspace path typos.
   - Why keep: this is a real safety boundary, not task-quality policy.
   - Status: keep. The over-strict task `output/` child-output block has been removed.

2. `agent_py_agent/agent/agent_core/orchestration/background/launch_health.py`
   - What it blocks: background runner launches that immediately fail because the command, import, or CLI arguments are broken.
   - Why keep: a dead worker must become `CHANNEL_ERROR` instead of pretending to stay in `PLANNING`.
   - Status: keep.

3. Tool command/path/approval gates under `agent_py_agent/agent/contracts/gates/`
   - What they block: dangerous commands, missing approval, denied paths, unsafe side effects.
   - Why keep: these protect the machine and user data.
   - Status: keep.

4. `agent_py_agent/agent/agent_core/orchestration/create_constraints.py`
   - What it blocks: a child explicitly relaxing a parent hard constraint in structured parameters.
   - Why keep: a child should not silently weaken a parent safety or quality requirement.
   - Risk: do not auto-invent too many parent hard constraints from plain user prompts.
   - Status: keep but monitor.

## Convert To Softer Runtime Behavior

1. `runner_timeout_seconds` / `runner_timeout_by_role`
   - New hard behavior: default `auto` with `worker=300`, `tester=300`, `coordinator=600`, `root=600`, `default=300`.
   - Observed problem: deep source-reading subagents timed out after about 300 seconds even though they were still doing real work.
   - Decision: default back to no outer total-deadline. Use heartbeat, status, `cancel_subagents`, and takeover for stuck runs.
   - Status: changed to `runner_timeout_seconds: "off"` and empty `runner_timeout_by_role`.

2. `agent_py_agent/agent/agent_core/orchestration/create_active_guard.py`
   - New hard behavior: if the current task already had active children, second-batch `create_subagents` was rejected unless explicitly marked additional/replacement.
   - Observed problem: normal workflow can dispatch 10 agents, then dispatch 5 more for newly discovered scope.
   - Decision: creation should allow additional batches; idempotency/work-scope identity should handle true duplicates.
   - Status: removed.

3. `task_output_target_error` in `write_guard.py`
   - New hard behavior: child `output_files` / `output_refs` under parent task `output/` were rejected.
   - Observed problem: collaborative coding and joint debugging need shared output files.
   - Decision: allow child staged/shared outputs. Final user delivery still requires parent aggregation and closeout.
   - Status: removed.

4. `agent_py_agent/agent/agent_core/orchestration/dispatch/state_contract.py`
   - New hard-ish behavior: dispatch used to emit a hard-sounding “do not report done” field when remembered runs were unfinished, blocked, or unreadable.
   - Why it exists: prevent false completion after dispatch.
   - Risk: can make parent loop on status inspection if paired with noisy prompts or stale remembered run ids.
   - Decision: remove that field entirely. Dispatch now reports `completion_risk` and `recommended_next_action`; closeout handles unresolved children as warning evidence.
   - Status: removed.

5. `agent_py_agent/agent/agent_core/delivery_closeout/subagent_aggregation.py`
   - New hard behavior: main-agent closeout was blocked when current task children were unfinished or failed without explicit handling.
   - Why it exists: warns the parent before submitting while delegated work is unresolved.
   - Risk: if timeout/channel failures are too eager, a hard gate amplifies them.
   - Decision: convert to warning evidence in closeout report. `CANCELLED`, `ABANDONED`, and `TAKEN_OVER` still count as handled.
   - Status: softened.

6. Tool guardrail no-progress / repeat-failure action blocks
   - New stricter behavior: repeated same failure or identical read result can deny that exact next tool call.
   - Defaults: `repeat_fail_threshold=10`, `readonly_no_progress_threshold=3`, `terminal_block_enabled=false`.
   - Why it exists: prevent infinite repeated tool loops.
   - Risk: `readonly_no_progress_threshold=3` can be tight for status polling and report re-reading.
   - Decision: keep action-level only for now; do not let it become a terminal task block.
   - Status: monitor.

7. Artifact provenance / current-run repair decisions
   - New behavior: artifact provenance can report missing provenance, run mismatch, stale source hash, or output hash mismatch.
   - Why it exists: prevent old-run artifacts from silently passing as current-run deliverables.
   - Risk: if treated as a hard gate for ordinary user-requested output directories, it can block valid delivery.
   - Decision: keep as warning/repair evidence by default. It should become blocking only when an explicit external contract marks provenance enforcement as required.
   - Status: monitor.

## Removed From This Audit

The following controls already existed in the 2026-05-31 baseline and should not be counted as new post-2026-05-31 hard gates:

- `repeat_fail_threshold=10`
- `tool_rate_max_calls=60`
- `tool_agent_budget_max_calls=200`
- `runner_failure_retry_limit=2`
- `same_run_redispatch_limit=1`

## Not A Hard Gate But A Major Behavior Change

1. `memory_compact_auto_trigger_percent`
   - Change: default was 90 at baseline, now 50.
   - Effect: compact happens much earlier and more often.
   - Decision: useful for compact stress testing; probably too aggressive as the normal long-task default.
   - Status: keep only while testing compact behavior, then reconsider.
