# Subagent Real E2E Findings

This document is append-only. Record every real subagent E2E issue found during live model/API runs, including the test scene, observed behavior, root cause, fix, verification, and remaining gap.

## 2026-05-09 Real 3-Subagent Parallel E2E

- Test scene:
  - Workspace: `/Users/xiaoyezi/my-claude-code`
  - Runtime output: `/Users/xiaoyezi/my-claude-code/real3_parallel_e2e`
  - Subagent workspace: `/Users/xiaoyezi/my-claude-code/.my_agent_subagents`
  - Model backend: `anthropic_compatible`
  - Model name: `MiniMax-M2.7`
  - Real tasks: string utilities, statistics utilities, graph utilities
  - Real execution mode: `subagent-run --execute --no-probe`

### Finding 1: Parent Test Execution Used The Wrong Directory

- Discovered at: 2026-05-09 during the 3-subagent real E2E run.
- Symptom:
  - The string and stats subagents created files successfully.
  - Manual validation in each artifact directory passed:
    - strings: `14 tests OK`
    - stats: `16 tests OK`
  - Parent validation through `subagents-tests <run_id> --re-run` initially failed for strings with `NO TESTS RAN` and exit code `5`.
- Root cause:
  - Runner output declared commands such as `python3 -m unittest discover -s . -p 'test_*.py'`.
  - Parent `subagents-tests` executed the command from the workspace root instead of the artifact directory.
  - The command was safe, but the working directory was wrong.
- Fix:
  - Added `agent_py_agent/agent/subagents/execution_test_items.py`.
  - Added `TestItemPreparationRequest` and `prepare_test_items()`.
  - The preprocessor infers a workspace-local `working_dir` from runner `artifacts` when a command test has no explicit cwd.
  - Updated `TestExecutor` to accept `working_dir` / `cwd`, enforce workspace boundaries, and record the actual cwd in `validation_result` and `metadata`.
  - Wired the preprocessor into both explicit acceptance execution and `subagents-tests --re-run`.
- Verification:
  - `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_subagent_test_item_preparation.py agent_py_agent/tests/test_subagent_test_executor.py agent_py_agent/tests/test_subagents_tests_command.py` -> `22 passed`
  - Real rerun:
    - `subagents-tests subagent-1778300301-b11e5640 --re-run` -> `passed=1 failed=0`
    - `subagents-tests subagent-1778300301-61949945 --re-run` -> `passed=1 failed=0`
- Status: solved for the observed strings/stats case.
- Remaining risk:
  - Multi-directory tasks need explicit test cwd from the runner or more precise artifact-to-test mapping.
  - The inference stays conservative: if artifacts point to multiple unrelated directories, parent tests should not guess.

### Finding 2: Graph Runner Wrote Files But Did Not Reach A Clean Final State

- Discovered at: 2026-05-09 during the 3-subagent real E2E run.
- Symptom:
  - Graph subagent wrote `graph_tools.py`, `test_graph_tools.py`, and `README.md`.
  - Its runner CLI log was empty, and `runner_result.json` was missing.
  - Board state became `BLOCKED / UNVERIFIED`.
  - Due check reported a P1 blocked issue.
  - Independent validation failed:
    - `test_single_component` expected one connected component but got two.
- Root cause:
  - The produced graph implementation or test package ended in a failing state.
  - The subagent reached its tool-round/finalization boundary before producing a clean final result.
  - The failure handoff claims a possible `__pycache__` issue, but parent validation still observed a real test failure.
- System behavior observed:
  - `subagents` board surfaced the task as `BLOCKED`.
  - `subagents-due-check` surfaced a P1 issue.
  - `failure_handoff.json` and `takeover_readiness.json` were written.
  - `subagents-plan-actions` recommended `classify_blocker`.
- Status: detected, not repaired yet.
- Remaining gap:
  - For a blocked task with failure handoff and takeover packet, the current action plan classifies the blocker but does not directly offer `takeover_or_reassign`.
  - Need decide whether blocked real-run failures should get a controlled rescue path earlier, or whether classification must happen before takeover.

### Finding 3: Tests Passed But Formal Acceptance Still Rejected The Task

- Discovered at: 2026-05-09 after fixing parent test cwd and rerunning real parent tests.
- Symptom:
  - strings and stats both passed real parent tests.
  - `subagents-acceptance-plan --next-action` correctly recommended `apply_acceptance`.
  - `subagents-acceptance-plan --apply` still returned `acceptance_decision=REJECT`.
  - After the rejected apply, both tasks appeared as `BLOCKED / FAILED` in the board, even though their real parent tests passed.
- Rejection reasons:
  - Missing evidence packets with evidence/artifact refs.
  - For strings, there were applied patch records that had not passed review.
  - Verifier reported evidence packets without refs.
- Root cause:
  - The formal acceptance gate is stricter than test success.
  - It requires evidence/ref hygiene and patch review status, not just passing unit tests.
- Fix:
  - Added `agent_py_agent/agent/subagents/services/acceptance_machine_evidence.py`.
  - When runner emitted no evidence packets, a passed parent `reports/test_execution.json` can now satisfy the traceable machine evidence chain.
  - If runner emitted malformed evidence packets, the test report does not hide the bad packets.
  - Verifier output now explains whether traceability came from worker evidence packets or parent machine tests.
- Verification:
  - `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_agent/test_subagent_acceptance.py` -> `8 passed`
- Status: partly solved.
- Remaining gap:
  - Real runner prompts should still prefer evidence packets with artifact/evidence refs.
  - Patch review and acceptance follow-up should be clearer in the board so users can see why "tests passed" is not yet "accepted".
  - Rejected formal acceptance should probably preserve a clearer state split such as "tests passed, evidence rejected" instead of making the task look like the implementation tests failed.

### Finding 4: Test Harness Wrapper Session Was Not A Reliable Source Of Truth

- Discovered at: 2026-05-09 during the first real 3-run wrapper launch.
- Symptom:
  - The wrapper exec session appeared stale from the tool side.
  - `ps` showed no matching live `python3` / `agent_py_agent` runner process.
  - The wrapper did not produce a trustworthy final JSON summary.
- Root cause:
  - The shell/tool wrapper state drifted from the real child process state.
  - Filesystem reports and product CLI state were more reliable than the wrapper session.
- Status: test-method note, not a product bug yet.
- Follow-up:
  - Real E2E reports should be assembled from product facts: task files, reports, board, due-check, acceptance reports, and validation command outputs.
  - Avoid relying on one outer wrapper process as the only truth source.

## 2026-05-09 Real 3-Subagent Parallel E2E v2

- Test scene:
  - Workspace: `/Users/xiaoyezi/my-claude-code`
  - Runtime output: `/Users/xiaoyezi/my-claude-code/real3_parallel_e2e_v2`
  - Subagent workspace: `/Users/xiaoyezi/my-claude-code/.my_agent_subagents_real3v2`
  - Config: `/Users/xiaoyezi/my-claude-code/.my-agent-e2e-config-real3v2.yaml`
  - Model backend: `anthropic_compatible`
  - Model name: `MiniMax-M2.7`
  - Real tasks: string utilities, statistics utilities, graph utilities
  - Real execution mode: three concurrent `subagent-run --execute --no-probe` CLI processes

### Result: Positive Path Passed End To End

- Observed behavior:
  - All three runner processes completed with `ok=OK`.
  - All three tasks moved to `AWAITING_ACCEPTANCE / NEEDS_ACCEPTANCE`.
  - Parent `subagents-tests --re-run` passed for all three:
    - strings: `total=3 executed=3 passed=3 failed=0`
    - stats: `total=1 executed=1 passed=1 failed=0`
    - graphs: `total=1 executed=1 passed=1 failed=0`
  - `subagents-acceptance-plan --next-action` recommended `apply_acceptance` for all three.
  - `subagents-acceptance-plan --apply` accepted all three.
  - Board summary after apply: `DONE=3`, `VERIFIED=3`, `hot=0`.
  - Due check after apply: `total_issues=0`.
- What this verified:
  - Direct parallel CLI runner execution is a more reliable test harness than the previous wrapper process.
  - Parent test cwd inference works in a real model/API run.
  - Passed parent `test_execution.json` can support formal acceptance when runner evidence packets are absent.
  - The positive path now completes: runner -> parent tests -> next-action -> explicit apply -> `DONE/VERIFIED`.
- Status: passed.
- Remaining risk:
  - Need repeat at 5 and 10 concurrent workers.
  - Need intentionally kill, stall, or timeout one worker in a 3 to 5 worker batch and verify the recovery path is understandable and actionable.
  - Need improve rescue path for `BLOCKED` tasks from v1: current planner classifies blockers but does not yet give a direct controlled takeover action in the first action plan.

## 2026-05-09 Real 5-Subagent Fault E2E

- Test scene:
  - Workspace: `/Users/xiaoyezi/my-claude-code`
  - Runtime output: `/Users/xiaoyezi/my-claude-code/real5_fault_e2e`
  - Subagent workspace: `/Users/xiaoyezi/my-claude-code/.my_agent_subagents_real5fault`
  - Config: `/Users/xiaoyezi/my-claude-code/.my-agent-e2e-config-real5fault.yaml`
  - Fast due-check config: `/Users/xiaoyezi/my-claude-code/.my-agent-capability-e2e-fast.yaml`
  - Model backend: `anthropic_compatible`
  - Model name: `MiniMax-M2.7`
  - Real tasks: array utilities, search utilities, tree utilities, interval utilities, geometry utilities
  - Fault injection: the geometry runner process was sent `SIGTERM` after 5 seconds.

### Result: Parent Detected And Took Over A Killed Runner

- Observed behavior:
  - Four normal runners completed with `AWAITING_ACCEPTANCE / NEEDS_ACCEPTANCE`.
  - The killed geometry runner stayed `RUNNING / UNVERIFIED`.
  - Fast due-check reported two P0 issues for the killed run:
    - `run_timeout`
    - `heartbeat_stale`
  - `subagents-plan-actions` recommended `takeover_or_reassign`.
  - `subagents-apply-actions --apply --action takeover_or_reassign --take-over-by real5-parent-recovery` moved the run to `TAKEN_OVER`.
- What this verified:
  - A parent can detect a killed child process through stale heartbeat/run timeout.
  - The action planner can recommend takeover for timeout/stale-heartbeat runs.
  - The action apply path records takeover without treating the killed task as done.
- Status: passed.

### Result: Parent Caught A Real Test Failure In A Finished Runner

- Observed behavior:
  - Interval runner completed normally.
  - Parent `subagents-tests --re-run` failed:
    - `test_interval_tools.py`
    - `TestIntervalIntersection.test_unsorted_input`
    - expected `[(2, 4), (6, 7)]`, actual `[(2, 3), (6, 7)]`
  - Parent next action reported `plan_rescue`.
- Root cause:
  - The generated test expectation appears mathematically questionable for interval intersection: `(1, 3)` intersect `(2, 4)` should normally be `(2, 3)`, not `(2, 4)`.
  - The system correctly did not accept the task because the parent test suite failed, even if the failing assertion may be a bad test.
- Status: detected and routed.

### Finding 5: Existing Failed Test Report Did Not Produce Follow-Up Before Fix

- Discovered at: 2026-05-09 during the 5-subagent fault E2E run.
- Symptom:
  - `reports/test_execution.json` already existed and showed a failed parent test.
  - `subagents-acceptance-plan --next-action` returned `plan_rescue`.
  - `subagents-acceptance-plan --followup` still returned `missing_followup` and recommended `--auto-execution --execute-auto-tests`.
  - `--auto-execution --execute-auto-tests` was blocked because the current action was already rescue, not run_tests.
- Root cause:
  - Follow-up generation only happened in the auto-execution path.
  - Plain `subagents-tests --re-run` could write `test_execution.json` without creating `parent_acceptance_auto_followup.json`.
- Fix:
  - Updated `manager_parent_acceptance.py`.
  - `plan_parent_acceptance_followup()` and `apply_parent_acceptance_followup()` now synthesize a refs-only follow-up from the current `test_execution.json` when the follow-up file is missing.
  - Failed reports now become `needs_manual_rescue`.
  - `--apply-followup --take-over-by ...` can now use the existing action handler takeover gate.
- Verification:
  - `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_parent_acceptance_followup_control.py agent_py_agent/tests/test_parent_acceptance_controller.py` -> `20 passed`
  - Real rerun on the interval task:
    - `subagents-acceptance-plan subagent-1778301855-9c537869 --followup` -> `needs_manual_rescue`
    - `subagents-acceptance-plan subagent-1778301855-9c537869 --apply-followup --take-over-by real5-parent-interval-rescue` -> `takeover_recorded`
- Status: solved.

### Result: Normal Runs Still Accepted

- Observed behavior:
  - Arrays, search, and trees passed parent tests.
  - Explicit acceptance apply accepted all three.
  - Final board state:
    - `DONE=3`
    - `VERIFIED=3`
    - `TAKEN_OVER=2`
  - Fast due-check after takeover: `total_issues=0`.
- Remaining risk:
  - `TAKEN_OVER` tasks still need a separate repair/resume run to finish the actual deliverable.
  - Board `hot=2` still shows taken-over tasks as attention-worthy even when due-check has no open issue; this is useful, but the UI should explain the difference between "attention" and "overdue".

## 2026-05-09 Real 10-Subagent Parallel E2E

- Test scene:
  - Workspace: `/Users/xiaoyezi/my-claude-code`
  - Runtime output: `/Users/xiaoyezi/my-claude-code/real10_parallel_e2e`
  - Subagent workspace: `/Users/xiaoyezi/my-claude-code/.my_agent_subagents_real10`
  - Config: `/Users/xiaoyezi/my-claude-code/.my-agent-e2e-config-real10.yaml`
  - Model backend: `anthropic_compatible`
  - Model name: `MiniMax-M2.7`
  - Real tasks: primes, roman numerals, csv, dates, colors, stack, matrix, cache, url, text wrapping
  - Real execution mode: ten concurrent `subagent-run --execute --no-probe` CLI processes

### Result: Ten-Way Concurrency Partly Passed And Exposed Provider Limits

- Discovered at: 2026-05-09 13:02 CST during the 10-subagent real E2E run.
- Observed behavior:
  - Seven runners reached `AWAITING_ACCEPTANCE / NEEDS_ACCEPTANCE`.
  - Three runners reached `BLOCKED / UNVERIFIED`.
  - Parent tests passed for six of the seven finished runners:
    - primes, csvlite, dates, cache, url
    - roman also passed parent tests, but later hit the patch-review gate described below
  - Parent tests failed for colors because the generated test expectation was wrong.
  - Five clean passed tasks were accepted and moved to `DONE / VERIFIED`.
  - Five non-clean tasks were routed into `TAKEN_OVER`.
  - Final due-check after takeover reported `total_issues=0`.
- Status: passed as a stress/recovery test, not as a pure positive-path test.
- Remaining risk:
  - Ten fully simultaneous MiniMax calls can hit provider concurrency/rate limits.
  - Throughput needs a scheduler/backoff layer before enabling large automatic batches by default.

### Finding 6: MiniMax 429 Rate Limit Appeared Under 10 Concurrent Runners

- Discovered at: 2026-05-09 13:02 CST during the 10-subagent real E2E run.
- Symptom:
  - Two runners failed with HTTP 429 `rate_limit_error`.
  - Affected runs:
    - stack: `subagent-1778302224-d5d2c229`
    - textwrapx: `subagent-1778302225-d2d34d9f`
  - Failure handoff recorded the provider message:
    - current request volume is high
    - higher plan or pay-as-you-go API may be needed for higher concurrency or automation
- Root cause:
  - Provider-side concurrency/rate limit, not a local code failure.
- System behavior observed:
  - Both tasks moved to `BLOCKED`.
  - `failure_handoff.json` and takeover readiness refs were available.
  - After the follow-up fix below, `--followup` recommended `plan_rescue`.
  - `--apply-followup --take-over-by real10-parent-rescue` recorded takeover.
- Fix in this slice:
  - No retry/backoff scheduler was added yet.
  - The rescue/follow-up path was improved so blocked provider failures become actionable.
- Verification:
  - Real commands moved both 429 runs to `TAKEN_OVER`.
  - Final `subagents-due-check --all` returned `total_issues=0`.
- Status: routed, not fully solved.
- Next improvement:
  - Add provider-aware concurrency limits, exponential backoff, and retry classification for transient 429s.

### Finding 7: Structured Output Missing End Marker Blocks A Runner Cleanly

- Discovered at: 2026-05-09 13:02 CST during the 10-subagent real E2E run.
- Symptom:
  - Matrix run `subagent-1778302224-aceb1087` completed a model call but failed structured output parsing.
  - Failure type: `structured_output_parse_error`.
  - Failure reason: missing `[/SUBAGENT_RESULT]` end marker.
- Root cause:
  - The model response did not conform to the required structured output envelope.
- System behavior observed:
  - The task moved to `BLOCKED / UNVERIFIED`.
  - The board exposed failure handoff and takeover readiness refs.
  - After the follow-up fix below, `--followup` recommended rescue instead of asking for tests.
  - Controlled takeover succeeded.
- Fix in this slice:
  - No automatic structured-output repair was added yet.
  - Follow-up rescue for already-blocked tasks was improved.
- Verification:
  - `subagents-acceptance-plan subagent-1778302224-aceb1087 --followup` -> `needs_manual_rescue`
  - `--apply-followup --take-over-by real10-parent-rescue` -> `takeover_recorded`
- Status: routed, not fully solved.
- Next improvement:
  - Add one safe repair/retry attempt for missing end marker or partial structured output, capped and fully logged.

### Finding 8: Subagent-Written Tests Can Be Wrong Even When The Implementation Is Correct

- Discovered at: 2026-05-09 13:02 CST during the 10-subagent real E2E run.
- Symptom:
  - Colors run `subagent-1778302224-3774361c` generated `color_tools.py` and `test_color_tools.py`.
  - Parent test failed:
    - `hex_to_rgb('0F0')` returned `(0, 255, 0)`
    - generated test expected `(15, 255, 15)`
- Root cause:
  - The generated test expectation was wrong for 3-digit hex shorthand.
  - Standard `#RGB` shorthand repeats each digit, so `0F0` expands to `00FF00`.
- System behavior observed:
  - Parent tests caught the mismatch and did not accept the task.
  - Follow-up routed the task to manual rescue/takeover.
- Fix in this slice:
  - No color-task artifact was manually corrected as product code.
  - The finding was recorded as an acceptance-quality problem.
- Verification:
  - `subagents-tests subagent-1778302224-3774361c --re-run` produced `failed=1`.
  - `subagents-acceptance-plan subagent-1778302224-3774361c --followup` -> `needs_manual_rescue`.
  - Controlled takeover succeeded.
- Status: detected and routed.
- Next improvement:
  - Add parent-owned fixed validation cases or external oracle tests for common task families.
  - Do not treat child-authored tests as the only acceptance truth.

### Finding 9: Parent Acceptance Skipped A Required Patch Review Step

- Discovered at: 2026-05-09 13:02 CST during the 10-subagent real E2E run.
- Symptom:
  - Roman run `subagent-1778302224-ea7c9d95` passed parent tests.
  - `subagents-acceptance-plan --apply` rejected it because `output.json` contained one `status=applied` patch without `review_status=APPROVED`.
  - The rejected apply moved the task to `BLOCKED / FAILED`.
- Root cause:
  - The strict patch-review gate was correct, but the parent next-action planner skipped it.
  - After tests passed, the planner recommended `apply_acceptance` instead of first recommending patch review.
- Fix:
  - Updated `parent_acceptance_controller.py`.
  - If tests pass but `output.json` contains unreviewed `status=applied` patches, the decision is now `review_patches`.
  - Updated `parent_acceptance_next_action.py` to recommend:
    - `subagents-patches --review-apply --run-id <run_id>`
  - Updated follow-up command/status mapping so post-test follow-up can return `needs_patch_review`.
- Verification:
  - `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_parent_acceptance_controller.py agent_py_agent/tests/test_parent_acceptance_followup_control.py` -> `24 passed`
- Status: solved for future runs.
- Remaining risk:
  - Existing tasks already rejected by the old flow need rescue or manual repair because their state was already changed.

### Finding 10: Blocked Runners Without Test Reports Got Misleading Follow-Up Guidance

- Discovered at: 2026-05-09 13:02 CST during the 10-subagent real E2E run.
- Symptom:
  - For 429 and structured-output blocked runs, `subagents-acceptance-plan --followup` initially returned `missing_followup`.
  - It recommended `--auto-execution --execute-auto-tests`, but those runs had no reliable artifacts to test.
- Root cause:
  - Follow-up control assumed the next meaningful step after missing follow-up was always test execution.
  - That is true for normal finished runners, but false for blocked runner-level failures.
- Fix:
  - Updated `manager_parent_acceptance.py`.
  - When a task is already `BLOCKED` / failed and no test follow-up exists, follow-up now synthesizes a non-mutating rescue preview from current task state.
  - `--apply-followup --take-over-by ...` can now record takeover without requiring a failed test report.
  - Follow-up consistency now allows rescue when the current task state itself is failed/blocked, even if tests passed or no test report exists.
- Verification:
  - `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_parent_acceptance_controller.py agent_py_agent/tests/test_parent_acceptance_followup_control.py` -> `24 passed`
  - Real blocked runs:
    - matrix: `--followup` -> `needs_manual_rescue`, apply -> `takeover_recorded`
    - stack: `--followup` -> `needs_manual_rescue`, apply -> `takeover_recorded`
    - textwrapx: `--followup` -> `needs_manual_rescue`, apply -> `takeover_recorded`
    - roman old rejected state: apply follow-up rescue -> `takeover_recorded`
- Status: solved for the observed blocked/failure cases.
- Remaining risk:
  - The takeover state means the parent has claimed ownership; a separate repair/resume runner is still needed to finish the actual files.

## 2026-05-09 Multi-Level Subagent Control-Plane E2E

- Test scene:
  - Workspace: `/Users/xiaoyezi/my-claude-code`
  - Runtime output: `/Users/xiaoyezi/my-claude-code/.my_agent_e2e_multilevel_run_1778304762`
  - Config: `/Users/xiaoyezi/my-claude-code/.my-agent-e2e-config-multilevel-1778304762.yaml`
  - Subagent workspace: `/Users/xiaoyezi/my-claude-code/.my_agent_e2e_multilevel_run_1778304762/subagents`
  - Model backend: `echo`
  - Real mode: product persistence, LocalStore control plane, board, due-check, takeover view, and `TestExecutor` safety checks; no model/API calls in this hierarchy-control slice.
  - Hierarchy: 1 main coordinator, 2 child roles, 4 grandchild workers.
    - Child role 1: reporter / progress collector.
    - Child role 2: checker / quality finder.
    - Grandchildren: status reader, risk summarizer, acceptance checker, timeout permission checker.

### Result: 1 Main / 2 Child / 4 Grandchild Tree Passed

- Observed behavior:
  - Seven runs were created and persisted.
  - `LocalStore.list_agent_tree(root_id)` returned all seven runs.
  - Reporter subtree returned exactly three runs: reporter plus two grandchildren.
  - Checker subtree returned exactly three runs: checker plus two grandchildren.
  - `subagents --all --root-id <root>` displayed the tree, shared progress, takeover view, acceptance plan, and acceptance next action.
  - `subagents-due-check --all` reported:
    - one P0 `status_timeout`
    - one P1 `status_blocked`
  - `takeover_readiness.json` and failure handoff refs were visible for both blocked checker and timeout grandchild.
  - `TestExecutor` safety probes passed:
    - `python3 -c "print(123)"` executed inside the workspace artifact directory.
    - `rm -rf /` was rejected before execution.
    - `python3 ... ; rm -rf /` was rejected for high-risk shell chaining.
    - `working_dir: /` was rejected as workspace escape.
    - `file_path: ../outside.txt` was rejected as workspace escape.
- Status: passed after the two fixes below.

### Finding 11: Stale Parent Snapshots Could Drop Child Links

- Discovered at: 2026-05-09 during the multi-level hierarchy E2E.
- Symptom:
  - Creating a child correctly called `add_child(parent_id, child_id)`.
  - Later saving an older in-memory copy of the parent overwrote the newly written `child_ids`.
  - The same issue could happen one level lower: saving an older child object could remove grandchild links.
- Root cause:
  - `SubAgentPersistenceService.save()` persisted the full task object.
  - Append-only hierarchy edges in `child_ids` were not merged with the existing on-disk task before writing.
- Fix:
  - Updated `agent_py_agent/agent/subagents/services/persistence.py`.
  - Before writing, save now merges existing on-disk `child_ids` with the incoming task snapshot.
  - Added a regression test for stale parent and stale child saves.
- Verification:
  - `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_local_store_control_plane.py` -> `5 passed`
  - Re-ran the multi-level E2E and verified:
    - parent has two child ids
    - reporter has two child ids
    - checker has two child ids
- Status: solved for hierarchy links.
- Remaining risk:
  - This fix preserves `child_ids`; other append-only collections may still need separate merge policy if future concurrent writers update them from stale snapshots.

### Finding 12: Takeover Candidate Query Missed TIMEOUT Grandchildren

- Discovered at: 2026-05-09 during the multi-level hierarchy E2E.
- Symptom:
  - `due-check` correctly reported the timeout grandchild as P0.
  - `takeover_readiness.json` existed for that timeout run.
  - But `query_agent_runtime(scope="takeover_candidates")` initially returned only `BLOCKED` runs, not `TIMEOUT` / failed runs.
- Root cause:
  - `blocked_runs` and `takeover_candidates` shared the same blocked-only selector.
- Fix:
  - Updated `agent_py_agent/agent/local_storage/control_plane.py`.
  - `blocked_runs` remains blocked-only.
  - `takeover_candidates` now returns `BLOCKED`, `FAILED`, `ERROR`, and `TIMEOUT`.
  - The reserved hint changed to `blocked_failed_timeout_runs`.
- Verification:
  - `python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_local_store_control_plane.py` -> `5 passed`
  - Re-ran the multi-level E2E and verified takeover candidates include both:
    - blocked checker
    - timeout grandchild
- Status: solved.

### Prior Pain Point Matrix

- 3 / 5 / 10 concurrent subagents:
  - Covered by earlier real MiniMax E2E runs.
  - Current status: positive path works at 3, killed runner/takeover works at 5, 10-way stress exposes provider 429 and routes failures to takeover.
- Runner killed / blocked / timeout:
  - Covered by real 5-run SIGTERM and this hierarchy timeout run.
  - Current status: due-check/action plan/takeover readiness all surface recovery refs.
- Tests pass but acceptance still blocked:
  - Covered by real 3-run and 10-run E2E.
  - Current status: machine evidence fallback and patch-review next action are implemented.
- Wrong child-authored tests:
  - Covered by real colors/interval cases.
  - Current status: parent detects and routes to rescue; external oracle / parent-owned test packs are still future work.
- Permission and high-risk command safety:
  - Covered in this hierarchy E2E through `TestExecutor` allowlist and workspace-boundary checks.
  - Current status: acceptance test executor blocks high-risk shell commands and workspace escapes.
  - Remaining gap: deeper runner/tool-layer command policy should eventually centralize high-risk filesystem operations such as deleting or moving system paths.
- Reporter subagent and checker subagent pattern:
  - Covered as normal subagent roles in the hierarchy E2E.
  - Current status: product can represent reporter/checker roles and show refs-only reports without loading large bodies.
  - Remaining gap: dedicated automatic reporter/checker orchestration is not a separate feature yet.
- Main / child / grandchild mode:
  - Covered at persistence/control-plane level.
  - Current status: `create_run(parent_id=..., root_id=..., depth=...)`, inheritance manifest, LocalStore tree/subtree queries, board, and takeover view work.
  - Remaining gap: a child runner autonomously spawning grandchildren and supervising them end-to-end is not fully implemented yet.
